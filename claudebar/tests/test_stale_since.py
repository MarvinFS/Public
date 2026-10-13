"""How old the cached figures say they are: each engine's own last good reading, never
the other engine's."""

import json
from datetime import datetime, timedelta

import data_collector as dc
import snapshot_cache
from oauth_usage import OAuthUsageData
from openai_usage import OpenAIUsageData
from tests.test_snapshot_banked import codex_ok, collector, oauth_ok  # noqa: F401  (collector is a fixture)

REFUSED = OAuthUsageData(error="API access restricted (429)")


def stamp_cache(claude_at, openai_at):
    """Say when each engine was last read, as the cache file records it."""
    path = snapshot_cache.get_cache_path()
    data = json.loads(path.read_text(encoding="utf-8"))
    data["claude_cached_at"] = claude_at.isoformat()
    data["openai_cached_at"] = openai_at.isoformat()
    path.write_text(json.dumps(data), encoding="utf-8")


def seeded(collector, monkeypatch, claude_ago, openai_ago):
    """A cache holding both engines, read at the given ages."""
    monkeypatch.setattr(dc, "fetch_oauth_usage", lambda: oauth_ok())
    monkeypatch.setattr(dc, "fetch_openai_usage", lambda: codex_ok())
    collector.collect()
    collector.collect_openai()
    now = datetime.now().replace(microsecond=0)
    claude_at, openai_at = now - claude_ago, now - openai_ago
    stamp_cache(claude_at, openai_at)
    return claude_at, openai_at


def test_claude_is_as_old_as_claudes_own_reading_not_codexs(collector, monkeypatch):
    claude_at, _ = seeded(collector, monkeypatch, timedelta(hours=3), timedelta(minutes=1))
    monkeypatch.setattr(dc, "fetch_oauth_usage", lambda: REFUSED)
    snap = collector.collect()
    assert snap.is_stale is True
    assert snap.stale_since == claude_at


def test_codex_is_as_old_as_codexs_own_reading_not_claudes(collector, monkeypatch):
    _, openai_at = seeded(collector, monkeypatch, timedelta(minutes=1), timedelta(hours=3))
    monkeypatch.setattr(dc, "fetch_openai_usage", lambda: OpenAIUsageData(error="Network error: offline"))
    snap = collector.collect_openai()
    assert snap.is_stale is True
    assert snap.stale_since == openai_at
