"""Refresh requests and the panel's engine across threads (verification P2, P3).

The TLA+ model in verification/PanelThreads.tla found both; these reproduce
them against the real ClaudeBar._refresh and ClaudeBarWindow.update_combined.
"""

import threading

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
    """P3: a refresh asked for while one is running (a sign-in, a tab click) is
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
