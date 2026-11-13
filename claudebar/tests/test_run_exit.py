"""ClaudeBar.run returns whenever an exit is requested, also one requested before the Tk loop
starts.

A tray that fails at once queues its exit (TrayManager.start), and run() used to run the first
tick by hand before mainloop(): that tick found the exit, called root.quit(), and Tk discards a
quit made before the loop is entered. The app then ran for ever with no icon and no timer.
"""

import tkinter

import pytest

import main
import tray
from tests.test_signin_cleanup import FakeBrowsers, make_dialog


class FailingIcon:
    """pystray.Icon whose loop raises at once, as a broken shell integration can make it."""

    def __init__(self, *args, **kwargs):
        pass

    def run(self):
        raise OSError("the shell refused the icon")

    def stop(self):
        pass


@pytest.fixture
def app(monkeypatch, tk_root):
    """The real run(), with pystray replaced, no network, and the session's one Tk root in
    place of a new one (a root per test makes Tcl's init fail now and then)."""
    app = main.ClaudeBar()
    monkeypatch.setattr(app, "_refresh", lambda: None)
    monkeypatch.setattr(tkinter, "Tk", lambda: tk_root)
    return app


def run_with_a_watchdog(app, tk_root, seconds=3.0):
    """Run it; returns True when run() came back by itself, False when only the watchdog ended
    the loop (a hang, which would otherwise stop the test run)."""
    hung = []
    watchdog = tk_root.after(int(seconds * 1000), lambda: (hung.append(True), tk_root.quit()))
    try:
        app.run()
    finally:
        tk_root.after_cancel(watchdog)
    return not hung


def test_a_tray_that_fails_at_once_ends_run(app, monkeypatch, tk_root):
    monkeypatch.setattr(tray.pystray, "Icon", FailingIcon)
    real_start_detached = tray.TrayManager.start_detached

    def start_and_wait(self):
        """The failure is queued before run() makes its Tk root, as it was when this hung."""
        thread = real_start_detached(self)
        thread.join(5)
        assert not thread.is_alive()
        return thread

    monkeypatch.setattr(tray.TrayManager, "start_detached", start_and_wait)

    assert run_with_a_watchdog(app, tk_root), "run() went into the Tk loop and stayed there"
    assert not app._running


def test_a_tray_that_fails_a_moment_later_ends_run_too(app, monkeypatch, tk_root):
    """The same exit, queued while the loop is already running."""
    monkeypatch.setattr(tray.pystray, "Icon", FailingIcon)
    real_start_detached = tray.TrayManager.start_detached

    def start_late(self):
        thread = real_start_detached(self)
        tk_root.after(300, lambda: None)      # the loop has something to do before the failure lands
        return thread

    monkeypatch.setattr(tray.TrayManager, "start_detached", start_late)
    assert run_with_a_watchdog(app, tk_root)


def test_the_exit_menu_item_ends_run(app, monkeypatch, tk_root):
    """The ordinary exit still works: the tray's Exit is queued for a tick of the loop."""
    class QuietIcon(FailingIcon):
        def run(self):
            pass                                # a tray whose loop simply ends is not a failure

    monkeypatch.setattr(tray.pystray, "Icon", QuietIcon)
    real_start_detached = tray.TrayManager.start_detached

    def start_then_click_exit(self):
        thread = real_start_detached(self)
        thread.join(5)
        self._on_exit_click(None, None)
        return thread

    monkeypatch.setattr(tray.TrayManager, "start_detached", start_then_click_exit)
    assert run_with_a_watchdog(app, tk_root)


# --------------------------------------------------------- Exit during a DeepSeek sign-in

def click_exit_in_the_tray(monkeypatch):
    """A tray that starts and then has Exit clicked, as run() would see it."""
    class QuietIcon(FailingIcon):
        def run(self):
            pass

    monkeypatch.setattr(tray.pystray, "Icon", QuietIcon)
    real_start_detached = tray.TrayManager.start_detached

    def start_then_click_exit(self):
        thread = real_start_detached(self)
        thread.join(5)
        self._on_exit_click(None, None)
        return thread

    monkeypatch.setattr(tray.TrayManager, "start_detached", start_then_click_exit)


def test_exit_during_a_sign_in_closes_its_browser_and_deletes_its_profile(app, monkeypatch, tk_root):
    """The sign-in's worker is a daemon thread and its cleanup is the worker's last step, so the
    process ended with the browser open and the plaintext capture profile on disk."""
    world = FakeBrowsers(monkeypatch)
    dialog = make_dialog()
    dialog._begin_signin()
    assert world.polling[1].wait(3) and world.running == [1]
    click_exit_in_the_tray(monkeypatch)

    assert run_with_a_watchdog(app, tk_root)

    try:
        assert world.running == [], "the browser is still open"
        assert ("forget",) in world.events, "the capture profile was not deleted"
    finally:
        world.late[1].set()
        dialog._stop_signin(wait=5)


def test_exit_with_no_sign_in_touches_no_browser_and_no_profile(app, monkeypatch, tk_root):
    world = FakeBrowsers(monkeypatch)
    click_exit_in_the_tray(monkeypatch)
    assert run_with_a_watchdog(app, tk_root)
    assert world.events == []
