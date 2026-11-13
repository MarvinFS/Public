"""The snapshot cache file: what one engine's snapshot becomes on disk and what comes back.

CACHE_JSON is the file the 1.8.x build wrote for the three fully populated snapshots
below, saved Claude first, Codex second, DeepSeek last. It is pinned byte for byte:
a cache written by an older build must still load, and one written now must still load
in an older build, so neither the keys nor the layout may drift.
"""

import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest

import snapshot_cache
from banked_resets import BankedResets, ResetCredit
from models import (DeepSeekSnapshot, ModelUsage, OpenAISnapshot, TokenUsage, UsageSnapshot,
                    SESSION_WINDOW_HOURS, WEEKLY_WINDOW_HOURS)

UTC = timezone.utc

CLAUDE_SAVED = datetime(2026, 11, 12, 9, 0, 0)
OPENAI_SAVED = datetime(2026, 11, 12, 9, 5, 0)
DEEPSEEK_SAVED = datetime(2026, 11, 12, 9, 10, 0)


def claude_snapshot() -> UsageSnapshot:
    return UsageSnapshot(
        timestamp=datetime(2026, 11, 12, 8, 55, 1, 123456),
        session_percent=42.5, session_reset="in 2h 5m",
        session_resets_at=datetime(2026, 11, 12, 11, 0, tzinfo=UTC),
        weekly_percent=17.25, weekly_reset="in 3d 4h",
        weekly_resets_at=datetime(2026, 11, 15, 12, 0, tzinfo=UTC),
        extra_enabled=True, extra_percent=12.0, extra_used=6.0, extra_limit=50.0,
        extra_currency="eur",
        banked=BankedResets(
            provider="claude", state="ok", available=2,
            credits=[ResetCredit(id="grant-1", label="Launch reset",
                                 expires_at=datetime(2026, 11, 20, 16, 0, tzinfo=UTC), count=2,
                                 clears=("five_hour", "seven_day"), paused=False,
                                 usable_now=False, requires_limit=True)],
            applicable=None, next_credit_id="grant-1",
            cooldown_until=datetime(2026, 11, 12, 12, 30, tzinfo=UTC), at_limit=True, reason=None),
        today_cost_usd=1.23, month_cost_usd=45.67,
        today_tokens=TokenUsage(1000, 2000, 3000, 4000, 1500),
        month_tokens=TokenUsage(10_000, 20_000, 30_000, 40_000, 15_000),
        models_used=[ModelUsage("claude-opus-5", TokenUsage(1, 2, 3, 4, 5), 30.5, 120),
                     ModelUsage("claude-sonnet-5", TokenUsage(), 15.17, 80)],
        last31_cost_usd=99.9, last31_tokens=123_456_789, daily_costs=[0.0, 1.5, 2.25],
        pricing_source="models.dev", cli_available=False, logs_available=False,
        error_message="logs: boom", is_stale=True, stale_since=datetime(2026, 11, 12, 8, 0))


def openai_snapshot() -> OpenAISnapshot:
    return OpenAISnapshot(
        timestamp=datetime(2026, 11, 12, 8, 56, 2, 654321),
        session_percent=61.0, session_reset="in 1h 10m", session_available=False,
        session_resets_at=datetime(2026, 11, 12, 10, 6, tzinfo=UTC),
        weekly_percent=8.5, weekly_reset="in 29d 2h",
        weekly_resets_at=datetime(2026, 12, 11, 11, 0, tzinfo=UTC),
        session_window_hours=5.0, weekly_window_hours=720.0,
        banked=BankedResets(
            provider="codex", state="ok", available=3,
            credits=[ResetCredit(id="credit-a", label="Monthly",
                                 expires_at=datetime(2026, 11, 22, 9, 0, tzinfo=UTC)),
                     ResetCredit(id="credit-b", label="Bonus", count=2, paused=True)],
            applicable=1, next_credit_id=None, cooldown_until=None, at_limit=False,
            reason="none right now"),
        today_input_tokens=11, today_output_tokens=22, today_cached_tokens=33,
        today_reasoning_tokens=44, month_input_tokens=555, month_output_tokens=666,
        today_cost_usd=0.5, month_cost_usd=12.75, last31_cost_usd=20.125, last31_tokens=987_654,
        daily_costs=[0.25, 0.0, 0.5], pricing_source="bundled", plan_type="pro",
        credits_remaining=7.5, available=True, error_message=None,
        is_stale=True, stale_since=datetime(2026, 11, 12, 8, 1))


def deepseek_snapshot() -> DeepSeekSnapshot:
    return DeepSeekSnapshot(
        timestamp=datetime(2026, 11, 12, 8, 57, 3, 111111),
        balance_available=True, balance_usable=True, balance_total=14.58,
        balance_topped_up=10.0, balance_granted=4.58, balance_currency="CNY",
        total_cost_usd=15.42, total_cost_available=True,
        usage_available=True, usage_currency="CNY",
        today_cost_usd=0.4, today_tokens=4000, month_cost_usd=8.8, month_tokens=88_000,
        last7_cost_usd=3.3, last7_tokens=33_000, prev_month_cost_usd=6.6, prev_month_tokens=66_000,
        daily_costs=[0.1, 0.2], daily_tokens=[1000, 2000], daily_requests=[3, 4],
        daily_dates=["2026-11-11", "2026-11-12"],
        models_used=[ModelUsage("deepseek-chat", cost_usd=5.5, message_count=0),
                     ModelUsage("deepseek-reasoner", TokenUsage(7, 8, 9, 10, 11), 3.3, 5)],
        error_message="earlier problem", usage_error="Session expired",
        balance_fetched_at=datetime(2026, 11, 12, 8, 40), usage_fetched_at=datetime(2026, 11, 12, 8, 45),
        is_stale=True, stale_since=datetime(2026, 11, 12, 8, 40))


CACHE_JSON = """\
{
  "claude": {
    "timestamp": "2026-11-12T08:55:01.123456",
    "session_percent": 42.5,
    "session_reset": "in 2h 5m",
    "session_resets_at": "2026-11-12T11:00:00+00:00",
    "weekly_percent": 17.25,
    "weekly_reset": "in 3d 4h",
    "weekly_resets_at": "2026-11-15T12:00:00+00:00",
    "extra_enabled": true,
    "extra_percent": 12.0,
    "extra_used": 6.0,
    "extra_limit": 50.0,
    "extra_currency": "eur",
    "banked": {
      "provider": "claude",
      "state": "ok",
      "available": 2,
      "credits": [
        {
          "id": "grant-1",
          "label": "Launch reset",
          "expires_at": "2026-11-20T16:00:00+00:00",
          "count": 2,
          "clears": [
            "five_hour",
            "seven_day"
          ],
          "paused": false,
          "usable_now": false,
          "requires_limit": true
        }
      ],
      "applicable": null,
      "next_credit_id": "grant-1",
      "cooldown_until": "2026-11-12T12:30:00+00:00",
      "at_limit": true,
      "reason": null
    },
    "today_cost_usd": 1.23,
    "month_cost_usd": 45.67,
    "today_tokens": {
      "input_tokens": 1000,
      "output_tokens": 2000,
      "cache_read_input_tokens": 3000,
      "cache_creation_input_tokens": 4000,
      "cache_creation_1h_input_tokens": 1500
    },
    "month_tokens": {
      "input_tokens": 10000,
      "output_tokens": 20000,
      "cache_read_input_tokens": 30000,
      "cache_creation_input_tokens": 40000,
      "cache_creation_1h_input_tokens": 15000
    },
    "models_used": [
      {
        "model": "claude-opus-5",
        "tokens": {
          "input_tokens": 1,
          "output_tokens": 2,
          "cache_read_input_tokens": 3,
          "cache_creation_input_tokens": 4,
          "cache_creation_1h_input_tokens": 5
        },
        "cost_usd": 30.5,
        "message_count": 120
      },
      {
        "model": "claude-sonnet-5",
        "tokens": {
          "input_tokens": 0,
          "output_tokens": 0,
          "cache_read_input_tokens": 0,
          "cache_creation_input_tokens": 0,
          "cache_creation_1h_input_tokens": 0
        },
        "cost_usd": 15.17,
        "message_count": 80
      }
    ],
    "last31_cost_usd": 99.9,
    "last31_tokens": 123456789,
    "daily_costs": [
      0.0,
      1.5,
      2.25
    ],
    "pricing_source": "models.dev",
    "cli_available": false,
    "logs_available": false,
    "error_message": "logs: boom"
  },
  "openai": {
    "timestamp": "2026-11-12T08:56:02.654321",
    "session_percent": 61.0,
    "session_reset": "in 1h 10m",
    "session_available": false,
    "session_resets_at": "2026-11-12T10:06:00+00:00",
    "weekly_percent": 8.5,
    "weekly_reset": "in 29d 2h",
    "weekly_resets_at": "2026-12-11T11:00:00+00:00",
    "session_window_hours": 5.0,
    "weekly_window_hours": 720.0,
    "banked": {
      "provider": "codex",
      "state": "ok",
      "available": 3,
      "credits": [
        {
          "id": "credit-a",
          "label": "Monthly",
          "expires_at": "2026-11-22T09:00:00+00:00",
          "count": 1,
          "clears": [],
          "paused": false,
          "usable_now": true,
          "requires_limit": false
        },
        {
          "id": "credit-b",
          "label": "Bonus",
          "expires_at": null,
          "count": 2,
          "clears": [],
          "paused": true,
          "usable_now": true,
          "requires_limit": false
        }
      ],
      "applicable": 1,
      "next_credit_id": null,
      "cooldown_until": null,
      "at_limit": false,
      "reason": "none right now"
    },
    "today_input_tokens": 11,
    "today_output_tokens": 22,
    "today_cached_tokens": 33,
    "today_reasoning_tokens": 44,
    "month_input_tokens": 555,
    "month_output_tokens": 666,
    "today_cost_usd": 0.5,
    "month_cost_usd": 12.75,
    "last31_cost_usd": 20.125,
    "last31_tokens": 987654,
    "daily_costs": [
      0.25,
      0.0,
      0.5
    ],
    "pricing_source": "bundled",
    "plan_type": "pro",
    "credits_remaining": 7.5,
    "available": true,
    "error_message": null
  },
  "deepseek": {
    "timestamp": "2026-11-12T08:57:03.111111",
    "balance_available": true,
    "balance_usable": true,
    "balance_total": 14.58,
    "balance_topped_up": 10.0,
    "balance_granted": 4.58,
    "balance_currency": "CNY",
    "total_cost_usd": 15.42,
    "total_cost_available": true,
    "usage_available": true,
    "usage_currency": "CNY",
    "today_cost_usd": 0.4,
    "today_tokens": 4000,
    "month_cost_usd": 8.8,
    "month_tokens": 88000,
    "last7_cost_usd": 3.3,
    "last7_tokens": 33000,
    "prev_month_cost_usd": 6.6,
    "prev_month_tokens": 66000,
    "daily_costs": [
      0.1,
      0.2
    ],
    "daily_tokens": [
      1000,
      2000
    ],
    "daily_requests": [
      3,
      4
    ],
    "daily_dates": [
      "2026-11-11",
      "2026-11-12"
    ],
    "models_used": [
      {
        "model": "deepseek-chat",
        "tokens": {
          "input_tokens": 0,
          "output_tokens": 0,
          "cache_read_input_tokens": 0,
          "cache_creation_input_tokens": 0,
          "cache_creation_1h_input_tokens": 0
        },
        "cost_usd": 5.5,
        "message_count": 0
      },
      {
        "model": "deepseek-reasoner",
        "tokens": {
          "input_tokens": 7,
          "output_tokens": 8,
          "cache_read_input_tokens": 9,
          "cache_creation_input_tokens": 10,
          "cache_creation_1h_input_tokens": 11
        },
        "cost_usd": 3.3,
        "message_count": 5
      }
    ],
    "error_message": "earlier problem",
    "usage_error": "Session expired",
    "balance_fetched_at": "2026-11-12T08:40:00",
    "usage_fetched_at": "2026-11-12T08:45:00"
  },
  "cached_at": "2026-11-12T09:10:00",
  "claude_cached_at": "2026-11-12T09:00:00",
  "openai_cached_at": "2026-11-12T09:05:00",
  "deepseek_cached_at": "2026-11-12T09:10:00"
}"""


class _AnyDatetime(type):
    """isinstance(x, frozen_clock) holds for any datetime, so a snapshot's real
    datetimes still serialise while the module's clock stands still."""

    def __instancecheck__(cls, obj):
        return isinstance(obj, datetime)


class _Frozen(datetime, metaclass=_AnyDatetime):
    at = CLAUDE_SAVED

    @classmethod
    def now(cls, tz=None):
        return cls.at


def save_all(monkeypatch):
    """Save the three snapshots one after another, each at its own frozen time."""
    with monkeypatch.context() as clock:
        clock.setattr(snapshot_cache, "datetime", _Frozen)
        for saved_at, kwargs in ((CLAUDE_SAVED, {"claude": claude_snapshot()}),
                                 (OPENAI_SAVED, {"openai": openai_snapshot()}),
                                 (DEEPSEEK_SAVED, {"deepseek": deepseek_snapshot()})):
            _Frozen.at = saved_at
            snapshot_cache.save_cache(**kwargs)


def write_cache(text: str):
    path = snapshot_cache.get_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def loaded_claude():
    """What a load gives back for claude_snapshot(): the staleness fields are the cache's
    own, and the two figures the cache has never read back (the 1-hour share of the cache
    writes, and a model's own token counts) come back at their defaults."""
    snap = claude_snapshot()
    return replace(
        snap, is_stale=True, stale_since=CLAUDE_SAVED,
        today_tokens=replace(snap.today_tokens, cache_creation_1h_input_tokens=0),
        month_tokens=replace(snap.month_tokens, cache_creation_1h_input_tokens=0),
        models_used=[replace(m, tokens=TokenUsage()) for m in snap.models_used])


def loaded_openai():
    return replace(openai_snapshot(), is_stale=True, stale_since=OPENAI_SAVED)


def loaded_deepseek():
    """Its two halves keep their own fetch times, and the entry is as old as the oldest."""
    snap = deepseek_snapshot()
    return replace(snap, is_stale=True, stale_since=snap.balance_fetched_at,
                   models_used=[replace(m, tokens=TokenUsage()) for m in snap.models_used])


class TestTheFileOnDisk:
    def test_saving_writes_exactly_these_bytes(self, monkeypatch):
        save_all(monkeypatch)
        assert snapshot_cache.get_cache_path().read_bytes() == CACHE_JSON.encode("utf-8")

    def test_the_staleness_fields_are_never_written(self):
        entry = json.loads(CACHE_JSON)["claude"]
        assert "is_stale" not in entry and "stale_since" not in entry

    def test_the_json_pinned_here_is_the_snapshots_it_was_made_from(self):
        data = json.loads(CACHE_JSON)
        assert data["claude"]["today_tokens"]["cache_creation_1h_input_tokens"] == 1500
        assert data["claude"]["models_used"][0]["tokens"]["output_tokens"] == 2
        assert data["deepseek"]["models_used"][1]["message_count"] == 5


class TestLoadingWhatWasWritten:
    def test_claude_and_codex_come_back_as_saved(self):
        write_cache(CACHE_JSON)
        claude, openai, cached_at = snapshot_cache.load_cache()
        assert claude == loaded_claude()
        assert openai == loaded_openai()
        # The later of the two engines' own times, not the time of the last save.
        assert cached_at == OPENAI_SAVED

    def test_deepseek_comes_back_as_saved(self):
        write_cache(CACHE_JSON)
        snap, cached_at = snapshot_cache.load_deepseek_cache()
        assert snap == loaded_deepseek()
        assert cached_at == snap.stale_since == datetime(2026, 11, 12, 8, 40)

    def test_loading_with_a_replaced_clock_still_reads_every_time(self, monkeypatch):
        """The verification model swaps snapshot_cache.datetime for a subclass; the
        times in the file must still come back as times."""
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 1, 1, 12, 0, 0)

        write_cache(CACHE_JSON)
        monkeypatch.setattr(snapshot_cache, "datetime", Clock)
        claude, openai, cached_at = snapshot_cache.load_cache()
        deepseek, deepseek_at = snapshot_cache.load_deepseek_cache()
        assert (claude, openai, cached_at) == (loaded_claude(), loaded_openai(), OPENAI_SAVED)
        assert (deepseek, deepseek_at) == (loaded_deepseek(), datetime(2026, 11, 12, 8, 40))


class TestWhatAFileMayLeaveOut:
    def test_unknown_keys_are_ignored_and_missing_keys_take_the_default(self):
        write_cache(json.dumps({
            "claude": {"timestamp": "2026-11-12T08:55:01", "session_percent": 40.0,
                       "never_a_field": [1, 2], "today_tokens": {"input_tokens": 7, "colour": "red"},
                       "models_used": [{"model": "claude-opus-5", "cost_usd": 2.0, "extra": True}],
                       "is_stale": False, "stale_since": "2020-01-01T00:00:00"},
            "claude_cached_at": "2026-11-12T09:00:00"}))
        claude, openai, cached_at = snapshot_cache.load_cache()
        assert claude == UsageSnapshot(
            timestamp=datetime(2026, 11, 12, 8, 55, 1), session_percent=40.0,
            today_tokens=TokenUsage(input_tokens=7),
            models_used=[ModelUsage("claude-opus-5", cost_usd=2.0)],
            is_stale=True, stale_since=CLAUDE_SAVED)
        assert openai is None
        assert cached_at == CLAUDE_SAVED

    def test_a_model_with_no_name_reads_as_the_empty_name(self):
        write_cache(json.dumps({"claude": {"timestamp": "2026-11-12T08:55:01",
                                           "models_used": [{"cost_usd": 1.0}]}}))
        claude, _, _ = snapshot_cache.load_cache()
        assert claude.models_used == [ModelUsage("", cost_usd=1.0)]

    def test_an_entry_from_before_per_engine_times_uses_the_one_legacy_time(self):
        write_cache(json.dumps({"claude": {"timestamp": "2026-11-12T08:55:01"},
                                "openai": {"timestamp": "2026-11-12T08:56:00"},
                                "cached_at": "2026-11-12T09:30:00"}))
        claude, openai, cached_at = snapshot_cache.load_cache()
        assert claude.stale_since == openai.stale_since == cached_at == datetime(2026, 11, 12, 9, 30)

    def test_a_deepseek_entry_without_half_times_takes_the_entrys_time(self):
        write_cache(json.dumps({"deepseek": {"timestamp": "2026-11-12T08:57:00", "balance_available": True,
                                             "usage_available": True},
                                "deepseek_cached_at": "2026-11-12T09:10:00"}))
        snap, cached_at = snapshot_cache.load_deepseek_cache()
        assert snap.balance_fetched_at == snap.usage_fetched_at == datetime(2026, 11, 12, 9, 10)
        assert cached_at == snap.stale_since == datetime(2026, 11, 12, 9, 10)

    def test_a_missing_timestamp_is_now(self):
        write_cache(json.dumps({"openai": {"session_percent": 5.0}}))
        _, openai, _ = snapshot_cache.load_cache()
        assert abs((datetime.now() - openai.timestamp).total_seconds()) < 60

    @pytest.mark.parametrize("stored", [None, "soon", True, False, 0, -5, [], {}, "168"])
    def test_a_window_length_that_is_not_a_positive_number_is_the_default(self, stored):
        write_cache(json.dumps({"openai": {"timestamp": "2026-11-12T08:56:00",
                                           "session_window_hours": stored,
                                           "weekly_window_hours": stored}}))
        _, openai, _ = snapshot_cache.load_cache()
        assert (openai.session_window_hours, openai.weekly_window_hours) == (
            SESSION_WINDOW_HOURS, WEEKLY_WINDOW_HOURS)

    @pytest.mark.parametrize("stored", [10 ** 400, float("inf")], ids=["an integer too large for a float", "infinity"])
    def test_a_window_length_that_is_not_a_finite_number_is_the_default_too(self, stored):
        """float() of such an integer raises OverflowError, which no loader catches: the cache
        was unreadable, and a refresh that fell back to it with it."""
        write_cache(json.dumps({"openai": {"timestamp": "2026-11-12T08:56:00",
                                           "session_window_hours": stored,
                                           "weekly_window_hours": stored}}))
        _, openai, _ = snapshot_cache.load_cache()
        assert (openai.session_window_hours, openai.weekly_window_hours) == (
            SESSION_WINDOW_HOURS, WEEKLY_WINDOW_HOURS)

    def test_a_window_length_is_read_as_a_float(self):
        write_cache(json.dumps({"openai": {"timestamp": "2026-11-12T08:56:00",
                                           "weekly_window_hours": 720}}))
        _, openai, _ = snapshot_cache.load_cache()
        assert openai.weekly_window_hours == 720.0 and isinstance(openai.weekly_window_hours, float)

    def test_a_time_that_does_not_parse_is_no_time_but_a_bad_timestamp_is_no_cache(self):
        write_cache(json.dumps({"claude": {"timestamp": "2026-11-12T08:55:01",
                                           "session_resets_at": "tomorrow"}}))
        claude, _, _ = snapshot_cache.load_cache()
        assert claude.session_resets_at is None
        write_cache(json.dumps({"claude": {"timestamp": "yesterday"}}))
        assert snapshot_cache.load_cache() == (None, None, None)

    def test_a_list_field_that_is_null_is_empty(self):
        write_cache(json.dumps({"deepseek": {"timestamp": "2026-11-12T08:57:00", "daily_costs": None,
                                             "daily_dates": None}}))
        snap, _ = snapshot_cache.load_deepseek_cache()
        assert (snap.daily_costs, snap.daily_dates) == ([], [])

    def test_a_models_entry_that_is_not_an_object_is_skipped(self):
        write_cache(json.dumps({"deepseek": {"timestamp": "2026-11-12T08:57:00",
                                             "models_used": ["junk", None, {"model": "deepseek-chat"}]}}))
        snap, _ = snapshot_cache.load_deepseek_cache()
        assert snap.models_used == [ModelUsage("deepseek-chat")]


class TestWhenThereIsNothingToLoad:
    def test_no_file(self):
        assert snapshot_cache.load_cache() == (None, None, None)
        assert snapshot_cache.load_deepseek_cache() == (None, None)

    @pytest.mark.parametrize("text", [
        "", "{not json", '{"claude": {"timestamp": null}}', '{"deepseek": {"models_used": null}}',
        '{"deepseek": {"timestamp": "2026-11-12T08:57:00", "models_used": null}}'])
    def test_a_file_that_cannot_be_read_is_no_cache(self, text):
        write_cache(text)
        assert snapshot_cache.load_cache() == (None, None, None)
        assert snapshot_cache.load_deepseek_cache() == (None, None)

    def test_an_engine_with_an_empty_entry_has_no_snapshot(self):
        write_cache(json.dumps({"claude": {}, "openai": None, "deepseek": {}}))
        assert snapshot_cache.load_cache() == (None, None, None)
        assert snapshot_cache.load_deepseek_cache() == (None, None)

    @pytest.mark.parametrize("text", [
        "[]", '"text"', "3", "null",                          # valid JSON, not an object
        '{"claude": [1, 2]}', '{"claude": "x"}', '{"openai": 5}',
        '{"deepseek": [{"a": 1}]}'])                          # an engine's entry of the wrong shape
    def test_a_file_of_the_wrong_shape_is_no_cache(self, text):
        write_cache(text)
        assert snapshot_cache.load_cache() == (None, None, None)
        assert snapshot_cache.load_deepseek_cache() == (None, None)

    def test_a_file_that_is_not_utf8_is_no_cache(self):
        path = snapshot_cache.get_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'{"claude": "\xff\xfe"}')
        assert snapshot_cache.load_cache() == (None, None, None)
        assert snapshot_cache.load_deepseek_cache() == (None, None)

    def test_a_file_that_cannot_be_opened_is_no_cache(self):
        """A sharing violation or a permissions error is an OSError; a directory in the file's
        place raises the same one."""
        snapshot_cache.get_cache_path().mkdir(parents=True)
        assert snapshot_cache.load_cache() == (None, None, None)
        assert snapshot_cache.load_deepseek_cache() == (None, None)


class TestSavingOverAFileThatCannotBeRead:
    """save_cache merges with what is on disk, and what is on disk may be anything."""

    @pytest.mark.parametrize("raw", [b"[]", b'"text"', b"3", b"\xff\xfe\x00", b"{not json", b""])
    def test_the_save_replaces_it(self, raw):
        path = snapshot_cache.get_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)

        snapshot_cache.save_cache(claude=claude_snapshot())

        claude, openai, _ = snapshot_cache.load_cache()
        assert claude is not None and claude.session_percent == 42.5
        assert openai is None
