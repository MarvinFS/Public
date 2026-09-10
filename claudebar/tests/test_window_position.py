"""The panel remembers where it was dragged, unless that spot is off-screen."""

import ui_window
from config import Config


def make_window(monkeypatch, saved, on_screen=True):
    monkeypatch.setattr(ui_window, "save_config", lambda cfg: saved.append((cfg.window_x, cfg.window_y)))
    monkeypatch.setattr(ui_window, "_point_on_screen", lambda x, y: on_screen)
    return ui_window.ClaudeBarWindow(on_refresh=lambda: None, on_settings=lambda: None,
                                     on_exit=lambda: None, config=Config(window_x=1200, window_y=300))


def test_saved_position_is_restored_when_still_on_a_monitor(monkeypatch):
    w = make_window(monkeypatch, saved=[], on_screen=True)
    assert w._user_position == (1200, 300)


def test_saved_position_off_every_monitor_falls_back_to_default(monkeypatch):
    """A monitor was unplugged: the header point (x+40, y+20) is nowhere visible."""
    w = make_window(monkeypatch, saved=[], on_screen=False)
    assert w._user_position is None


def test_reset_clears_the_saved_position(monkeypatch):
    saved = []
    w = make_window(monkeypatch, saved, on_screen=True)
    w.reset_position()
    assert w._user_position is None
    assert saved == [(None, None)]


def test_drag_end_persists_the_new_position(monkeypatch):
    saved = []
    w = make_window(monkeypatch, saved, on_screen=True)
    w._drag_origin = (0, 0, 0, 0)
    w._user_position = (50, 60)
    w._on_drag_end(None)
    assert saved == [(50, 60)]
    assert w._drag_origin is None


def test_point_on_screen_uses_a_real_monitor_query():
    """The primary monitor's origin is always on screen; a point far outside is not."""
    assert ui_window._point_on_screen(0, 0) is True
    assert ui_window._point_on_screen(-100_000, -100_000) is False
