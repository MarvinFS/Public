"""Parse Codex CLI session JSONL files for token usage and cost calculation."""

import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterator, Optional

import codex_pricing
from codex_pricing import calculate_cost
from jsonl_tail import TailedFile
from validation import safe_get_int

logger = logging.getLogger("claudebar")

# Files already reported as unreadable. A refresh runs every few minutes and meets the
# same broken file each time, so each is named once per process, not once per refresh.
_reported_unreadable: set = set()


def _note_unreadable(file_path: Path) -> None:
    key = str(file_path)
    if key not in _reported_unreadable:
        _reported_unreadable.add(key)
        logger.warning("Codex session file %s could not be read, its usage is left out",
                       file_path.name)


@dataclass
class CodexTokenUsage:
    """Token usage from Codex sessions."""
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: "CodexTokenUsage") -> "CodexTokenUsage":
        return CodexTokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            cached_input_tokens=self.cached_input_tokens + other.cached_input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
            cost_usd=self.cost_usd + other.cost_usd,
        )


def get_codex_sessions_dir() -> Path:
    """Get the Codex sessions directory."""
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        return Path(codex_home) / "sessions"
    return Path.home() / ".codex" / "sessions"


def _scan_line(model: str, billable: bool, line: str) -> tuple[str, bool, Optional[tuple]]:
    """Read one line of a session file: (model in force, billable, (ISO timestamp, usage)).

    The last is None for a line that is not a request. Each turn's own `last_token_usage` is
    priced at the model in force for that turn, so OpenAI's per-request long-context tier
    applies to the right requests. The session-cumulative `total_token_usage` is ignored;
    `codex resume` restarts it mid-file, which per-turn sums do not need to care about. The
    model and the provider are set by lines of their own and carry to the turns below them,
    so whoever reads a file in parts has to carry them from one part to the next.
    """
    # Session files reach hundreds of MB on base64 payloads; only
    # parse the three line kinds we need.
    if ('"turn_context"' not in line and '"token_count"' not in line
            and '"session_meta"' not in line):
        return model, billable, None
    try:
        entry = json.loads(line)
    except json.JSONDecodeError:
        return model, billable, None

    payload = entry.get("payload") if isinstance(entry, dict) else None
    if not isinstance(payload, dict):
        return model, billable, None

    kind = entry.get("type")
    if kind == "session_meta":
        # Only OpenAI models have an API price; a session routed to another
        # provider (Ollama, ...) counts its tokens but costs nothing.
        provider = payload.get("model_provider") or "openai"
        return model, str(provider).lower() == "openai", None

    # The model in force for the turn.
    if kind == "turn_context":
        return (payload["model"] if payload.get("model") else model), billable, None

    if kind != "event_msg" or payload.get("type") != "token_count":
        return model, billable, None

    info = payload.get("info")
    if not isinstance(info, dict):
        return model, billable, None
    last = info.get("last_token_usage")
    if not isinstance(last, dict):
        return model, billable, None

    turn = CodexTokenUsage(
        input_tokens=safe_get_int(last, "input_tokens"),
        cached_input_tokens=safe_get_int(last, "cached_input_tokens"),
        output_tokens=safe_get_int(last, "output_tokens"),
        reasoning_tokens=safe_get_int(last, "reasoning_output_tokens"),
    )
    if billable:
        turn.cost_usd = calculate_cost(
            model=model,
            input_tokens=turn.input_tokens,
            output_tokens=turn.output_tokens,
            cached_input_tokens=turn.cached_input_tokens,
            cache_write_input_tokens=safe_get_int(last, "cache_write_input_tokens"),
        )
    return model, billable, (entry.get("timestamp"), turn)


def _turns(file_path: Path) -> Iterator[tuple[Optional[str], CodexTokenUsage]]:
    """Yield (ISO timestamp, usage) for every request in a session file."""
    model = "default"
    billable = True

    # errors="ignore" like the Claude log parser: one bad byte costs that byte, not
    # every turn after it (the decode error surfaced as ValueError and the caller
    # skipped the rest of the file without a word).
    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            model, billable, found = _scan_line(model, billable, line)
            if found:
                yield found


def parse_session_file(file_path: Path) -> CodexTokenUsage:
    """Total token usage and cost of a single Codex session JSONL file."""
    try:
        return sum((turn for _, turn in _turns(file_path)), CodexTokenUsage())
    except (IOError, OSError):
        return CodexTokenUsage()


def _local_day(timestamp: Optional[str], fallback: date) -> date:
    """Local calendar day of an ISO-8601 UTC event timestamp, `fallback` if it has none."""
    try:
        return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone().date()
    except (AttributeError, ValueError):
        return fallback


_NOTHING = CodexTokenUsage()


class _Session(TailedFile):
    """One session file, reduced to the usage of each local day it holds.

    Codex appends to it, `codex resume` too, so a file is read once and then only for the
    lines it gained. What the lines read so far leave behind is the usage per day and the model
    and provider in force (see _scan_line), which is what a resumed read starts from."""

    def __init__(self, file_day: date) -> None:
        self.file_day = file_day        # the day of the file's folder, for a request with no time
        super().__init__()

    def reset(self) -> None:
        self.model, self.billable = "default", True
        self.days: dict[date, CodexTokenUsage] = {}

    def add_line(self, line: str) -> None:
        self.model, self.billable, found = _scan_line(self.model, self.billable, line)
        if found:
            timestamp, turn = found
            day = _local_day(timestamp, self.file_day)
            self.days[day] = self.days.get(day, _NOTHING) + turn

    def peek_line(self, line: str):
        return _scan_line(self.model, self.billable, line)[2]

    def usage_by_day(self) -> dict[date, CodexTokenUsage]:
        """The days as a full read of the file would give them, a last line the writer has not
        finished included."""
        if self.tail is None:
            return self.days
        timestamp, turn = self.tail
        day = _local_day(timestamp, self.file_day)
        return {**self.days, day: self.days.get(day, _NOTHING) + turn}


# Per sessions directory, the sessions read from it. A turn's cost is worked out when it is
# read, at the prices of that moment, and models.dev changes the table while the app runs:
# the table is remembered with them and a different one starts every file over.
_directories: dict[str, dict[str, _Session]] = {}
_priced_with: Optional[str] = None
_lock = threading.Lock()


def _price_table() -> str:
    return json.dumps([codex_pricing.LONG_CONTEXT_INPUT_TOKENS, codex_pricing.CODEX_PRICING],
                      sort_keys=True)


def reset_cache() -> None:
    """Forget every session read so far; the next series reads them all again."""
    global _priced_with
    with _lock:
        _directories.clear()
        _priced_with = None


def get_codex_daily_usage(sessions_dir: Optional[Path] = None, days: int = 31,
                          end: Optional[datetime] = None) -> list[CodexTokenUsage]:
    """Usage per day for the last `days` days ending on `end` (today), oldest first.

    Every request counts on the local day it happened, not the day its session
    file was started: a session left running past midnight, or an old one
    resumed today (`codex resume` appends to the original file), spends today's
    tokens.
    """
    global _priced_with
    if sessions_dir is None:
        sessions_dir = get_codex_sessions_dir()
    last_day = (end or datetime.now()).date()
    first_day = last_day - timedelta(days=days - 1)
    series = {first_day + timedelta(days=i): CodexTokenUsage() for i in range(days)}
    window_start = datetime(first_day.year, first_day.month, first_day.day).timestamp()

    with _lock:
        prices = _price_table()
        if prices != _priced_with:
            _directories.clear()
            _priced_with = prices
        known = _directories.get(str(sessions_dir), {})
        visited: dict[str, _Session] = {}
        for file_path in sessions_dir.glob("*/*/*/*.jsonl"):
            try:
                file_day = date(*(int(part) for part in file_path.parts[-4:-1]))
                # A file dated before the window only matters if it was written to
                # since (a resumed session). ponytail: Windows updates an open
                # file's mtime late, so a resumed session shows up once it closes.
                if file_day < first_day and file_path.stat().st_mtime < window_start:
                    continue
                path = str(file_path)
                session = known.get(path) or _Session(file_day)
                session.refresh(file_path)
                visited[path] = session
            except (ValueError, IOError, OSError):
                _note_unreadable(file_path)
                continue
            for day, usage in session.usage_by_day().items():
                if day in series:
                    series[day] = series[day] + usage
        _directories[str(sessions_dir)] = visited      # a file that is gone is forgotten
    return list(series.values())
