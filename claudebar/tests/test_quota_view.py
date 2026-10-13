"""The quota view on the real panel: what it says when the reading it holds belongs
to a session that has already ended, and that a live one still draws."""

import time
import tkinter as tk
from datetime import datetime, timedelta, timezone

import pytest

import ui_window
from config import Config
from models import CombinedSnapshot, Engine, OpenAISnapshot, UsageSnapshot

UTC = timezone.utc


def h(hours):
    return timedelta(hours=hours)


@pytest.fixture(scope="module")
def tk_root():
    """One hidden Tk root for the module: a root per test makes Tcl's init fail
    intermittently."""
    try:
        root = tk.Tk()
    except tk.TclError as e:
        pytest.skip(f"no display for Tk: {e}")
    root.withdraw()
    yield root
    root.destroy()


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
    def test_a_reading_from_before_the_reset_is_not_shown_as_the_current_quota(self, panel):
        snap = claude(-timedelta(minutes=2, seconds=10), used=14.0, stale=True)
        snap.session_reset = "in 3m"                        # the words from when it was collected
        show(panel, claude=snap)
        shown = bars(panel)
        assert shown["reset"] == "Reset 2m ago"
        assert shown["left"] == "Waiting for the next reading"
        assert shown["bar"] == (0.0, None)
        assert shown["note"] == "" and shown["pace"] == ""

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
        assert bars(panel)["left"] == "Waiting for the next reading"

    def test_nothing_is_redrawn_before_the_minute_is_up(self, panel):
        panel._window.deiconify()
        snap = claude(h(1))
        show(panel, claude=snap)
        snap.session_resets_at = datetime.now(UTC) - timedelta(seconds=30)
        panel._redraw_at = time.monotonic() + 30
        panel._redraw_if_due()
        assert bars(panel)["left"] == "71% left"


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

    def test_a_cached_reading_inside_its_session_is_charted_and_marked_cached(self, panel):
        show(panel, claude=claude(h(3), stale=True))
        assert panel._fc_chart._data is not None
        assert "cached" in panel._fc_rate.cget("text")
