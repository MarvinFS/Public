"""The test run's windows live on a desktop of their own (conftest.HiddenDesktop): a panel shown, dragged
and keyed, and both dialogs, leave the user's foreground window alone and appear on no monitor. The
foreground window is read from the user's own desktop, before, during and after."""

import os
from types import SimpleNamespace

import pytest

import ui_window
from config import Config
from models import CombinedSnapshot
from tests.test_panel_scale import STANDARD, snapshot
from ui_scale import Scale

SPOT = (300, 100)
ATTEMPTS = 3                # the user may switch windows while the check runs; a switch of ours never repeats


def event(x, y):
    return SimpleNamespace(x_root=x, y_root=y, delta=0)


@pytest.fixture
def panel(tk_root, monkeypatch):
    monkeypatch.setattr(ui_window, "monitor_at", lambda x, y, nearest=True: STANDARD)
    monkeypatch.setattr(ui_window, "save_config", lambda cfg: None)
    monkeypatch.setattr(ui_window.ClaudeBarWindow, "_load_initial_data", lambda self: None)
    window = ui_window.ClaudeBarWindow(on_refresh=lambda: None, on_settings=lambda: None, on_exit=lambda: None,
                                       config=Config(panel_scale=1.0), on_engine_change=lambda e: None)
    window._user_position = SPOT
    window.update_combined(CombinedSnapshot(claude=snapshot()))
    yield window
    window.hide()
    if window._tick_job is not None:
        tk_root.after_cancel(window._tick_job)
    window.destroy()


def play(panel, watch):
    """Show the panel, drag its right edge out and let go, press Ctrl+plus, open both dialogs; `watch()`
    is called between the steps. Returns the panel's own window handle."""
    panel.show()
    window = panel._window
    window.update()
    assert window.winfo_viewable()                                    # a real, mapped window
    watch()
    x, y, width, height = window.winfo_rootx(), window.winfo_rooty(), window.winfo_width(), window.winfo_height()
    start = (x + width - 3, y + height // 2)
    panel._on_drag_start(event(*start))
    watch()
    for dx in (40, 90, 160):
        panel._on_drag_move(event(start[0] + dx, start[1]))
        window.update()
        watch()
    panel._on_drag_end(event(start[0] + 160, start[1]))
    window.update()
    watch()
    scale = panel.config.panel_scale
    window.focus_force()
    window.event_generate("<Control-plus>")
    window.update()
    watch()
    assert panel.config.panel_scale == pytest.approx(scale + 0.1)      # the key reached the panel
    settings = ui_window.SettingsDialog(window, Config(), lambda: None)
    settings.dialog.update()
    watch()
    settings.dialog.destroy()
    style = SimpleNamespace(bg="#1c1c1e", primary="#fff", secondary="#aaa", muted="#777", accent="#F59E0B",
                            warn="#f00", button_bg="#333", hover_bg="#444", scale=Scale(1.0),
                            font=lambda size, weight="normal": ("Segoe UI", -size, weight))
    confirm = ui_window.ResetConfirmDialog(window, ["Opus launch reset", "Expires Oct 22", "Cannot be undone."],
                                           lambda: None, lambda: None, style)
    confirm.dialog.update()
    watch()
    confirm._cancel()
    wrapper = int(window.wm_frame(), 16)
    panel.hide()
    watch()
    return wrapper


def test_a_panel_a_drag_a_key_and_both_dialogs_leave_the_users_foreground_window_alone(panel, hidden_desktop):
    mine = os.getpid()
    for attempt in range(ATTEMPTS):
        before = hidden_desktop.user_foreground()
        seen = [before]
        wrapper = play(panel, lambda: seen.append(hidden_desktop.user_foreground()))
        assert [h for h in seen if h and hidden_desktop.owner_pid(h) == mine] == [], "a window of the run took focus"
        if set(seen) == {before} or attempt == ATTEMPTS - 1:
            break
    assert set(seen) == {before}, f"the foreground window changed: {sorted(set(seen))}"
    assert len(seen) > 10                                              # it was watched through every step

    on_users, on_ours = hidden_desktop.windows(True), hidden_desktop.windows(False)
    assert on_users                                                    # the listing works, so absence means something
    assert wrapper in on_ours and wrapper not in on_users
    assert hidden_desktop.owner_pid(wrapper) == mine
    print(f"foreground before={before:#x} samples={len(seen)} distinct={[hex(h) for h in sorted(set(seen))]} "
          f"panel window={wrapper:#x} on the run's desktop={wrapper in on_ours} "
          f"on the user's desktop={wrapper in on_users} (user's desktop lists {len(on_users)} windows)")
