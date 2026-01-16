"""Parse Claude Code JSONL logs for token usage."""

import json
from datetime import datetime, date
from pathlib import Path
from typing import Iterator, Optional
from collections import defaultdict

from models import TokenUsage, ModelUsage
from pricing import calculate_cost
from config import get_claude_projects_dir


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
        # Remove trailing Z and parse ISO format
        return datetime.fromisoformat(ts_str.rstrip("Z"))
    except (ValueError, TypeError):
        return None


def extract_usage_from_entry(entry: dict) -> Optional[tuple[str, TokenUsage, datetime]]:
    """Extract model, token usage, and timestamp from a JSONL entry."""
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

    tokens = TokenUsage(
        input_tokens=usage.get("input_tokens", 0),
        output_tokens=usage.get("output_tokens", 0),
        cache_read_input_tokens=usage.get("cache_read_input_tokens", 0),
        cache_creation_input_tokens=usage.get("cache_creation_input_tokens", 0),
    )

    return model, tokens, timestamp


def parse_jsonl_file(file_path: Path) -> Iterator[tuple[str, TokenUsage, datetime]]:
    """Parse a single JSONL file and yield usage entries."""
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
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


def aggregate_usage(
    projects_dir: Optional[Path] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> tuple[dict[str, ModelUsage], TokenUsage, float]:
    """
    Aggregate token usage across all JSONL files.

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

    for jsonl_file in find_jsonl_files(projects_dir):
        for model, tokens, timestamp in parse_jsonl_file(jsonl_file):
            # Filter by date range
            entry_date = timestamp.date()
            if start_date and entry_date < start_date:
                continue
            if end_date and entry_date > end_date:
                continue

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


def get_today_usage(projects_dir: Optional[Path] = None) -> tuple[TokenUsage, float, list[ModelUsage]]:
    """Get usage statistics for today."""
    today = date.today()
    models, tokens, cost = aggregate_usage(projects_dir, start_date=today, end_date=today)
    return tokens, cost, list(models.values())


def get_month_usage(projects_dir: Optional[Path] = None) -> tuple[TokenUsage, float, list[ModelUsage]]:
    """Get usage statistics for the current month."""
    today = date.today()
    month_start = today.replace(day=1)
    models, tokens, cost = aggregate_usage(projects_dir, start_date=month_start, end_date=today)
    return tokens, cost, list(models.values())
