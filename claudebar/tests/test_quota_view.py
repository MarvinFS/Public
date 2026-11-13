"""The quota view on the real panel: what it says when the reading it holds belongs
to a session that has already ended, and that a live one still draws."""

import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

import log_parser
import quota_history
import ui_window
from config import Config
from models import CombinedSnapshot, Engine, OpenAISnapshot, UsageSnapshot

UTC = timezone.utc


def h(hours):
    return timedelta(hours=hours)


@pytest.fixture
def panel(tk_root, monkeypatch):
    monkeypatch.setattr(ui_window, "get_exchange_rates", lambda: None)
    monkeypatch.setattr(ui_window, "check_claude_status", lambda: None)
    monkeypatch.setattr(ui_window, "save_config", lambda cfg: None)
    config = Config()
    config.panel_view = "forecast"
    window = ui_window.ClaudeBarWindow(on_refresh=lambda: None, on_settings=lambda: None,
                                       on_exit=lambda: None, config=config,
                                       on_engine_change=lambda e: None)
    window._create_window()
    yield window
    window.destroy()


def show(window, claude=None, openai=None, engine=Engine.CLAUDE):
    window._active_engine = engine
    window.update_combined(CombinedSnapshot(claude=claude, openai=openai))
    window._apply_snapshot_update()
    window._window.update()


def claude(resets_in, used=29.0, stale=False):
    now = datetime.now(UTC)
    return UsageSnapshot(
        timestamp=datetime.now(), session_percent=used, session_resets_at=now + resets_in,
        weekly_percent=50.0, weekly_resets_at=now + h(60), is_stale=stale,
        stale_since=datetime.now() if stale else None)


class TestASessionThatHasEnded:
    def test_a_reading_from_before_the_reset_is_not_charted(self, panel):
        show(panel, claude=claude(-h(0.1), stale=True))
        assert panel._fc_chart._data is None
        assert "reset" in panel._fc_chart._message.lower()
        assert panel._fc_value.cget("text") == "\u2014"

    def test_a_live_reading_whose_reset_just_passed_says_the_same(self, panel):
        show(panel, claude=claude(-h(0.01)))
        assert panel._fc_chart._data is None
        assert "reset" in panel._fc_chart._message.lower()

    def test_no_rows_claim_anything_about_the_old_session(self, panel):
        show(panel, claude=claude(-h(0.1), stale=True))
        assert not [row for row, _, _ in panel._fc_rows if row.winfo_manager()]

    def test_a_codex_long_window_that_ended_says_so_too(self, panel):
        codex = OpenAISnapshot(session_available=False, weekly_percent=4.0, weekly_window_hours=720.0,
                               available=True, weekly_resets_at=datetime.now() - h(1))
        show(panel, claude=claude(h(2)), openai=codex, engine=Engine.CODEX)
        assert panel._fc_chart._data is None
        assert "reset" in panel._fc_chart._message.lower()


def bars(panel, key="session"):
    w = panel._limits[key]
    return {"reset": w["reset"].cget("text"), "left": w["left"].cget("text"),
            "note": w["note"].cget("text"), "pace": w["pace"].cget("text"), "bar": w["bar"]._value}


class TestTheLimitBarsAfterAReset:
    def test_a_window_that_has_reset_shows_as_full_until_the_next_reading(self, panel):
        # A reset always brings the window back to nothing used, and a refused request
        # can keep the next reading away for twenty minutes or more.
        snap = claude(-timedelta(minutes=2, seconds=10), used=14.0, stale=True)
        snap.session_reset = "in 3m"                        # the words from when it was collected
        show(panel, claude=snap)
        shown = bars(panel)
        assert shown["reset"] == "Reset 2m ago"
        assert shown["left"] == "100% left"
        assert shown["pace"] == "New session"
        assert shown["bar"] == (0.0, None)
        assert shown["note"] == ""

    def test_a_weekly_window_that_has_reset_is_full_too(self, panel):
        snap = claude(h(2))
        snap.weekly_resets_at = datetime.now(UTC) - timedelta(minutes=5)
        show(panel, claude=snap)
        shown = bars(panel, "weekly")
        assert shown["left"] == "100% left"
        assert shown["pace"] == "New window"
        assert shown["bar"] == (0.0, None)

    def test_the_weekly_bar_is_not_touched(self, panel):
        show(panel, claude=claude(-h(0.1), stale=True))
        assert bars(panel, "weekly")["left"] == "50% left"

    def test_a_running_session_counts_down_from_its_reset_time(self, panel):
        snap = claude(h(1.5) + timedelta(seconds=30))
        snap.session_reset = "in 3m"                        # frozen words must not win
        show(panel, claude=snap)
        shown = bars(panel)
        assert shown["reset"] == "Resets in 1h 30m"
        assert shown["left"] == "71% left"

    def test_a_reading_with_no_reset_time_keeps_its_own_words(self, panel):
        snap = claude(h(1))
        snap.session_resets_at, snap.session_reset = None, "in about an hour"
        show(panel, claude=snap)
        assert bars(panel)["reset"] == "Resets in about an hour"

    def test_the_bars_are_redrawn_each_minute_so_a_reset_is_noticed(self, panel):
        panel._window.deiconify()
        snap = claude(timedelta(minutes=58, seconds=30))
        show(panel, claude=snap)
        assert bars(panel)["reset"] == "Resets in 58m"
        snap.session_resets_at = datetime.now(UTC) - timedelta(seconds=30)
        panel._redraw_at = 0.0                              # a minute has gone by
        panel._redraw_if_due()
        assert bars(panel)["left"] == "100% left"

    def test_nothing_is_redrawn_before_the_minute_is_up(self, panel):
        panel._window.deiconify()
        snap = claude(h(1))
        show(panel, claude=snap)
        snap.session_resets_at = datetime.now(UTC) - timedelta(seconds=30)
        panel._redraw_at = time.monotonic() + 30
        panel._redraw_if_due()
        assert bars(panel)["left"] == "71% left"


class TestAFailedFetchIsNotAPendingOne:
    """The first render after start-up has `_is_collecting` still set (it is cleared at the end of the render, which
    the meta line needs), and with both percentages at zero that made a definitive failure read as "Collecting..."."""

    def test_unavailable_wins_over_collecting_on_the_first_render(self, panel):
        assert panel._is_collecting
        show(panel, claude=UsageSnapshot(cli_available=False))
        assert bars(panel)["left"] == "Unavailable" and bars(panel, "weekly")["left"] == "Unavailable"

    def test_a_first_reading_with_nothing_wrong_still_says_collecting(self, panel):
        show(panel, claude=UsageSnapshot())
        assert bars(panel)["left"] == "Collecting..." and bars(panel, "weekly")["left"] == "Collecting..."

    def test_a_cached_reading_is_neither(self, panel):
        panel._is_collecting = False
        show(panel, claude=claude(h(2), stale=True))
        assert bars(panel)["left"] == "71% left"


def wrote_log(minutes_ago):
    """A Claude Code log last written this long ago, in the folder the panel looks in."""
    path = log_parser.get_claude_projects_dir() / "project" / "session.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n")
    stamp = time.time() - minutes_ago * 60
    os.utime(path, (stamp, stamp))


def no_session(read_minutes_ago=14):
    """A cached reading taken when no session had begun, as on 16:39 in the field."""
    return UsageSnapshot(timestamp=datetime.now(), session_percent=0.0, session_resets_at=None,
                         weekly_percent=50.0, weekly_resets_at=datetime.now(UTC) + h(60), is_stale=True,
                         stale_since=datetime.now() - timedelta(minutes=read_minutes_ago))


def looked(window):
    """The look at the logs runs on a thread of its own: wait for the one that is running."""
    deadline = time.monotonic() + 5
    while window._looking and time.monotonic() < deadline:
        time.sleep(0.005)
    assert not window._looking


def look(window):
    """Wait for the look, then redraw as the tick does when the look has found the screen out of date."""
    looked(window)
    window._window.deiconify()
    window._window.update()
    window._redraw_if_due()


class TestASessionTheReadingCannotSee:
    """The usage endpoint can refuse for twenty minutes. A cached reading that shows no session
    must not say "100% left" once the logs show requests since: it does not know."""

    def test_requests_since_the_reading_say_a_session_is_running(self, panel):
        wrote_log(minutes_ago=3)
        show(panel, claude=no_session())
        look(panel)
        shown = bars(panel)
        assert shown["left"] == "Session running"
        assert shown["pace"] == "Usage shows with the next reading"
        assert shown["reset"] == shown["note"] == ""
        assert shown["bar"] == (0.0, None)
        assert panel._fc_chart._message == "Session running - the chart starts with the next reading"
        assert panel._fc_chart._data is None

    def test_the_weekly_bar_keeps_its_figure(self, panel):
        wrote_log(minutes_ago=3)
        show(panel, claude=no_session())
        look(panel)
        assert bars(panel, "weekly")["left"] == "50% left"

    def test_no_requests_since_the_reading_leaves_it_as_it_was(self, panel):
        wrote_log(minutes_ago=20)                       # before the reading was taken
        show(panel, claude=no_session())
        look(panel)
        assert bars(panel)["left"] == "100% left"
        assert panel._fc_chart._message == "No session running"

    def test_no_logs_at_all_leaves_it_as_it_was(self, panel):
        show(panel, claude=no_session())
        look(panel)
        assert bars(panel)["left"] == "100% left"

    def test_a_request_after_the_reset_of_the_reading_s_session_says_it_too(self, panel):
        wrote_log(minutes_ago=3)
        show(panel, claude=claude(-h(0.8), used=59.0, stale=True))
        look(panel)
        assert bars(panel)["left"] == "Session running"
        assert "starts with the next reading" in panel._fc_chart._message

    def test_a_request_before_the_reset_is_the_old_session(self, panel):
        wrote_log(minutes_ago=90)
        show(panel, claude=claude(-h(0.8), used=59.0, stale=True))
        look(panel)
        assert bars(panel)["left"] == "100% left"
        assert bars(panel)["pace"] == "New session"

    def test_a_session_at_work_when_the_window_ended_is_running_before_its_next_request(self, panel):
        """21:50 in the field: the last request came 8 s before the reset and the next one 39 s
        after it (a tool call in between). Drawn in that gap, the panel said "New session"."""
        wrote_log(minutes_ago=38 / 60)
        show(panel, claude=claude(-timedelta(seconds=30), used=9.0, stale=True))
        looked(panel)
        assert panel._redraw_at == 0.0              # the look found the screen out of date by this rule
        look(panel)
        assert bars(panel)["left"] == "Session running"

    def test_a_session_quiet_for_minutes_at_the_reset_is_a_new_session(self, panel):
        wrote_log(minutes_ago=5)
        show(panel, claude=claude(-timedelta(seconds=30), used=9.0, stale=True))
        look(panel)
        assert bars(panel)["left"] == "100% left"
        assert bars(panel)["pace"] == "New session"

    def test_a_live_reading_is_believed(self, panel):
        wrote_log(minutes_ago=1)
        snap = no_session()
        snap.is_stale = False
        show(panel, claude=snap)
        assert bars(panel)["left"] == "100% left"

    def test_the_next_reading_replaces_it(self, panel):
        wrote_log(minutes_ago=3)
        show(panel, claude=no_session())
        show(panel, claude=claude(h(4.9), used=1.0))
        assert bars(panel)["left"] == "99% left"
        assert bars(panel)["pace"] != "Usage shows with the next reading"
        assert panel._fc_chart._data is not None


class TestTheLookAtTheLogsIsNotMadeOnTheUiThread:
    """last_log_write stats every transcript (about 90 ms for 1,500 of them). A redraw of a cached
    reading that shows no session asked for it inline, on the Tk thread, each time."""

    @pytest.fixture
    def spied(self, monkeypatch):
        """Every directory walk and every stat of a transcript, with whether the UI thread made it."""
        seen = {"ui_stats": 0, "other_stats": 0, "ui_walks": 0, "other_walks": 0}
        real_stat, real_scandir = os.stat, os.scandir

        def on_ui():
            return threading.current_thread() is threading.main_thread()

        def stat(path, *args, **kwargs):
            if str(path).endswith(".jsonl"):
                seen["ui_stats" if on_ui() else "other_stats"] += 1
            return real_stat(path, *args, **kwargs)

        def scandir(path=".", *args, **kwargs):
            if str(log_parser.get_claude_projects_dir()) in str(path):
                seen["ui_walks" if on_ui() else "other_walks"] += 1
            return real_scandir(path, *args, **kwargs)

        monkeypatch.setattr(os, "stat", stat)
        monkeypatch.setattr(os, "scandir", scandir)
        return seen

    def test_a_redraw_makes_no_directory_walk_and_no_stat_storm_on_the_ui_thread(self, panel, spied):
        for i in range(300):
            path = log_parser.get_claude_projects_dir() / f"project-{i % 10}" / f"session-{i}.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}\n")
        wrote_log(minutes_ago=3)
        spied.update(ui_stats=0, other_stats=0, ui_walks=0, other_walks=0)

        show(panel, claude=no_session())                 # the redraw that asks
        panel._refresh_display_immediate()               # and the minute's redraw
        panel._refresh_display_immediate()
        assert spied["ui_stats"] == 0 and spied["ui_walks"] == 0

        look(panel)                                      # the walk was made, by the thread that looks
        assert spied["other_stats"] >= 300 and spied["other_walks"] >= 1
        assert spied["ui_stats"] == 0 and spied["ui_walks"] == 0
        assert bars(panel)["left"] == "Session running"

    def test_the_screen_is_corrected_when_the_look_finds_it_out_of_date(self, panel):
        wrote_log(minutes_ago=3)
        show(panel, claude=no_session())                 # nothing is known yet: drawn as no session
        looked(panel)
        assert panel._redraw_at == 0.0                   # the look asks for a redraw
        look(panel)
        assert bars(panel)["left"] == "Session running"

    def test_a_look_that_changes_nothing_asks_for_no_redraw(self, panel):
        wrote_log(minutes_ago=20)                        # before the reading: nothing running
        show(panel, claude=no_session())
        look(panel)
        assert panel._redraw_at > time.monotonic()
        wrote_log(minutes_ago=3)                         # a session begins
        panel._redraw_at = 0.0                           # the minute is up: redrawn, and it looks again
        look(panel)                                      # this redraw still draws what the last look saw ...
        looked(panel)
        assert panel._redraw_at == 0.0                   # ... and the look it started asks for another
        look(panel)
        looked(panel)
        assert bars(panel)["left"] == "Session running"
        assert panel._redraw_at > time.monotonic()       # the answer holds: no further redraw is asked for

    def test_one_look_at_a_time(self, panel, monkeypatch):
        gate, looks = threading.Event(), []

        def blocked():
            looks.append(1)
            gate.wait(5)
            return None

        monkeypatch.setattr(log_parser, "last_log_write", blocked)
        for _ in range(5):
            show(panel, claude=no_session())
        time.sleep(0.1)
        assert looks == [1]
        gate.set()
        look(panel)

    def test_a_look_that_fails_leaves_the_screen_as_it_was(self, panel, monkeypatch, caplog):
        def broken():
            raise RuntimeError("the folder went away")

        monkeypatch.setattr(log_parser, "last_log_write", broken)
        show(panel, claude=no_session())
        look(panel)
        assert bars(panel)["left"] == "100% left" and not panel._looking
        assert "the folder went away" in caplog.text


class TestTheRunOutOnTheLimitBar:
    """The bar and the quota view answer one question, so they use one rate: how fast the
    quota drained lately, not the average over the whole session."""

    def record(self, key, snap, minutes_ago_remaining):
        now = datetime.now(UTC)
        for minutes, remaining in minutes_ago_remaining:
            quota_history.append(key, remaining, snap.session_resets_at, now=now - timedelta(minutes=minutes))

    def test_a_charted_session_runs_out_at_the_recent_rate(self, panel):
        snap = claude(h(2), used=76.0)          # 3 h in: the session average is 25 %/h
        self.record("claude_session", snap, [(60, 72.0), (30, 48.0), (15, 36.0)])   # 48 %/h lately
        show(panel, claude=snap)
        assert bars(panel)["note"] == "Runs out in 0h 30m"      # 24% left at 48 %/h

    def test_the_quota_view_row_gives_the_clock_time_the_bar_has_as_a_countdown(self, panel):
        # One prediction, said once in each form: the countdown under the bar, the clock time in
        # the view's row, and the countdown is not printed a second time.
        snap = claude(h(2), used=76.0)
        self.record("claude_session", snap, [(60, 72.0), (30, 48.0), (15, 36.0)])
        show(panel, claude=snap)
        row = next(right.cget("text") for _, left, right in panel._fc_rows
                   if left.cget("text") == "At this rate")
        assert bars(panel)["note"] == "Runs out in 0h 30m"
        assert re.fullmatch(r"Runs out [A-Z][a-z]{2} \d\d:\d\d", row), row

    def test_a_session_with_no_history_keeps_the_window_average(self, panel):
        show(panel, claude=claude(h(2), used=76.0))
        assert bars(panel)["note"] == "Runs out in 0h 56m"      # 24% left at 76% over 3 h

    def test_a_flat_recent_history_lasts_until_the_reset(self, panel):
        snap = claude(h(2), used=76.0)
        self.record("claude_session", snap, [(60, 24.0), (30, 24.0), (15, 24.0)])
        show(panel, claude=snap)
        assert bars(panel)["note"] == "Lasts until reset"

    def test_the_weekly_bar_has_no_history_and_keeps_the_window_average(self, panel):
        snap = claude(h(2), used=76.0)
        snap.weekly_percent = 70.0              # 108 h in: 0.65 %/h, so 30% left lasts 46 h
        self.record("claude_session", snap, [(60, 72.0), (30, 48.0), (15, 36.0)])
        show(panel, claude=snap)
        assert bars(panel, "weekly")["note"] == "Runs out in 1d 22h"

    def test_a_codex_long_window_is_the_charted_one_when_the_plan_has_no_session(self, panel):
        resets = datetime.now(UTC) + h(24 * 20)
        codex = OpenAISnapshot(session_available=False, weekly_percent=50.0, weekly_window_hours=720.0,
                               available=True, weekly_resets_at=resets)
        now = datetime.now(UTC)
        for hours_ago, remaining in [(48, 80.0), (36, 70.0), (24, 60.0), (12, 54.0)]:
            quota_history.append("codex", remaining, resets, now=now - h(hours_ago))
        show(panel, claude=claude(h(2)), openai=codex, engine=Engine.CODEX)
        # 26 points over 36 h = 0.72 %/h: the 50% left lasts 69 h. The window average (10 days
        # in, 50% used) would say 10 days.
        assert bars(panel, "weekly")["note"] == "Runs out in 2d 21h"


class TestWhatTheHeaderSaysAboutOldFigures:
    """One time: when the figures were last updated. Old ones say so beside it, instead of
    a second time ("Updated 16:28 (from 16:23)") that reads as a contradiction."""

    def test_old_figures_show_their_own_time_and_say_cached(self, panel):
        panel._is_collecting = False                        # a first snapshot has come in already
        snap = claude(h(1), stale=True)
        snap.timestamp = datetime.now().replace(hour=16, minute=28, second=3, microsecond=0)   # the failed attempt
        snap.stale_since = datetime.now().replace(hour=16, minute=22, second=57, microsecond=0)
        show(panel, claude=snap)
        assert panel._updated_label.cget("text") == "Updated 16:22"
        assert panel._stale_label.cget("text") == "(cached)"

    def test_a_reading_from_an_earlier_day_carries_its_date(self, panel):
        panel._is_collecting = False
        snap = claude(h(1), stale=True)
        snap.stale_since = datetime(2026, 10, 11, 9, 5)
        show(panel, claude=snap)
        assert panel._updated_label.cget("text") == "Updated Oct 11 09:05"

    def test_the_first_cached_snapshot_says_fresh_data_is_being_collected(self, panel):
        show(panel, claude=claude(h(1), stale=True))
        assert panel._updated_label.cget("text") == "Collecting fresh data..."
        assert panel._stale_label.cget("text") == "(cached)"

    def test_a_fresh_reading_shows_its_time_and_nothing_else(self, panel):
        snap = claude(h(1))
        snap.timestamp = datetime.now().replace(hour=16, minute=28, second=3, microsecond=0)
        show(panel, claude=snap)
        assert panel._updated_label.cget("text") == "Updated 16:28"
        assert panel._stale_label.cget("text") == ""


class TestASessionStillRunning:
    def test_a_reading_inside_its_session_is_charted(self, panel):
        show(panel, claude=claude(h(3)))
        assert panel._fc_chart._data is not None

    def test_a_cached_reading_inside_its_session_is_charted_and_the_header_alone_says_cached(self, panel):
        panel._is_collecting = False
        show(panel, claude=claude(h(3), stale=True))
        assert panel._fc_chart._data is not None
        assert panel._stale_label.cget("text") == "(cached)"
        assert panel._fc_caption.cget("text") == "5-hour session"

    def test_the_headline_is_the_burn_rate_and_not_the_percent_left(self, panel):
        show(panel, claude=claude(h(3), used=29.0))
        assert bars(panel)["left"] == "71% left"
        assert panel._fc_value.cget("text").startswith("~") and panel._fc_value.cget("text").endswith("%/h")
        assert panel._fc_caption.cget("text") == "5-hour session"

    def test_a_codex_long_window_is_captioned_with_its_length(self, panel):
        codex = OpenAISnapshot(session_available=False, weekly_percent=4.0, weekly_window_hours=720.0,
                               available=True, weekly_resets_at=datetime.now(UTC) + h(24 * 20))
        show(panel, claude=claude(h(2)), openai=codex, engine=Engine.CODEX)
        assert panel._fc_caption.cget("text") == "30-day window"
