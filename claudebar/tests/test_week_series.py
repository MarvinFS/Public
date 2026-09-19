"""The weekday labels under the daily spend chart.

DeepSeek supplies its own ISO dates alongside the costs, oldest first, and the
chart draws its values in that same direction. The labels were read backwards,
so every bar carried the wrong weekday and the row as a whole read newest to
oldest: on Saturday 2026-09-19 it read "Sat Fri Thu Wed Tue Mon Sun", with the
highlighted bar for today labelled Sunday.
"""

from datetime import date

from ui_window import ClaudeBarWindow


def week_series(values, today, dates=()):
    return ClaudeBarWindow._week_series(values, today, dates=dates)


class TestWeekSeries:
    TODAY = date(2026, 9, 19)          # a Saturday
    LAST_WEEK = [f"2026-09-{day:02d}" for day in range(13, 20)]
    OLDEST_FIRST = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]

    def test_supplied_dates_read_oldest_first(self):
        _, labels = week_series([1.0] * 7, self.TODAY, self.LAST_WEEK)
        assert labels == self.OLDEST_FIRST

    def test_the_last_label_is_today(self):
        _, labels = week_series([1.0] * 7, self.TODAY, self.LAST_WEEK)
        assert labels[-1] == self.TODAY.strftime("%a")

    def test_every_label_is_the_weekday_of_its_own_value(self):
        """The pairing, not just the order: a reversal trips this too."""
        dates = [f"2026-09-{day:02d}" for day in range(10, 20)]
        values = [float(day) for day in range(10, 20)]
        week, labels = week_series(values, self.TODAY, dates)
        for value, label in zip(week, labels):
            assert label == date(2026, 9, int(value)).strftime("%a")

    def test_without_dates_the_labels_count_back_from_today(self):
        _, labels = week_series([1.0] * 7, self.TODAY)
        assert labels == self.OLDEST_FIRST

    def test_a_malformed_date_falls_back_to_counting_back(self):
        dates = list(self.LAST_WEEK)
        dates[0] = "not-a-date"
        _, labels = week_series([1.0] * 7, self.TODAY, dates)
        assert labels[0] == "Sun"                       # 2026-09-13
        assert labels[1:] == self.OLDEST_FIRST[1:]

    def test_no_values_is_an_empty_series(self):
        assert week_series([], self.TODAY, self.LAST_WEEK) == ([], [])
