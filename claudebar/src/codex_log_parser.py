"""Parse Codex CLI session JSONL files for token usage and cost calculation."""

import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterator, Optional

from codex_pricing import calculate_cost
from validation import safe_get_int


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


def _turns(file_path: Path) -> Iterator[tuple[Optional[str], CodexTokenUsage]]:
    """Yield (ISO timestamp, usage) for every request in a session file.

    Each turn's own `last_token_usage` is priced at the model in force for that
    turn, so OpenAI's per-request long-context tier applies to the right
    requests. The session-cumulative `total_token_usage` is ignored;
    `codex resume` restarts it mid-file, which per-turn sums do not need to
    care about.
    """
    model = "default"
    # Only OpenAI models have an API price; a session routed to another
    # provider (Ollama, ...) counts its tokens but costs nothing.
    billable = True

    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            # Session files reach hundreds of MB on base64 payloads; only
            # parse the three line kinds we need.
            if ('"turn_context"' not in line and '"token_count"' not in line
                    and '"session_meta"' not in line):
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            payload = entry.get("payload")
            if not isinstance(payload, dict):
                continue

            kind = entry.get("type")
            if kind == "session_meta":
                provider = payload.get("model_provider") or "openai"
                billable = str(provider).lower() == "openai"
                continue

            # The model in force for the turn.
            if kind == "turn_context":
                if payload.get("model"):
                    model = payload["model"]
                continue

            if kind != "event_msg" or payload.get("type") != "token_count":
                continue

            info = payload.get("info")
            if not isinstance(info, dict):
                continue
            last = info.get("last_token_usage")
            if not isinstance(last, dict):
                continue

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
            yield entry.get("timestamp"), turn


def parse_session_file(file_path: Path) -> CodexTokenUsage:
    """Total token usage and cost of a single Codex session JSONL file."""
    try:
        return sum((turn for _, turn in _turns(file_path)), CodexTokenUsage())
    except (IOError, OSError):
        return CodexTokenUsage()


def get_sessions_for_date(sessions_dir: Path, date: datetime) -> list[Path]:
    """Get all session files for a specific date."""
    date_dir = sessions_dir / str(date.year) / f"{date.month:02d}" / f"{date.day:02d}"

    if not date_dir.exists():
        return []

    return list(date_dir.glob("*.jsonl"))


def _local_day(timestamp: Optional[str], fallback: date) -> date:
    """Local calendar day of an ISO-8601 UTC event timestamp, `fallback` if it has none."""
    try:
        return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone().date()
    except (AttributeError, ValueError):
        return fallback


def get_codex_daily_usage(sessions_dir: Optional[Path] = None, days: int = 31,
                          end: Optional[datetime] = None) -> list[CodexTokenUsage]:
    """Usage per day for the last `days` days ending on `end` (today), oldest first.

    Every request counts on the local day it happened, not the day its session
    file was started: a session left running past midnight, or an old one
    resumed today (`codex resume` appends to the original file), spends today's
    tokens.
    """
    if sessions_dir is None:
        sessions_dir = get_codex_sessions_dir()
    last_day = (end or datetime.now()).date()
    first_day = last_day - timedelta(days=days - 1)
    series = {first_day + timedelta(days=i): CodexTokenUsage() for i in range(days)}
    window_start = datetime(first_day.year, first_day.month, first_day.day).timestamp()

    for file_path in sessions_dir.glob("*/*/*/*.jsonl"):
        try:
            file_day = date(*(int(part) for part in file_path.parts[-4:-1]))
            # A file dated before the window only matters if it was written to
            # since (a resumed session). ponytail: Windows updates an open
            # file's mtime late, so a resumed session shows up once it closes.
            if file_day < first_day and file_path.stat().st_mtime < window_start:
                continue
            for timestamp, turn in _turns(file_path):
                day = _local_day(timestamp, file_day)
                if day in series:
                    series[day] = series[day] + turn
        except (ValueError, IOError, OSError):
            continue
    return list(series.values())


def get_month_codex_usage(sessions_dir: Optional[Path] = None) -> CodexTokenUsage:
    """Get token usage for the current calendar month, so the figure resets on the 1st."""
    if sessions_dir is None:
        sessions_dir = get_codex_sessions_dir()

    total = CodexTokenUsage()
    today = datetime.now()

    for day in range(1, today.day + 1):
        date = today.replace(day=day)
        session_files = get_sessions_for_date(sessions_dir, date)

        for file_path in session_files:
            session_usage = parse_session_file(file_path)
            total = total + session_usage

    return total
