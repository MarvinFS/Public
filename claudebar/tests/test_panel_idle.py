"""What the panel does when nobody is looking at it.

The 50 ms update tick used to run for the life of the process once the panel had been opened
once, and every Refresh click and every window build spawned a `claude auth status`.
"""

import logging
import threading
import time
import tkinter as tk
import types

import pytest

import ui_window
from claude_check import ClaudeStatus
from config import Config


class FakeRoot:
    """The hidden root the tick is scheduled on."""

    def __init__(self):
        self.scheduled = []         # (delay in ms, job id)
        self.cancelled = []

    def after(self, ms, fn):
        job = f"after#{len(self.scheduled) + 1}"
        self.scheduled.append((ms, job))
        self.fn = fn
        return job

    def after_cancel(self, job):
        self.cancelled.append(job)


class FakeWindow:
    def __init__(self, viewable):
        self.master = FakeRoot()
        self.viewable = viewable

    def winfo_exists(self):
        return True

    def winfo_viewable(self):
        return self.viewable


@pytest.fixture
def panel(monkeypatch):
    window = ui_window.ClaudeBarWindow(on_refresh=lambda: None, on_settings=lambda: None,
                                       on_exit=lambda: None, config=Config())
    window.steps = []
    for name in ("_apply_snapshot_update", "_apply_redeem_result", "_redraw_if_due", "_update_status_display"):
        monkeypatch.setattr(window, name, lambda name=name: window.steps.append(name))
    return window


def with_window(panel, viewable):
    panel._window = FakeWindow(viewable)
    return panel._window.master


class TestTheUpdateTick:
    def test_it_runs_every_50_ms_while_the_panel_is_visible(self, panel):
        root = with_window(panel, viewable=True)
        panel._update_tick()
        assert [ms for ms, _ in root.scheduled] == [50]

    def test_it_slows_to_500_ms_while_the_panel_is_hidden(self, panel):
        root = with_window(panel, viewable=False)
        panel._update_tick()
        assert [ms for ms, _ in root.scheduled] == [500]

    def test_everything_it_does_still_happens_while_hidden(self, panel):
        """A snapshot that arrives, a banked-reset result that lands and the status flag are
        picked up on the slow tick: later, not never."""
        root = with_window(panel, viewable=False)
        panel._status_needs_update = True
        panel._update_tick()
        assert panel.steps == ["_apply_snapshot_update", "_apply_redeem_result", "_redraw_if_due",
                               "_update_status_display"]
        assert panel._status_needs_update is False

    def test_opening_the_panel_does_not_wait_out_the_slow_tick(self, panel):
        """show() starts the loop; when it already runs slowly it is brought forward, or the
        panel would open with a half second of old figures."""
        root = with_window(panel, viewable=False)
        panel._start_update_loop()                      # first open: runs, schedules the slow tick
        (_, slow_job), = root.scheduled
        panel._window.viewable = True

        panel._start_update_loop()                      # opened again

        assert root.cancelled == [slow_job]
        assert [ms for ms, _ in root.scheduled][-1] == 50

    def test_only_one_loop_runs(self, panel):
        root = with_window(panel, viewable=True)
        panel._start_update_loop()
        panel._start_update_loop()
        panel._start_update_loop()
        assert len(root.scheduled) - len(root.cancelled) == 1       # one pending tick, ever

    def test_a_status_update_that_fails_is_logged_and_the_loop_goes_on(self, panel, monkeypatch, caplog):
        """The three steps before it were guarded, this one was not: an exception from it left
        the tick unscheduled, and the panel never updated again for the rest of the process."""
        root = with_window(panel, viewable=True)
        panel._loop_running = True
        panel._status_needs_update = True

        def broken():
            raise RuntimeError("status row broke")

        monkeypatch.setattr(panel, "_update_status_display", broken)
        with caplog.at_level(logging.ERROR, logger="claudebar"):
            panel._update_tick()

        (record,) = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert record.exc_info and "status row broke" in str(record.exc_info[1])
        assert [ms for ms, _ in root.scheduled] == [50], "the next tick is still scheduled"
        assert panel._loop_running and panel._status_needs_update is False

    def test_a_tcl_error_in_the_status_update_still_ends_the_loop(self, panel, monkeypatch):
        """The window is gone: as for the other steps, that is the end of the loop and no error."""
        root = with_window(panel, viewable=True)
        panel._loop_running = True
        panel._status_needs_update = True

        def gone():
            raise tk.TclError("invalid command name")

        monkeypatch.setattr(panel, "_update_status_display", gone)
        panel._update_tick()
        assert root.scheduled == [] and not panel._loop_running


class TestTheClaudeStatusLoader:
    @pytest.fixture
    def loader(self, panel, monkeypatch):
        """The panel's two lookups, counted. `gate` holds the CLI call open."""
        calls = types.SimpleNamespace(cli=0, rates=0, fail=False, gate=threading.Event(),
                                      status=ClaudeStatus(installed=True, authenticated=True, plan="max"))
        calls.gate.set()

        def check():
            calls.cli += 1
            calls.gate.wait(5)
            if calls.fail:
                raise OSError("claude is not there")
            return calls.status

        def rates():
            calls.rates += 1

        monkeypatch.setattr(ui_window, "check_claude_status", check)
        monkeypatch.setattr(ui_window, "get_exchange_rates", rates)
        return calls

    @staticmethod
    def load(panel):
        """One load, waited for: the loader's last act is to tell the status row to look again."""
        panel._status_needs_update = False
        panel._load_initial_data()
        deadline = time.monotonic() + 5
        while not panel._status_needs_update and time.monotonic() < deadline:
            time.sleep(0.005)
        assert panel._status_needs_update, "the loader never finished"

    def test_clicks_while_a_check_runs_start_no_other(self, panel, loader):
        loader.gate.clear()
        for _ in range(4):                      # a window build, then three clicks on Refresh
            panel._load_initial_data()
        time.sleep(0.2)                         # long enough for every thread that started to reach the CLI
        loader.gate.set()
        deadline = time.monotonic() + 5
        while not panel._status_needs_update and time.monotonic() < deadline:
            time.sleep(0.005)
        assert loader.cli == 1

    def test_the_cli_runs_at_most_once_a_minute_and_the_answer_is_reused(self, panel, loader):
        self.load(panel)
        self.load(panel)
        self.load(panel)
        assert loader.cli == 1
        assert panel._claude_status is loader.status

        panel._claude_status_at -= 61           # a minute has passed
        self.load(panel)
        assert loader.cli == 2

    def test_the_exchange_rates_keep_their_own_rules(self, panel, loader):
        """They were looked up on every load, and are: the cache file and the failure memory
        decide whether that costs a request."""
        self.load(panel)
        self.load(panel)
        assert loader.rates == 2

    def test_a_check_that_failed_is_asked_again_at_the_next_load(self, panel, loader):
        loader.fail = True
        self.load(panel)                        # the row is still told to look again
        loader.fail = False
        self.load(panel)
        assert loader.cli == 2 and panel._claude_status is loader.status

    def test_a_switched_off_claude_is_not_asked_about(self, panel, loader):
        panel.config.claude_enabled = False
        self.load(panel)
        assert loader.cli == 0
        panel.config.claude_enabled = True      # switched back on: the rebuild loads again
        self.load(panel)
        assert loader.cli == 1
