"""Parse Claude Code JSONL logs for token usage."""

import json
import threading
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional
from collections import defaultdict

from jsonl_tail import TailedFile
from models import TokenUsage, ModelUsage
from pricing import calculate_cost
from config import get_claude_projects_dir
from validation import safe_get_int


def find_jsonl_files(projects_dir: Optional[Path] = None) -> Iterator[Path]:
    """Find all JSONL log files in the Claude projects directory."""
    if projects_dir is None:
        projects_dir = get_claude_projects_dir()

    if not projects_dir.exists():
        return

    for jsonl_file in projects_dir.rglob("*.jsonl"):
        yield jsonl_file


def last_log_write(projects_dir: Optional[Path] = None) -> Optional[datetime]:
    """When any Claude Code log was last written (aware, local), None when there are none.

    Reads file times only: every log is stat-ed, about 90 ms for 1,500 of them, so the panel
    asks from a thread of its own and never from the Tk thread (ui_window._logs_written_since).
    It cannot be answered from the transcript cache: that holds the times of the last refresh,
    and a session that began since is what it is asked about."""
    newest = 0.0
    try:
        for path in find_jsonl_files(projects_dir):
            try:
                newest = max(newest, path.stat().st_mtime)
            except OSError:
                continue    # removed between the listing and the stat
    except OSError:
        pass                # a folder that cannot be read: use what was seen
    return datetime.fromtimestamp(newest).astimezone() if newest else None


def parse_timestamp(ts_str: str) -> Optional[datetime]:
    """Parse ISO timestamp from JSONL entry."""
    if not ts_str:
        return None

    try:
        # Claude writes UTC timestamps (Z-suffixed). Parse as aware UTC and
        # convert to local time so .date() buckets match date.today() and the
        # local-date cost tracker; naive .date() mis-buckets near local midnight.
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone()


def _usage_tokens(usage: dict) -> TokenUsage:
    # cache_creation splits the write total by TTL; absent = all 5-minute.
    cache_creation = usage.get("cache_creation")
    if not isinstance(cache_creation, dict):
        cache_creation = {}
    return TokenUsage(
        input_tokens=safe_get_int(usage, "input_tokens"),
        output_tokens=safe_get_int(usage, "output_tokens"),
        cache_read_input_tokens=safe_get_int(usage, "cache_read_input_tokens"),
        cache_creation_input_tokens=safe_get_int(usage, "cache_creation_input_tokens"),
        cache_creation_1h_input_tokens=safe_get_int(cache_creation, "ephemeral_1h_input_tokens"),
    )


def extract_usage_from_entry(entry: dict) -> Optional[tuple[list, datetime, tuple]]:
    """Extract model, token usage, timestamp, and dedup key from a JSONL entry.

    Claude Code writes one line per content block of a response and repeats the
    whole response's usage on every one, so a four-block answer appears four
    times. The (message id, requestId) pair identifies the API call the line
    belongs to; counting each pair once is what makes the total real. None for a
    line that is no usage record, or has no usable timestamp.
    """
    if not isinstance(entry, dict) or entry.get("type") != "assistant":
        return None     # a line of JSON that is no record would otherwise end the whole parse

    message = entry.get("message", {})
    if not isinstance(message, dict):
        return None

    model = message.get("model", "")
    if not model:
        return None

    usage = message.get("usage", {})
    if not isinstance(usage, dict):
        return None

    timestamp = parse_timestamp(entry.get("timestamp", ""))
    if timestamp is None:
        # No date, so no day to count it on. Stamped "now" it was counted as today's
        # spend again on every refresh.
        return None

    iterations = usage.get("iterations")
    if isinstance(iterations, list) and len(iterations) > 1:
        # An advisor turn runs a second model inside the same response. Its
        # tokens reach the top-level figures for cache only - input and output
        # there cover the outer messages alone, so read the iterations instead.
        rows = [(it.get("model") or model, _usage_tokens(it))
                for it in iterations if isinstance(it, dict)]
    else:
        rows = [(model, _usage_tokens(usage))]

    # uuid keeps a line with no requestId (2 of 27k here) counted, not merged.
    key = (message.get("id"), entry.get("requestId") or entry.get("uuid"))

    return rows, timestamp, key


def _claim(line: str) -> Optional[tuple[list, datetime, tuple]]:
    """The usage record in one line of a transcript, None for any other line."""
    # Only assistant lines carry usage; skip the rest unparsed.
    if '"assistant"' not in line:
        return None
    try:
        return extract_usage_from_entry(json.loads(line))
    except json.JSONDecodeError:
        return None


def parse_jsonl_file(file_path: Path) -> Iterator[tuple[list, datetime, tuple]]:
    """Parse a single JSONL file and yield usage entries.

    No size or line caps: a transcript bloated by pasted images is still real
    spend, and a cap silently drops it from the totals.
    """
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                result = _claim(line)
                if result:
                    yield result
    except (IOError, OSError):
        pass


def _keep_latest(into: dict, key: tuple, claim: tuple) -> None:
    """File `claim` under `key`: the claim with the most output tokens wins, the later one among
    equals. The one rule for lines of a file and for the files themselves, so the two cannot
    differ: the winner is the greatest by (output tokens, position), however the claims are
    grouped on the way."""
    held = into.get(key)
    if held is None or _output_tokens(claim[0]) >= _output_tokens(held[0]):
        into[key] = claim


class _Transcript(TailedFile):
    """One transcript, reduced to its claim on each API call it mentions.

    A call's lines repeat inside a file (one per content block), so keeping the best claim per
    key as the lines arrive is exact, and the cache holds one entry per call per file."""

    def reset(self) -> None:
        self.claims: dict[tuple, tuple[list, datetime]] = {}

    def add_line(self, line: str) -> None:
        claim = _claim(line)
        if claim:
            rows, timestamp, key = claim
            _keep_latest(self.claims, key, (rows, timestamp))

    def peek_line(self, line: str):
        return _claim(line)


class _Directory:
    """What collect_entries remembers of one projects directory."""

    def __init__(self) -> None:
        self.transcripts: dict[str, _Transcript] = {}
        self.merged: dict = {}
        self.merged_from: Optional[tuple] = None    # (path, version) of each file merged


_directories: dict[str, _Directory] = {}
_lock = threading.Lock()


def reset_cache() -> None:
    """Forget every transcript read so far; the next collect_entries reads them all again."""
    with _lock:
        _directories.clear()


def collect_entries(projects_dir: Optional[Path] = None) -> dict[tuple, tuple[list, datetime]]:
    """Dedup the transcripts to one entry per API call.

    The mid-stream lines of a response carry a placeholder output_tokens and
    only the last one has the real count, so the claim with the most output
    tokens wins, and the later line among equals. Not simply the last line
    seen: files are visited in directory order, and a resumed transcript can
    hold a mid-stream copy of a call whose final line is in a file visited
    earlier. This used to parse everything on every call, 4 s for 1.7 GB; now a file
    is read once and then only for the lines it gained (see jsonl_tail), and the dedup
    below runs over what is kept. The result is a new dict, the callers' to change.

    Old files are still read on the first call, though no figure comes from most of them.
    Leaving a file out by its mtime is exact only if no line is stamped later than its file's
    mtime, and a call's lines carry one time in every file that holds them (or a copy in a
    newer file could outlive the original's claim). Both held on every transcript measured;
    nothing in this program enforces either, and the saving is one start-up parse.
    """
    with _lock:
        directory = _directories.setdefault(str(projects_dir), _Directory())
        visited: dict[str, _Transcript] = {}
        for jsonl_file in find_jsonl_files(projects_dir):
            path = str(jsonl_file)
            transcript = directory.transcripts.get(path) or _Transcript()
            try:
                transcript.refresh(jsonl_file)
            except OSError:
                continue    # removed between the listing and the read, or unreadable: counts for nothing
            visited[path] = transcript
        directory.transcripts = visited      # a file that is gone is forgotten

        merged_from = tuple((path, t.version) for path, t in visited.items())
        if merged_from != directory.merged_from:
            latest: dict[tuple, tuple[list, datetime]] = {}
            for transcript in visited.values():
                for key, claim in transcript.claims.items():
                    _keep_latest(latest, key, claim)
                if transcript.tail is not None:          # a line the writer has not finished
                    rows, timestamp, key = transcript.tail
                    _keep_latest(latest, key, (rows, timestamp))
            directory.merged, directory.merged_from = latest, merged_from
        return dict(directory.merged)


def _output_tokens(rows: list) -> int:
    return sum(tokens.output_tokens for _, tokens in rows)


def aggregate_usage(
    projects_dir: Optional[Path] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    entries: Optional[dict] = None,
) -> tuple[dict[str, ModelUsage], TokenUsage, float]:
    """
    Aggregate token usage across all JSONL files (or pre-collected entries).

    Returns:
        - Dictionary of model -> ModelUsage
        - Total TokenUsage
        - Total cost in USD
    """
    models: dict[str, ModelUsage] = defaultdict(
        lambda: ModelUsage(model="", tokens=TokenUsage())
    )
    total_tokens = TokenUsage()
    total_cost = 0.0

    if entries is None:
        entries = collect_entries(projects_dir)

    for rows, timestamp in entries.values():
        # Filter by date range
        entry_date = timestamp.date()
        if start_date and entry_date < start_date:
            continue
        if end_date and entry_date > end_date:
            continue

        for model, tokens in rows:
            # Aggregate by model
            if models[model].model == "":
                models[model] = ModelUsage(model=model, tokens=TokenUsage())

            models[model].tokens = models[model].tokens + tokens
            models[model].message_count += 1

            # Calculate cost for this entry
            entry_cost = calculate_cost(model, tokens)
            models[model].cost_usd += entry_cost

            # Totals
            total_tokens = total_tokens + tokens
            total_cost += entry_cost

    return dict(models), total_tokens, total_cost


def daily_costs(entries: dict, days: int, end: Optional[date] = None) -> list[float]:
    """Cost per day for the `days` days ending on `end` (today), oldest first."""
    end = end or date.today()
    start = end - timedelta(days=days - 1)
    buckets = [0.0] * days
    for rows, timestamp in entries.values():
        offset = (timestamp.date() - start).days
        if 0 <= offset < days:
            buckets[offset] += sum(calculate_cost(model, tokens) for model, tokens in rows)
    return buckets
