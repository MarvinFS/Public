"""Drag to resize with fluid content: the outer margin resizes, the width sets the scale, the height sets
how much is shown, blocks leave in a fixed order and come back in reverse, and the keys and the tray
reach the same state. Drags are made by calling the handlers a mouse would, with events carrying screen
coordinates; the window is real (mapped, on the test run's own desktop), the monitor Windows reports is a
stand-in. One panel serves the whole module and is put back to its start before every test."""

import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import tray
import ui_window
from config import Config
from models import CombinedSnapshot, Engine, OpenAISnapshot
from monitor import Monitor
from tests.test_panel_scale import STANDARD, snapshot
from tests.test_redeem_ui import claude_snap
from ui_scale import DROP_ORDER, PANEL_MAX_SCALE, PANEL_MIN_SCALE

UTC = timezone.utc
SPOT = (300, 100)           # where the panel is put, so a drag has room on every side
FOUR_K = Monitor(work=(0, 0, 3840, 2100), height=2160, dpi=144)       # 4K at 150%


def event(x, y, delta=0):
    return SimpleNamespace(x_root=x, y_root=y, delta=delta)


@pytest.fixture
def monitor(monkeypatch):
    state = SimpleNamespace(found=STANDARD)
    monkeypatch.setattr(ui_window, "monitor_at", lambda x, y, nearest=True: state.found)
    return state


@pytest.fixture
def saves(monkeypatch):
    """Every save_config call, as the (scale, cap, x, y) it wrote."""
    calls = []
    monkeypatch.setattr(ui_window, "save_config",
                        lambda cfg: calls.append((cfg.panel_scale, cfg.panel_max_height, cfg.window_x, cfg.window_y)))
    return calls


@pytest.fixture(scope="module")
def stage(tk_root):
    """Holds the module's one panel, made by the first test that asks (under that test's fakes) and
    taken down with the module."""
    holder = SimpleNamespace(panel=None)
    yield holder
    window = holder.panel
    if window is not None:
        window.hide()
        if window._tick_job is not None:
            tk_root.after_cancel(window._tick_job)
        window.destroy()


def restore(window):
    """What a new panel starts with: scale 1.0, no cap, the Cost view, Claude, nothing dragged, no notice. The
    content is built again only when the last test left another scale; the reading, the fit and the width
    (which only grows) are set again either way."""
    window.hide()                                  # ends a drag and takes the curtain away
    config = window.config
    config.panel_scale, config.panel_max_height = 1.0, None
    config.window_x = config.window_y = None
    window.on_refresh = lambda: None
    window._wheel = 0
    window._snapshot = window._openai_snapshot = window._deepseek_snapshot = None
    window._log_write = window._log_asked = None
    if window._notice_job is not None:
        window._window.after_cancel(window._notice_job)
    window._notice_job = window._banked_notice = None
    window._window.config(cursor="")
    if window.scale.k != 1.0:
        window._build_content(1.0)
    window._natural_width = window._window_width = window._px(380)
    window._on_view_select("cost")
    window._on_engine_select(Engine.CLAUDE)


@pytest.fixture
def panel(stage, monitor, saves, pictures, monkeypatch):
    monkeypatch.setattr(ui_window.ClaudeBarWindow, "_load_initial_data", lambda self: None)
    window = stage.panel
    if window is None:
        window = stage.panel = ui_window.ClaudeBarWindow(
            on_refresh=lambda: None, on_settings=lambda: None, on_exit=lambda: None,
            config=Config(panel_scale=1.0), on_engine_change=lambda e: None)
    else:
        restore(window)
    window._user_position = SPOT
    window.update_combined(CombinedSnapshot(claude=snapshot()))
    window.show()
    window._window.update()
    pictures.size = frame(window)[2:]              # the fake picture is as big as the panel, like a real one
    pictures.calls.clear()
    saves.clear()                                  # what restoring saved is not the test's
    return window


@pytest.fixture
def builds(panel, monkeypatch):
    """The scale of every build of the content, in order."""
    built = []
    real = panel._build_content
    monkeypatch.setattr(panel, "_build_content", lambda k: (built.append(k), real(k)))
    return built


def frame(panel):
    """(x, y, width, height) of the panel window on the screen."""
    w = panel._window
    return w.winfo_rootx(), w.winfo_rooty(), w.winfo_width(), w.winfo_height()


def flush(panel):
    """What the Tk loop does between two events: the idle jobs, which is where a resize is applied."""
    panel._window.update_idletasks()
    panel._window.update()


def edge_point(panel, zone, inset=2):
    """The screen point `inset` pixels inside the margin of `zone`, in the middle of the edge."""
    x, y, width, height = frame(panel)
    return (x + inset if "w" in zone else x + width - 1 - inset if "e" in zone else x + width // 2,
            y + inset if "n" in zone else y + height - 1 - inset if "s" in zone else y + height // 2)


class Drag:
    """A press, some motion and a release on one zone of the panel, event by event."""

    def __init__(self, panel, zone=None, point=None):
        self.panel = panel
        self.start = point or edge_point(panel, zone)
        self.at = self.start
        panel._on_drag_start(event(*self.start))

    def move(self, dx, dy, now=True):
        self.at = (self.start[0] + dx, self.start[1] + dy)
        self.panel._on_drag_move(event(*self.at))
        if now:
            flush(self.panel)

    def release(self):
        self.panel._on_drag_end(event(*self.at))
        flush(self.panel)


def drag(panel, zone, dx=0, dy=0, steps=1):
    d = Drag(panel, zone)
    for i in range(1, steps + 1):
        d.move(round(dx * i / steps), round(dy * i / steps))
    d.release()
    return d


def press(panel, sequence, **fields):
    """A key or the wheel, delivered through Tk: the window has the focus on the run's own desktop."""
    panel._window.focus_force()
    panel._window.event_generate(sequence, **fields)


def shown(window):
    """The named blocks of DROP_ORDER that are on screen."""
    return {name for name, widgets in window._fit_widgets.items() if any(w.winfo_ismapped() for w in widgets)}


def buttons(panel):
    return {b.cget("text"): b for b in panel._source_label.master.winfo_children() if b.winfo_class() == "Button"}


class TestADragOnAnEdge:
    @pytest.mark.parametrize("zone, dx, dy", [
        ("e", 150, 0), ("w", 120, 0), ("s", 0, -200), ("n", 0, 150), ("nw", -90, 60)])
    def test_the_far_edges_stay_put_and_the_width_sets_the_scale(self, panel, builds, zone, dx, dy):
        x, y, width, height = frame(panel)
        sideways, tall = "e" in zone or "w" in zone, "n" in zone or "s" in zone
        d = Drag(panel, zone)
        for i in (1, 2, 3):
            mx, my = round(dx * i / 3), round(dy * i / 3)
            d.move(mx, my)
            x2, y2, width2, height2 = frame(panel)
            assert (x2 + width2 if "w" in zone else x2) == (x + width if "w" in zone else x), zone
            assert (y2 + height2 if "n" in zone else y2) == (y + height if "n" in zone else y), zone
            if sideways:
                assert width2 == width + (mx if "e" in zone else -mx), zone       # the window follows the pointer
            if sideways and tall:
                assert height2 == height - my, zone                               # and a corner the height it gave
        d.release()
        x2, y2, width2, height2 = frame(panel)
        if sideways:
            assert width2 == pytest.approx(width + (dx if "e" in zone else -dx), rel=0.05)
            assert panel.config.panel_scale == pytest.approx(width2 / width, rel=0.05)
            assert panel.scale.k == panel.config.panel_scale and len(builds) == 1
        else:
            assert builds == [] and panel.config.panel_scale == 1.0 and height2 < height
        assert (panel.config.panel_max_height is not None) == tall

    def test_narrowing_stops_at_the_smallest_scale_and_widening_at_the_room_there_is(self, panel, monitor):
        drag(panel, "e", dx=-3000)
        assert panel.config.panel_scale == PANEL_MIN_SCALE and frame(panel)[2] == panel._window_width
        small = frame(panel)[2]
        drag(panel, "e", dx=-300)
        assert frame(panel)[2] == small and panel.config.panel_scale == PANEL_MIN_SCALE
        drag(panel, "e", dx=4000)
        assert 2.3 < panel.config.panel_scale <= PANEL_MAX_SCALE
        assert frame(panel)[0] + frame(panel)[2] <= STANDARD.work[2]
        monitor.found = Monitor(work=(0, 0, 1000, 1392), height=1440, dpi=96)
        drag(panel, "e", dx=3000)
        x, _, width, _ = frame(panel)
        assert x + width <= 1000 and width > 600 and panel.config.panel_scale < 2.0     # it filled what room there was


class TestTheHeightSetsWhatIsShown:
    def test_blocks_leave_in_the_documented_order_and_come_back_in_reverse(self, panel, pictures):
        assert DROP_ORDER == ("source", "top_model", "pace", "chart", "stats", "half", "status")
        seen = [frozenset(panel._fit_hidden)]
        d = Drag(panel, "s")
        for dy in list(range(0, -800, -16)) + list(range(-800, 1, 16)):
            hidden, taken = set(panel._fit_hidden), len(pictures.calls)
            d.move(0, dy)
            changed = panel._fit_hidden != hidden
            if changed:
                seen.append(frozenset(panel._fit_hidden))
                assert shown(panel) == set(DROP_ORDER) - panel._fit_hidden       # hidden is what is off the screen
            assert len(pictures.calls) - taken == int(changed) and panel._curtain is None    # one picture a change
        d.release()
        prefix = lambda n: frozenset(DROP_ORDER[:n])
        # The footer's note frees no height, so it goes with the top-model row; then pace, chart, stats, half, status.
        shrinking = [prefix(0), prefix(2), prefix(3), prefix(4), prefix(5), prefix(6), prefix(7)]
        assert seen == shrinking + shrinking[-2::-1]

    def test_the_always_kept_blocks_survive_the_smallest_height(self, panel):
        full = frame(panel)[3]
        drag(panel, "s", dy=-3000, steps=4)
        assert panel._fit_hidden == set(DROP_ORDER)
        assert shown(panel) == set()
        for widget in (panel._engine_name, panel._limits["session"]["frame"], panel._limits["weekly"]["frame"]):
            assert widget.winfo_ismapped()
        footer = buttons(panel)
        assert set(footer) == {"Settings", "Refresh", "Exit"} and all(b.winfo_ismapped() for b in footer.values())
        smallest = frame(panel)[3]
        assert smallest == panel._fit_range[0] and 0 < smallest < full
        drag(panel, "s", dy=5000, steps=4)
        assert frame(panel)[3] == full and panel._fit_hidden == set()

    def test_dragging_at_or_beyond_the_full_height_clears_the_cap(self, panel):
        drag(panel, "s", dy=-300, steps=3)
        assert panel.config.panel_max_height is not None
        drag(panel, "s", dy=2000, steps=3)
        assert panel.config.panel_max_height is None
        assert panel._fit_hidden == set()

    def test_the_cap_is_kept_in_design_pixels_and_never_below_the_smallest_panel(self, panel):
        drag(panel, "s", dy=-300, steps=3)
        cap, hidden = panel.config.panel_max_height, set(panel._fit_hidden)
        assert frame(panel)[3] <= panel._px(cap)
        drag(panel, "e", dx=400, steps=3)                                     # k about 1.9 now
        assert panel.config.panel_max_height == cap and panel.scale.k > 1.7
        assert frame(panel)[3] <= panel._px(cap) and panel._fit_hidden == hidden     # the same blocks, bigger
        drag(panel, "s", dy=-3000, steps=4)
        assert panel._px(panel.config.panel_max_height) >= panel._fit_range[0] - 1


class TestTheBankedResetsBlock:
    def live(self, panel, **kw):
        panel.update_combined(CombinedSnapshot(claude=claude_snap(**kw)))
        panel._apply_snapshot_update()
        flush(panel)

    def test_it_stays_at_the_smallest_height_only_while_it_offers_a_reset(self, panel):
        self.live(panel)
        assert panel._banked_use.winfo_manager()
        drag(panel, "s", dy=-3000, steps=4)
        assert panel._fit_hidden == set(DROP_ORDER)
        assert panel._banked_frame.winfo_ismapped() and panel._banked_use.winfo_ismapped()
        self.live(panel, stale=True)                       # the data went stale: it only informs now
        assert not panel._banked_use.winfo_manager() and not panel._banked_frame.winfo_ismapped()
        smallest = frame(panel)[3]
        self.live(panel, banked=None, stale=True)          # and the block going while the half is dropped
        assert frame(panel)[3] == smallest                 # changes nothing visible
        self.live(panel, stale=True)
        drag(panel, "s", dy=5000, steps=4)                 # it comes back with the half
        assert "half" not in panel._fit_hidden and panel._banked_frame.winfo_ismapped()
        assert not panel._banked_use.winfo_manager()

    def test_a_result_notice_stays_too(self, panel):
        self.live(panel, banked=None)
        panel._set_notice("Reset applied", "ok", "claude")
        flush(panel)
        drag(panel, "s", dy=-3000, steps=4)
        assert panel._banked_frame.winfo_ismapped()


class TestSavingAndCoalescing:
    def test_nothing_is_saved_during_a_drag_and_one_save_comes_on_release(self, panel, saves):
        d = Drag(panel, "nw")
        for i in range(1, 4):
            d.move(-i * 30, i * 20)
        assert saves == []
        d.release()
        assert len(saves) == 1
        scale, cap, x, y = saves[0]
        assert scale == panel.config.panel_scale and cap == panel.config.panel_max_height
        assert (x, y) == panel._user_position == frame(panel)[:2] and x < SPOT[0]     # where the resize left it

    def test_a_press_in_the_margin_with_no_motion_changes_saves_and_takes_nothing(self, panel, saves, pictures):
        before = frame(panel)
        Drag(panel, "e").release()
        assert saves == [] and frame(panel) == before and panel.config.panel_scale == 1.0
        assert pictures.calls == [] and panel._curtain is None

    def test_many_motions_build_nothing_and_the_release_builds_once_at_the_scale_it_gave(self, panel, saves, builds):
        _, _, width, _ = frame(panel)
        d = Drag(panel, "e")
        for i in range(1, 40):
            d.move(i * 3, 0, now=False)                   # a burst of motion, no idle in between
        flush(panel)
        for i in range(40, 46):                           # and more, with an idle after each
            d.move(i * 3, 0)
        assert builds == []
        d.at = (d.start[0] + 240, d.start[1])             # the release is the last word, not the last motion
        d.release()
        assert len(builds) == 1 and builds[0] == panel.scale.k
        assert saves[-1][0] == round((width + 240) / width, 3) == panel.scale.k

    def test_a_panel_at_the_trays_spot_is_pinned_where_it_is_by_a_resize(self, panel):
        panel._user_position = None
        panel._update_window_size()
        flush(panel)
        x, y, width, height = frame(panel)
        assert (x + width, y + height) == (STANDARD.work[2] - 20, STANDARD.work[3] - 12)
        drag(panel, "w", dx=60, steps=3)
        x2, y2, width2, height2 = frame(panel)
        assert x2 + width2 == x + width and y2 == y and height2 < height      # the top stays, it does not creep up
        assert panel._user_position == (x2, y2)


class TestAPressInTheContentStillMoves:
    def test_it_moves_the_panel_and_changes_no_size(self, panel, saves):
        x, y, width, height = frame(panel)
        d = Drag(panel, point=(x + width // 2, y + 36))           # on the header
        assert panel._resize is None and panel._drag_origin is not None
        d.move(60, 40)
        assert frame(panel)[:2] == (x + 60, y + 40) and frame(panel)[2:] == (width, height)
        d.release()
        assert saves == [(1.0, None, x + 60, y + 40)] and panel.config.panel_scale == 1.0

    def test_a_press_on_a_button_or_a_tab_is_still_theirs(self, panel):
        clicked = []
        panel.on_refresh = lambda: clicked.append("refresh")
        refresh = buttons(panel)["Refresh"]
        refresh.invoke()
        assert clicked == ["refresh"]
        x, y, width, height = frame(panel)                        # both lie inside the content, not the margin
        margin = panel._px(8)
        assert ui_window.zone_at(refresh.winfo_rootx() - x + 3, refresh.winfo_rooty() - y + 3, width, height,
                                 margin) is None
        label, _ = panel._tabs["forecast"]
        assert ui_window.zone_at(label.winfo_rootx() - x + 2, label.winfo_rooty() - y + 2, width, height,
                                 margin) is None
        panel._on_view_select("forecast")
        assert panel.config.panel_view == "forecast"


class TestTheCursor:
    def test_each_zone_shows_its_cursor_only_in_the_eight_design_pixel_margin(self, panel):
        for zone, cursor in (("n", "size_ns"), ("s", "size_ns"), ("e", "size_we"), ("w", "size_we"),
                             ("nw", "size_nw_se"), ("se", "size_nw_se"), ("ne", "size_ne_sw"), ("sw", "size_ne_sw")):
            panel._on_pointer(event(*edge_point(panel, zone)))
            assert panel._window.cget("cursor") == cursor, zone
        x, y, width, height = frame(panel)
        for inset, expected in ((0, "size_we"), (panel._px(8) - 1, "size_we"), (panel._px(8), "")):
            panel._on_pointer(event(x + width - 1 - inset, y + height // 2))
            assert panel._window.cget("cursor") == expected
        panel._on_pointer(event(x + width - 2, y + height // 2))
        panel._on_pointer(event(x + width + 400, y))                    # left the window
        assert panel._window.cget("cursor") == ""
        for sequence in ("<Motion>", "<Leave>"):
            assert panel._window.bind(sequence), sequence

    def test_the_cursor_does_not_change_while_a_resize_is_on(self, panel):
        panel._on_pointer(event(*edge_point(panel, "e")))
        d = Drag(panel, "e")
        panel._on_pointer(event(d.start[0] - 200, d.start[1]))
        assert panel._window.cget("cursor") == "size_we"
        d.release()


class TestTheKeysAndTheWheel:
    def test_ctrl_plus_equals_minus_and_zero_step_the_scale_and_save_each_time(self, panel, saves):
        # The keypad's keys have a binding too, but Tk on Windows has no keycode to make them with.
        for sequence in ("<Control-KP_Add>", "<Control-KP_Subtract>"):
            assert panel._window.bind(sequence), sequence
        drag(panel, "s", dy=-200, steps=2)                           # a cap for Ctrl+0 to clear
        saves.clear()
        steps = (("<Control-plus>", 1.1), ("<Control-equal>", 1.2), ("<Control-minus>", 1.1))
        for sequence, expected in steps:
            press(panel, sequence)
            assert panel.config.panel_scale == pytest.approx(expected), sequence
        assert [s[0] for s in saves] == pytest.approx([expected for _, expected in steps])
        assert panel.config.panel_max_height is not None
        saves.clear()
        press(panel, "<Control-0>")                                  # back to automatic, scale and height
        assert panel.config.panel_scale is None and panel.config.panel_max_height is None
        assert panel.scale.k == 1.0 and panel._fit_hidden == set()
        assert len(saves) == 1 and saves[0][:2] == (None, None)

    def test_ctrl_wheel_steps_the_scale_and_a_fine_wheel_steps_when_a_notch_has_come(self, panel, saves):
        press(panel, "<Control-MouseWheel>", delta=120)
        assert panel.config.panel_scale == pytest.approx(1.1)
        press(panel, "<Control-MouseWheel>", delta=-240)
        assert panel.config.panel_scale == pytest.approx(0.9) and len(saves) == 2
        panel._on_wheel(event(0, 0, delta=50))
        panel._on_wheel(event(0, 0, delta=50))
        assert panel.config.panel_scale == pytest.approx(0.9)
        panel._on_wheel(event(0, 0, delta=40))
        assert panel.config.panel_scale == pytest.approx(1.0)

    def test_a_step_starts_from_the_scale_in_use_and_one_that_changes_nothing_leaves_no_curtain(self, panel, monitor):
        panel._step_scale(0)
        assert panel._curtain is None and panel._main_frame.winfo_ismapped()
        monitor.found = Monitor(work=(0, 0, 1920, 1032), height=1080, dpi=96)         # auto: 0.75
        panel.config.panel_scale = None
        panel.show()
        assert panel.scale.k == pytest.approx(0.75)
        panel._step_scale(1)
        assert panel.config.panel_scale == pytest.approx(0.85)

    def test_a_step_keeps_the_anchored_corner_and_stays_inside_the_work_area(self, panel, monitor):
        x, y, width, height = frame(panel)
        panel._step_scale(2)                                          # a user-placed panel keeps its top left
        assert frame(panel)[:2] == (x, y) and frame(panel)[2] > width
        panel._user_position = None                                   # the tray's spot keeps its bottom right
        panel._update_window_size()
        flush(panel)
        x, y, width, height = frame(panel)
        panel._step_scale(-2)
        x2, y2, width2, height2 = frame(panel)
        assert width2 < width
        # the spot's margins are scaled too (20 and 12 design pixels), which is all that moves it
        assert abs(x2 + width2 - (x + width)) <= 12 and abs(y2 + height2 - (y + height)) <= 8
        monitor.found = Monitor(work=(0, 0, 700, 1392), height=1440, dpi=96)
        panel._user_position = (50, 50)
        panel._step_scale(10)
        x, y, width, height = frame(panel)
        assert x + width <= 700 and width > 560 and panel.config.panel_scale < 2.0


class TestAPanelTallerThanTheScreen:
    def test_it_drops_blocks_to_fit_keeps_its_width_and_gets_them_back_when_there_is_room(self, panel, monitor):
        """The footer's note is the first block to go and the widest thing in the panel: the width
        stays a matter of the scale alone, or a width drag would find a different panel each time."""
        monitor.found = Monitor(work=(0, 0, 5000, 4000), height=4320, dpi=96)
        panel.config.panel_scale = 2.0
        panel._user_position = None
        panel.show()
        flush(panel)
        roomy = frame(panel)[2]
        assert not panel._fit_hidden and roomy > 800
        monitor.found = Monitor(work=(0, 0, 2560, 1392), height=1440, dpi=96)        # too short for 2.0
        panel.show()
        panel._build_content(2.0)
        panel._refill()
        flush(panel)
        x, y, width, height = frame(panel)
        assert height <= 1392 and y >= 0 and y + height <= 1392
        assert panel._fit_hidden and set(DROP_ORDER[:len(panel._fit_hidden)]) == panel._fit_hidden
        assert "source" in panel._fit_hidden and width == roomy
        assert panel._fit_range[1] > 1392                       # it did need more than the area gave
        assert panel._engine_name.winfo_ismapped() and panel._limits["weekly"]["frame"].winfo_ismapped()
        monitor.found = Monitor(work=(0, 0, 1280, 600), height=648, dpi=96)          # and at the normal scale
        panel.config.panel_scale = 1.0
        panel.show()
        flush(panel)
        assert frame(panel)[3] <= 600 and panel._fit_hidden
        monitor.found = Monitor(work=(0, 0, 1280, 150), height=198, dpi=96)          # below the smallest panel
        panel.show()
        flush(panel)
        _, y, _, height = frame(panel)
        assert panel._fit_hidden == set(DROP_ORDER) and y == 0 and height > 150
        monitor.found = STANDARD
        panel._update_window_size()
        assert panel._fit_hidden == set()


class TestWhileCapped:
    @staticmethod
    def codex():
        return OpenAISnapshot(session_available=True, session_percent=10.0, weekly_percent=5.0,
                              weekly_window_hours=168.0, available=True,
                              session_resets_at=datetime.now(UTC) + timedelta(hours=2),
                              weekly_resets_at=datetime.now(UTC) + timedelta(days=2))

    def test_the_fit_follows_an_engine_switch_a_view_switch_and_a_new_snapshot(self, panel):
        panel.update_combined(CombinedSnapshot(claude=snapshot(), openai=self.codex()))
        panel._apply_snapshot_update()
        panel.config.panel_max_height = 450
        panel._update_window_size()
        flush(panel)
        assert frame(panel)[3] <= panel._px(450) and panel._fit_hidden
        panel._on_engine_select(Engine.CODEX)
        flush(panel)
        assert panel._active_engine == Engine.CODEX and frame(panel)[3] <= panel._px(450) and panel._fit_hidden
        panel._on_engine_select(Engine.CLAUDE)
        flush(panel)
        assert frame(panel)[3] <= panel._px(450)
        panel._on_view_select("forecast")
        flush(panel)
        quota = panel._fit_range[1]
        assert frame(panel)[3] <= panel._px(450) and shown(panel) == set(DROP_ORDER) - panel._fit_hidden
        panel._on_view_select("cost")
        flush(panel)
        assert frame(panel)[3] <= panel._px(450) and shown(panel) == set(DROP_ORDER) - panel._fit_hidden
        assert quota < panel._fit_range[1]            # the Quota view has no top-model row and no second row
        panel.config.panel_max_height = 520
        panel._update_window_size()
        taller = snapshot()
        taller.extra_enabled, taller.extra_percent, taller.extra_used, taller.extra_limit = True, 40.0, 4.0, 10.0
        panel.update_combined(CombinedSnapshot(claude=taller))
        panel._apply_snapshot_update()
        flush(panel)
        assert frame(panel)[3] <= panel._px(520) and panel._extra_frame.winfo_ismapped()


class TestARebuildForAnotherMonitor:
    def test_a_resize_or_a_move_that_ends_on_a_monitor_of_another_dpi_builds_again_at_its_dpi(
            self, panel, monitor, monkeypatch):
        window, before = panel._window, panel._main_frame
        monitor.found = FOUR_K
        panel.show()
        assert panel.scale.k == pytest.approx(1.5)
        width = frame(panel)[2]
        d = Drag(panel, "e")
        d.move(60, 0)
        monitor.found = STANDARD
        d.release()
        assert panel.config.panel_scale == pytest.approx((width + 60) / width, abs=0.02)     # not 1.5 times that
        assert panel.scale.k == pytest.approx(panel.config.panel_scale)     # the user's part rides on the DPI
        assert panel._window is window and panel._main_frame is not before
        calls = []
        real = panel._sync_scale
        monkeypatch.setattr(panel, "_sync_scale", lambda: (calls.append(1), real())[1])
        panel.show()
        x, y, _, _ = frame(panel)
        d = Drag(panel, point=(x + 40, y + 36))
        d.move(30, 20)
        monitor.found = FOUR_K
        d.release()
        assert len(calls) == 2                                      # show and the end of a drag share one path
        assert panel.scale.k == pytest.approx(1.5 * panel.config.panel_scale)


class TestALiveWidthDrag:
    """While the pointer moves a width drag builds nothing: the window takes the size the panel will have at the
    scale the pointer asks for, and one picture of the panel, stretched to it, stands in for the content."""

    def test_the_window_follows_the_pointer_under_one_stretched_picture_and_a_drag_back_changes_nothing(
            self, panel, pictures, builds):
        before, main = frame(panel), panel._main_frame
        x, y, width, height = before
        d = Drag(panel, "e")
        for dx in (40, 90, 160, 80):
            d.move(dx, 0)
            x2, y2, width2, height2 = frame(panel)
            assert (x2, y2) == (x, y) and width2 == width + dx
            assert height2 == pytest.approx(height * width2 / width, abs=1)
            assert panel._curtain.photo.width() == width2 and panel._curtain.photo.height() == height2
        assert len(pictures.calls) == 1                         # one picture, taken at the first motion
        assert pictures.layered == [panel._curtain.winfo_id()]
        panel._update_window_size()                             # a snapshot arriving mid-drag asks for this
        flush(panel)
        assert frame(panel)[2] == width + 80
        d.move(0, 0)                                            # back to where it started
        d.release()
        assert frame(panel) == before and builds == [] and panel._main_frame is main and panel._curtain is None
        assert main.winfo_manager() == "pack" and main.winfo_ismapped()


class TestWithoutAPicture:
    def test_the_frame_follows_the_pointer_and_one_build_comes_on_release(self, panel, pictures, builds):
        pictures.size = None                              # Windows will not give a picture
        x, y, width, _ = frame(panel)
        old_content = panel._main_frame
        d = Drag(panel, "e")
        for dx in (50, 120, 200):
            d.move(dx, 0)
            assert frame(panel)[:2] == (x, y) and frame(panel)[2] == width + dx
            assert panel._curtain is None and panel._main_frame is old_content and builds == []
        d.release()
        assert len(builds) == 1 and panel._main_frame is not old_content
        assert panel.config.panel_scale == round((width + 200) / width, 3)


class TestALiveDragBeyondTheRoomBelowThePinnedEdge:
    """The picture is the whole panel at its proportion, and the real panel drops blocks to fit: a picture taller
    than the room beyond the edge that stays put is cut there, and the window does not slide up to make room, to
    drop back to its place when the build came."""

    def test_the_top_stays_where_it_is(self, panel, monitor):
        x, y, width, height = frame(panel)
        work = monitor.found.work
        room = work[3] - y
        wide = round(width * (room / height + 0.05))                 # a picture a little taller than the room
        assert room < height * wide / width < work[3] - work[1] - 2 * panel._px(12)
        d = Drag(panel, "e")
        for dx in (wide - width + 30, wide - width):
            d.move(dx, 0)
            x2, y2, width2, height2 = frame(panel)
            assert (x2, y2) == (x, y) and width2 == width + dx and height2 == room
        d.release()
        x3, y3, width3, height3 = frame(panel)                       # what the build fitted is its own
        assert x3 == x and y3 == min(y, work[3] - height3)

    def test_the_bottom_stays_where_it_is_when_the_top_edge_is_dragged(self, panel, monitor):
        panel._user_position = (700, 500)
        panel._update_window_size()
        flush(panel)
        x, y, width, height = frame(panel)
        work = monitor.found.work
        bottom = y + height
        room = bottom - work[1]
        wide = round(width * (room / height + 0.05))                 # a picture a little taller than the room above
        assert room < height * wide / width < work[3] - work[1] - 2 * panel._px(12)
        d = Drag(panel, "nw")
        for dx in (width - wide - 30, width - wide):                 # the left edge out, and the top as far as it goes
            d.move(dx, -700)
            x2, y2, width2, height2 = frame(panel)
            assert (x2 + width2, y2 + height2) == (x + width, bottom) and width2 == width - dx
        d.release()
        assert frame(panel)[0] + frame(panel)[2] == x + width


class TestAMoveDragLeavesThePanelInsideTheWorkArea:
    """The pointer can take the panel anywhere while it moves, the corner of the screen included. Where it lets go
    is not a place to keep: the release pulls the panel back, and it is the pulled-back place that is saved."""

    @staticmethod
    def grab(panel):
        x, y, width, _ = frame(panel)
        return x + width // 2, y + panel._px(36)                     # inside the content, beyond the resize margin

    @pytest.mark.parametrize("dx, dy", [(-3000, -3000), (4000, 4000), (-3000, 4000), (4000, -3000), (-3000, 0), (0, 4000)])
    def test_the_release_pulls_it_back_and_the_corrected_place_is_what_is_kept_and_saved(
            self, panel, saves, dx, dy):
        left, top, right, bottom = STANDARD.work
        x, y, width, height = frame(panel)
        d = Drag(panel, point=self.grab(panel))
        d.move(dx, dy)
        assert frame(panel)[:2] == (x + dx, y + dy)                          # while it moves it is where the pointer is
        d.release()
        expected = (max(left, min(x + dx, right - width)), max(top, min(y + dy, bottom - height)))
        assert frame(panel)[:2] == expected and panel._user_position == expected
        assert len(saves) == 1 and saves[0][2:] == expected
        panel.hide()
        panel.show()
        assert frame(panel)[:2] == expected                                  # and hide and show agree with the release

    def test_a_drag_that_ends_inside_is_not_moved(self, panel, saves):
        x, y, _, _ = frame(panel)
        d = Drag(panel, point=self.grab(panel))
        d.move(90, 40)
        d.release()
        assert frame(panel)[:2] == (x + 90, y + 40) == panel._user_position and saves[0][2:] == (x + 90, y + 40)

    def test_it_is_the_monitor_the_panel_is_on_that_it_is_kept_inside(self, panel, monitor, saves):
        monitor.found = Monitor(work=(0, 0, 1000, 900), height=1080, dpi=96)
        x, y, width, height = frame(panel)
        d = Drag(panel, point=self.grab(panel))
        d.move(1500, 0)
        d.release()
        assert frame(panel)[:2] == (1000 - width, y) and saves[0][2:] == (1000 - width, y)


class TestTheEdgeThatStaysPut:
    """A right edge dragged out to the work area's edge fills the room there is, and the left edge, which a resize
    pins, stays where it was. The width is a step function of the factor (text and fonts go by whole pixels), so
    a build can come out a pixel or two wider than the room, and the window was then pulled that far inwards."""

    @staticmethod
    def place(panel, scale, gap, right):
        """The panel at `scale`, its right edge `gap` pixels short of `right`, the work area's edge."""
        panel.config.panel_scale = scale
        panel._sync_scale()
        panel._user_position = (100, 100)
        panel.show()
        flush(panel)
        width = frame(panel)[2]
        panel.hide()
        panel._user_position = (right - width - gap, 100)
        panel.show()
        flush(panel)
        return frame(panel)

    @pytest.mark.parametrize("scale, gap", [(0.75, 34), (0.75, 106), (0.75, 238), (1.0, 58), (1.0, 130), (1.0, 190),
                                            (1.3, 16), (1.0, 154)])
    def test_the_left_edge_does_not_move_when_the_drag_runs_into_the_edge_of_the_work_area(
            self, panel, builds, scale, gap):
        x, y, width, _ = self.place(panel, scale, gap, STANDARD.work[2])
        builds.clear()
        d = Drag(panel, "e")
        for dx in range(20, 700, 80):
            d.move(dx, 0)
        d.release()
        x2, _, width2, _ = frame(panel)
        assert x2 == x and x2 + width2 <= STANDARD.work[2]
        assert len(builds) <= 6

    @pytest.mark.parametrize("scale, gap", [(0.75, 196), (1.3, 160), (1.3, 232)])
    def test_a_drag_into_the_edge_fills_the_room_and_does_not_fall_back_to_the_scale_it_had(
            self, panel, builds, scale, gap):
        """Below a width that misses the room the width falls by far less than in proportion until the next font
        size, a plateau of up to 0.075 of the factor: estimates that aimed 0.001, 0.002 ... under spent their builds
        on the plateau, and the panel went back to the size it had with the room beside it empty."""
        x, y, width, _ = self.place(panel, scale, gap, STANDARD.work[2])
        room = STANDARD.work[2] - x
        builds.clear()
        d = Drag(panel, "e")
        for dx in range(20, 700, 80):
            d.move(dx, 0)
        d.release()
        x2, _, width2, _ = frame(panel)
        assert x2 == x and panel.scale.k > 1.1 * scale and room - width2 <= 0.1 * room
        assert len(builds) <= 6

    def test_a_width_that_cannot_be_made_to_fit_leaves_the_panel_at_the_scale_it_has(self, panel, builds):
        before = frame(panel)
        panel._set_scale(2.0, 100)                                           # no scale makes this fit 100 pixels
        assert panel.config.panel_scale == 1.0 and len(builds) <= 6 and frame(panel) == before

    @pytest.mark.parametrize("scale, gap", [(0.76, 13), (0.78, 5), (0.86, 5), (0.91, 13), (0.93, 13), (1.01, 5)])
    def test_an_outward_drag_into_the_edge_never_leaves_the_panel_smaller_than_it_was(
            self, panel, monitor, builds, scale, gap):
        """The width is a step of the factor: with a few pixels of room at the edge the estimate goes over, and the
        estimates after it jumped over the scale the panel had, to one below it."""
        monitor.found = Monitor(work=(0, 0, 1920, 1032), height=1080, dpi=96)
        x, y, width, height = self.place(panel, scale, gap, 1920)
        builds.clear()
        d = Drag(panel, "e")
        for dx in (20, 60, 120, 200):
            d.move(dx, 0)
        d.release()
        x2, y2, width2, height2 = frame(panel)
        assert (x2, y2) == (x, y) and width2 >= width and height2 >= height and panel.scale.k >= scale
        assert x2 + width2 <= 1920 and len(builds) <= 6

    def test_a_drag_that_does_not_reach_the_edge_is_one_build(self, panel, builds):
        before = panel.scale.k
        drag(panel, "e", dx=-120, steps=3)                                   # inward: below the scale the panel had
        assert panel.scale.k < before and len(builds) == 1
        builds.clear()
        before = panel.scale.k
        drag(panel, "e", dx=60, steps=3)                                     # outward, with room to spare
        assert panel.scale.k > before and len(builds) == 1


class TestTheCurtain:
    @pytest.fixture
    def seen(self, panel, monkeypatch):
        """Whether the curtain was up each time the content began to be built."""
        seen = []
        real = panel._build_content
        monkeypatch.setattr(panel, "_build_content", lambda k: (seen.append(panel._curtain is not None), real(k)))
        return seen

    def test_it_is_up_while_the_content_is_built_and_gone_after(self, panel, monitor, seen, pictures):
        flows = {
            "monitor": lambda: (setattr(monitor, "found", FOUR_K), panel.show()),
            "release": lambda: drag(panel, "e", dx=150, steps=3),
            "reset": panel.reset_size,                        # from the scale the release left
            "settings": panel._rebuild_window,
        }
        for name, flow in flows.items():
            seen.clear()
            pictures.calls.clear()
            flow()
            assert seen == [True], name
            assert panel._curtain is None, name
            assert not any(hasattr(child, "photo") for child in panel._window.winfo_children()), name
            assert len(pictures.calls) == 1, name             # one picture for the whole change, however many builds

    def test_a_hidden_panel_is_built_without_one(self, panel, monitor, seen, pictures):
        panel.hide()
        monitor.found = FOUR_K
        panel.show()
        assert seen == [False] and pictures.calls == [] and panel._curtain is None
        panel.hide()
        panel._rebuild_window()
        assert seen == [False, False] and pictures.calls == []

    def test_a_build_that_raises_leaves_no_curtain_and_no_drag_behind(self, panel, monkeypatch):
        real = panel._build_content

        def boom(k):
            raise RuntimeError("boom")

        def build_then_boom(k):
            real(k)
            raise RuntimeError("boom")

        for build in (boom, build_then_boom):
            monkeypatch.setattr(panel, "_build_content", build)
            with pytest.raises(RuntimeError):
                panel._step_scale(1)
            assert panel._curtain is None
            assert not any(hasattr(child, "photo") for child in panel._window.winfo_children())
        monkeypatch.setattr(panel, "_build_content", real)
        d = Drag(panel, "e")
        d.move(120, 0)
        assert panel._curtain is not None
        monkeypatch.setattr(panel, "_build_content", boom)
        with pytest.raises(RuntimeError):
            d.release()                                   # a release whose build raises
        assert panel._curtain is None and panel._resize is None and panel._pin is None
        assert panel._main_frame.winfo_manager() == "pack"       # the content is back in the window

    def test_a_motion_that_arrives_while_the_curtain_goes_does_not_start_another_picture(self, panel, pictures,
                                                                                         monkeypatch):
        """The curtain goes after a pass of the event loop, which may hold a motion queued behind the release."""
        d = Drag(panel, "e")
        d.move(120, 0)
        pictures.calls.clear()
        real, sent = panel._window.update, []

        def update():
            if not sent:
                sent.append(panel._on_drag_move(event(d.start[0] + 200, d.start[1])))
            real()

        monkeypatch.setattr(panel._window, "update", update)
        d.release()
        assert sent and panel._curtain is None and pictures.calls == [] and panel._resize_job is None

    def test_hiding_the_panel_in_a_drag_ends_the_drag_and_takes_it_away(self, panel):
        d = Drag(panel, "e")
        d.move(80, 0)
        assert panel._curtain is not None and not panel._main_frame.winfo_manager()
        panel.hide()
        assert panel._curtain is None and panel._resize is None and panel._window.grab_current() is None
        assert panel._main_frame.winfo_manager() == "pack"


class TestResetSize:
    def test_it_works_on_a_panel_that_was_never_built(self, monitor, saves):
        window = ui_window.ClaudeBarWindow(on_refresh=lambda: None, on_settings=lambda: None,
                                           on_exit=lambda: None, config=Config(panel_scale=2.0, panel_max_height=300))
        window.reset_size()
        assert window.config.panel_scale is None and window.config.panel_max_height is None and len(saves) == 1


class TestTheTrayItem:
    @staticmethod
    def manager(window):
        manager = tray.TrayManager(on_refresh=lambda: None, on_exit=lambda: None, use_premium_ui=False)
        manager._ui_window = window
        return manager

    def test_the_menu_has_it_beside_reset_window_position(self):
        texts = [item.text for item in self.manager(SimpleNamespace())._create_menu() if item.text]
        assert texts.index("Reset panel size") == texts.index("Reset window position") + 1

    def test_the_item_reaches_the_panel_on_the_main_thread_through_the_queue(self):
        called = []
        manager = self.manager(SimpleNamespace(reset_size=lambda: called.append(threading.current_thread())))
        thread = threading.Thread(target=manager._on_reset_size, args=(None, None))
        thread.start()
        thread.join(5)
        assert called == []                                # pystray's thread did not touch the panel
        manager.run_pending()                              # the main thread's tick
        assert called == [threading.current_thread()]
        manager = self.manager(None)                       # with no panel it does nothing
        manager._on_reset_size(None, None)
        manager.run_pending()


class TestTheSettingsLabelStatesTheRealRange:
    def test_the_interval_says_both_ends(self, panel):
        import config
        dialog = ui_window.SettingsDialog(panel._window, Config(), lambda: None)
        try:
            texts = [w.cget("text") for w in _labels(dialog.dialog)]
        finally:
            dialog.dialog.destroy()
        label = next(t for t in texts if t.startswith("Refresh Interval"))
        assert str(config.MIN_REFRESH_INTERVAL) in label and str(config.MAX_REFRESH_INTERVAL) in label
        assert "min 30" not in label


def _labels(root):
    for child in root.winfo_children():
        if child.winfo_class() == "Label":
            yield child
        yield from _labels(child)
