"""The panel at one scale factor: what it asks of Tk follows the factor, a rebuild leaves the window
alone, and the factor and the place come from the monitor the panel is on."""

import ast
import re
import tkinter as tk
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import ui_window
from config import Config
from models import CombinedSnapshot, ModelUsage, TokenUsage, UsageSnapshot
from monitor import Monitor
from ui_scale import Scale

UTC = timezone.utc
STANDARD = Monitor(work=(0, 0, 2560, 1392), height=1440, dpi=96)

# What the panel asked Tk for before it scaled, with the snapshot below in the cost view
# (the main frame, measured on the tree at the commit before the scale factor: 429 x 718).
BEFORE_SCALING = (429, 718)


def snapshot():
    """A Claude reading with every row of the cost view filled; the reset times sit mid-minute, so
    the countdown words ("in 2h 10m") do not change while a test runs."""
    now = datetime.now(UTC)
    return UsageSnapshot(
        timestamp=datetime.now(), session_percent=42.0,
        session_resets_at=now + timedelta(hours=2, minutes=10, seconds=30),
        weekly_percent=61.0, weekly_resets_at=now + timedelta(days=3, seconds=30),
        today_cost_usd=12.34, month_cost_usd=345.67, last31_cost_usd=401.2, last31_tokens=2_730_000_000,
        today_tokens=TokenUsage(input_tokens=377_900_000), month_tokens=TokenUsage(input_tokens=2_100_000_000),
        daily_costs=[float((i * 7) % 23 + 1) for i in range(31)],
        models_used=[ModelUsage("claude-opus-4-1", cost_usd=200.0)])


@pytest.fixture
def monitor(monkeypatch):
    """The monitor Windows reports; a test changes `.found` to move the panel to another."""
    state = SimpleNamespace(found=STANDARD)
    monkeypatch.setattr(ui_window, "monitor_at", lambda x, y, nearest=True: state.found)
    return state


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


@pytest.fixture
def panel(stage, monitor, monkeypatch):
    """The panel as a new one is: hidden, no reading, the automatic scale, the Cost view. Its content is as the
    last test left it (see `fresh`): most tests build what they measure."""
    monkeypatch.setattr(ui_window.ClaudeBarWindow, "_load_initial_data", lambda self: None)
    monkeypatch.setattr(ui_window, "save_config", lambda cfg: None)
    window = stage.panel
    if window is None:
        window = stage.panel = ui_window.ClaudeBarWindow(
            on_refresh=lambda: None, on_settings=lambda: None, on_exit=lambda: None, config=Config(),
            on_engine_change=lambda e: None)
        window._create_window()
        return window
    window.hide()
    config = window.config
    config.panel_scale = config.panel_max_height = None
    config.panel_view = "cost"
    window._user_position = None
    window._snapshot = window._openai_snapshot = window._deepseek_snapshot = None
    window._pending_snapshot = window._pending_openai_snapshot = window._pending_deepseek_snapshot = None
    return window


def fresh(panel):
    """The content built as a new panel builds it, at the scale the monitor and the config ask for."""
    panel._build_content(panel._choose_scale())


def fill(panel, claude=None):
    panel.update_combined(CombinedSnapshot(claude=claude or snapshot()))
    panel._apply_snapshot_update()
    panel._window.update_idletasks()


def required(panel, k):
    """The size the content asks for when built at factor `k` and filled."""
    panel._build_content(k)
    fill(panel)
    frame = panel._main_frame
    return frame.winfo_reqwidth(), frame.winfo_reqheight()


def widgets(root):
    for child in root.winfo_children():
        yield child
        yield from widgets(child)


class TestTheSizeItAsksFor:
    @pytest.fixture(autouse=True)
    def room(self, monitor):
        """What the content asks for, not what a screen gives it: a screen tall enough that the fit
        hides nothing at the largest factor (it would, on the 1440p one: see TestThePlace)."""
        monitor.found = Monitor(work=(0, 0, 5000, 4000), height=4320, dpi=96)

    def test_at_one_it_asks_for_what_it_asked_for_before_it_scaled(self, panel):
        width, height = required(panel, 1.0)
        assert abs(width - BEFORE_SCALING[0]) <= 2 and abs(height - BEFORE_SCALING[1]) <= 2

    def test_at_the_other_factors_it_is_within_five_percent_of_the_factor_times_the_size_at_one(self, panel):
        base_width, base_height = required(panel, 1.0)
        for k in (0.7, 0.75, 1.5, 2.0):                                     # 0.75: the automatic factor of a 1080p screen
            width, height = required(panel, k)
            assert width == pytest.approx(k * base_width, rel=0.05), (k, width, k * base_width)
            assert height == pytest.approx(k * base_height, rel=0.05), (k, height, k * base_height)
        panel.config.panel_view = "forecast"                                # the Quota view scales too
        base = required(panel, 1.0)
        width, height = required(panel, 2.0)
        assert width == pytest.approx(2 * base[0], rel=0.05) and height == pytest.approx(2 * base[1], rel=0.05)

    def test_the_bars_the_charts_the_border_and_the_width_floor_are_as_big_as_the_factor_says(self, panel):
        panel._build_content(1.5)
        fill(panel)
        assert int(panel._limits["session"]["bar"].cget("width")) == 522          # 348 x 1.5
        assert int(panel._chart.cget("width")) == 522 and int(panel._chart.cget("height")) == 117
        assert int(panel._fc_chart.cget("width")) == 522 and int(panel._fc_chart.cget("height")) == 165
        panel._build_content(2.0)
        assert int(panel._main_frame.cget("highlightthickness")) == 2
        assert panel._window_width == 760


class TestAFlatLabelRoundsItsBorderAndPaddingAsOneLength:
    """The 2 pixel border of a flat label is only space, so it is rounded with its 1 pixel of padding, once. Rounded
    apart, the 1.5 pixels of the border at 0.75 (the automatic factor of a 1080p screen at 100%) became 2 on every
    label at once, and the panel was about 50 pixels taller than at 0.74 or 0.76."""

    @staticmethod
    def asked(panel, k):
        """(width, height) a label of the panel asks for, and the same label with the border and the padding
        rounded apart, as `_label` used to give them."""
        panel._build_content(k)
        font = panel._font(9)
        new = panel._label(panel._window, text="Ag", font=font)
        old = tk.Label(panel._window, text="Ag", font=font,
                       bd=panel._px(2), padx=panel._px(1), pady=panel._px(1))
        panel._window.update_idletasks()
        return (new.winfo_reqwidth(), new.winfo_reqheight()), (old.winfo_reqwidth(), old.winfo_reqheight())

    def test_the_border_is_no_border_and_the_space_is_the_padding_rounded_once(self, panel):
        for k in (0.7, 0.75, 1.0, 1.75, 2.0):
            panel._build_content(k)
            label = panel._label(panel._window, text="x")
            assert int(label.cget("bd")) == 0, k
            assert int(label.cget("padx")) == int(label.cget("pady")) == panel._px(3), k

    def test_a_label_is_the_size_it_was_where_the_two_roundings_agree(self, panel):
        for k in (0.7, 0.84, 1.0, 1.25, 1.5, 2.0, 2.5):
            new, old = self.asked(panel, k)
            assert new == old, k

    def test_at_the_half_way_factors_it_is_the_one_pixel_a_side_the_rounding_gave_it_too_much(self, panel):
        for k in (0.75, 1.75):
            (new_width, new_height), (old_width, old_height) = self.asked(panel, k)
            assert (old_width - new_width, old_height - new_height) == (2, 2), k

    def test_a_label_that_says_its_own_padding_border_or_image_keeps_the_scaled_defaults(self, panel):
        panel._build_content(1.5)
        image = tk.PhotoImage(master=panel._window, width=1, height=1)
        for options, bd, padx, pady in (({"padx": 4}, 3, 4, 2), ({"pady": 4}, 3, 2, 4), ({"bd": 5}, 5, 2, 2),
                                        ({"borderwidth": 5}, 5, 2, 2), ({"image": image}, 3, 2, 2)):
            label = panel._label(panel._window, **options)
            assert (int(label.cget("bd")), int(label.cget("padx")), int(label.cget("pady"))) == (bd, padx, pady), options


class TestFontsAreSizedInPixels:
    def test_every_label_and_button_has_a_pixel_font_and_the_helper_keeps_the_bump(self, panel):
        panel._build_content(1.0)
        assert panel._font(9)[1] == -15                           # 9 + 2 points: 14.7 pixels
        assert panel._font(9, scale=Scale(2.0))[1] == -29         # and takes the dialog's scale
        assert panel._font(8, "bold", mono=True) == (panel.mono, -13, "bold")
        panel._build_content(1.5)
        fill(panel)
        checked = 0
        for widget in widgets(panel._main_frame):
            if widget.winfo_class() in ("Label", "Button") and not str(widget.cget("image")):
                size = re.search(r"\s(-?\d+)\s", str(widget.cget("font")) + " ")
                assert size and int(size.group(1)) < 0, f"{widget} has the font {widget.cget('font')}"
                checked += 1
        assert checked > 30


class TestTheSourceLeavesNoPixelUnscaled:
    """What the panel and its dialogs hand to Tk goes through _px, _spacing and _font. A nonzero
    number written straight into a call, or a font tuple, is a length that stays put when the rest
    of the panel scales."""

    LENGTHS = {"padx", "pady", "wraplength", "highlightthickness", "bd", "borderwidth"}
    TK_CALLS = {"pack", "grid", "place", "Label", "Button", "Frame", "Canvas", "Checkbutton", "Entry",
                "Toplevel", "Combobox", "config", "configure"}

    @classmethod
    def calls(cls):
        tree = ast.parse(Path(ui_window.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
                if name in cls.TK_CALLS:
                    yield name, node

    @classmethod
    def bare(cls, value):
        """A nonzero number in `value` that is not inside a call (self._px(8), self._spacing((0, 8)))."""
        if isinstance(value, ast.Call):
            return False
        if isinstance(value, ast.Constant):
            return isinstance(value.value, (int, float)) and not isinstance(value.value, bool) and value.value != 0
        return any(cls.bare(child) for child in ast.iter_child_nodes(value))

    def test_no_padding_wrap_or_border_is_a_bare_number(self):
        found = [(name, kw.arg, kw.value.lineno) for name, node in self.calls() for kw in node.keywords
                 if kw.arg in self.LENGTHS and self.bare(kw.value)]
        assert found == []

    def test_no_font_is_a_literal_tuple(self):
        found = [(name, kw.value.lineno) for name, node in self.calls() for kw in node.keywords
                 if kw.arg == "font" and isinstance(kw.value, ast.Tuple)]
        assert found == []

    def test_the_check_sees_a_bare_number(self):
        call = ast.parse("tk.Label(frame, padx=8, pady=(0, 4), bd=0, font=('Segoe UI', 9))").body[0].value
        assert [kw.arg for kw in call.keywords if self.bare(kw.value)] == ["padx", "pady", "font"]
        assert not self.bare(ast.parse("self._px(8)", mode="eval").body)
        assert not self.bare(ast.parse("self._spacing((0, 8) if row == 0 else 0)", mode="eval").body)


class TestOneWindow:
    def test_a_rebuild_reuses_the_window_and_replaces_only_the_content(self, panel):
        fill(panel)
        window, old, old_bar = panel._window, panel._main_frame, panel._limits["session"]["bar"]
        before = panel._window.winfo_id()
        shown = panel._stats["today"][1].cget("text")
        panel._rebuild_window()
        assert panel._window is window and panel._window.winfo_id() == before
        assert panel._main_frame is not old and not old.winfo_exists()
        assert panel._limits["session"]["bar"] is not old_bar and not old_bar.winfo_exists()
        assert set(panel._limits) == {"session", "weekly"} and len(panel._fc_rows) == 6
        assert panel._stats["today"][1].cget("text") == shown != ""        # filled again from what it holds
        for sequence in ("<Escape>", "<Button-1>", "<B1-Motion>", "<ButtonRelease-1>"):
            assert panel._window.bind(sequence), sequence                  # the bindings belong to the window
        panel.hide()

    def test_show_rebuilds_in_the_same_window_for_another_dpi_or_height_and_honours_a_new_scale(
            self, panel, monitor):
        panel.show()
        assert panel.scale.k == 1.0
        window_id, frame = panel._window.winfo_id(), panel._main_frame
        monitor.found = Monitor(work=(0, 0, 3840, 2112), height=2160, dpi=96)       # another height only
        panel.show()
        assert panel.scale.k == pytest.approx(1.5)
        assert panel._window.winfo_id() == window_id and panel._main_frame is not frame
        built = panel._main_frame
        panel.show()                                                                # nothing changed
        assert panel._main_frame is built
        monitor.found = Monitor(work=(0, 0, 2560, 1392), height=1440, dpi=144)      # another DPI: 960 logical
        panel.show()
        assert panel.scale.k == pytest.approx(1.05)
        monitor.found = STANDARD
        panel.config.panel_scale = 1.25
        panel.show()
        assert panel.scale.k == 1.25
        panel.hide()


class TestTheFactor:
    def test_it_is_the_dpi_scale_times_the_panel_scale_or_the_screens(self, panel, monitor):
        for height, dpi, configured, expected in [
                (1440, 96, None, 1.0),
                (1080, 96, None, 0.75),
                (2160, 96, None, 1.5),
                (2160, 192, None, 1.5),            # 4K at 200%: 0.75 of the logical screen, doubled by the desktop
                (1440, 144, None, 1.05),           # 150%: 960 logical, so the 0.7 floor, times 1.5
                (600, 96, None, 0.7),
                (1440, 96, 1.25, 1.25),            # the user's own factor wins over the screen's height
                (1080, 96, 1.0, 1.0),
                (1440, 144, 1.2, 1.8)]:            # and rides on the monitor's DPI scale
            monitor.found = Monitor(work=(0, 0, 3000, height - 48), height=height, dpi=dpi)
            panel.config.panel_scale = configured
            assert panel._choose_scale() == pytest.approx(expected), (height, dpi, configured)

    def test_the_dialogs_take_the_larger_of_the_panels_factor_and_the_dpi_scale(self, panel):
        for k, dpi_scale, expected in [
                (0.7, 1.0, 1.0),                   # a dialog never shrinks
                (1.0, 1.0, 1.0),
                (2.0, 1.0, 2.0),                   # it grows with the panel
                (1.05, 1.5, 1.5),                  # and keeps the monitor's own DPI scale whole
                (1.8, 1.5, 1.8)]:
            panel.scale, panel._dpi_scale = Scale(k), dpi_scale
            assert panel._dialog_scale().k == pytest.approx(expected), (k, dpi_scale)


class TestThePlace:
    @staticmethod
    def placed(panel):
        """(x, y, width, height) the panel asked for. Tk forces a window that cannot be resized to the
        size it requests, so the size is read from the panel and only the place from the window."""
        panel._update_window_size()
        x, y = (int(v) for v in re.findall(r"-?\d+", panel._window.geometry())[2:])
        return x, y, panel._window_width, panel._main_frame.winfo_reqheight() + panel._px(4)

    def test_a_dragged_spot_is_kept_the_default_is_the_bottom_right_and_it_never_hangs_off(self, panel, monitor):
        fresh(panel)
        fill(panel)
        panel._user_position = (1200, 300)
        x, y, _, _ = self.placed(panel)
        assert (x, y) == (1200, 300)                                  # a dragged spot inside the area is kept
        panel._user_position = None
        monitor.found = Monitor(work=(100, 50, 1700, 950), height=1000, dpi=96)
        x, y, w, h = self.placed(panel)
        assert (x + w, y + h) == (1700 - 20, 950 - 12)                # by default the bottom right of the work area
        for spot in ((5000, 5000), (-500, -500), (1650, 940), (101, 51)):
            panel._user_position = spot
            x, y, w, h = self.placed(panel)
            assert 100 <= x and x + w <= 1700 and 50 <= y and y + h <= 950, (spot, (x, y, w, h))
        monitor.found = Monitor(work=(0, 0, 3840, 2100), height=2160, dpi=96)
        panel._user_position = None
        panel._build_content(2.0)
        fill(panel)
        x, y, w, h = self.placed(panel)
        assert (x + w, y + h) == (3840 - 40, 2100 - 24)               # the margins scale with the panel

    def test_the_monitor_is_the_one_under_where_it_was_dragged_else_the_primary_screen_at_96_dpi(
            self, panel, monkeypatch):
        seen = []
        second = Monitor(work=(2560, 0, 5120, 1392), height=1440, dpi=96)

        def monitor_at(x, y, nearest=True):
            seen.append((x, y))
            return second if x >= 2560 else STANDARD
        monkeypatch.setattr(ui_window, "monitor_at", monitor_at)
        panel._user_position = (2700, 100)
        assert panel._target_monitor() is second
        assert seen[-1] == (2740, 120)                    # the header point, 40 right and 20 down
        panel._user_position = None
        assert panel._target_monitor() is STANDARD and seen[-1] == (0, 0)    # the primary monitor's corner
        monkeypatch.setattr(ui_window, "monitor_at", lambda x, y, nearest=True: None)       # Windows will not say
        found = panel._target_monitor()
        assert found.dpi == 96
        assert found.work == (0, 0, panel._window.winfo_screenwidth(), panel._window.winfo_screenheight())
        assert panel._choose_scale() > 0


class TestTheConfirmationDialogGrowsNeverShrinks:
    @staticmethod
    def style(scale):
        return SimpleNamespace(
            bg="#1c1c1e", primary="#fff", secondary="#aaa", muted="#777", accent="#F59E0B",
            warn="#f00", button_bg="#333", hover_bg="#444", scale=scale,
            font=lambda size, weight="normal": ("Segoe UI", scale.font_px(size), weight))

    def open(self, tk_root, scale, on_confirm=lambda: None):
        return ui_window.ResetConfirmDialog(
            tk_root, ["Opus launch reset", "Expires Oct 22", "This cannot be undone."],
            on_confirm, lambda: None, self.style(scale))

    def test_it_is_at_least_as_wide_as_the_scale_says_and_still_ignores_a_stray_click(self, tk_root, monkeypatch):
        """The root is withdrawn, so the dialog is never mapped and has no width to read: what it
        asks to be placed at is the width."""
        asked = {}
        monkeypatch.setattr(ui_window, "_place_over",
                            lambda dialog, parent, width, height, margin: asked.update(
                                width=width, height=height, margin=margin))
        sizes = {}
        for k in (1.0, 2.0):
            dialog = self.open(tk_root, Scale(k))
            try:
                sizes[k] = dict(asked)
            finally:
                dialog._cancel()
        assert sizes[1.0]["width"] >= 360 and sizes[2.0]["width"] >= 720
        assert sizes[2.0]["height"] > 1.5 * sizes[1.0]["height"]
        assert (sizes[1.0]["margin"], sizes[2.0]["margin"]) == (12, 24)
        confirmed = []
        dialog = self.open(tk_root, Scale(2.0), lambda: confirmed.append(1))
        try:
            dialog.use_button.invoke()
            assert confirmed == [] and dialog.ARM_SECONDS == 0.6
        finally:
            dialog._cancel()


class TestTheMarksAreMadeOncePerSize:
    def test_a_rebuild_at_the_same_factor_reads_no_image_file_again_and_the_marks_grow_with_the_factor(self, panel):
        panel._build_content(1.0)
        glyphs, logos = ui_window._engine_glyph.cache_info(), ui_window._logo.cache_info()
        small = panel._engine_icon_images(ui_window.Engine.CODEX)[0]
        panel._build_content(1.0)
        assert ui_window._engine_glyph.cache_info().misses == glyphs.misses
        assert ui_window._logo.cache_info().misses == logos.misses
        assert ui_window._engine_glyph.cache_info().hits > glyphs.hits
        panel._build_content(2.0)
        large = panel._engine_icon_images(ui_window.Engine.CODEX)[0]
        assert (small.width(), large.width()) == (20, 40)

    def test_a_mark_has_the_size_asked_for_and_each_size_is_its_own_entry(self):
        path = ui_window._ENGINE_META[ui_window.Engine.CLAUDE][1]
        small, large = ui_window._engine_glyph(path, 20), ui_window._engine_glyph(path, 40)
        assert small.size == (20, 20) and large.size == (40, 40)
        assert ui_window._engine_glyph(path, 20) is small
        assert ui_window._logo(28).size == (28, 28) and ui_window._logo(28) is ui_window._logo(28)

    def test_a_missing_mark_is_none_and_not_an_error(self, tmp_path):
        assert ui_window._engine_glyph(tmp_path / "nothing.png", 20) is None
