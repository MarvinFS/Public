"""The panel's scale factor: the pure helpers that turn design pixels into screen pixels."""

import pytest

from ui_scale import (DROP_ORDER, MAX_SCALE, MIN_SCALE, PANEL_MAX_SCALE, PANEL_MIN_SCALE, Scale, auto_scale,
                      blocks_to_hide, scale_for_width, width_for_scale, zone_at)


class TestPx:
    @pytest.mark.parametrize("k", [0.1, 0.7, 1.0, 1.5, 2.5, 4.0])
    def test_zero_stays_zero(self, k):
        """pady=(0, 15) must not grow a pixel above the row."""
        assert Scale(k).px(0) == 0

    @pytest.mark.parametrize("pixels", [1, 2, 5, 16, 348, 380])
    def test_whole_pixels_are_unchanged_at_one(self, pixels):
        assert Scale(1.0).px(pixels) == pixels

    @pytest.mark.parametrize("k", [0.1, 0.3, 0.7])
    def test_a_hairline_never_vanishes(self, k):
        assert Scale(k).px(1) == 1
        assert Scale(k).px(2) >= 1

    def test_the_sign_is_kept(self):
        scale = Scale(2.0)
        assert scale.px(-3) == -6
        assert Scale(0.1).px(-1) == -1          # and a negative one is not rounded to zero either

    @pytest.mark.parametrize("pixels, k, expected", [
        (8, 1.5, 12), (10, 0.7, 7), (348, 2.0, 696), (380, 0.7, 266), (1, 1.5, 2),   # half rounds up
        (1, 2.4, 2), (3, 0.5, 2), (4.5, 1.0, 5),
    ])
    def test_it_rounds_to_the_nearest_pixel(self, pixels, k, expected):
        assert Scale(k).px(pixels) == expected

    def test_the_result_is_an_int_for_tk(self):
        assert isinstance(Scale(1.37).px(7), int)


class TestSpacing:
    def test_a_number_is_scaled(self):
        assert Scale(2.0).spacing(6) == 12

    def test_a_pair_is_scaled_part_by_part_as_tk_takes_it(self):
        assert Scale(2.0).spacing((0, 15)) == (0, 30)
        assert Scale(0.7).spacing((10, 6)) == (7, 4)

    def test_a_list_becomes_a_tuple(self):
        assert Scale(1.5).spacing([4, 8]) == (6, 12)

    def test_zero_in_a_pair_stays_zero(self):
        assert Scale(1.5).spacing((0, 0)) == (0, 0)


class TestFontPx:
    def test_it_is_negative_because_tk_reads_that_as_pixels(self):
        assert Scale(1.0).font_px(9) < 0

    def test_nine_points_at_one_is_what_tk_makes_of_nine_points_at_96_dpi(self):
        assert Scale(1.0).font_px(9) == -12
        assert Scale(1.0).font_px(10) == -13          # 13.3 rounded, as Tk does

    @pytest.mark.parametrize("k", [0.7, 1.5, 2.0, 2.5])
    def test_it_follows_the_factor(self, k):
        assert abs(Scale(k).font_px(12)) == round(12 * 96 / 72 * k)

    def test_it_is_never_zero(self):
        assert Scale(0.1).font_px(1) == -1


class TestAutoScale:
    @pytest.mark.parametrize("height, expected", [(1440, 1.0), (1080, 0.75), (2160, 1.5), (1800, 1.25)])
    def test_it_is_the_screen_height_over_the_reference(self, height, expected):
        assert auto_scale(height) == pytest.approx(expected)

    def test_a_desktop_that_scales_itself_is_not_scaled_twice(self):
        """4K at 200% is 1080 logical: 0.75 here, and the desktop's own 2x on top makes 1.5."""
        assert auto_scale(2160, 2.0) == pytest.approx(0.75)
        assert auto_scale(2160, 2.0) * 2.0 == pytest.approx(auto_scale(2160, 1.0))

    @pytest.mark.parametrize("height, dpi", [(300, 1.0), (600, 1.0), (1440, 3.0)])
    def test_it_does_not_go_below_the_floor(self, height, dpi):
        assert auto_scale(height, dpi) == MIN_SCALE

    @pytest.mark.parametrize("height", [4320, 5000, 100_000])
    def test_it_does_not_go_above_the_ceiling(self, height):
        assert auto_scale(height) == MAX_SCALE

    @pytest.mark.parametrize("height", [0, -1])
    def test_a_screen_with_no_height_is_one(self, height):
        assert auto_scale(height) == 1.0

    def test_a_nonsense_dpi_scale_does_not_divide_by_zero(self):
        assert MIN_SCALE <= auto_scale(1440, 0.0) <= MAX_SCALE
        assert MIN_SCALE <= auto_scale(1440, -2.0) <= MAX_SCALE


class TestWidthAndScale:
    BASE = 380

    def test_the_base_width_is_one(self):
        assert scale_for_width(self.BASE, self.BASE) == 1.0
        assert width_for_scale(1.0, self.BASE) == self.BASE

    @pytest.mark.parametrize("k", [0.7, 0.85, 1.0, 1.3, 2.0, 2.5])
    def test_they_are_each_others_inverse(self, k):
        assert scale_for_width(width_for_scale(k, self.BASE), self.BASE) == pytest.approx(k, abs=0.002)

    @pytest.mark.parametrize("width", [50, 200, 265])
    def test_a_narrow_panel_stops_at_the_floor(self, width):
        assert scale_for_width(width, self.BASE) == PANEL_MIN_SCALE

    @pytest.mark.parametrize("width", [960, 2000, 10_000])
    def test_a_wide_panel_stops_at_the_ceiling(self, width):
        assert scale_for_width(width, self.BASE) == PANEL_MAX_SCALE

    def test_a_factor_outside_the_range_gives_the_width_of_the_nearest_end(self):
        assert width_for_scale(0.1, self.BASE) == round(PANEL_MIN_SCALE * self.BASE)
        assert width_for_scale(9.0, self.BASE) == round(PANEL_MAX_SCALE * self.BASE)

    def test_the_width_is_whole_pixels(self):
        assert isinstance(width_for_scale(1.234, self.BASE), int)


class TestZoneAt:
    W, H, M = 400, 600, 8

    def zone(self, x, y):
        return zone_at(x, y, self.W, self.H, self.M)

    @pytest.mark.parametrize("x, y, expected", [
        (200, 3, "n"), (200, 596, "s"), (3, 300, "w"), (396, 300, "e"),
        (3, 3, "nw"), (396, 3, "ne"), (3, 596, "sw"), (396, 596, "se"),
        (200, 300, None), (50, 50, None), (350, 550, None),
    ])
    def test_each_edge_and_corner_and_the_inside(self, x, y, expected):
        assert self.zone(x, y) == expected

    def test_the_margin_is_exactly_margin_pixels_wide_on_every_side(self):
        assert [self.zone(x, 300) for x in (0, 7, 8)] == ["w", "w", None]
        assert [self.zone(x, 300) for x in (391, 392, 399)] == [None, "e", "e"]
        assert [self.zone(200, y) for y in (0, 7, 8)] == ["n", "n", None]
        assert [self.zone(200, y) for y in (591, 592, 599)] == [None, "s", "s"]

    def test_a_corner_is_where_two_margins_overlap_and_no_more(self):
        assert self.zone(7, 7) == "nw" and self.zone(8, 7) == "n" and self.zone(7, 8) == "w"

    @pytest.mark.parametrize("x, y", [(-1, 300), (400, 300), (200, -1), (200, 600), (-5, -5), (900, 900)])
    def test_outside_the_window_is_no_zone(self, x, y):
        assert self.zone(x, y) is None

    def test_the_margin_is_what_the_caller_says(self):
        assert zone_at(10, 300, 400, 600, 12) == "w" and zone_at(10, 300, 400, 600, 8) is None
        assert zone_at(0, 300, 400, 600, 0) is None                     # no margin, no zone

    def test_a_window_narrower_than_two_margins_still_answers(self):
        assert zone_at(2, 50, 10, 100, 8) == "w" and zone_at(8, 50, 10, 100, 8) == "e"


class TestBlocksToHide:
    ORDER = ("a", "b", "c", "d")

    def hide(self, available, **heights):
        return blocks_to_hide({"base": 100, **heights}, available, self.ORDER)

    def test_everything_fits_hides_nothing(self):
        assert self.hide(500, a=50, b=50, c=50, d=50) == set()

    def test_an_exact_fit_hides_nothing(self):
        assert self.hide(300, a=50, b=50, c=50, d=50) == set()

    def test_one_pixel_short_hides_the_first_block_and_only_that(self):
        assert self.hide(299, a=50, b=50, c=50, d=50) == {"a"}

    def test_blocks_go_in_order_until_what_is_left_fits(self):
        assert self.hide(299, a=50, b=50, c=50, d=50) == {"a"}
        assert self.hide(250, a=50, b=50, c=50, d=50) == {"a"}
        assert self.hide(249, a=50, b=50, c=50, d=50) == {"a", "b"}
        assert self.hide(200, a=50, b=50, c=50, d=50) == {"a", "b"}
        assert self.hide(199, a=50, b=50, c=50, d=50) == {"a", "b", "c"}

    def test_what_is_hidden_is_always_a_prefix_of_the_order(self):
        for available in range(0, 400, 7):
            hidden = self.hide(available, a=30, b=0, c=45, d=60)
            assert hidden == set(self.ORDER[:len(hidden)])

    def test_nothing_fits_hides_every_block_and_the_rest_stays(self):
        assert self.hide(10, a=50, b=50, c=50, d=50) == set(self.ORDER)
        assert self.hide(-5, a=50, b=50, c=50, d=50) == set(self.ORDER)

    def test_the_kept_blocks_are_never_hidden(self):
        assert "base" not in self.hide(0, a=50, b=50, c=50, d=50)

    def test_a_block_that_frees_no_height_goes_with_the_next_one_that_does(self):
        assert self.hide(250, a=0, b=50, c=50, d=50) == set()           # it fits: the note stays
        assert self.hide(249, a=0, b=50, c=50, d=50) == {"a", "b"}      # one pixel short: both go
        assert self.hide(200, a=0, b=50, c=50, d=50) == {"a", "b"}
        assert self.hide(199, a=0, b=50, c=50, d=50) == {"a", "b", "c"}

    def test_a_block_with_no_height_in_the_middle_goes_when_the_ones_before_it_are_not_enough(self):
        assert self.hide(180, a=50, b=0, c=50, d=50) == {"a", "b", "c"}
        assert self.hide(200, a=50, b=0, c=50, d=50) == {"a"}           # 250 -> 200 fits before b is reached

    def test_a_name_without_a_height_counts_as_nothing(self):
        assert blocks_to_hide({"base": 100, "a": 50}, 120, ("a", "z")) == {"a"}

    def test_the_default_order_is_the_documented_one(self):
        assert DROP_ORDER == ("source", "top_model", "pace", "chart", "stats", "half", "status")
        heights = {"base": 200, "source": 0, "top_model": 20, "pace": 30, "chart": 130, "stats": 80,
                   "half": 190, "status": 30}
        assert blocks_to_hide(heights, 10_000) == set() == blocks_to_hide(heights, 680)
        assert blocks_to_hide(heights, 679) == {"source", "top_model"}
        assert blocks_to_hide(heights, 0) == set(DROP_ORDER)

    def test_the_heights_are_not_changed(self):
        heights = {"base": 100, "a": 50}
        blocks_to_hide(heights, 0, ("a",))
        assert heights == {"base": 100, "a": 50}
