"""Banked resets and window lengths on the snapshots, through the collector and
the snapshot cache, including the cached fallback (which may be shown but is
marked stale so it can never be spent)."""

from datetime import datetime, timezone

import pytest

import banked_resets as br
import data_collector as dc
import snapshot_cache
from codex_log_parser import CodexTokenUsage
from models import (OpenAISnapshot, UsageSnapshot, SESSION_WINDOW_HOURS, WEEKLY_WINDOW_HOURS)
from oauth_usage import OAuthUsageData, UsageWindow
from openai_usage import OpenAIUsageData, OpenAIUsageWindow

UTC = timezone.utc
NOW = datetime.now(UTC)


def claude_banked():
    return br.parse_claude_banked({
        "eligible": True, "next_grant_id": "g1", "at_limit": False,
        "grants": [{"id": "g1", "label": "Launch reset", "resets_total": 1, "resets_left": 1,
                    "ends_at": "2099-10-22T16:00:00+00:00", "clears": ["five_hour", "seven_day"],
                    "paused": False, "usable_now": True, "use_requires_limit": False}]}, NOW)


def codex_banked():
    return br.parse_codex_banked(
        {"available_count": 3, "applicable_available_count": 1},
        {"credits": [{"id": "c1", "status": "available", "reset_type": "codex_rate_limits",
                      "title": "Full reset (Monthly)", "expires_at": "2099-10-22T09:00:00Z"}]}, NOW)


def oauth_ok():
    return OAuthUsageData(
        session=UsageWindow(utilization=12.0),
        weekly=UsageWindow(utilization=39.0, resets_at=datetime(2099, 1, 1, tzinfo=UTC)),
        banked=claude_banked())


def codex_ok(window_seconds=2592000):
    return OpenAIUsageData(
        weekly=OpenAIUsageWindow(percent=4.0, resets_at=datetime(2099, 1, 1),
                                 window_seconds=window_seconds),
        plan_type="free", banked=codex_banked())


@pytest.fixture
def collector(tmp_path, monkeypatch):
    """A collector with the log parsers and the network stubbed out."""
    monkeypatch.setattr(dc, "collect_entries", lambda projects_dir: [])
    monkeypatch.setattr(dc, "get_codex_daily_usage",
                        lambda days, end: [CodexTokenUsage() for _ in range(days)])
    return dc.DataCollector(projects_dir=tmp_path)


class TestDefaults:
    def test_a_snapshot_defaults_to_the_old_window_constants(self):
        snap = OpenAISnapshot()
        assert snap.weekly_window_hours == WEEKLY_WINDOW_HOURS == 168
        assert snap.session_window_hours == SESSION_WINDOW_HOURS == 5

    def test_nothing_banked_by_default(self):
        assert OpenAISnapshot().banked is None
        assert UsageSnapshot().banked is None


class TestClaudeCollector:
    def test_a_live_fetch_carries_banked_resets(self, collector, monkeypatch):
        monkeypatch.setattr(dc, "fetch_oauth_usage", lambda: oauth_ok())
        snap = collector.collect()
        assert snap.is_stale is False
        assert snap.banked == claude_banked()

    def test_the_cached_fallback_carries_them_stale(self, collector, monkeypatch):
        monkeypatch.setattr(dc, "fetch_oauth_usage", lambda: oauth_ok())
        collector.collect()                                       # fills the cache
        monkeypatch.setattr(dc, "fetch_oauth_usage",
                            lambda: OAuthUsageData(error="Network error: offline"))
        snap = collector.collect()
        assert snap.is_stale is True
        assert snap.banked == claude_banked()
        assert br.claim_for(snap.banked, live=not snap.is_stale, now=NOW) is None

    def test_a_response_without_banked_data_has_none(self, collector, monkeypatch):
        data = oauth_ok()
        data.banked = None
        monkeypatch.setattr(dc, "fetch_oauth_usage", lambda: data)
        assert collector.collect().banked is None


class TestCodexCollector:
    def test_the_window_length_comes_from_the_data(self, collector, monkeypatch):
        monkeypatch.setattr(dc, "fetch_openai_usage", lambda: codex_ok(2592000))
        snap = collector.collect_openai()
        assert snap.weekly_window_hours == 720
        assert snap.session_window_hours == SESSION_WINDOW_HOURS     # no session window reported

    def test_a_weekly_and_a_session_window_each_keep_their_own_length(self, collector, monkeypatch):
        data = OpenAIUsageData(
            five_hour=OpenAIUsageWindow(percent=44.0, window_seconds=18000),
            weekly=OpenAIUsageWindow(percent=7.0, window_seconds=604800))
        monkeypatch.setattr(dc, "fetch_openai_usage", lambda: data)
        snap = collector.collect_openai()
        assert (snap.session_window_hours, snap.weekly_window_hours) == (5, 168)

    def test_a_window_with_no_reported_length_uses_the_default(self, collector, monkeypatch):
        monkeypatch.setattr(dc, "fetch_openai_usage", lambda: codex_ok(0))
        assert collector.collect_openai().weekly_window_hours == 168

    def test_a_live_fetch_carries_banked_resets(self, collector, monkeypatch):
        monkeypatch.setattr(dc, "fetch_openai_usage", lambda: codex_ok())
        snap = collector.collect_openai()
        assert snap.banked == codex_banked()
        assert snap.is_stale is False

    def test_the_cached_fallback_keeps_the_window_and_the_banked_data_stale(
            self, collector, monkeypatch):
        monkeypatch.setattr(dc, "fetch_openai_usage", lambda: codex_ok())
        collector.collect_openai()
        monkeypatch.setattr(dc, "fetch_openai_usage",
                            lambda: OpenAIUsageData(error="Network error: offline"))
        snap = collector.collect_openai()
        assert snap.is_stale is True
        assert snap.weekly_window_hours == 720
        assert snap.banked == codex_banked()
        assert br.claim_for(snap.banked, live=not snap.is_stale, now=NOW) is None


class TestCacheRoundTrip:
    def test_claude(self):
        snap = UsageSnapshot(weekly_percent=39.0, banked=claude_banked())
        snapshot_cache.save_cache(claude=snap)
        claude, _, _ = snapshot_cache.load_cache()
        assert claude.banked == claude_banked()
        assert claude.is_stale is True

    def test_codex_with_a_30_day_window(self):
        snap = OpenAISnapshot(weekly_percent=4.0, weekly_window_hours=720.0,
                              session_window_hours=5.0, banked=codex_banked())
        snapshot_cache.save_cache(openai=snap)
        _, openai, _ = snapshot_cache.load_cache()
        assert openai.weekly_window_hours == 720
        assert openai.banked == codex_banked()

    def test_an_old_cache_without_the_new_fields_still_loads(self):
        import json
        path = snapshot_cache.get_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "openai": {"timestamp": NOW.isoformat(), "weekly_percent": 7.0},
            "claude": {"timestamp": NOW.isoformat(), "weekly_percent": 9.0},
            "openai_cached_at": NOW.isoformat(), "claude_cached_at": NOW.isoformat()}),
            encoding="utf-8")
        claude, openai, _ = snapshot_cache.load_cache()
        assert claude.banked is None and openai.banked is None
        assert openai.weekly_window_hours == 168
        assert openai.session_window_hours == 5

    def test_a_corrupt_banked_entry_is_dropped_not_fatal(self):
        import json
        path = snapshot_cache.get_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "claude": {"timestamp": NOW.isoformat(), "weekly_percent": 9.0, "banked": "garbage"},
            "claude_cached_at": NOW.isoformat()}), encoding="utf-8")
        claude, _, _ = snapshot_cache.load_cache()
        assert claude.banked is None
        assert claude.weekly_percent == 9.0
