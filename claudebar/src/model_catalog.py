"""Runtime price catalog from models.dev, with the bundled tables as fallback."""

import json
from datetime import datetime, timedelta
from typing import Optional

import codex_pricing
import http_json
import pricing
from config import atomic_write_text, get_config_dir
from version import USER_AGENT

URL = "https://models.dev/api.json"
CACHE_TTL = timedelta(hours=24)
PROVIDERS = ("anthropic", "openai")

# models.dev being down must cost one 10 s timeout per ten minutes, not one per refresh.
_fetch_failures = http_json.FailureMemory()


def _cache_path():
    return get_config_dir() / "models_dev.json"


def _load_cached() -> Optional[dict]:
    try:
        with open(_cache_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if datetime.now() - datetime.fromisoformat(data["timestamp"]) > CACHE_TTL:
            return None
        return data
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _fetch() -> Optional[dict]:
    """Fetch and cache only the two provider cost maps; None on any failure
    (an existing, expired cache file is left in place). A failure is remembered and
    nothing is asked for ten minutes after it."""
    if _fetch_failures.blocked():
        return None
    try:
        # Public and credential-free, so it may follow a redirect.
        api = http_json.request_json(URL, {"User-Agent": USER_AGENT}, timeout=10,
                                     follow_redirects=True)
        data = {
            provider: {mid: {"cost": m.get("cost", {})}
                       for mid, m in api[provider]["models"].items() if isinstance(m, dict)}
            for provider in PROVIDERS
        }
        data["timestamp"] = datetime.now().isoformat()

        atomic_write_text(_cache_path(), json.dumps(data))
        return data
    except Exception:
        _fetch_failures.failed()
        return None


def apply() -> bool:
    """Load models.dev rates into the pricing tables. False = bundled rates in force."""
    data = _load_cached() or _fetch()
    if not data:
        return False
    pricing.load_catalog(data.get("anthropic", {}))
    codex_pricing.load_catalog(data.get("openai", {}))
    return True
