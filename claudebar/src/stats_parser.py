"""Parse local Claude Code stats files for usage data."""

import json
from datetime import date
from pathlib import Path
from typing import Optional

from config import get_claude_dir


def get_daily_stats_path() -> Path:
    """Get the path to daily_stats.json."""
    return get_claude_dir() / "daily_stats.json"


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


def get_today_api_cost() -> float:
    """Get today's API cost from daily_stats.json."""
    daily = load_daily_stats()
    if daily:
        today_str = date.today().isoformat()
        if daily.get("date") == today_str:
            return daily.get("daily_total", 0.0)
    return 0.0
