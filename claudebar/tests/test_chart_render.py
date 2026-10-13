"""The chart's graphics layer: what render_plot draws, checked on pixels.

render_plot is pure (no Tk, no display): it takes the chart's data and returns
the plot as an image, which the canvas lays under its own text. Each test reads
the pixels where the thing it names has to be.
"""

from datetime import datetime, timedelta, timezone

import pytest

from forecast_chart import ChartData, render_plot, reset_pill_rect, x_of, y_of

UTC = timezone.utc
NOW = datetime(2026, 10, 12, 22, 6, tzinfo=UTC)
START, END = NOW - timedelta(hours=72), NOW + timedelta(hours=96)
W, H = 348, 110
ACCENT, BG, GRID, MUTED = "#F59E0B", "#1c1c1e", "#2c2c2e", "#7f8899"
ACCENT_RGB, BG_RGB = (245, 158, 11), (28, 28, 30)
TICKS = [(0, "0%"), (50, "50%"), (100, "100%")]


def h(hours):
    return timedelta(hours=hours)


def data(recorded=(), projected=(), estimated=(), reset=None, label="", y_max=100.0):
    return ChartData(recorded=list(recorded), projected=list(projected), now=NOW, start=START, end=END,
                     y_max=y_max, y_ticks=TICKS, estimated=list(estimated), reset=reset, reset_label=label)


def draw(d, pill=None):
    return render_plot(d, W, H, ACCENT, BG, GRID, MUTED, pill=pill)


def away(pixel, colour=BG_RGB):
    """How far a pixel is from a colour (sum of channel differences)."""
    return sum(abs(a - b) for a, b in zip(pixel, colour))


def strongest(img, x, y0, y1):
    """The pixel in column x, rows y0..y1, farthest from the background."""
    return max((img.getpixel((x, y)) for y in range(y0, y1 + 1)), key=away)


def row_y(value):
    return round(y_of(value, 100.0, H))


FLAT = [(START, 50.0), (NOW, 50.0)]
AHEAD = [(NOW, 50.0), (END, 50.0)]


class TestTheImage:
    def test_it_is_the_requested_size_and_opaque(self):
        img = draw(data(FLAT))
        assert img.size == (W, H) and img.mode == "RGB"

    def test_an_empty_corner_is_the_background(self):
        assert draw(data(FLAT)).getpixel((W - 4, H - 4)) == BG_RGB

    def test_gridlines_are_drawn_where_the_ticks_say(self):
        img = draw(data())
        for value in (0, 50, 100):
            assert away(strongest(img, 300, row_y(value) - 1, row_y(value) + 1)) > 15

    def test_no_data_draws_the_default_grid_and_does_not_raise(self):
        img = render_plot(None, W, H, ACCENT, BG, GRID, MUTED)
        assert away(strongest(img, 300, row_y(50) - 1, row_y(50) + 1)) > 15

    def test_a_single_point_does_not_raise(self):
        assert draw(data([(NOW, 50.0)])).size == (W, H)

    def test_nothing_recorded_and_nothing_projected_does_not_raise(self):
        assert draw(data()).size == (W, H)


class TestTheRecordedLine:
    def test_it_is_the_accent(self):
        px = strongest(draw(data(FLAT)), 75, row_y(50) - 3, row_y(50) + 3)
        assert away(px, ACCENT_RGB) < 40

    def test_the_fill_fades_downward(self):
        """Sampled well clear of the line, where its glow no longer reaches."""
        img = draw(data([(START, 80.0), (NOW, 80.0)]))
        under_the_line = away(img.getpixel((75, row_y(80) + 14)))
        near_the_base = away(img.getpixel((75, row_y(0) - 16)))
        assert under_the_line > 40
        assert under_the_line > near_the_base + 20

    def test_the_line_has_a_soft_glow_that_does_not_reach_far(self):
        img = draw(data([(START, 30.0), (NOW, 30.0)]))             # nothing is filled above it
        assert away(img.getpixel((75, row_y(30) - 3))) > 30
        assert away(img.getpixel((75, row_y(30) - 12))) < 8

    def test_nothing_is_filled_above_the_line(self):
        img = draw(data([(START, 30.0), (NOW, 30.0)]))
        assert away(img.getpixel((75, row_y(30) - 12))) < 8

    def test_the_estimated_stretch_is_softer_than_the_recorded_one(self):
        d = data(recorded=[(NOW - h(24), 50.0), (NOW, 50.0)], estimated=[(START, 50.0), (NOW - h(24), 50.0)])
        img = draw(d)
        estimated = away(strongest(img, 30, row_y(50) - 3, row_y(50) + 3))
        recorded = away(strongest(img, 130, row_y(50) - 3, row_y(50) + 3))
        assert 40 < estimated < recorded - 30

    def test_the_estimated_stretch_is_filled_more_lightly_than_the_recorded_one(self):
        row = row_y(80) + 14
        estimated = away(draw(data(estimated=[(START, 80.0), (NOW, 80.0)])).getpixel((75, row)))
        recorded = away(draw(data(recorded=[(START, 80.0), (NOW, 80.0)])).getpixel((75, row)))
        assert 25 < estimated < recorded - 15


class TestTheProjection:
    def test_it_is_dashed(self):
        img = draw(data(FLAT, AHEAD))
        on = [away(strongest(img, x, row_y(50) - 1, row_y(50) + 1)) > 70 for x in range(160, 340)]
        changes = sum(1 for a, b in zip(on, on[1:]) if a != b)
        assert changes >= 8 and any(on) and not all(on)

    def test_it_fades_toward_the_far_end(self):
        img = draw(data(FLAT, AHEAD))
        near = max(away(strongest(img, x, row_y(50) - 1, row_y(50) + 1)) for x in range(156, 216))
        far = max(away(strongest(img, x, row_y(50) - 1, row_y(50) + 1)) for x in range(290, 345))
        assert near > far + 20

    def test_a_projection_off_the_end_of_the_view_does_not_raise(self):
        assert draw(data(FLAT, [(NOW, 50.0), (END + h(500), 0.0)])).size == (W, H)


class TestNowAndTheReset:
    def test_now_has_a_dot_on_the_line(self):
        x = round(x_of(NOW, START, END, W))
        px = strongest(draw(data(FLAT)), x, row_y(50) - 2, row_y(50) + 2)
        assert away(px, ACCENT_RGB) < 60

    def test_now_has_a_dashed_rule_above_the_dot(self):
        img = draw(data(FLAT))
        x = round(x_of(NOW, START, END, W))
        column = [away(img.getpixel((x, y))) for y in range(18, row_y(50) - 8)]
        assert max(column) > 30 and min(column) < 15              # dashes and gaps

    def test_the_reset_has_a_hairline(self):
        reset = NOW + h(69)
        img = draw(data(FLAT, AHEAD, reset=reset, label="+80%"))
        x = round(x_of(reset, START, END, W))
        assert away(strongest(img, x, row_y(30), row_y(30) + 2)) > 20

    def test_a_pill_is_filled_with_the_accent(self):
        img = draw(data(FLAT), pill=(100, 20, 140, 34))
        assert away(img.getpixel((120, 27)), ACCENT_RGB) < 20


class TestThePillRectangle:
    def rect(self, reset, label="+80%", text=(24, 11)):
        return reset_pill_rect(data(FLAT, reset=reset, label=label), W, H, *text)

    def test_it_sits_just_left_of_the_reset_line_below_the_top_gridline(self):
        """Left, where the plot is empty: the projection restarts from the right of the line."""
        reset = NOW + h(69)
        x0, y0, x1, y1 = self.rect(reset)
        rx = x_of(reset, START, END, W)
        assert rx - 8 < x1 < rx
        assert y0 > y_of(100, 100.0, H)
        assert x1 - x0 > 24 and y1 - y0 > 11                     # padded around the text

    def test_it_flips_to_the_right_when_there_is_no_room_on_the_left(self):
        reset = NOW - h(70)                                       # a hair inside the view's left edge
        x0, y0, x1, y1 = self.rect(reset)
        assert x0 >= x_of(reset, START, END, W)

    def test_a_reset_outside_the_view_has_no_pill(self):
        assert self.rect(NOW + h(120)) is None
        assert self.rect(NOW - h(100)) is None

    def test_no_reset_or_no_label_has_no_pill(self):
        assert reset_pill_rect(data(FLAT), W, H, 24, 11) is None
        assert self.rect(NOW + h(69), label="") is None

    def test_it_drops_below_a_projection_it_would_cover(self):
        """Quota still high before the reset runs under the top gridline, so the pill moves under the line."""
        reset = NOW + h(69)
        high = [(NOW, 92.0), (reset, 88.0), (reset, 100.0), (END, 95.0)]
        x0, y0, x1, y1 = reset_pill_rect(data(FLAT, high, reset=reset, label="+12%"), W, H, 24, 11)
        assert y0 > y_of(88.0, 100.0, H)
        assert y1 < y_of(0, 100.0, H)
