"""Refresh requests and the panel's engine across threads (verification P2, P3).

The TLA+ model in verification/PanelThreads.tla found both; these reproduce
them against the real ClaudeBar._refresh and ClaudeBarWindow.update_combined.
"""

import threading
import time
import types

import pytest

import config
import main
import ui_window
from config import Config
from models import CombinedSnapshot, Engine


class BlockingCollector:
    """collect_combined waits on `gate` the first time, and counts calls."""

    def __init__(self):
        self.gate = threading.Event()
        self.started = threading.Event()
        self.calls = 0
        self.active_engine = Engine.CLAUDE

    def collect_combined(self):
        self.calls += 1
        if self.calls == 1:
            self.started.set()
            self.gate.wait(5)
        return CombinedSnapshot(active_engine=self.active_engine)


def make_app(collector):
    app = main.ClaudeBar.__new__(main.ClaudeBar)
    app.collector = collector
    app.tray = None
    app._refresh_lock = threading.Lock()
    app._refresh_again = False
    return app


def test_request_during_a_collect_gets_a_collect_of_its_own():
    """P3: a refresh asked for while one is running (the Refresh button, a sign-in) is
    followed by a collect that starts after it, not dropped."""
    collector = BlockingCollector()
    app = make_app(collector)
    running = threading.Thread(target=app._refresh)
    running.start()
    assert collector.started.wait(5)

    app._refresh()                    # arrives mid-collect, returns at once
    collector.gate.set()
    running.join(5)

    assert collector.calls == 2


def test_delivery_does_not_move_the_panel_off_the_chosen_tab():
    """P2: a snapshot collected before a tab click carries the old engine; the
    panel keeps the engine the user picked."""
    window = ui_window.ClaudeBarWindow(on_refresh=lambda: None, on_settings=lambda: None,
                                       on_exit=lambda: None, config=Config())
    window._active_engine = Engine.DEEPSEEK
    window.update_combined(CombinedSnapshot(active_engine=Engine.CLAUDE))
    assert window._active_engine == Engine.DEEPSEEK


class RecordingCollector:
    """Counts collects; every one answers at once."""

    def __init__(self):
        self.calls = 0
        self.active_engine = Engine.CLAUDE
        self.chosen = []

    def collect_combined(self):
        self.calls += 1
        return CombinedSnapshot()

    def set_active_engine(self, engine):
        self.active_engine = engine
        self.chosen.append(engine)


def test_a_tab_click_only_records_the_engine(monkeypatch):
    """The panel already holds every engine's snapshot, so switching tabs needs no collect
    (a log parse and three network calls) and no thread of its own."""
    collector = RecordingCollector()
    app = make_app(collector)
    threads = []
    monkeypatch.setattr(main.threading, "Thread", lambda *a, **k: threads.append(k) or types.SimpleNamespace(
        start=lambda: None))

    app._on_engine_change(Engine.CODEX)

    assert collector.chosen == [Engine.CODEX]
    assert threads == [] and collector.calls == 0


def test_the_panel_switches_tabs_from_the_snapshots_it_holds():
    """ClaudeBarWindow._on_engine_select tells the app which engine is shown and redraws; it
    never asks for new data (on_refresh is the Refresh button's)."""
    told, refreshed = [], []
    window = ui_window.ClaudeBarWindow(on_refresh=lambda: refreshed.append(1), on_settings=lambda: None,
                                       on_exit=lambda: None, config=Config(),
                                       on_engine_change=told.append)
    redrawn = []
    window._refresh_display_immediate = lambda: redrawn.append(window._active_engine)

    window._on_engine_select(Engine.DEEPSEEK)

    assert told == [Engine.DEEPSEEK] and redrawn == [Engine.DEEPSEEK] and refreshed == []


def make_loop_app(collector, interval):
    app = main.ClaudeBar()
    app.collector = collector
    app.config.refresh_interval = interval
    app._running = True
    return app


def wait_for(condition, seconds=3.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def test_exit_ends_the_wait_between_refreshes_at_once():
    collector = RecordingCollector()
    app = make_loop_app(collector, 300)
    loop = threading.Thread(target=app._refresh_loop, daemon=True)
    loop.start()
    assert wait_for(lambda: collector.calls == 1)

    app._on_exit()

    loop.join(0.3)                      # it used to look again once a second
    assert not loop.is_alive()


def test_a_saved_refresh_interval_applies_at_once_and_a_save_alone_collects_nothing():
    collector = RecordingCollector()
    app = make_loop_app(collector, 300)
    loop = threading.Thread(target=app._refresh_loop, daemon=True)
    loop.start()
    assert wait_for(lambda: collector.calls == 1)

    app.config.refresh_interval = 300
    app._on_settings_saved()            # the same interval: the wait starts over, no refresh
    time.sleep(0.3)
    assert collector.calls == 1

    app.config.refresh_interval = 1     # a shorter one is used from this moment, not after the old one runs out
    app._on_settings_saved()
    assert wait_for(lambda: collector.calls == 2, seconds=3.0)

    app._on_exit()
    loop.join(2)


def test_the_cached_snapshot_shown_at_start_leaves_out_an_engine_switched_off(monkeypatch):
    """The figures are on screen before the first refresh; an engine the user switched off
    must not be among them (the first refresh would drop it, but only seconds later)."""
    from datetime import datetime
    import snapshot_cache
    from models import OpenAISnapshot, UsageSnapshot
    snapshot_cache.save_cache(claude=UsageSnapshot(timestamp=datetime.now(), session_percent=10.0),
                              openai=OpenAISnapshot(timestamp=datetime.now(), session_percent=20.0, available=True))
    shown = []
    app = main.ClaudeBar()
    app.config.codex_enabled = False
    app.tray = types.SimpleNamespace(update_combined=shown.append)

    app._load_cached_initial()

    (combined,) = shown
    assert combined.claude.session_percent == 10.0
    assert combined.openai is None


def app_after_a_save(collector):
    """An app whose collector is the recording one, and which has taken one Settings save with
    everything on, as at start."""
    app = main.ClaudeBar()
    app.collector = collector
    app._on_settings_saved()
    return app


def settle(collector, expected):
    """Wait for the refresh a save should have started (none: just wait a moment), then give a
    second one time to show; returns how many collects there were."""
    if expected:
        wait_for(lambda: collector.calls >= expected, seconds=2.0)
    time.sleep(0.3)
    return collector.calls


def test_a_save_that_switches_an_engine_on_starts_one_refresh():
    """The panel would show nothing for it until the next scheduled refresh, up to a whole
    interval away."""
    collector = RecordingCollector()
    app = app_after_a_save(collector)
    app.config.codex_enabled = False
    app._on_settings_saved()                         # switching one off fetches nothing
    assert settle(collector, 0) == 0

    app.config.codex_enabled = True
    app._on_settings_saved()
    assert settle(collector, 1) == 1                 # one refresh, started by the save


def test_a_save_that_switches_two_engines_on_still_starts_one_refresh():
    collector = RecordingCollector()
    app = app_after_a_save(collector)
    app.config.codex_enabled = app.config.deepseek_enabled = False
    app._on_settings_saved()
    app.config.codex_enabled = app.config.deepseek_enabled = True
    app._on_settings_saved()
    assert settle(collector, 1) == 1


@pytest.mark.parametrize("change", [
    pytest.param(lambda c: None, id="nothing changed"),
    pytest.param(lambda c: setattr(c, "refresh_interval", 60), id="the interval"),
    pytest.param(lambda c: setattr(c, "currency", "EUR"), id="the currency"),
    pytest.param(lambda c: setattr(c, "claude_enabled", False), id="an engine switched off"),
])
def test_a_save_that_switches_nothing_on_refreshes_nothing(change):
    collector = RecordingCollector()
    app = app_after_a_save(collector)
    change(app.config)
    app._on_settings_saved()
    assert settle(collector, 0) == 0


def test_an_engine_that_was_on_and_still_is_not_switched_on_again():
    collector = RecordingCollector()
    app = app_after_a_save(collector)
    app.config.codex_enabled = False
    app._on_settings_saved()
    app._on_settings_saved()                         # saved twice with it still off
    app.config.claude_enabled = False
    app._on_settings_saved()
    assert settle(collector, 0) == 0


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
@pytest.mark.parametrize("interval", [9999999, 10 ** 12, float("inf"), "soon", None])
def test_an_interval_nothing_can_wait_for_does_not_end_the_refresh_loop(interval):
    """Event.wait raises OverflowError above threading.TIMEOUT_MAX, which killed the loop's
    thread after its first refresh: the app then showed whatever it had loaded at start, for good.
    Config.refresh_interval is clamped where it is read in, but the loop stands on its own."""
    collector = RecordingCollector()
    app = make_loop_app(collector, interval)
    loop = threading.Thread(target=app._refresh_loop, daemon=True)
    loop.start()
    assert wait_for(lambda: collector.calls == 1)

    time.sleep(0.3)
    assert loop.is_alive(), "the loop's thread ended after its first refresh"

    app._on_exit()
    loop.join(2)
    assert not loop.is_alive()


def test_the_loop_never_waits_longer_than_the_ceiling(monkeypatch):
    waits = []

    class Recording(threading.Event):
        def wait(self, timeout=None):
            waits.append(timeout)
            app._running = False
            return False

    app = make_loop_app(RecordingCollector(), 9999999)
    app._wake = Recording()
    app._refresh_loop()
    app.config.refresh_interval = 90
    app._running = True
    app._refresh_loop()
    assert waits == [config.MAX_REFRESH_INTERVAL, 90]
