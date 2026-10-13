"""The reset countdown drawn from the reset time itself, not from a string that was
worded when the reading was collected."""

from datetime import datetime, timedelta, timezone

import pytest

from models import reset_clock

NOW = datetime(2026, 10, 13, 16, 0, 0, tzinfo=timezone.utc)


def test_no_reset_time_has_no_clock():
    assert reset_clock(None, NOW) is None


@pytest.mark.parametrize("ahead, text", [
    (timedelta(minutes=3, seconds=20), "in 3m"),
    (timedelta(hours=1, minutes=30, seconds=5), "in 1h 30m"),
    (timedelta(days=2, hours=4, minutes=40), "in 2d 4h"),
    (timedelta(hours=24), "in 1d 0h"),
])
def test_a_reset_ahead_counts_down(ahead, text):
    assert reset_clock(NOW + ahead, NOW) == (False, text)


@pytest.mark.parametrize("ago, text", [
    (timedelta(seconds=40), "just now"),
    (timedelta(minutes=4, seconds=10), "4m ago"),
    (timedelta(hours=2, minutes=5), "2h 5m ago"),
    (timedelta(days=1, hours=3), "1d 3h ago"),
])
def test_a_reset_that_passed_says_how_long_ago(ago, text):
    assert reset_clock(NOW - ago, NOW) == (True, text)


def test_the_instant_of_the_reset_has_ended():
    assert reset_clock(NOW, NOW) == (True, "just now")


def test_a_naive_reset_time_is_read_on_the_local_clock():
    local_now = datetime(2026, 10, 13, 16, 0, 0)
    assert reset_clock(local_now + timedelta(minutes=10, seconds=1), local_now) == (False, "in 10m")
