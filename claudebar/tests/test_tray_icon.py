from tray import TrayManager, create_icon


def test_custom_icon_gets_a_badge_past_the_thresholds():
    """The bundled icon replaces the generated one, so status must show as a corner badge."""
    normal, warning, critical = (create_icon(s) for s in ("normal", "warning", "critical"))
    assert normal.size == (128, 128) and normal.mode == "RGBA"
    assert normal.getpixel((122, 122))[3] == 0                    # outside the round icon, no badge
    assert warning.getpixel((118, 118))[:3] == (0xF5, 0x9E, 0x0B)  # amber
    assert critical.getpixel((118, 118))[:3] == (0xEF, 0x44, 0x44)  # red


def test_menu_reaches_the_log_file():
    """The log is only useful if the tray points at it."""
    tray = TrayManager(on_refresh=lambda: None, on_exit=lambda: None, use_premium_ui=False)
    assert "Open log file" in [item.text for item in tray._create_menu()]

def test_update_before_start_is_kept():
    """main.py shows the cached snapshot before the icon thread is up."""
    from models import CombinedSnapshot
    tray = TrayManager(on_refresh=lambda: None, on_exit=lambda: None, use_premium_ui=False)
    combined = CombinedSnapshot()
    tray.update_combined(combined)
    assert tray._combined is combined


def test_menu_actions_wait_for_the_main_thread():
    """pystray's thread only queues; the main thread's tick runs the action."""
    exits = []
    tray = TrayManager(on_refresh=lambda: None, on_exit=lambda: exits.append(1),
                       use_premium_ui=False)
    tray._on_exit_click(None, None)
    assert exits == []
    tray.run_pending()
    assert exits == [1]
