"""Parse Claude Code JSONL logs for token usage."""

import json
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional
from collections import defaultdict

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
    belongs to; counting each pair once is what makes the total real.
    """
    if entry.get("type") != "assistant":
        return None

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
        timestamp = datetime.now()

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


def parse_jsonl_file(file_path: Path) -> Iterator[tuple[list, datetime, tuple]]:
    """Parse a single JSONL file and yield usage entries.

    No size or line caps: a transcript bloated by pasted images is still real
    spend, and a cap silently drops it from the totals.
    """
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                # Only assistant lines carry usage; skip the rest unparsed.
                if '"assistant"' not in line:
                    continue

                try:
                    entry = json.loads(line)
                    result = extract_usage_from_entry(entry)
                    if result:
                        yield result
                except json.JSONDecodeError:
                    continue
    except (IOError, OSError):
        pass


def collect_entries(projects_dir: Optional[Path] = None) -> dict[tuple, tuple[list, datetime]]:
    """Parse every transcript once and dedup to one entry per API call.

    The mid-stream lines of a response carry a placeholder output_tokens and
    only the last one has the real count, so the last line to claim a key
    wins. This is the expensive step; the aggregations below are cheap.
    """
    latest: dict[tuple, tuple[list, datetime]] = {}
    for jsonl_file in find_jsonl_files(projects_dir):
        for rows, timestamp, key in parse_jsonl_file(jsonl_file):
            latest[key] = (rows, timestamp)
    return latest


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
