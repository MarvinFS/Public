"""The words on the quota and balance views, built from the forecasts."""

from datetime import datetime, timedelta, timezone

import pytest

import banked_resets as br
import forecast as fc
import forecast_text as ft

UTC = timezone.utc
NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)          # a Monday


def h(hours):
    return timedelta(hours=hours)


def money(usd):
    return f"${usd:,.2f}"


def samples(n_hours, start, per_hour):
    return [(NOW - h(n_hours - i), start - per_hour * i) for i in range(n_hours + 1)]


def quota(used=24.0, reset_in=100, window=168, per_hour=1.0, hours=24):
    hist = samples(hours, 100.0, per_hour) if hours else []
    return fc.forecast_quota(hist, used, NOW + h(reset_in) if reset_in is not None else None,
                             window, NOW)


def claude_banked(expires="2026-10-22T16:00:00+00:00", **kw):
    grant = {"id": "g1", "label": "Launch reset", "resets_total": 1, "resets_left": 1,
             "ends_at": expires, "clears": [], "paused": False, "usable_now": True,
             "use_requires_limit": False}
    grant.update(kw)
    return br.parse_claude_banked({"eligible": True, "next_grant_id": "g1", "grants": [grant]}, NOW)


def rows(f, banked=None, tz=UTC):
    return ft.quota_rows(f, banked, NOW, tz)


class TestWindowTitle:
    @pytest.mark.parametrize("hours,title", [
        (168, "Weekly"), (168.0, "Weekly"), (720, "30-day"), (360, "15-day"),
        (24 * 28, "28-day"), (48, "2-day"), (24, "24-hour"), (12, "12-hour")])
    def test_titles(self, hours, title):
        assert ft.window_title(hours) == title


class TestRateText:
    @pytest.mark.parametrize("rate,text", [
        (0.9, "~0.9%/h"), (3.3, "~3.3%/h"), (12.4, "~12%/h"), (10.0, "~10%/h"),
        (0.0238, "~0.02%/h"), (0.1, "~0.1%/h"), (0.0, "no recent use")])
    def test_rates(self, rate, text):
        assert ft.rate_text(rate) == text


class TestRunOutText:
    @pytest.mark.parametrize("hours,text", [
        (0.4, "Runs out in 0h 24m"), (1.5, "Runs out in 1h 30m"), (23.99, "Runs out in 23h 59m"),
        (76.0, "Runs out in 3d 4h"), (46.5, "Runs out in 1d 22h"), (0.0, "Runs out in 0h 0m")])
    def test_the_words_the_limit_bar_and_the_quota_row_share(self, hours, text):
        assert ft.runout_text(hours) == text

    def test_a_row_that_never_runs_out_says_never(self):
        assert ft.hours_text(float("inf")) == "never"


class TestStamp:
    def test_within_a_week_it_is_a_weekday_and_time(self):
        assert ft.stamp(NOW + h(34), NOW, UTC) == "Tue 22:00"

    def test_a_week_or_more_away_it_is_a_date_and_time(self):
        assert ft.stamp(NOW + h(24 * 23), NOW, UTC) == "Nov 4 12:00"

    def test_the_past_follows_the_same_rule(self):
        assert ft.stamp(NOW - h(24), NOW, UTC) == "Sun 12:00"
        assert ft.stamp(NOW - h(24 * 10), NOW, UTC) == "Oct 2 12:00"

    def test_it_is_shown_in_the_requested_zone(self):
        assert ft.stamp(NOW + h(34), NOW, timezone(h(3))) == "Wed 01:00"


class TestQuotaHeader:
    def test_the_remaining_percent_and_the_rate(self):
        assert ft.quota_header(quota()) == ("76% left", "~1.0%/h")

    def test_it_matches_the_limit_bar_rounding(self):
        assert ft.quota_header(quota(used=38.6))[0] == "61% left"        # 100 - 38.6 = 61.4


class TestQuotaRows:
    def test_next_reset_shows_the_gain_and_the_time(self):
        f = quota(used=24.0, reset_in=34, per_hour=1.0)                  # 76 -> 42 at the reset
        assert rows(f)[0] == ("Next reset", "+58% · Tue 22:00", "normal")

    def test_a_quota_that_lasts_says_how_much_is_unused(self):
        f = quota(used=12.0, reset_in=40, per_hour=0.5)                  # 88 - 20 = 68 left
        assert ("At this rate", "Lasts until reset · ~68% unused", "ok") in rows(f)

    def test_a_quota_that_runs_out_says_when(self):
        f = quota(used=24.0, reset_in=100, per_hour=1.0)                 # dry at +76 h
        assert ("At this rate", "Runs out in 3d 4h · Thu 16:00", "warn") in rows(f)

    def test_a_banked_reset_that_would_restore_it_is_mentioned(self):
        f = quota(used=24.0, reset_in=100, per_hour=1.0)
        out = rows(f, claude_banked())
        i = out.index(("At this rate", "Runs out in 3d 4h · Thu 16:00", "warn"))
        assert out[i + 1] == ("", "A banked reset would restore it", "muted")

    def test_a_banked_reset_that_expires_first_is_not_promised(self):
        f = quota(used=24.0, reset_in=100, per_hour=1.0)                 # dry at +76 h
        soon = claude_banked(expires="2026-10-13T00:00:00+00:00")        # expires at +12 h
        assert all("would restore" not in value for _, value, _ in rows(f, soon))

    def test_no_restore_line_when_the_quota_lasts(self):
        f = quota(used=12.0, reset_in=40, per_hour=0.5)
        assert all("would restore" not in value for _, value, _ in rows(f, claude_banked()))

    def test_the_banked_row(self):
        out = rows(quota(), claude_banked())
        assert ("Banked", "1 available, expires Oct 22", "normal") in out

    def test_the_banked_row_turns_amber_within_a_week(self):
        out = rows(quota(), claude_banked(expires="2026-10-15T00:00:00+00:00"))
        assert ("Banked", "1 available, expires Oct 15", "warn") in out

    def test_nothing_banked_has_no_row(self):
        empty = br.parse_codex_banked({"available_count": 0}, {}, NOW)
        for banked in (None, empty):
            assert all(label != "Banked" for label, _, _ in rows(quota(), banked))

    def test_a_banked_count_with_no_known_expiry(self):
        banked = br.parse_codex_banked({"available_count": 3}, None, NOW)
        assert ("Banked", "3 available", "normal") in rows(quota(), banked)

    def test_the_banked_row_is_in_the_zone_asked_for(self):
        banked = claude_banked(expires="2026-10-22T23:30:00+00:00")
        assert ("Banked", "1 available, expires Oct 23", "normal") in rows(
            quota(), banked, tz=timezone(h(3)))

    def test_recording_since_while_the_history_is_young(self):
        f = quota(hours=24)
        assert rows(f)[-1] == ("Recording since", "Sun 12:00", "muted")

    def test_no_recording_row_once_three_days_are_covered(self):
        f = quota(hours=80)
        assert all(label != "Recording since" for label, _, _ in rows(f))

    def test_no_recording_row_without_any_history(self):
        assert all(label != "Recording since" for label, _, _ in rows(quota(hours=0)))

    def test_without_a_reset_time_only_a_run_out_is_reported(self):
        f = quota(used=24.0, reset_in=None, per_hour=1.0)
        labels = [label for label, _, _ in rows(f)]
        assert "Next reset" not in labels
        assert ("At this rate", "Runs out in 3d 4h · Thu 16:00", "warn") in rows(f)

    def test_without_a_reset_time_and_no_drain_there_is_nothing_to_say(self):
        f = quota(used=24.0, reset_in=None, per_hour=0.0)
        assert [label for label, _, _ in rows(f) if label in ("Next reset", "At this rate")] == []

    def test_a_30_day_window_reads_in_dates(self):
        f = fc.forecast_quota([], 4.0, NOW + h(23 * 24), 720, NOW)
        out = rows(f)
        assert out[0][0] == "Next reset" and out[0][1].endswith("Nov 4 12:00")
        assert ("At this rate", "Lasts until reset · ~83% unused", "ok") in out


class TestBalance:
    def balance(self, bal=12.40, costs=None, samples_=()):
        return fc.forecast_balance(list(samples_), bal, costs if costs is not None else [0.4] * 31,
                                   NOW)

    def test_header(self):
        assert ft.balance_header(self.balance(), money, money) == ("$12.40 left", "~$0.40/day")

    def test_the_run_out_with_its_date_and_days(self):
        f = self.balance(12.40, [0.4] * 31)
        assert ft.balance_rows(f, NOW, UTC, money)[0] == ("Runs out", "~Nov 12 (31 days)", "normal")

    def test_one_day_is_singular(self):
        f = self.balance(0.4, [0.4] * 31)
        assert ft.balance_rows(f, NOW, UTC, money)[0] == ("Runs out", "~Oct 13 (1 day)", "normal")

    def test_under_a_day_says_today_or_tomorrow_is_not_invented(self):
        f = self.balance(0.1, [0.4] * 31)
        assert ft.balance_rows(f, NOW, UTC, money)[0] == ("Runs out", "~Oct 12 (<1 day)", "normal")

    def test_the_spend_rate_row(self):
        f = self.balance(12.40, [0.4] * 31)
        assert ft.balance_rows(f, NOW, UTC, money)[1] == (
            "Spend rate", "$0.40/day, last 7 days", "normal")

    def test_more_than_a_year_is_not_a_date(self):
        f = self.balance(100.0, [0.01] * 31)
        assert ft.balance_rows(f, NOW, UTC, money)[0] == (
            "At this rate", "Lasts over a year", "ok")

    def test_a_balance_that_outlasts_any_date_says_over_a_year(self):
        f = self.balance(1e9, [1e-9] * 7)
        assert ft.balance_rows(f, NOW, UTC, money)[0] == ("At this rate", "Lasts over a year", "ok")

    def test_no_spend_at_all(self):
        f = self.balance(12.40, [0.0] * 31)
        assert ft.balance_rows(f, NOW, UTC, money) == [
            ("Spend rate", "none in the last 7 days", "muted")]


class TestTipText:
    def test_a_recorded_point(self):
        text = ft.tip_text(NOW - h(22), 54.0, False, NOW, UTC, ft.percent_left)
        assert text == "Sun 14:00 · 54% left"

    def test_a_projected_point_says_so(self):
        text = ft.tip_text(NOW + h(26), 54.0, True, NOW, UTC, ft.percent_left)
        assert text == "Tue 14:00 · 54% left (projected)"

    def test_a_balance(self):
        text = ft.tip_text(NOW - h(2), 12.4, False, NOW, UTC, lambda v: f"{money(v)} left")
        assert text == "Mon 10:00 · $12.40 left"

    def test_the_value_belongs_to_its_own_time(self):
        """Two parallel pieces feed one label: check they stay paired."""
        pairs = [(NOW - h(5), 80.0), (NOW - h(1), 70.0), (NOW + h(30), 40.0)]
        texts = [ft.tip_text(t, v, t > NOW, NOW, UTC, ft.percent_left) for t, v in pairs]
        assert texts == ["Mon 07:00 · 80% left", "Mon 11:00 · 70% left",
                         "Tue 18:00 · 40% left (projected)"]


class TestGainText:
    def test_the_gain_the_next_reset_adds_back(self):
        assert ft.gain_text(quota(used=24.0, reset_in=34, per_hour=1.0)) == "+58%"

    def test_no_reset_no_gain(self):
        assert ft.gain_text(quota(reset_in=None)) == ""

    def test_the_next_reset_row_uses_it(self):
        f = quota(used=24.0, reset_in=34, per_hour=1.0)
        assert rows(f)[0][1].startswith(ft.gain_text(f))
