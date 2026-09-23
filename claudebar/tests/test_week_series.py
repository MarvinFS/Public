"""The date behind each bar of the daily spend chart.

DeepSeek supplies its own ISO dates alongside the costs, oldest first, and the
chart draws its values in that same direction. The dates were once read
backwards, so every bar carried the wrong day and the row as a whole read
newest to oldest: on Saturday 2026-09-19 the highlighted bar for today was
labelled Sunday.
"""

from datetime import date

from ui_window import ClaudeBarWindow


def chart_days(count, today, dates=()):
    return ClaudeBarWindow._chart_days(count, today, dates=dates)


class TestChartDays:
    TODAY = date(2026, 9, 19)
    LAST_WEEK = [f"2026-09-{day:02d}" for day in range(13, 20)]

    def test_supplied_dates_read_oldest_first(self):
        assert chart_days(7, self.TODAY, self.LAST_WEEK) == [date(2026, 9, d) for d in range(13, 20)]

    def test_the_last_bar_is_today(self):
        assert chart_days(7, self.TODAY, self.LAST_WEEK)[-1] == self.TODAY

    def test_a_longer_date_series_lines_up_with_its_last_values(self):
        """The pairing, not just the order: a reversal trips this too."""
        dates = [f"2026-09-{day:02d}" for day in range(10, 20)]
        assert chart_days(7, self.TODAY, dates) == [date(2026, 9, d) for d in range(13, 20)]

    def test_without_dates_the_days_count_back_from_today(self):
        days = chart_days(31, self.TODAY)
        assert days[-1] == self.TODAY
        assert days[0] == date(2026, 8, 20)

    def test_a_malformed_date_falls_back_to_counting_back(self):
        dates = list(self.LAST_WEEK)
        dates[0] = "not-a-date"
        assert chart_days(7, self.TODAY, dates)[0] == date(2026, 9, 13)

    def test_no_bars_is_an_empty_series(self):
        assert chart_days(0, self.TODAY, self.LAST_WEEK) == []
