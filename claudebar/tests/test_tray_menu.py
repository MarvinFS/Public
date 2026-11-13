"""The tray menu is rebuilt on the main thread, and not while the user has it open.

pystray's Win32 backend replaces `icon.menu` by destroying the old menu handle and
building a new one on whichever thread assigns it, with no lock against its own message
loop. A refresh thread that assigned it could pull the menu from under an open one, so the
assignment is queued for main.py's tick, like the other pystray-facing actions.
"""

import threading

import pytest

import tray
from models import CombinedSnapshot, OpenAISnapshot
from tray import TrayManager


class FakeIcon:
    """What the tray touches on a pystray.Icon, recording who assigned the menu."""

    def __init__(self):
        self.icon = None
        self.title = ""
        self.menu_assignments = []          # (thread, texts of the menu items)
        self.stopped = False

    @property
    def menu(self):
        return None

    @menu.setter
    def menu(self, value):
        self.menu_assignments.append((threading.current_thread(), [item.text for item in value]))

    def stop(self):
        self.stopped = True


@pytest.fixture
def manager():
    manager = TrayManager(on_refresh=lambda: None, on_exit=lambda: None, use_premium_ui=False)
    manager._icon = FakeIcon()
    return manager


def refresh_thread(fn, *args):
    thread = threading.Thread(target=fn, args=args, name="refresh")
    thread.start()
    thread.join(5)
    assert not thread.is_alive()


def test_the_refresh_thread_does_not_assign_the_menu(manager):
    refresh_thread(manager.update_combined, CombinedSnapshot())
    assert manager._icon.menu_assignments == []         # queued, not made
    manager.run_pending()                               # the main thread's tick
    (thread, texts), = manager._icon.menu_assignments
    assert thread is threading.current_thread()
    assert "Open log file" in "".join(texts) or "Refresh" in texts


def test_the_single_snapshot_update_is_queued_too(manager):
    from models import UsageSnapshot
    refresh_thread(manager.update, UsageSnapshot())
    assert manager._icon.menu_assignments == []
    manager.run_pending()
    assert len(manager._icon.menu_assignments) == 1


def test_updates_that_arrive_before_the_tick_make_one_rebuild_from_the_latest(manager):
    first, second = CombinedSnapshot(), CombinedSnapshot()
    refresh_thread(manager.update_combined, first)
    refresh_thread(manager.update_combined, second)
    manager.run_pending()
    assert len(manager._icon.menu_assignments) == 1
    assert manager._combined is second
    refresh_thread(manager.update_combined, CombinedSnapshot())      # and the next refresh queues again
    manager.run_pending()
    assert len(manager._icon.menu_assignments) == 2


def test_an_open_menu_is_left_alone_until_it_closes(manager, monkeypatch):
    menu_open = [True]
    monkeypatch.setattr(tray, "_in_menu_mode", lambda thread_id: menu_open[0])
    refresh_thread(manager.update_combined, CombinedSnapshot())

    ticks = threading.Thread(target=lambda: [manager.run_pending() for _ in range(3)])
    ticks.start()
    ticks.join(3)
    assert not ticks.is_alive(), "a rebuild put back for later made the tick spin"
    assert manager._icon.menu_assignments == []

    refresh_thread(manager.update_combined, CombinedSnapshot())      # still only one waiting
    menu_open[0] = False
    manager.run_pending()
    assert len(manager._icon.menu_assignments) == 1


def test_the_menu_mode_asked_about_is_the_tray_thread(manager, monkeypatch):
    asked = []
    monkeypatch.setattr(tray, "_in_menu_mode", lambda thread_id: asked.append(thread_id) or False)
    manager._tray_thread_id = 4242
    refresh_thread(manager.update_combined, CombinedSnapshot())
    manager.run_pending()
    assert asked == [4242]


def test_a_rebuild_queued_before_stop_is_dropped(manager):
    refresh_thread(manager.update_combined, CombinedSnapshot())
    icon = manager._icon
    manager.stop()
    manager.run_pending()
    assert icon.menu_assignments == [] and icon.stopped


def test_before_start_nothing_is_queued(manager):
    manager._icon = None
    manager.update_combined(CombinedSnapshot())
    manager.run_pending()                      # nothing to run, nothing to fail


class TestMenuModeProbe:
    def test_it_is_false_without_a_thread_to_ask_about(self):
        assert tray._in_menu_mode(None) is False
        assert tray._in_menu_mode(0) is False

    def test_this_thread_is_not_showing_a_menu(self):
        assert tray._in_menu_mode(threading.get_native_id()) is False

    def test_a_thread_that_does_not_exist_is_not_showing_one(self):
        assert tray._in_menu_mode(0x7FFFFFF0) is False


def test_with_claude_switched_off_the_icon_is_neutral_and_the_tooltip_is_not_loading(manager, monkeypatch):
    """A refresh that does not collect Claude carries no Claude snapshot. The icon and the
    tooltip are Claude's by design, and "no snapshot" there means "still loading" for good."""
    icons = []
    monkeypatch.setattr(tray, "create_icon", lambda status="normal", percent=None: icons.append((status, percent)))
    codex = OpenAISnapshot(available=True, session_available=True, session_percent=12.0, weekly_percent=34.0)

    manager.update_combined(CombinedSnapshot(claude=None, openai=codex, deepseek=None))
    manager.run_pending()

    assert icons[-1] == ("normal", None)
    assert "Loading" not in manager._icon.title
    assert "OpenAI: 12%/34%" in manager._icon.title and "Updated:" in manager._icon.title
    (_, texts), = manager._icon.menu_assignments
    assert "OpenAI" in texts and "Claude" not in texts


def test_before_the_first_refresh_the_tray_still_says_loading(manager):
    assert manager._get_tooltip() == "ClaudeBar - Loading..."
