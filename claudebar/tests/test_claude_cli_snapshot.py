"""The Claude CLI's saved copy of the usage reply: a second source that costs no request.

The usage endpoint answers 429 to whoever asks within a short spacing of someone
else's call, and Claude Code asks at every start, so ClaudeBar was often refused
while the CLI had a fresh reading on disk. These tests pin how that reading is
read and when the collector prefers it over its own cache.
"""

import json
import logging
from datetime import datetime, timedelta, timezone

import pytest

import claude_cli_snapshot as ccs
import data_collector as dc
import quota_history
from tests.test_snapshot_banked import collector, oauth_ok  # noqa: F401  (collector is a fixture)
from tests.test_stale_since import REFUSED, stamp_cache

NOW = datetime.now(timezone.utc).replace(microsecond=0)
ACCOUNT = "11111111-2222-3333-4444-555555555555"


def window(used, resets_at):
    return {"utilization": used, "resets_at": resets_at.isoformat(), "limit_dollars": None,
            "used_dollars": None, "remaining_dollars": None, "locked_reason": None}


def write_config(path, fetched_at=NOW - timedelta(minutes=2), session=(13, NOW + timedelta(hours=3)),
                 weekly=(4, NOW + timedelta(days=5)), account=ACCOUNT, signed_in=ACCOUNT, **extra_keys):
    """~/.claude.json as the CLI keeps it: a big config with the usage reply tucked inside."""
    reply = {"five_hour": window(*session) if session else None,
             "seven_day": window(*weekly) if weekly else None,
             "extra_usage": {"is_enabled": True, "monthly_limit": 5000, "used_credits": 900,
                             "utilization": 18, "currency": "EUR"},
             "cedar_ember": {"eligible": True, "grants": [], "next_grant_id": None, "at_limit": False},
             "limits": []}
    reply.update(extra_keys)
    cached = {"fetchedAtMs": int(fetched_at.timestamp() * 1000), "utilization": reply}
    if account is not None:
        cached["accountUuid"] = account
    config = {"numStartups": 7, "cachedUsageUtilization": cached}
    if signed_in is not None:
        config["oauthAccount"] = {"accountUuid": signed_in, "emailAddress": "someone@example.com"}
    path.write_text(json.dumps(config), encoding="utf-8")


@pytest.fixture(autouse=True)
def cli_config(tmp_path, monkeypatch):
    """The CLI's config, never the real one; absent until a test writes it."""
    path = tmp_path / "home" / ".claude.json"
    path.parent.mkdir()
    monkeypatch.setattr(ccs, "config_path", lambda: path)
    monkeypatch.setattr(ccs, "_warned", False)
    return path


class TestRead:
    def test_the_windows_and_the_time_the_cli_got_them(self, cli_config):
        write_config(cli_config)
        reading = ccs.read(NOW)
        assert reading.t == NOW - timedelta(minutes=2)
        assert (reading.usage.session.percent, reading.usage.weekly.percent) == (13, 4)
        assert reading.usage.session.resets_at == NOW + timedelta(hours=3)

    def test_only_the_windows_are_carried_not_the_banked_resets_or_extra_usage(self, cli_config):
        write_config(cli_config)
        reading = ccs.read(NOW)
        assert reading.usage.banked is None and reading.usage.extra is None

    @pytest.mark.parametrize("raw", [None, "", "{not json", "[]", '{"numStartups": 3}',
                                     '{"cachedUsageUtilization": "x"}',
                                     '{"cachedUsageUtilization": {"fetchedAtMs": true, "utilization": {}}}'])
    def test_a_missing_or_unreadable_file_is_no_reading_and_never_raises(self, cli_config, raw):
        if raw is not None:
            cli_config.write_text(raw, encoding="utf-8")
        assert ccs.read(NOW) is None

    def test_a_reading_from_another_account_is_ignored(self, cli_config):
        write_config(cli_config, account="other", signed_in=ACCOUNT)
        assert ccs.read(NOW) is None

    def test_an_account_the_cli_does_not_name_on_either_side_still_matches(self, cli_config):
        write_config(cli_config, account=None, signed_in=None)
        assert ccs.read(NOW) is not None

    def test_a_reading_older_than_the_hour_the_cli_itself_trusts_is_ignored(self, cli_config):
        write_config(cli_config, fetched_at=NOW - timedelta(minutes=61))
        assert ccs.read(NOW) is None
        write_config(cli_config, fetched_at=NOW - timedelta(minutes=59))
        assert ccs.read(NOW) is not None

    def test_a_reading_from_the_future_is_ignored(self, cli_config):
        write_config(cli_config, fetched_at=NOW + timedelta(minutes=5))
        assert ccs.read(NOW) is None

    def test_a_window_that_has_reset_is_dropped_and_the_other_one_kept(self, cli_config):
        write_config(cli_config, session=(80, NOW - timedelta(minutes=1)))
        reading = ccs.read(NOW)
        assert reading.usage.session is None and reading.usage.weekly.percent == 4

    def test_a_reading_with_no_running_window_is_no_reading(self, cli_config):
        write_config(cli_config, session=(80, NOW - timedelta(minutes=1)),
                     weekly=(9, NOW - timedelta(minutes=1)))
        assert ccs.read(NOW) is None

    def test_a_changed_format_is_no_reading_and_is_mentioned_once(self, cli_config, caplog):
        write_config(cli_config, five_hour="a string now")
        with caplog.at_level(logging.WARNING, logger="claudebar"):
            assert ccs.read(NOW) is None
            assert ccs.read(NOW) is None
        assert [r.getMessage() for r in caplog.records].count(
            "Claude CLI usage snapshot not understood; the CLI's format may have changed") == 1


@pytest.fixture
def refused(collector, monkeypatch):  # noqa: F811
    """A collector holding a good reading from an hour ago, whose endpoint now refuses."""
    monkeypatch.setattr(dc, "fetch_oauth_usage", lambda: oauth_ok())
    collector.collect()
    cached_at = datetime.now().replace(microsecond=0) - timedelta(hours=1)
    stamp_cache(cached_at, cached_at)
    monkeypatch.setattr(dc, "fetch_oauth_usage", lambda: REFUSED)
    return collector


def local(when):
    """How the cache and the panel carry a time: naive, local."""
    return when.astimezone().replace(tzinfo=None)


class TestCollector:
    def test_a_newer_reading_replaces_the_cached_figures_and_says_how_old_it_is(self, refused, cli_config):
        seen = NOW - timedelta(minutes=3)
        write_config(cli_config, fetched_at=seen, session=(7, NOW + timedelta(hours=4)),
                     weekly=(41, NOW + timedelta(days=5)))
        snap = refused.collect()
        assert (snap.session_percent, snap.weekly_percent) == (7, 41)
        assert snap.session_resets_at == NOW + timedelta(hours=4)
        assert snap.is_stale and snap.stale_since == local(seen)
        assert "OAuth" not in (snap.error_message or "")

    def test_banked_resets_stay_cached_so_none_is_offered_from_it(self, refused, cli_config):
        write_config(cli_config)
        cached = refused.collect().banked
        assert cached is not None and cached.available == 1
        assert refused.last_snapshot.is_stale  # the panel never offers a reset from stale data

    def test_extra_usage_stays_the_cached_value(self, refused, cli_config):
        write_config(cli_config)  # the file says extra usage is on at 18 percent
        assert refused.collect().extra_enabled is False

    def test_a_reading_older_than_the_cache_leaves_the_cache_alone(self, refused, cli_config):
        cached_at = datetime.now().replace(microsecond=0) - timedelta(minutes=10)
        stamp_cache(cached_at, cached_at)  # ClaudeBar itself read 10 minutes ago, the CLI 30
        write_config(cli_config, fetched_at=NOW - timedelta(minutes=30), session=(70, NOW + timedelta(hours=1)))
        snap = refused.collect()
        assert snap.session_percent == 12 and snap.stale_since == cached_at

    def test_a_good_endpoint_reading_ignores_the_file(self, collector, monkeypatch, cli_config):
        write_config(cli_config, session=(70, NOW + timedelta(hours=1)))
        monkeypatch.setattr(dc, "fetch_oauth_usage", lambda: oauth_ok())
        snap = collector.collect()
        assert snap.session_percent == 12 and not snap.is_stale

    def test_with_no_cache_at_all_the_file_fills_the_panel(self, collector, monkeypatch, cli_config):
        write_config(cli_config, session=(7, NOW + timedelta(hours=4)))
        monkeypatch.setattr(dc, "fetch_oauth_usage", lambda: REFUSED)
        snap = collector.collect()
        assert snap.session_percent == 7 and snap.is_stale
        assert "OAuth" not in (snap.error_message or "")  # not 'unavailable' while the file has figures

    def test_a_dead_session_window_keeps_the_cached_session_and_takes_the_weekly(self, refused, cli_config):
        write_config(cli_config, session=(80, NOW - timedelta(minutes=1)), weekly=(41, NOW + timedelta(days=5)))
        snap = refused.collect()
        assert (snap.session_percent, snap.weekly_percent) == (12, 41)
        assert not [r for r in quota_history.load("claude_session", NOW - timedelta(days=1))
                    if r.value == 20]  # the dead window's 80 percent is never charted

    def test_the_reading_is_charted_once_at_the_time_the_cli_took_it(self, refused, cli_config):
        seen = NOW - timedelta(minutes=3)
        resets = NOW + timedelta(hours=4)
        write_config(cli_config, fetched_at=seen, session=(7, resets))
        refused.collect()
        refused.collect()  # the next refused refresh reads the same file
        recorded = [r for r in quota_history.load("claude_session", seen) if r.t == seen]
        assert [(r.value, r.resets_at) for r in recorded] == [(93, resets)]

    def test_a_reader_that_breaks_never_breaks_a_refresh(self, refused, monkeypatch):
        def broken(*args, **kwargs):
            raise RuntimeError("boom")
        monkeypatch.setattr(ccs, "read", broken)
        snap = refused.collect()
        assert snap.session_percent == 12 and snap.is_stale
