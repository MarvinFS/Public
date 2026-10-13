"""The quota history file: what the solid line of the forecast is drawn from.

One JSON object per line in %LOCALAPPDATA%\\ClaudeBar\\quota_history.jsonl,
appended after every live fetch (never on a cache fallback) and pruned to ten
days the first time a process writes, and again once a day after that.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

import data_collector as dc
import quota_history as qh
from codex_log_parser import CodexTokenUsage
from config import get_config_dir
from models import DeepSeekSnapshot
from oauth_usage import OAuthUsageData, UsageWindow
from openai_usage import OpenAIUsageData, OpenAIUsageWindow
import deepseek_usage as du

UTC = timezone.utc
NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)


def h(hours):
    return timedelta(hours=hours)


@pytest.fixture(autouse=True)
def fresh_process(monkeypatch):
    """Each test is the first write of a new process."""
    monkeypatch.setattr(qh, "_last_prune", None)


def lines():
    path = qh.history_path()
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def write_lines(*raw):
    path = qh.history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(raw) + "\n", encoding="utf-8")


def record(t, p="claude", **kw):
    body = {"t": t.isoformat(), "p": p}
    body.update(kw)
    return json.dumps(body)


class TestAppendAndLoad:
    def test_the_file_lives_in_the_config_dir(self):
        assert qh.history_path() == get_config_dir() / "quota_history.jsonl"

    def test_a_round_trip_for_a_percentage_provider(self):
        reset = NOW + h(40)
        qh.append("claude", 61.0, resets_at=reset, now=NOW)
        assert qh.load("claude", NOW - h(1)) == [qh.Record(NOW, 61.0, reset)]

    def test_the_stored_line_has_the_documented_shape(self):
        qh.append("codex", 96.0, resets_at=NOW + h(40), now=NOW)
        assert json.loads(lines()[0]) == {
            "t": "2026-10-12T12:00:00+00:00", "p": "codex", "remaining": 96.0,
            "resets_at": "2026-10-14T04:00:00+00:00"}

    def test_deepseek_stores_a_balance_not_a_percentage(self):
        qh.append("deepseek", 12.4, now=NOW)
        assert json.loads(lines()[0]) == {"t": "2026-10-12T12:00:00+00:00", "p": "deepseek",
                                          "balance": 12.4}
        assert qh.load("deepseek", NOW - h(1)) == [qh.Record(NOW, 12.4, None)]

    def test_a_naive_reset_time_is_stored_as_utc(self):
        """Codex's reset time is naive local."""
        naive = datetime(2026, 10, 13, 4, 0)
        qh.append("codex", 50.0, resets_at=naive, now=NOW)
        stored = qh.load("codex", NOW - h(1))[0].resets_at
        assert stored.tzinfo == UTC
        assert stored.astimezone().replace(tzinfo=None) == naive

    def test_no_reset_time_is_simply_absent(self):
        qh.append("claude", 61.0, now=NOW)
        assert "resets_at" not in json.loads(lines()[0])
        assert qh.load("claude", NOW - h(1))[0].resets_at is None

    def test_every_append_is_one_more_line(self):
        for i in range(3):
            qh.append("claude", 90 - i, now=NOW + h(i))
        assert len(lines()) == 3

    def test_load_returns_only_the_provider_and_only_since(self):
        qh.append("claude", 90.0, now=NOW - h(30))
        qh.append("claude", 80.0, now=NOW - h(5))
        qh.append("codex", 70.0, now=NOW - h(5))
        qh.append("deepseek", 12.0, now=NOW - h(5))
        got = qh.load("claude", NOW - h(10))
        assert [(r.t, r.value) for r in got] == [(NOW - h(5), 80.0)]

    def test_load_is_sorted_by_time(self):
        write_lines(record(NOW - h(1), remaining=70.0), record(NOW - h(9), remaining=90.0),
                    record(NOW - h(5), remaining=80.0))
        assert [r.value for r in qh.load("claude", NOW - h(10))] == [90.0, 80.0, 70.0]

    def test_a_missing_file_is_an_empty_history(self):
        assert qh.load("claude", NOW - h(10)) == []

    def test_the_value_is_not_confused_between_providers(self):
        """The two arrays (time, value) must stay paired per record."""
        write_lines(record(NOW - h(3), "claude", remaining=61.0),
                    record(NOW - h(2), "deepseek", balance=12.4),
                    record(NOW - h(1), "claude", remaining=60.0))
        assert [(r.t, r.value) for r in qh.load("claude", NOW - h(10))] == [
            (NOW - h(3), 61.0), (NOW - h(1), 60.0)]
        assert [(r.t, r.value) for r in qh.load("deepseek", NOW - h(10))] == [(NOW - h(2), 12.4)]


class TestMalformedLines:
    def test_garbage_is_skipped_and_the_neighbours_survive(self):
        write_lines(
            record(NOW - h(5), remaining=80.0),
            "not json at all",
            "{",
            "[]",
            json.dumps({"p": "claude", "remaining": 1.0}),                       # no time
            json.dumps({"t": "yesterday", "p": "claude", "remaining": 1.0}),     # bad time
            json.dumps({"t": NOW.isoformat(), "p": "claude"}),                   # no value
            json.dumps({"t": NOW.isoformat(), "p": "claude", "remaining": "61"}),  # wrong type
            json.dumps({"t": NOW.isoformat(), "p": "claude", "remaining": True}),
            json.dumps({"t": NOW.isoformat(), "p": "claude", "remaining": float("nan")}),
            record(NOW - h(1), remaining=70.0),
            "",
        )
        assert [r.value for r in qh.load("claude", NOW - h(10))] == [80.0, 70.0]

    def test_a_half_written_last_line_is_ignored(self):
        path = qh.history_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(record(NOW - h(1), remaining=70.0) + '\n{"t": "2026-10-12T12', encoding="utf-8")
        assert [r.value for r in qh.load("claude", NOW - h(10))] == [70.0]

    def test_a_bad_reset_time_only_loses_the_reset_time(self):
        write_lines(record(NOW - h(1), remaining=70.0, resets_at="soon"))
        got = qh.load("claude", NOW - h(10))
        assert got == [qh.Record(NOW - h(1), 70.0, None)]


class TestPruning:
    def test_the_first_append_drops_what_is_older_than_ten_days(self):
        write_lines(record(NOW - h(24 * 11), remaining=99.0),
                    record(NOW - h(24 * 9), remaining=80.0))
        qh.append("claude", 70.0, now=NOW)
        assert [json.loads(line)["remaining"] for line in lines()] == [80.0, 70.0]

    def test_pruning_also_drops_malformed_lines(self):
        write_lines("junk", record(NOW - h(5), remaining=80.0))
        qh.append("claude", 70.0, now=NOW)
        assert all(line.startswith("{") and json.loads(line) for line in lines())
        assert len(lines()) == 2

    def test_appends_within_a_day_of_a_prune_do_not_rewrite_the_file(self):
        write_lines(record(NOW - h(24 * 11), remaining=99.0))
        qh.append("claude", 70.0, now=NOW)                  # prunes
        old = record(NOW - h(24 * 12), remaining=98.0)
        with open(qh.history_path(), "a", encoding="utf-8") as f:
            f.write(old + "\n")
        qh.append("claude", 69.0, now=NOW + h(23))          # must not rewrite again
        assert old in lines()

    def test_a_process_that_keeps_running_prunes_again_a_day_later(self):
        write_lines(record(NOW - h(24 * 11), remaining=99.0))
        qh.append("claude", 70.0, now=NOW)                  # prunes
        aged = record(NOW - h(24 * 9), remaining=80.0)      # kept now, ten days old a day later
        with open(qh.history_path(), "a", encoding="utf-8") as f:
            f.write(aged + "\n")
        qh.append("claude", 69.0, now=NOW + h(24 * 2))      # a day or more on: prunes again
        assert aged not in lines()
        assert [json.loads(line)["remaining"] for line in lines()] == [70.0, 69.0]

    def test_a_clean_file_is_left_untouched(self):
        write_lines(record(NOW - h(5), remaining=80.0))
        before = qh.history_path().stat().st_mtime_ns
        qh.append("claude", 70.0, now=NOW)
        assert len(lines()) == 2
        assert qh.history_path().stat().st_mtime_ns >= before

    def test_a_missing_file_is_created(self):
        qh.append("claude", 70.0, now=NOW)
        assert len(lines()) == 1


class TestBytesThatAreNotUtf8:
    def write_raw(self, data: bytes):
        path = qh.history_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def good(self):
        return record(NOW - h(1), remaining=70.0).encode("utf-8") + b"\n"

    def test_load_skips_the_bad_bytes_and_keeps_the_good_lines(self):
        self.write_raw(b"\xff\xfe\x80 not text\n" + self.good())
        assert [r.value for r in qh.load("claude", NOW - h(10))] == [70.0]

    def test_append_does_not_raise_and_prunes_the_bad_line(self):
        self.write_raw(b"\xff\xfe\x80 not text\n" + self.good())
        qh.append("claude", 65.0, now=NOW)                  # must not raise
        assert [r.value for r in qh.load("claude", NOW - h(10))] == [70.0, 65.0]
        assert b"\xef\xbf\xbd" not in qh.history_path().read_bytes()   # no replacement characters kept


class TestFailureNeverBreaksARefresh:
    def test_an_unwritable_location_is_swallowed(self, monkeypatch, tmp_path):
        blocker = tmp_path / "blocker"
        blocker.write_text("a file where the folder should be")
        monkeypatch.setattr(qh, "history_path", lambda: blocker / "quota_history.jsonl")
        qh.append("claude", 70.0, now=NOW)                  # must not raise
        assert qh.load("claude", NOW - h(1)) == []


# --- the collectors: recorded after a live fetch, never on a cache fallback ------------------

@pytest.fixture
def collector(tmp_path, monkeypatch):
    monkeypatch.setattr(dc, "collect_entries", lambda projects_dir: [])
    monkeypatch.setattr(dc, "get_codex_daily_usage",
                        lambda days, end: [CodexTokenUsage() for _ in range(days)])
    return dc.DataCollector(projects_dir=tmp_path)


def stored(provider):
    return qh.load(provider, datetime.now(UTC) - h(1))


class TestCollectorsRecordLiveFetchesOnly:
    def oauth(self, session=True):
        return OAuthUsageData(
            session=UsageWindow(utilization=12.0, resets_at=datetime(2099, 1, 1, tzinfo=UTC))
            if session else None,
            weekly=UsageWindow(utilization=39.0, resets_at=datetime(2099, 1, 5, tzinfo=UTC)))

    def test_claude_records_the_remaining_session_quota_and_its_reset(self, collector, monkeypatch):
        monkeypatch.setattr(dc, "fetch_oauth_usage", lambda: self.oauth())
        collector.collect()
        got = stored("claude_session")
        assert [(r.value, r.resets_at) for r in got] == [(88.0, datetime(2099, 1, 1, tzinfo=UTC))]
        assert stored("claude") == []                        # the weekly window is not charted

    def test_claude_records_nothing_on_the_cached_fallback(self, collector, monkeypatch):
        monkeypatch.setattr(dc, "fetch_oauth_usage", lambda: self.oauth())
        collector.collect()
        monkeypatch.setattr(dc, "fetch_oauth_usage", lambda: OAuthUsageData(error="offline"))
        collector.collect()
        assert len(stored("claude_session")) == 1

    def test_a_response_with_no_session_window_is_not_read_as_full(self, collector, monkeypatch):
        monkeypatch.setattr(dc, "fetch_oauth_usage", lambda: self.oauth(session=False))
        collector.collect()
        assert stored("claude_session") == []

    def test_each_refresh_adds_one_line(self, collector, monkeypatch):
        monkeypatch.setattr(dc, "fetch_oauth_usage", lambda: self.oauth())
        collector.collect()
        collector.collect()
        assert len(stored("claude_session")) == 2

    def codex(self, weekly=True, session=False):
        return OpenAIUsageData(
            five_hour=OpenAIUsageWindow(percent=44.0, window_seconds=18000) if session else None,
            weekly=OpenAIUsageWindow(percent=4.0, resets_at=datetime(2099, 1, 1),
                                     window_seconds=2592000) if weekly else None,
            plan_type="free")

    def test_codex_records_the_session_where_the_plan_has_one(self, collector, monkeypatch):
        monkeypatch.setattr(dc, "fetch_openai_usage", lambda: self.codex(session=True))
        collector.collect_openai()
        assert [r.value for r in stored("codex_session")] == [56.0]
        assert stored("codex") == []

    def test_codex_without_a_session_records_its_long_window(self, collector, monkeypatch):
        monkeypatch.setattr(dc, "fetch_openai_usage", lambda: self.codex())
        collector.collect_openai()
        assert [r.value for r in stored("codex")] == [96.0]
        assert stored("codex_session") == []

    def test_codex_records_nothing_on_the_cached_fallback(self, collector, monkeypatch):
        monkeypatch.setattr(dc, "fetch_openai_usage", lambda: self.codex())
        collector.collect_openai()
        monkeypatch.setattr(dc, "fetch_openai_usage", lambda: OpenAIUsageData(error="offline"))
        collector.collect_openai()
        assert len(stored("codex")) == 1

    def test_codex_with_only_a_session_window_records_the_session(self, collector, monkeypatch):
        monkeypatch.setattr(dc, "fetch_openai_usage", lambda: self.codex(weekly=False, session=True))
        collector.collect_openai()
        assert [r.value for r in stored("codex_session")] == [56.0]
        assert stored("codex") == []

    def isolate_deepseek(self, monkeypatch, summary, cached=None):
        monkeypatch.setattr(dc.deepseek_auth, "discover_user_token",
                            lambda: dc.deepseek_auth.Credential("tok", "saved"))
        monkeypatch.setattr(dc, "fetch_usage", lambda token, today: du.UsageData(error="no usage"))
        monkeypatch.setattr(dc, "fetch_summary", lambda token: summary)
        monkeypatch.setattr(dc, "load_deepseek_cache", lambda: (cached, None))

    def summary(self):
        return du.AccountSummary(available=True, currency="USD", topped_up=12.4, granted=0.0,
                                 total_cost=2.18, total_cost_available=True)

    def test_deepseek_records_the_balance(self, collector, monkeypatch):
        self.isolate_deepseek(monkeypatch, self.summary())
        collector.collect_deepseek()
        assert [r.value for r in stored("deepseek")] == [12.4]

    def test_deepseek_records_nothing_when_the_balance_came_from_the_cache(self, collector, monkeypatch):
        cached = DeepSeekSnapshot(balance_available=True, balance_usable=True, balance_total=99.0)
        self.isolate_deepseek(monkeypatch, du.AccountSummary(error="down"), cached=cached)
        collector.collect_deepseek()
        assert stored("deepseek") == []

    def test_deepseek_records_nothing_when_signed_out(self, collector, monkeypatch):
        monkeypatch.setattr(dc.deepseek_auth, "discover_user_token", lambda: None)
        collector.collect_deepseek()
        assert stored("deepseek") == []
