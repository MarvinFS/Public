"""The quota forecast: drain rate, projection, resets, run-out and the chart's
time axis. Every function is pure, so each case is a plain example."""

from datetime import datetime, timedelta, timezone

import pytest

import forecast as fc

UTC = timezone.utc
NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)


def h(hours):
    return timedelta(hours=hours)


class TestAsUtc:
    def test_an_aware_time_is_converted(self):
        plus3 = timezone(h(3))
        assert fc.as_utc(datetime(2026, 10, 12, 15, 0, tzinfo=plus3)) == NOW
        assert fc.as_utc(datetime(2026, 10, 12, 15, 0, tzinfo=plus3)).tzinfo == UTC

    def test_a_naive_time_is_read_as_local(self):
        """Codex's reset time is a naive local datetime."""
        naive = datetime(2026, 10, 12, 12, 0)
        out = fc.as_utc(naive)
        assert out.tzinfo == UTC
        assert out.astimezone().replace(tzinfo=None) == naive

    def test_none_stays_none(self):
        assert fc.as_utc(None) is None


class TestWindowRate:
    def test_used_over_elapsed_hours(self):
        # 30% used, 126 h left of 168 -> 42 h elapsed
        assert fc.window_rate(30, NOW + h(126), 168, NOW) == pytest.approx(30 / 42)

    def test_a_30_day_window_uses_its_own_length(self):
        # 4% used, 23 days left of 30 -> 7 days (168 h) elapsed
        assert fc.window_rate(4, NOW + h(23 * 24), 720, NOW) == pytest.approx(4 / 168)

    def test_the_same_data_under_a_168_hour_assumption_would_differ(self):
        assert fc.window_rate(4, NOW + h(23 * 24), 168, NOW) == 0.0   # reset beyond the window

    def test_unknown_reset_is_zero(self):
        assert fc.window_rate(30, None, 168, NOW) == 0.0

    def test_a_window_that_has_just_begun_is_zero(self):
        assert fc.window_rate(0, NOW + h(168), 168, NOW) == 0.0

    def test_a_reset_in_the_past_counts_the_whole_window(self):
        assert fc.window_rate(30, NOW - h(1), 168, NOW) == pytest.approx(30 / 168)


class TestDrainRate:
    def hourly(self, values, start=-10):
        return [(NOW + h(start + i), v) for i, v in enumerate(values)]

    def test_drops_over_the_span(self):
        samples = self.hourly([100 - 2 * i for i in range(11)])      # 100 -> 80 in 10 h
        assert fc.drain_rate(samples, NOW, fallback=9.9) == pytest.approx(2.0)

    def test_a_reset_rise_is_ignored(self):
        # 50 -> 30 (4 drops of 5), reset to 100, then 100 -> 75 (5 drops of 5): 45 over 10 h
        values = [50, 45, 40, 35, 30, 100, 95, 90, 85, 80, 75]
        assert fc.drain_rate(self.hourly(values), NOW, fallback=9.9) == pytest.approx(4.5)

    def test_samples_older_than_72_hours_are_ignored(self):
        old = [(NOW - h(100), 100.0), (NOW - h(90), 40.0)]            # a steep drop, long ago
        recent = [(NOW - h(60), 90.0), (NOW, 30.0)]                   # 60 over 60 h
        assert fc.drain_rate(old + recent, NOW, fallback=9.9) == pytest.approx(1.0)

    def test_samples_after_now_are_ignored(self):
        samples = self.hourly([100 - 2 * i for i in range(11)]) + [(NOW + h(5), 0.0)]
        assert fc.drain_rate(samples, NOW, fallback=9.9) == pytest.approx(2.0)

    def test_under_six_hours_of_history_uses_the_fallback(self):
        samples = [(NOW - h(5), 90.0), (NOW, 50.0)]                   # 8 %/h measured
        assert fc.drain_rate(samples, NOW, fallback=1.5) == 1.5

    def test_exactly_six_hours_is_enough(self):
        samples = [(NOW - h(6), 90.0), (NOW, 60.0)]
        assert fc.drain_rate(samples, NOW, fallback=1.5) == pytest.approx(5.0)

    def test_one_sample_or_none_uses_the_fallback(self):
        assert fc.drain_rate([], NOW, fallback=2.5) == 2.5
        assert fc.drain_rate([(NOW, 50.0)], NOW, fallback=2.5) == 2.5

    def test_no_drop_over_a_long_span_is_a_real_zero_not_the_fallback(self):
        samples = [(NOW - h(24), 50.0), (NOW - h(12), 50.0), (NOW, 50.0)]
        assert fc.drain_rate(samples, NOW, fallback=2.5) == 0.0

    def test_the_span_is_the_samples_not_the_lookback(self):
        """10 h of samples with a 20 point drop is 2 %/h, not 20/72."""
        samples = [(NOW - h(10), 70.0), (NOW, 50.0)]
        assert fc.drain_rate(samples, NOW, fallback=9.9) == pytest.approx(2.0)


class TestProjectedRemaining:
    def test_drains_and_floors_at_zero(self):
        assert fc.projected_remaining(NOW, 80, 2, NOW + h(10)) == 60
        assert fc.projected_remaining(NOW, 10, 2, NOW + h(10)) == 0

    def test_a_time_before_now_is_the_current_value(self):
        assert fc.projected_remaining(NOW, 80, 2, NOW - h(10)) == 80


class TestNextReset:
    def test_the_given_reset_when_it_lies_ahead(self):
        assert fc.next_reset(NOW, NOW + h(5), 168) == NOW + h(5)

    def test_a_stale_reset_is_rolled_forward_by_whole_windows(self):
        assert fc.next_reset(NOW, NOW - h(10), 168) == NOW + h(158)
        assert fc.next_reset(NOW, NOW - h(400), 168) == NOW + h(104)     # 3 windows on

    def test_a_30_day_window_rolls_by_30_days(self):
        assert fc.next_reset(NOW, NOW - h(10), 720) == NOW + h(710)

    def test_unknown_stays_unknown(self):
        assert fc.next_reset(NOW, None, 168) is None

    def test_a_reset_exactly_now_is_in_the_past(self):
        assert fc.next_reset(NOW, NOW, 168) == NOW + h(168)


class TestProject:
    def values(self, points):
        return [(round((t - NOW).total_seconds() / 3600, 6), round(v, 6)) for t, v in points]

    def test_drains_linearly_to_the_horizon(self):
        pts = fc.project(NOW, 80, 2, None, 168, NOW + h(10))
        assert self.values(pts) == [(0, 80), (10, 60)]

    def test_floors_at_zero_and_stays_there(self):
        pts = fc.project(NOW, 10, 2, None, 168, NOW + h(10))
        assert self.values(pts) == [(0, 10), (5, 0), (10, 0)]

    def test_a_reset_jumps_to_full_then_keeps_draining(self):
        pts = fc.project(NOW, 40, 4, NOW + h(5), 168, NOW + h(10))
        assert self.values(pts) == [(0, 40), (5, 20), (5, 100), (10, 80)]

    def test_it_can_run_dry_before_the_reset(self):
        pts = fc.project(NOW, 10, 5, NOW + h(6), 168, NOW + h(10))
        assert self.values(pts) == [(0, 10), (2, 0), (6, 0), (6, 100), (10, 80)]

    def test_the_next_reset_is_one_window_later(self):
        pts = fc.project(NOW, 100, 1, NOW + h(5), 24, NOW + h(40))
        jumps = [(round((t - NOW).total_seconds() / 3600), v) for (t, v), (t2, v2)
                 in zip(pts, pts[1:]) if t == t2 for v in [v2]]
        assert jumps == [(5, 100), (29, 100)]

    def test_a_reset_exactly_on_the_horizon_draws_no_jump(self):
        pts = fc.project(NOW, 60, 2, NOW + h(10), 168, NOW + h(10))
        assert self.values(pts) == [(0, 60), (10, 40)]

    def test_rate_zero_is_flat_until_the_reset(self):
        pts = fc.project(NOW, 60, 0, NOW + h(5), 168, NOW + h(10))
        assert self.values(pts) == [(0, 60), (5, 60), (5, 100), (10, 100)]

    def test_a_missing_reset_never_jumps(self):
        pts = fc.project(NOW, 60, 1, None, 168, NOW + h(200))
        assert self.values(pts) == [(0, 60), (60, 0), (200, 0)]

    def test_a_stale_reset_is_rolled_forward(self):
        pts = fc.project(NOW, 50, 1, NOW - h(10), 168, NOW + h(96))
        assert self.values(pts) == [(0, 50), (50, 0), (96, 0)]            # next reset at +158 h

    def test_a_720_hour_window_resets_at_its_own_length(self):
        # 23 days to the reset, 30-day window: the reset is in view, the one after is not
        end = NOW + h(26 * 24)
        pts = fc.project(NOW, 96, 0.01, NOW + h(23 * 24), 720, end)
        assert self.values(pts)[:3] == [(0, 96), (552, pytest.approx(90.48)), (552, 100)]
        assert max(t for t, _ in self.values(pts)) == 26 * 24

    def test_a_balance_is_projected_with_the_same_function_and_no_reset(self):
        pts = fc.project(NOW, 12.40, 0.5, None, 168, NOW + h(10))
        assert self.values(pts) == [(0, 12.4), (10, 7.4)]

    def test_an_end_before_now_is_just_the_start(self):
        assert fc.project(NOW, 33, 1, None, 168, NOW - h(1)) == [(NOW, 33)]

    def test_the_start_is_always_now_at_the_current_value(self):
        pts = fc.project(NOW, 33, 1, NOW + h(3), 168, NOW + h(10))
        assert pts[0] == (NOW, 33)


class TestRunout:
    def test_before_the_reset(self):
        assert fc.runout_time(NOW, 10, 5, NOW + h(6), 168) == NOW + h(2)

    def test_after_the_reset_it_lasts(self):
        assert fc.runout_time(NOW, 60, 5, NOW + h(6), 168) is None

    def test_running_dry_exactly_at_the_reset_lasts(self):
        assert fc.runout_time(NOW, 10, 5, NOW + h(2), 168) is None

    def test_no_reset_known(self):
        assert fc.runout_time(NOW, 10, 5, None, 168) == NOW + h(2)

    def test_no_drain_never_runs_out(self):
        assert fc.runout_time(NOW, 10, 0, NOW + h(6), 168) is None
        assert fc.runout_time(NOW, 10, 0, None, 168) is None

    def test_already_empty_runs_out_now(self):
        assert fc.runout_time(NOW, 0, 5, NOW + h(6), 168) == NOW


class TestNextResetGain:
    def test_what_the_reset_adds_back(self):
        # 60% left, 1%/h, reset in 20 h -> 40% left at the reset, so +60
        assert fc.next_reset_gain(NOW, 60, 1, NOW + h(20), 168) == pytest.approx(60)

    def test_an_empty_quota_gains_a_full_window(self):
        assert fc.next_reset_gain(NOW, 5, 1, NOW + h(20), 168) == 100

    def test_no_reset_known_is_none(self):
        assert fc.next_reset_gain(NOW, 60, 1, None, 168) is None


class TestValueAt:
    POINTS = [(NOW, 100.0), (NOW + h(10), 80.0), (NOW + h(10), 100.0), (NOW + h(20), 90.0)]

    def test_interpolates_between_points(self):
        assert fc.value_at(self.POINTS, NOW + h(5)) == pytest.approx(90.0)
        assert fc.value_at(self.POINTS, NOW + h(15)) == pytest.approx(95.0)

    def test_a_vertical_jump_takes_the_later_value(self):
        assert fc.value_at(self.POINTS, NOW + h(10)) == 100.0

    def test_outside_the_series_is_none(self):
        assert fc.value_at(self.POINTS, NOW - h(1)) is None
        assert fc.value_at(self.POINTS, NOW + h(21)) is None
        assert fc.value_at([], NOW) is None

    def test_the_end_points_are_inside(self):
        assert fc.value_at(self.POINTS, NOW) == 100.0
        assert fc.value_at(self.POINTS, NOW + h(20)) == 90.0

    def test_a_single_point_is_known_only_at_its_own_time(self):
        assert fc.value_at([(NOW, 42.0)], NOW) == 42.0
        assert fc.value_at([(NOW, 42.0)], NOW + h(1)) is None


class TestViewSpan:
    def test_a_weekly_window_keeps_three_days_back_and_four_ahead(self):
        assert fc.view_span(168, NOW + h(100), NOW) == (h(72), h(96))

    def test_a_week_window_ignores_a_far_reset(self):
        assert fc.view_span(168, NOW + h(160), NOW) == (h(72), h(96))

    def test_an_eight_day_window_still_counts_as_short(self):
        assert fc.view_span(192, NOW + h(150), NOW) == (h(72), h(96))

    def test_a_long_window_stretches_to_the_reset(self):
        back, ahead = fc.view_span(720, NOW + h(23 * 24), NOW)
        assert ahead == h(23 * 24)
        assert back == h(23 * 24 * 0.75)

    def test_now_stays_three_sevenths_across(self):
        for window, reset_in in ((168, 100), (720, 23 * 24), (720, 5 * 24), (1000, 30 * 24)):
            back, ahead = fc.view_span(window, NOW + h(reset_in), NOW)
            assert back / (back + ahead) == pytest.approx(3 / 7)

    def test_a_close_reset_never_squeezes_the_view_below_four_days(self):
        assert fc.view_span(720, NOW + h(10), NOW)[1] == h(96)

    def test_the_view_never_exceeds_one_window(self):
        assert fc.view_span(720, NOW + h(2000), NOW)[1] == h(720)

    def test_an_unknown_reset_on_a_long_window_uses_the_default(self):
        assert fc.view_span(720, None, NOW) == (h(72), h(96))


class TestDayTicks:
    def test_midnights_with_their_own_weekday(self):
        start = datetime(2026, 10, 9, 15, 0, tzinfo=UTC)               # a Friday
        ticks = fc.day_ticks(start, start + h(7 * 24), tz=UTC)
        assert ticks == [
            (datetime(2026, 10, 10, tzinfo=UTC), "Sat"),
            (datetime(2026, 10, 11, tzinfo=UTC), "Sun"),
            (datetime(2026, 10, 12, tzinfo=UTC), "Mon"),
            (datetime(2026, 10, 13, tzinfo=UTC), "Tue"),
            (datetime(2026, 10, 14, tzinfo=UTC), "Wed"),
            (datetime(2026, 10, 15, tzinfo=UTC), "Thu"),
            (datetime(2026, 10, 16, tzinfo=UTC), "Fri"),
        ]

    def test_a_local_midnight_is_not_a_utc_midnight(self):
        plus3 = timezone(h(3))
        start = datetime(2026, 10, 9, 15, 0, tzinfo=UTC)               # 18:00 on Fri in +03:00
        ticks = fc.day_ticks(start, start + h(48), tz=plus3)
        assert ticks == [
            (datetime(2026, 10, 9, 21, 0, tzinfo=UTC), "Sat"),         # 00:00 Sat local
            (datetime(2026, 10, 10, 21, 0, tzinfo=UTC), "Sun"),
        ]

    def test_a_midnight_at_the_start_is_included_and_at_the_end_is_not(self):
        start = datetime(2026, 10, 10, tzinfo=UTC)
        ticks = fc.day_ticks(start, start + h(48), tz=UTC)
        assert [label for _, label in ticks] == ["Sat", "Sun"]

    def test_a_long_span_uses_dates_every_few_days(self):
        start = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
        ticks = fc.day_ticks(start, start + h(26 * 24), tz=UTC)
        labels = [label for _, label in ticks]
        assert labels[0] == "Oct 2"
        gaps = {(b[0] - a[0]).days for a, b in zip(ticks, ticks[1:])}
        assert gaps == {4}                                             # 26 days / 7 -> every 4th day
        assert all(" " in label and label.split()[0] in ("Oct", "Nov") for label in labels)

    def test_each_long_label_matches_its_own_date(self):
        start = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
        for t, label in fc.day_ticks(start, start + h(26 * 24), tz=UTC):
            assert label == f"{t:%b} {t.day}"

    def test_labels_never_sit_closer_than_the_width_allows(self):
        """At most seven labels for any span, so 348 px never crowds them."""
        start = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
        for days in (7, 9, 14, 26, 40, 90):
            assert len(fc.day_ticks(start, start + h(days * 24), tz=UTC)) <= 8


class TestForecastQuota:
    """The numbers behind the quota view, composed from the functions above."""

    def samples(self, n_hours, start_remaining, per_hour):
        """One sample an hour for the last n_hours, ending now."""
        return [(NOW - h(n_hours - i), start_remaining - per_hour * i) for i in range(n_hours + 1)]

    def test_a_weekly_window_with_a_day_of_history(self):
        samples = self.samples(24, 100.0, 1.0)                      # 100 -> 76 in 24 h
        f = fc.forecast_quota(samples, used_percent=24.0, resets_at=NOW + h(100),
                              window_hours=168, now=NOW)
        assert f.remaining == 76.0
        assert f.rate == pytest.approx(1.0)
        assert f.next_reset == NOW + h(100)
        # 76 h at 1 %/h runs dry at +76 h, before the reset at +100 h
        assert f.runout == NOW + h(76)
        assert f.unused is None

    def test_a_quota_that_lasts_reports_what_is_left_at_the_reset(self):
        samples = self.samples(24, 100.0, 0.5)                      # 100 -> 88 in 24 h
        f = fc.forecast_quota(samples, used_percent=12.0, resets_at=NOW + h(40),
                              window_hours=168, now=NOW)
        assert f.runout is None
        assert f.unused == pytest.approx(88 - 0.5 * 40)
        assert f.gain == pytest.approx(100 - f.unused)

    def test_the_recorded_line_ends_at_now_with_the_current_value(self):
        f = fc.forecast_quota(self.samples(24, 100.0, 1.0), 24.0, NOW + h(100), 168, NOW)
        assert f.recorded[-1] == (NOW, 76.0)
        assert f.recorded[0][0] == NOW - h(24)
        assert all(a[0] <= b[0] for a, b in zip(f.recorded, f.recorded[1:]))

    def test_samples_outside_the_view_are_not_drawn(self):
        old = [(NOW - h(100), 99.0)] + self.samples(24, 100.0, 1.0)
        f = fc.forecast_quota(old, 24.0, NOW + h(100), 168, NOW)
        assert min(t for t, _ in f.recorded) >= f.start

    def test_the_view_for_a_168_hour_window(self):
        f = fc.forecast_quota([], 10.0, NOW + h(100), 168, NOW)
        assert (f.start, f.end) == (NOW - h(72), NOW + h(96))

    def test_a_30_day_window_reaches_its_reset(self):
        """The Codex free plan: 4% of 720 h used, 23 days to the reset."""
        reset = NOW + h(23 * 24)
        f = fc.forecast_quota([], 4.0, reset, 720, NOW)
        assert f.end == reset
        assert f.start == NOW - h(23 * 24 * 0.75)
        assert f.rate == pytest.approx(4 / 168)                     # 7 days elapsed
        assert f.runout is None
        assert f.unused == pytest.approx(96 - (4 / 168) * 23 * 24)
        assert f.projected[-1][0] == reset                          # ends at the edge, no jump drawn

    def test_with_no_history_the_rate_is_the_window_average(self):
        f = fc.forecast_quota([], 42.0, NOW + h(126), 168, NOW)
        assert f.rate == pytest.approx(42 / 42)
        assert f.since is None and f.recording is True

    def test_recording_since_and_the_three_day_rule(self):
        young = self.samples(24, 100.0, 1.0)
        old = self.samples(80, 100.0, 1.0)
        assert fc.forecast_quota(young, 24.0, NOW + h(100), 168, NOW).since == NOW - h(24)
        assert fc.forecast_quota(young, 24.0, NOW + h(100), 168, NOW).recording is True
        assert fc.forecast_quota(old, 24.0, NOW + h(100), 168, NOW).recording is False

    def test_used_percent_is_clamped(self):
        assert fc.forecast_quota([], 130.0, None, 168, NOW).remaining == 0.0
        assert fc.forecast_quota([], -5.0, None, 168, NOW).remaining == 100.0

    def test_no_reset_known(self):
        f = fc.forecast_quota(self.samples(24, 100.0, 1.0), 24.0, None, 168, NOW)
        assert f.next_reset is None and f.gain is None and f.unused is None
        assert f.runout == NOW + h(76)

    def test_the_projection_starts_where_the_recorded_line_ends(self):
        f = fc.forecast_quota(self.samples(24, 100.0, 1.0), 24.0, NOW + h(100), 168, NOW)
        assert f.projected[0] == f.recorded[-1] == (NOW, 76.0)


class TestForecastBalance:
    def test_the_spend_rate_is_the_mean_of_the_last_seven_days(self):
        costs = [9.0, 9.0, 9.0] + [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7]
        f = fc.forecast_balance([], 12.40, costs, NOW)
        assert f.rate_per_day == pytest.approx(0.4)                 # not the older 9.0s

    def test_runs_out_after_balance_over_rate_days(self):
        f = fc.forecast_balance([], 12.40, [0.4] * 31, NOW)
        assert f.runout == NOW + timedelta(days=31)
        assert f.days_left == pytest.approx(31.0)

    @pytest.mark.parametrize("balance, cost", [(1e9, 1e-9), (5e6, 1e-12), (1e300, 1e-300)])
    def test_a_balance_that_outlasts_any_date_has_no_run_out_date(self, balance, cost):
        f = fc.forecast_balance([], balance, [cost] * 7, NOW)        # must not overflow a date
        assert f.runout is None and f.days_left > fc.YEAR_DAYS

    def test_a_balance_just_inside_a_year_still_gets_its_date(self):
        f = fc.forecast_balance([], 364.0, [1.0] * 7, NOW)
        assert f.runout == NOW + timedelta(days=364)

    def test_the_projection_falls_to_zero_and_stays_inside_the_view(self):
        f = fc.forecast_balance([], 1.0, [0.5] * 7, NOW)             # empty in 2 days
        assert f.end == NOW + h(96)
        assert f.projected == [(NOW, 1.0), (NOW + h(48), 0.0), (NOW + h(96), 0.0)]

    def test_no_spend_is_flat_and_never_runs_out(self):
        f = fc.forecast_balance([], 12.40, [0.0] * 31, NOW)
        assert f.rate_per_day == 0.0 and f.runout is None and f.days_left is None
        assert f.projected == [(NOW, 12.40), (NOW + h(96), 12.40)]

    def test_no_cost_series_at_all(self):
        f = fc.forecast_balance([], 12.40, [], NOW)
        assert f.rate_per_day == 0.0 and f.runout is None

    def test_fewer_than_seven_days_use_what_there_is(self):
        assert fc.forecast_balance([], 5.0, [1.0, 3.0], NOW).rate_per_day == 2.0

    def test_y_runs_to_one_point_one_times_the_largest_balance_in_view(self):
        samples = [(NOW - h(40), 20.0), (NOW - h(20), 15.0)]
        f = fc.forecast_balance(samples, 12.40, [0.4] * 7, NOW)
        assert f.y_max == pytest.approx(22.0)

    def test_a_balance_older_than_the_view_does_not_stretch_the_axis(self):
        samples = [(NOW - h(200), 90.0), (NOW - h(10), 12.5)]
        f = fc.forecast_balance(samples, 12.40, [0.4] * 7, NOW)
        assert f.y_max == pytest.approx(12.5 * 1.1)

    def test_a_top_up_shows_as_a_rise_in_the_recorded_line(self):
        samples = [(NOW - h(30), 2.0), (NOW - h(20), 1.5), (NOW - h(10), 21.5)]
        f = fc.forecast_balance(samples, 21.0, [0.4] * 7, NOW)
        assert [v for _, v in f.recorded] == [2.0, 1.5, 21.5, 21.0]

    def test_an_empty_balance_still_has_an_axis(self):
        assert fc.forecast_balance([], 0.0, [0.4] * 7, NOW).y_max > 0

    def test_the_view_matches_the_quota_view(self):
        f = fc.forecast_balance([], 12.40, [0.4] * 7, NOW)
        assert (f.start, f.end) == (NOW - h(72), NOW + h(96))


class TestEstimatePast:
    """Before the first reading the chart draws a straight line from a full quota
    at the start of the window to that reading, so a young history is not blank."""

    RESET = NOW + h(69)                       # a 168 h window that began 99 h ago
    START = NOW - h(99)

    def test_a_single_reading_gives_the_whole_window_so_far(self):
        got = fc.estimate_past([(NOW, 53.0)], 53.0, self.RESET, 168, NOW)
        assert got == [(self.START, 100.0), (NOW, 53.0)]

    def test_no_readings_at_all_is_the_same_line_to_now(self):
        assert fc.estimate_past([], 53.0, self.RESET, 168, NOW) == [(self.START, 100.0), (NOW, 53.0)]

    def test_it_stops_at_the_first_reading(self):
        samples = [(NOW - h(10), 60.0), (NOW, 53.0)]
        got = fc.estimate_past(samples, 53.0, self.RESET, 168, NOW)
        assert got == [(self.START, 100.0), (NOW - h(10), 60.0)]

    def test_a_recording_that_covers_the_start_needs_no_estimate(self):
        samples = [(self.START + timedelta(minutes=10), 99.0), (NOW, 53.0)]
        assert fc.estimate_past(samples, 53.0, self.RESET, 168, NOW) == []

    def test_a_recording_that_began_in_an_earlier_window_needs_no_estimate(self):
        samples = [(self.START - h(30), 20.0), (NOW, 53.0)]
        assert fc.estimate_past(samples, 53.0, self.RESET, 168, NOW) == []

    def test_stale_data_has_no_estimate(self):
        """The percent belongs to a window that has ended."""
        assert fc.estimate_past([(NOW, 53.0)], 53.0, NOW - h(1), 168, NOW) == []

    def test_an_unknown_reset_has_no_estimate(self):
        assert fc.estimate_past([(NOW, 53.0)], 53.0, None, 168, NOW) == []

    def test_a_reset_further_off_than_a_window_has_no_estimate(self):
        """The window has not begun by this data's own clock: nothing to extend."""
        assert fc.estimate_past([(NOW, 53.0)], 53.0, NOW + h(200), 168, NOW) == []

    def test_a_30_day_window_uses_its_own_length(self):
        reset = NOW + h(23 * 24)              # 7 days into a 720 h window
        got = fc.estimate_past([(NOW, 96.0)], 96.0, reset, 720, NOW)
        assert got == [(NOW - h(7 * 24), 100.0), (NOW, 96.0)]

    def test_forecast_quota_carries_it(self):
        f = fc.forecast_quota([(NOW, 53.0)], 47.0, self.RESET, 168, NOW)
        assert f.estimated == [(self.START, 100.0), (NOW, 53.0)]

    def test_forecast_quota_with_a_full_history_has_none(self):
        samples = [(self.START + h(i), 100.0 - 0.47 * i) for i in range(100)]
        f = fc.forecast_quota(samples, 47.0, self.RESET, 168, NOW)
        assert f.estimated == []


class TestShortWindow:
    """The 5-hour session: the view, the rate and the young-history rule are
    the weekly ones scaled to the window, so an hour of readings counts."""

    def test_the_view_is_the_current_session_from_its_start_to_its_reset(self):
        assert fc.view_span(5, NOW + h(3), NOW) == (h(2), h(3))

    def test_without_a_reset_the_view_is_one_window_each_way(self):
        assert fc.view_span(5, None, NOW) == (h(5), h(5))

    def test_the_projection_ends_at_the_reset_without_a_next_session(self):
        f = fc.forecast_quota([(NOW - h(1), 95.0)], 10.0, NOW + h(3), 5, NOW)
        assert (f.start, f.end) == (NOW - h(2), NOW + h(3))
        assert f.projected[-1][0] == NOW + h(3)
        assert max(v for _, v in f.projected) == 90.0

    def test_a_reset_on_the_left_edge_starts_the_line_full(self):
        records = [(NOW - h(2.5), 40.0, NOW - h(2)), (NOW - h(1), 95.0, NOW + h(3))]
        f = fc.forecast_quota(fc.with_resets(records, NOW), 10.0, NOW + h(3), 5, NOW)
        assert f.recorded[0] == (NOW - h(2), 100.0)

    def test_the_rate_reads_the_last_two_hours(self):
        heavy = [(NOW - h(4) + h(i / 6), 90.0 - 20.0 * i / 6) for i in range(7)]     # -4 h .. -3 h
        light = [(NOW - h(2) + h(i / 6), 60.0 - 5.0 * i / 6) for i in range(13)]     # -2 h .. now
        f = fc.forecast_quota(heavy + light, 50.0, NOW + h(1), 5, NOW)
        assert f.rate == pytest.approx(5.0)

    def test_twenty_minutes_of_readings_already_give_a_rate(self):
        samples = [(NOW - h(1 / 3), 92.0), (NOW - h(1 / 6), 91.0), (NOW, 90.0)]
        f = fc.forecast_quota(samples, 10.0, NOW + h(4), 5, NOW)
        assert f.rate == pytest.approx(6.0)                          # not the 10 %/h average

    def test_recording_is_young_for_about_two_hours(self):
        f = fc.forecast_quota([(NOW - h(1), 99.0)], 10.0, NOW + h(4), 5, NOW)
        assert f.recording is True
        f = fc.forecast_quota([(NOW - h(3), 99.0)], 10.0, NOW + h(4), 5, NOW)
        assert f.recording is False

    def test_the_weekly_rules_are_unchanged(self):
        assert fc.view_span(168, NOW + h(100), NOW) == (h(72), h(96))


class TestWithResets:
    """A window that ended between two readings is drawn as a step: flat to
    the reset, then full. Records are (time, remaining, resets_at)."""

    def test_a_reset_between_readings_is_a_step(self):
        reset = NOW - h(3)
        records = [(NOW - h(4), 20.0, reset), (NOW - h(1), 95.0, NOW + h(4))]
        assert fc.with_resets(records, NOW) == [
            (NOW - h(4), 20.0), (reset, 20.0), (reset, 100.0), (NOW - h(1), 95.0)]

    def test_readings_inside_one_window_are_left_alone(self):
        records = [(NOW - h(2), 80.0, NOW + h(1)), (NOW - h(1), 70.0, NOW + h(1))]
        assert fc.with_resets(records, NOW) == [(NOW - h(2), 80.0), (NOW - h(1), 70.0)]

    def test_a_reading_with_no_reset_has_no_step(self):
        records = [(NOW - h(2), 100.0, None), (NOW - h(1), 100.0, None)]
        assert fc.with_resets(records, NOW) == [(NOW - h(2), 100.0), (NOW - h(1), 100.0)]

    def test_the_last_reading_steps_up_when_its_window_ended_before_now(self):
        reset = NOW - h(1)
        assert fc.with_resets([(NOW - h(2), 30.0, reset)], NOW) == [
            (NOW - h(2), 30.0), (reset, 30.0), (reset, 100.0)]


class TestTimeTicks:
    def test_a_short_span_has_hour_ticks_every_two_hours(self):
        start = datetime(2026, 10, 12, 9, 30, tzinfo=UTC)
        ticks = fc.time_ticks(start, start + h(10), tz=UTC)
        assert [label for _, label in ticks] == ["10:00", "12:00", "14:00", "16:00", "18:00"]
        assert ticks[0][0] == datetime(2026, 10, 12, 10, 0, tzinfo=UTC)

    def test_hour_ticks_follow_the_local_clock(self):
        plus530 = timezone(h(5.5))
        start = datetime(2026, 10, 12, 4, 0, tzinfo=UTC)               # 09:30 local
        ticks = fc.time_ticks(start, start + h(4), tz=plus530)
        assert ticks == [(datetime(2026, 10, 12, 4, 30, tzinfo=UTC), "10:00"),
                         (datetime(2026, 10, 12, 5, 30, tzinfo=UTC), "11:00"),
                         (datetime(2026, 10, 12, 6, 30, tzinfo=UTC), "12:00"),
                         (datetime(2026, 10, 12, 7, 30, tzinfo=UTC), "13:00")]

    def test_a_span_of_days_keeps_the_day_ticks(self):
        start = datetime(2026, 10, 9, 15, 0, tzinfo=UTC)
        assert fc.time_ticks(start, start + h(7 * 24), tz=UTC) == fc.day_ticks(start, start + h(7 * 24), tz=UTC)
