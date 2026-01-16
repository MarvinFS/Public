"""Parse local Claude Code stats files for usage data."""

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

from config import get_claude_dir


@dataclass
class LocalStats:
    """Statistics parsed from local Claude Code files."""
    total_sessions: int = 0
    total_messages: int = 0
    today_cost_usd: float = 0.0
    today_sessions: dict[str, float] = None  # session_id -> cost

    def __post_init__(self):
        if self.today_sessions is None:
            self.today_sessions = {}


def get_stats_cache_path() -> Path:
    """Get the path to stats-cache.json."""
    return get_claude_dir() / "stats-cache.json"


def get_daily_stats_path() -> Path:
    """Get the path to daily_stats.json."""
    return get_claude_dir() / "daily_stats.json"


def load_stats_cache() -> Optional[dict]:
    """Load and parse stats-cache.json."""
    path = get_stats_cache_path()
    if not path.exists():
        return None

    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return None


def load_daily_stats() -> Optional[dict]:
    """Load and parse daily_stats.json."""
    path = get_daily_stats_path()
    if not path.exists():
        return None

    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return None


def get_local_stats() -> LocalStats:
    """Get combined local statistics."""
    stats = LocalStats()

    # Load stats cache
    cache = load_stats_cache()
    if cache:
        stats.total_sessions = cache.get("totalSessions", 0)
        stats.total_messages = cache.get("totalMessages", 0)

    # Load daily stats for today's cost
    daily = load_daily_stats()
    if daily:
        today_str = date.today().isoformat()
        if daily.get("date") == today_str:
            stats.today_cost_usd = daily.get("daily_total", 0.0)
            stats.today_sessions = daily.get("sessions", {})

    return stats


def get_today_api_cost() -> float:
    """Get today's API cost from daily_stats.json."""
    daily = load_daily_stats()
    if daily:
        today_str = date.today().isoformat()
        if daily.get("date") == today_str:
            return daily.get("daily_total", 0.0)
    return 0.0
