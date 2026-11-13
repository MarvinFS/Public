"""The usage bar and the daily bar chart at the panel's scale factor.

Both take design pixels and a `scale`; at the default 1.0 they are what they were before the panel
scaled, and at 2 every length of theirs is doubled.
"""

import types

import pytest

from ui_widgets import BarChart, PaceBar, _pill_image, bar_index_at

BG = "#1c1c1e"
FONT = ("Segoe UI", -12)
TIP_FONT = ("Segoe UI", -16, "bold")


def size(widget):
    return int(widget.cget("width")), int(widget.cget("height"))


@pytest.fixture
def made(tk_root):
    widgets = []

    def make(cls, **kw):
        widget = cls(tk_root, bg=BG, **kw)
        widgets.append(widget)
        return widget
    yield make
    for widget in widgets:
        widget.destroy()


class TestPaceBar:
    def test_at_the_default_scale_it_is_348_by_6_with_a_pixel_above_and_below(self, made):
        bar = made(PaceBar)
        assert size(bar) == (348, 8)
        assert bar._image.height() == 6

    def test_at_scale_two_every_length_is_doubled(self, made):
        bar = made(PaceBar, scale=2)
        assert size(bar) == (696, 16)
        assert (bar._image.width(), bar._image.height()) == (696, 12)

    @pytest.mark.parametrize("height", [6, 12, 15])
    def test_the_pill_has_round_ends_at_any_height(self, height):
        img = _pill_image(696, height, 50, "#3a3a3c", "#F59E0B", BG)
        assert img.size == (696, height)
        mid = height // 2
        assert sum(abs(a - b) for a, b in zip(img.getpixel((0, 0)), (28, 28, 30))) < 12      # the corner is background
        assert sum(abs(a - b) for a, b in zip(img.getpixel((height, mid)), (245, 158, 11))) < 40    # the filled part
        assert sum(abs(a - b) for a, b in zip(img.getpixel((690, mid)), (58, 58, 60))) < 40         # the track

    def test_the_pace_tick_is_as_wide_and_as_tall_as_the_scale_says(self, made):
        for scale, margin in ((1, 1), (2, 2)):
            bar = made(PaceBar, scale=scale)
            bar.set_value(40, 25)
            tick = bar.find_withtag("v")[0]
            x = bar.w * 25 / 100
            assert bar.coords(tick) == [x - margin, 0, x + margin, bar.h + 2 * margin]

    def test_no_pace_means_no_tick(self, made):
        bar = made(PaceBar, scale=2)
        bar.set_value(40, None)
        assert bar.find_withtag("v") == ()

    def test_a_narrow_scale_keeps_a_pixel_of_room(self, made):
        bar = made(PaceBar, scale=0.3)
        assert size(bar) == (104, 4)                    # 6 -> 2, plus a pixel above and below


class TestBarChart:
    VALUES = [3.0, 1.0, 4.0, 1.0, 5.0, 9.0, 2.0]

    @staticmethod
    def bars(chart):
        return sorted((chart.coords(i) for i in chart.find_withtag("v")), key=lambda c: c[0])

    def test_at_the_default_scale_it_is_348_by_78(self, made):
        chart = made(BarChart, width=348, height=78, font=FONT)
        assert size(chart) == (348, 78) and (chart.w, chart.h) == (348, 78)

    def test_at_scale_two_the_canvas_is_doubled(self, made):
        chart = made(BarChart, width=348, height=78, font=FONT, scale=2)
        assert size(chart) == (696, 156) and (chart.w, chart.h) == (696, 156)

    @pytest.mark.parametrize("scale", [1, 2])
    def test_a_week_has_wider_gaps_than_a_month_in_scaled_pixels(self, made, scale):
        chart = made(BarChart, width=348, height=78, font=FONT, scale=scale)
        chart.set_values(self.VALUES)
        assert chart.gap == 4 * scale
        chart.set_values([1.0] * 31)
        assert chart.gap == 2 * scale

    def test_the_bars_keep_their_pitch_and_height_at_twice_the_scale(self, made):
        one = made(BarChart, width=348, height=78, font=FONT)
        two = made(BarChart, width=348, height=78, font=FONT, scale=2)
        one.set_values(self.VALUES)
        two.set_values(self.VALUES)
        bars1, bars2 = self.bars(one), self.bars(two)
        assert len(bars1) == len(bars2) == 7
        for a, b in zip(bars1, bars2):
            assert b[0] == pytest.approx(2 * a[0], abs=0.01)
            assert b[2] == pytest.approx(2 * a[2], abs=0.01)
            assert b[3] == 156 and a[3] == 78                 # they stand on the bottom edge
        # the peak leaves two design pixels of headroom, and the shortest is at least one pixel tall
        assert min(b[1] for b in bars1) == 2 and min(b[1] for b in bars2) == 4

    def test_the_hover_follows_the_scaled_gap(self, made):
        chart = made(BarChart, width=348, height=78, font=FONT, tip_font=TIP_FONT, scale=2)
        chart.set_values(self.VALUES, [f"day {i}" for i in range(7)])
        for i, (x0, _, x1, _) in enumerate(self.bars(chart)):
            chart._on_motion(types.SimpleNamespace(x=(x0 + x1) / 2))
            assert chart._tip_index == i
            assert bar_index_at(int((x0 + x1) / 2), 7, chart.w, chart.gap) == i

    def test_the_flyout_pad_is_scaled(self, made):
        import tkinter.font as tkfont
        text = "Nov 9 · $12.34"
        widths = {}
        for scale in (1, 2):
            chart = made(BarChart, width=348, height=78, font=FONT, tip_font=TIP_FONT, scale=scale)
            chart.set_values(self.VALUES, [text] * 7)
            chart._show_tip(3)
            box = next(i for i in chart.find_withtag("tip") if chart.type(i) == "rectangle")
            x0, y0, x1, y1 = chart.coords(box)
            measure = tkfont.Font(root=chart, font=TIP_FONT)
            assert x1 - x0 == measure.measure(text) + 2 * 7 * scale
            assert y1 - y0 == measure.metrics("linespace") + 7 * scale
            widths[scale] = x1 - x0
        assert widths[2] > widths[1]

    def test_without_a_tip_font_the_flyout_uses_the_chart_font(self, made):
        chart = made(BarChart, width=348, height=78, font=FONT)
        assert chart.tip_font == FONT
