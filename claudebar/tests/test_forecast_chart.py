"""The forecast chart's coordinate maths and hover lookup (no Tk window)."""

from datetime import datetime, timedelta, timezone
from typing import NamedTuple

import pytest

import forecast as fc
import forecast_chart as chart

UTC = timezone.utc
NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
START, END = NOW - timedelta(hours=72), NOW + timedelta(hours=96)
W, H = 348, 110


def h(hours):
    return timedelta(hours=hours)


class P(NamedTuple):
    x: float
    tag: str


class TestNearestPoint:
    # Uneven spacing and an even count: a middle element cannot be right by accident.
    POINTS = [P(0, "a"), P(10, "b"), P(40, "c"), P(41, "d"), P(100, "e"), P(300, "f")]

    @pytest.mark.parametrize("x,tag", [
        (-50, "a"), (0, "a"), (4, "a"), (6, "b"), (10, "b"), (24, "b"), (26, "c"),
        (40.4, "c"), (40.6, "d"), (70, "d"), (71, "e"), (100, "e"), (199, "e"), (201, "f"),
        (348, "f"), (1000, "f")])
    def test_the_nearest_point_wins_with_its_own_payload(self, x, tag):
        assert chart.nearest_point(self.POINTS, x).tag == tag

    def test_a_tie_goes_to_the_left(self):
        assert chart.nearest_point([P(0, "l"), P(10, "r")], 5).tag == "l"

    def test_beyond_max_dist_is_none(self):
        assert chart.nearest_point(self.POINTS, 200, max_dist=50) is None
        assert chart.nearest_point(self.POINTS, 148, max_dist=50).tag == "e"

    def test_no_points_is_none(self):
        assert chart.nearest_point([], 10) is None

    def test_one_point(self):
        assert chart.nearest_point([P(5, "only")], 300).tag == "only"


class TestCoordinates:
    def test_x_runs_across_the_width(self):
        assert chart.x_of(START, START, END, W) == 0
        assert chart.x_of(END, START, END, W) == W
        assert chart.x_of(NOW, START, END, W) == pytest.approx(W * 3 / 7)

    def test_y_runs_from_the_plot_bottom_to_the_plot_top(self):
        assert chart.y_of(0, 100, H) == H - chart.PAD_BOTTOM
        assert chart.y_of(100, 100, H) == chart.PAD_TOP
        assert chart.y_of(50, 100, H) == pytest.approx((chart.PAD_TOP + H - chart.PAD_BOTTOM) / 2)

    def test_y_is_clamped_to_the_plot(self):
        assert chart.y_of(150, 100, H) == chart.PAD_TOP
        assert chart.y_of(-5, 100, H) == H - chart.PAD_BOTTOM

    def test_y_scales_to_the_given_maximum(self):
        assert chart.y_of(5.5, 11.0, H) == chart.y_of(50, 100, H)

    def test_a_polyline_in_pixels(self):
        pts = chart.to_pixels([(START, 100.0), (END, 0.0)], START, END, 100, W, H)
        assert pts == [(0, chart.PAD_TOP), (W, H - chart.PAD_BOTTOM)]


class TestHoverSamples:
    def build(self):
        recorded = [(NOW - h(24), 90.0), (NOW - h(12), 80.0), (NOW, 70.0)]
        projected = fc.project(NOW, 70.0, 1.0, NOW + h(50), 168, END)
        return recorded, projected, chart.hover_samples(recorded, projected, NOW, START, END,
                                                        100, W, H)

    def test_nothing_before_the_recording_started(self):
        _, _, samples = self.build()
        first_x = chart.x_of(NOW - h(24), START, END, W)
        assert samples[0].x >= first_x - 2
        assert all(s.x >= first_x - 2 for s in samples)

    def test_each_sample_pairs_its_x_time_value_and_flag(self):
        recorded, projected, samples = self.build()
        for s in samples:
            assert s.x == pytest.approx(chart.x_of(s.t, START, END, W), abs=1e-6)
            series = projected if s.t > NOW else recorded
            assert s.value == pytest.approx(fc.value_at(series, s.t))
            assert s.projected is (s.t > NOW)
            assert s.y == pytest.approx(chart.y_of(s.value, 100, H))

    def test_both_sides_of_now_are_covered(self):
        _, _, samples = self.build()
        assert any(not s.projected for s in samples) and any(s.projected for s in samples)

    def test_the_reset_jump_is_followed_by_the_full_value(self):
        _, projected, samples = self.build()
        after = [s for s in samples if s.t > NOW + h(50)]
        assert after and after[0].value > 90          # back near full just after the reset

    def test_no_data_no_samples(self):
        assert chart.hover_samples([], [], NOW, START, END, 100, W, H) == []

    def test_a_hover_at_a_pixel_finds_a_sample_at_that_time(self):
        _, _, samples = self.build()
        x = chart.x_of(NOW + h(20), START, END, W)
        hit = chart.nearest_point(samples, x)
        assert abs((hit.t - (NOW + h(20))).total_seconds()) < 3600 * 1.0 + 1
        assert hit.projected is True


class TestNiceTicks:
    @pytest.mark.parametrize("y_max,ticks", [
        (16.5, [0, 5, 10, 15]),
        (13.64, [0, 5, 10]),
        (1.1, [0, 0.5, 1.0]),
        (100, [0, 50, 100]),
        (4.4, [0, 2, 4]),
        (0.22, [0, 0.1, 0.2]),
        (480.0, [0, 200, 400]),
    ])
    def test_round_values_from_zero_up(self, y_max, ticks):
        assert chart.nice_ticks(y_max) == pytest.approx(ticks)

    def test_nothing_above_the_axis(self):
        for y_max in (0.37, 1.1, 2.0, 7.7, 13.64, 16.5, 99.0, 480.0):
            ticks = chart.nice_ticks(y_max)
            assert ticks[0] == 0 and max(ticks) <= y_max + 1e-9

    def test_few_lines_so_labels_never_touch(self):
        for y_max in (0.37, 1.1, 2.0, 7.7, 13.64, 16.5, 99.0, 480.0, 12345.0):
            assert 2 <= len(chart.nice_ticks(y_max)) <= 5

    def test_evenly_spaced(self):
        ticks = chart.nice_ticks(16.5)
        assert {round(b - a, 9) for a, b in zip(ticks, ticks[1:])} == {5}

    def test_an_empty_axis_is_just_zero(self):
        assert chart.nice_ticks(0) == [0.0]
        assert chart.nice_ticks(-3) == [0.0]

@pytest.fixture(scope="module")
def canvas():
    import tkinter as tk
    try:
        root = tk.Tk()
    except tk.TclError as e:
        pytest.skip(f"no display for Tk: {e}")
    root.withdraw()
    yield chart.ForecastChart(root, width=W, height=H, font=("Segoe UI", 7))
    root.destroy()


class TestAxisLabels:
    """The canvas text: a day label that would run past the right edge is left out."""

    def labels(self, canvas, ticks):
        canvas.set_data(chart.ChartData(recorded=[(START, 90.0), (NOW, 80.0)], projected=[(NOW, 80.0)],
                                        now=NOW, start=START, end=END, y_max=100.0, day_ticks=ticks))
        return [canvas.itemcget(i, "text") for i in canvas.find_all() if canvas.type(i) == "text"]

    def test_a_label_inside_the_plot_is_drawn(self, canvas):
        assert "Mid" in self.labels(canvas, [(NOW, "Mid")])

    def test_a_label_cut_by_the_right_edge_is_left_out(self, canvas):
        assert "Nov 2" not in self.labels(canvas, [(END - h(2), "Nov 2")])

    def test_now_stays_inside_the_plot_at_the_right_edge(self, canvas):
        late = END - h(0.5)
        canvas.set_data(chart.ChartData(recorded=[(START, 90.0), (late, 80.0)], projected=[(late, 80.0)],
                                        now=late, start=START, end=END, y_max=100.0))
        now = next(i for i in canvas.find_all()
                   if canvas.type(i) == "text" and canvas.itemcget(i, "text") == "Now")
        assert canvas.bbox(now)[2] <= W

    def test_the_estimated_caption_is_not_cut_by_the_top_edge(self, canvas):
        canvas.set_data(chart.ChartData(recorded=[(NOW, 90.0)], projected=[(NOW, 90.0)], now=NOW,
                                        start=START, end=END, y_max=100.0,
                                        estimated=[(START, 100.0), (NOW, 90.0)]))
        caption = next(i for i in canvas.find_all()
                       if canvas.type(i) == "text" and canvas.itemcget(i, "text") == "estimated")
        assert canvas.bbox(caption)[1] >= 0
