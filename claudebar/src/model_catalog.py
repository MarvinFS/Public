"""Runtime price catalog from models.dev, with the bundled tables as fallback."""

import json
import os
import urllib.request
from datetime import datetime, timedelta
from typing import Optional

import codex_pricing
import pricing
from config import get_config_dir

URL = "https://models.dev/api.json"
CACHE_TTL = timedelta(hours=24)
PROVIDERS = ("anthropic", "openai")


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
    (an existing, expired cache file is left in place)."""
    try:
        req = urllib.request.Request(URL, headers={"User-Agent": "ClaudeBar/1.0"})
        with urllib.request.urlopen(req, timeout=10) as response:
            api = json.loads(response.read().decode("utf-8"))
        data = {
            provider: {mid: {"cost": m.get("cost", {})}
                       for mid, m in api[provider]["models"].items() if isinstance(m, dict)}
            for provider in PROVIDERS
        }
        data["timestamp"] = datetime.now().isoformat()

        path = _cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
        return data
    except Exception:
        return None


def apply() -> bool:
    """Load models.dev rates into the pricing tables. False = bundled rates in force."""
    data = _load_cached() or _fetch()
    if not data:
        return False
    pricing.load_catalog(data.get("anthropic", {}))
    codex_pricing.load_catalog(data.get("openai", {}))
    return True
