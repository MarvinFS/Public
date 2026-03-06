"""Snapshot caching for session persistence.

Persists usage snapshots to disk so data can be displayed even when
OAuth tokens expire. Shows cached data with staleness indicators.
"""

import json
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from config import get_config_dir
from models import UsageSnapshot, OpenAISnapshot, TokenUsage, ModelUsage


def get_cache_path() -> Path:
    """Get the snapshot cache file path."""
    return get_config_dir() / "snapshot_cache.json"


@dataclass
class CachedSnapshot:
    """Container for cached snapshots with timestamp."""
    claude: Optional[dict] = None
    openai: Optional[dict] = None
    cached_at: str = ""  # ISO format timestamp


def _usage_snapshot_to_dict(snapshot: UsageSnapshot) -> dict:
    """Convert UsageSnapshot to JSON-serializable dict."""
    data = {
        "timestamp": snapshot.timestamp.isoformat(),
        "session_percent": snapshot.session_percent,
        "session_reset": snapshot.session_reset,
        "weekly_percent": snapshot.weekly_percent,
        "weekly_reset": snapshot.weekly_reset,
        "extra_enabled": snapshot.extra_enabled,
        "extra_percent": snapshot.extra_percent,
        "extra_used": snapshot.extra_used,
        "extra_limit": snapshot.extra_limit,
        "extra_currency": snapshot.extra_currency,
        "today_cost_usd": snapshot.today_cost_usd,
        "month_cost_usd": snapshot.month_cost_usd,
        "today_tokens": {
            "input_tokens": snapshot.today_tokens.input_tokens,
            "output_tokens": snapshot.today_tokens.output_tokens,
            "cache_read_input_tokens": snapshot.today_tokens.cache_read_input_tokens,
            "cache_creation_input_tokens": snapshot.today_tokens.cache_creation_input_tokens,
        },
        "month_tokens": {
            "input_tokens": snapshot.month_tokens.input_tokens,
            "output_tokens": snapshot.month_tokens.output_tokens,
            "cache_read_input_tokens": snapshot.month_tokens.cache_read_input_tokens,
            "cache_creation_input_tokens": snapshot.month_tokens.cache_creation_input_tokens,
        },
        "models_used": [
            {
                "model": m.model,
                "cost_usd": m.cost_usd,
                "message_count": m.message_count,
            }
            for m in snapshot.models_used
        ],
        "cli_available": snapshot.cli_available,
        "logs_available": snapshot.logs_available,
        "error_message": snapshot.error_message,
    }
    return data


def _dict_to_usage_snapshot(data: dict, is_stale: bool = False, stale_since: Optional[datetime] = None) -> UsageSnapshot:
    """Convert dict back to UsageSnapshot."""
    today_tokens = TokenUsage(
        input_tokens=data.get("today_tokens", {}).get("input_tokens", 0),
        output_tokens=data.get("today_tokens", {}).get("output_tokens", 0),
        cache_read_input_tokens=data.get("today_tokens", {}).get("cache_read_input_tokens", 0),
        cache_creation_input_tokens=data.get("today_tokens", {}).get("cache_creation_input_tokens", 0),
    )
    month_tokens = TokenUsage(
        input_tokens=data.get("month_tokens", {}).get("input_tokens", 0),
        output_tokens=data.get("month_tokens", {}).get("output_tokens", 0),
        cache_read_input_tokens=data.get("month_tokens", {}).get("cache_read_input_tokens", 0),
        cache_creation_input_tokens=data.get("month_tokens", {}).get("cache_creation_input_tokens", 0),
    )
    models_used = [
        ModelUsage(
            model=m.get("model", ""),
            cost_usd=m.get("cost_usd", 0.0),
            message_count=m.get("message_count", 0),
        )
        for m in data.get("models_used", [])
    ]

    snapshot = UsageSnapshot(
        timestamp=datetime.fromisoformat(data.get("timestamp", datetime.now().isoformat())),
        session_percent=data.get("session_percent", 0.0),
        session_reset=data.get("session_reset"),
        weekly_percent=data.get("weekly_percent", 0.0),
        weekly_reset=data.get("weekly_reset"),
        extra_enabled=data.get("extra_enabled", False),
        extra_percent=data.get("extra_percent", 0.0),
        extra_used=data.get("extra_used", 0.0),
        extra_limit=data.get("extra_limit", 0.0),
        extra_currency=data.get("extra_currency", "usd"),
        today_cost_usd=data.get("today_cost_usd", 0.0),
        month_cost_usd=data.get("month_cost_usd", 0.0),
        today_tokens=today_tokens,
        month_tokens=month_tokens,
        models_used=models_used,
        cli_available=data.get("cli_available", True),
        logs_available=data.get("logs_available", True),
        error_message=data.get("error_message"),
        is_stale=is_stale,
        stale_since=stale_since,
    )
    return snapshot


def _openai_snapshot_to_dict(snapshot: OpenAISnapshot) -> dict:
    """Convert OpenAISnapshot to JSON-serializable dict."""
    return {
        "timestamp": snapshot.timestamp.isoformat(),
        "session_percent": snapshot.session_percent,
        "session_reset": snapshot.session_reset,
        "weekly_percent": snapshot.weekly_percent,
        "weekly_reset": snapshot.weekly_reset,
        "today_input_tokens": snapshot.today_input_tokens,
        "today_output_tokens": snapshot.today_output_tokens,
        "today_cached_tokens": snapshot.today_cached_tokens,
        "today_reasoning_tokens": snapshot.today_reasoning_tokens,
        "month_input_tokens": snapshot.month_input_tokens,
        "month_output_tokens": snapshot.month_output_tokens,
        "today_cost_usd": snapshot.today_cost_usd,
        "month_cost_usd": snapshot.month_cost_usd,
        "plan_type": snapshot.plan_type,
        "credits_remaining": snapshot.credits_remaining,
        "available": snapshot.available,
        "error_message": snapshot.error_message,
    }


def _dict_to_openai_snapshot(data: dict, is_stale: bool = False, stale_since: Optional[datetime] = None) -> OpenAISnapshot:
    """Convert dict back to OpenAISnapshot."""
    snapshot = OpenAISnapshot(
        timestamp=datetime.fromisoformat(data.get("timestamp", datetime.now().isoformat())),
        session_percent=data.get("session_percent", 0.0),
        session_reset=data.get("session_reset"),
        weekly_percent=data.get("weekly_percent", 0.0),
        weekly_reset=data.get("weekly_reset"),
        today_input_tokens=data.get("today_input_tokens", 0),
        today_output_tokens=data.get("today_output_tokens", 0),
        today_cached_tokens=data.get("today_cached_tokens", 0),
        today_reasoning_tokens=data.get("today_reasoning_tokens", 0),
        month_input_tokens=data.get("month_input_tokens", 0),
        month_output_tokens=data.get("month_output_tokens", 0),
        today_cost_usd=data.get("today_cost_usd", 0.0),
        month_cost_usd=data.get("month_cost_usd", 0.0),
        plan_type=data.get("plan_type"),
        credits_remaining=data.get("credits_remaining"),
        available=data.get("available", False),
        error_message=data.get("error_message"),
        is_stale=is_stale,
        stale_since=stale_since,
    )
    return snapshot


def save_cache(claude: Optional[UsageSnapshot] = None, openai: Optional[OpenAISnapshot] = None) -> None:
    """Save snapshots to cache file, merging with existing data."""
    cache_path = get_cache_path()
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    # Read existing cache to avoid overwriting the other engine's data
    existing = {}
    try:
        if cache_path.exists():
            with open(cache_path, "r", encoding="utf-8") as f:
                existing = json.load(f)
    except (json.JSONDecodeError, OSError):
        pass

    cached = CachedSnapshot(
        claude=_usage_snapshot_to_dict(claude) if claude else existing.get("claude"),
        openai=_openai_snapshot_to_dict(openai) if openai else existing.get("openai"),
        cached_at=datetime.now().isoformat(),
    )

    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(asdict(cached), f, indent=2)
    except (OSError, IOError):
        pass  # Silently fail on cache write errors


def load_cache() -> tuple[Optional[UsageSnapshot], Optional[OpenAISnapshot], Optional[datetime]]:
    """Load cached snapshots from file.

    Returns:
        Tuple of (claude_snapshot, openai_snapshot, cached_at).
        All values may be None if no cache exists.
        Snapshots will have is_stale=True and stale_since set.
    """
    cache_path = get_cache_path()

    if not cache_path.exists():
        return None, None, None

    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        cached_at = datetime.fromisoformat(data.get("cached_at", ""))

        claude = None
        if data.get("claude"):
            claude = _dict_to_usage_snapshot(data["claude"], is_stale=True, stale_since=cached_at)

        openai = None
        if data.get("openai"):
            openai = _dict_to_openai_snapshot(data["openai"], is_stale=True, stale_since=cached_at)

        return claude, openai, cached_at

    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None, None, None


def get_staleness_text(stale_since: Optional[datetime]) -> str:
    """Get human-readable staleness text."""
    if not stale_since:
        return ""

    now = datetime.now()
    diff = now - stale_since

    total_seconds = int(diff.total_seconds())
    if total_seconds < 60:
        return "just now"

    minutes = total_seconds // 60
    hours = minutes // 60
    days = hours // 24

    if days > 0:
        return f"{days}d ago"
    elif hours > 0:
        return f"{hours}h ago"
    else:
        return f"{minutes}m ago"
