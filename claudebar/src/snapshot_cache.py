"""Snapshot caching for session persistence.

Persists usage snapshots to disk so data can be displayed even when
OAuth tokens expire. Shows cached data with staleness indicators.
"""

import json
import math
from dataclasses import MISSING, dataclass, asdict, fields, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional, get_args, get_origin

import banked_resets
from banked_resets import BankedResets
from config import atomic_write_text, get_config_dir
from models import UsageSnapshot, OpenAISnapshot, DeepSeekSnapshot, TokenUsage, ModelUsage


def get_cache_path() -> Path:
    """Get the snapshot cache file path."""
    return get_config_dir() / "snapshot_cache.json"


@dataclass
class CachedSnapshot:
    """Container for cached snapshots with per-engine timestamps.

    `cached_at` is kept for backward-compat reads only; per-engine
    `claude_cached_at` / `openai_cached_at` are authoritative so that
    refreshing one engine does not falsify the staleness of the other.
    """
    claude: Optional[dict] = None
    openai: Optional[dict] = None
    deepseek: Optional[dict] = None
    cached_at: str = ""  # ISO timestamp - legacy, fallback for old caches
    claude_cached_at: str = ""
    openai_cached_at: str = ""
    deepseek_cached_at: str = ""


def _json_default(o):
    """JSON encoder hook for datetime fields produced by asdict()."""
    if isinstance(o, datetime):
        return o.isoformat()
    raise TypeError(f"Not JSON serializable: {type(o)}")


def _to_dict(snapshot) -> dict:
    """Serialize a snapshot dataclass, dropping the volatile staleness fields.

    The loader recomputes is_stale/stale_since from the cache timestamps, so
    they are never persisted. Datetimes are handled by _json_default at dump
    time; _from_dict reads every field back defensively.
    """
    data = asdict(snapshot)
    data.pop("is_stale", None)
    data.pop("stale_since", None)
    return data


def _parse_iso(s: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(s) if s else None
    except (ValueError, TypeError):
        return None


def _hours(value, default: float) -> float:
    """A window length from the cache, or the default when it is missing or odd."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not value > 0:
        return default
    try:
        hours = float(value)
    except OverflowError:       # an integer too large for a float
        return default
    return hours if math.isfinite(hours) else default


# How a field's JSON becomes its value, by the type the dataclass declares. The
# snapshot's own timestamp is the one time that must parse (a bad one makes the
# whole file unreadable); any other time that does not parse is simply None.
# Keyed here by the real types, so replacing this module's `datetime` (the
# verification model does) cannot change which reader a field gets.
_READERS = {
    datetime: lambda value: datetime.fromisoformat(value),
    Optional[datetime]: _parse_iso,
    Optional[BankedResets]: banked_resets.from_dict,
}

# Window lengths: a stored value that is not a positive number is the default.
_WINDOW_HOURS = ("session_window_hours", "weekly_window_hours")

# Written by asdict() but never read back: no cached reading shows them (the cost
# they priced is already in the figures), so a load leaves them at their defaults.
_NOT_RESTORED = {TokenUsage: ("cache_creation_1h_input_tokens",), ModelUsage: ("tokens",)}


def _read(f, value):
    """One field's value from the JSON stored for it, by the field's declared type."""
    if f.name in _WINDOW_HOURS:
        return _hours(value, f.default)
    reader = _READERS.get(f.type)
    if reader:
        return reader(value)
    if is_dataclass(f.type):
        return _from_dict(f.type, value)
    if get_origin(f.type) is list:
        (item,) = get_args(f.type)
        if is_dataclass(item):
            return [_from_dict(item, entry) for entry in value if isinstance(entry, dict)]
        return list(value or [])
    return value


def _from_dict(cls, data: dict, **given):
    """Rebuild dataclass `cls` from the JSON asdict() wrote for it.

    Keys that are not fields are ignored and a field with no key keeps its
    default, so a file from an older or a newer build still loads. `given` are
    fields the caller sets itself instead of reading (the staleness fields).
    """
    values = dict(given)
    for f in fields(cls):
        if f.name in given or f.name in _NOT_RESTORED.get(cls, ()):
            continue
        stored = data.get(f.name, MISSING)
        if stored is not MISSING:
            values[f.name] = _read(f, stored)
        elif f.default is MISSING and f.default_factory is MISSING:
            values[f.name] = f.type()   # a required field (a model's name) reads as empty
    return cls(**values)


def save_cache(claude: Optional[UsageSnapshot] = None, openai: Optional[OpenAISnapshot] = None,
               deepseek: Optional[DeepSeekSnapshot] = None) -> None:
    """Save snapshots to cache file, merging with existing data.

    Only the engine being saved gets its `*_cached_at` bumped — the other
    engine's freshness is preserved so a Claude refresh does not make
    stale Codex data look fresh (and vice versa).
    """
    cache_path = get_cache_path()

    # Read existing cache to avoid overwriting the other engine's data. Whatever is
    # on disk may be anything (a ValueError is bad JSON and bad UTF-8 alike, and
    # valid JSON need not be an object): then there is nothing to merge.
    existing = {}
    try:
        if cache_path.exists():
            with open(cache_path, "r", encoding="utf-8") as f:
                existing = json.load(f)
    except (OSError, ValueError):
        pass
    if not isinstance(existing, dict):
        existing = {}

    now_iso = datetime.now().isoformat()
    legacy = existing.get("cached_at", "")

    cached = CachedSnapshot(
        claude=_to_dict(claude) if claude else existing.get("claude"),
        openai=_to_dict(openai) if openai else existing.get("openai"),
        deepseek=_to_dict(deepseek) if deepseek else existing.get("deepseek"),
        cached_at=now_iso,
        claude_cached_at=now_iso if claude else (existing.get("claude_cached_at") or legacy),
        openai_cached_at=now_iso if openai else (existing.get("openai_cached_at") or legacy),
        deepseek_cached_at=now_iso if deepseek else (existing.get("deepseek_cached_at") or legacy),
    )

    try:
        # Replaced whole: written in place, a crash mid-write left a truncated
        # file, and the next save read it as empty and dropped every engine.
        atomic_write_text(cache_path, json.dumps(asdict(cached), indent=2, default=_json_default))
    except OSError:
        pass  # Silently fail on cache write errors


def _read_cache(build, empty):
    """What `build` makes of the cache file's JSON, or `empty` when the file is
    missing, cannot be opened, or is not shaped like a cache.

    The cache is a convenience behind a refresh that failed, so no file may stop the
    refresh: a sharing violation (OSError), bad JSON or UTF-8 (ValueError), valid JSON
    of the wrong shape (a list where an object belongs: AttributeError, TypeError)."""
    cache_path = get_cache_path()
    if not cache_path.exists():
        return empty

    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            return build(json.load(f))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return empty


def _claude_and_openai(data: dict) -> tuple:
    legacy_at = _parse_iso(data.get("cached_at", ""))
    claude_at = _parse_iso(data.get("claude_cached_at", "")) or legacy_at
    openai_at = _parse_iso(data.get("openai_cached_at", "")) or legacy_at

    claude = None
    if data.get("claude"):
        claude = _from_dict(UsageSnapshot, data["claude"], is_stale=True, stale_since=claude_at)

    openai = None
    if data.get("openai"):
        openai = _from_dict(OpenAISnapshot, data["openai"], is_stale=True, stale_since=openai_at)

    # Most recent of the two for the legacy single-marker return slot
    candidates = [t for t in (claude_at, openai_at) if t is not None]
    cached_at = max(candidates) if candidates else legacy_at

    return claude, openai, cached_at


def load_cache() -> tuple[Optional[UsageSnapshot], Optional[OpenAISnapshot], Optional[datetime]]:
    """Load cached snapshots from file.

    Each snapshot's `stale_since` reflects when THAT engine was last
    successfully fetched (via per-engine timestamps), so refreshing one
    engine does not falsely advertise the other as fresh.

    Returns:
        Tuple of (claude_snapshot, openai_snapshot, cached_at).
        `cached_at` is the most recent of the two per-engine timestamps,
        kept for callers that want a single "anything updated" marker.
    """
    return _read_cache(_claude_and_openai, (None, None, None))


def _deepseek(data: dict) -> tuple:
    if not data.get("deepseek"):
        return None, None

    cached_at = (_parse_iso(data.get("deepseek_cached_at", ""))
                 or _parse_iso(data.get("cached_at", "")))
    snapshot = _from_dict(DeepSeekSnapshot, data["deepseek"], is_stale=True)
    # The entry is written whenever either half is fresh, so its own time can
    # be newer than a half carried in it. Each half keeps its own fetch time
    # (a cache from before that falls back to the entry's), and the entry is
    # as old as its oldest half.
    snapshot.balance_fetched_at = snapshot.balance_fetched_at or cached_at
    snapshot.usage_fetched_at = snapshot.usage_fetched_at or cached_at
    halves = [at for at, on in ((snapshot.balance_fetched_at, snapshot.balance_available),
                                (snapshot.usage_fetched_at, snapshot.usage_available))
              if on and at]
    if halves:
        cached_at = min(halves)
    snapshot.stale_since = cached_at
    return snapshot, cached_at


def load_deepseek_cache() -> tuple[Optional[DeepSeekSnapshot], Optional[datetime]]:
    """Load the cached DeepSeek snapshot.

    Separate from load_cache() so the existing three-value contract (and its
    callers) stay untouched.
    """
    return _read_cache(_deepseek, (None, None))
