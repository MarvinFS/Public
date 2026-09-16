"""OAuth fetch logging: only transitions earn a line.

A successful fetch repeats every refresh and says nothing, so it must not reach
INFO. Failures speak once when they start, once an hour while they last, and
once when they end.
"""

import logging
from datetime import datetime, timedelta

from data_collector import DataCollector, format_duration
from oauth_usage import OAuthUsageData, UsageWindow


def _ok():
    return OAuthUsageData(session=UsageWindow(utilization=12.0))


def _fail(reason="No OAuth token available"):
    return OAuthUsageData(error=reason)


def _records(caplog, level):
    return [r for r in caplog.records if r.levelno >= level]


def _collector(monkeypatch, tmp_path, results):
    """A collector whose OAuth fetch pops the next canned result."""
    collector = DataCollector(projects_dir=tmp_path)
    pending = list(results)
    monkeypatch.setattr("data_collector.fetch_oauth_usage",
                        lambda *a, **k: pending.pop(0))
    return collector


def test_healthy_refresh_stays_quiet(monkeypatch, caplog, tmp_path):
    collector = _collector(monkeypatch, tmp_path, [_ok() for _ in range(5)])
    caplog.set_level(logging.INFO, logger="claudebar")
    for _ in range(5):
        collector._fetch_usage_limits([])

    assert _records(caplog, logging.INFO) == []


def test_first_failure_warns_once_with_the_reason(monkeypatch, caplog, tmp_path):
    collector = _collector(monkeypatch, tmp_path, [_fail("no credentials file") for _ in range(5)])
    caplog.set_level(logging.INFO, logger="claudebar")
    for _ in range(5):
        collector._fetch_usage_limits([])

    warnings = _records(caplog, logging.WARNING)
    assert len(warnings) == 1
    assert "no credentials file" in warnings[0].getMessage()


def test_still_failing_warns_again_after_an_hour(monkeypatch, caplog, tmp_path):
    collector = _collector(monkeypatch, tmp_path, [_fail() for _ in range(3)])
    caplog.set_level(logging.INFO, logger="claudebar")
    collector._fetch_usage_limits([])
    collector._fetch_usage_limits([])
    assert len(_records(caplog, logging.WARNING)) == 1

    collector._oauth_last_reminder = datetime.now() - timedelta(hours=2)
    collector._fetch_usage_limits([])

    warnings = _records(caplog, logging.WARNING)
    assert len(warnings) == 2
    assert "still unavailable" in warnings[1].getMessage()
    assert "3 attempt(s)" in warnings[1].getMessage()


def test_recovery_reports_the_outage(monkeypatch, caplog, tmp_path):
    collector = _collector(monkeypatch, tmp_path, [_fail(), _fail(), _ok()])
    caplog.set_level(logging.INFO, logger="claudebar")
    collector._fetch_usage_limits([])
    collector._fetch_usage_limits([])
    collector._fetch_usage_limits([])

    infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert len(infos) == 1
    assert "recovered after 2 failed attempt(s)" in infos[0]


def test_second_outage_warns_again(monkeypatch, caplog, tmp_path):
    """The edge trigger resets, so a later outage is not swallowed."""
    collector = _collector(monkeypatch, tmp_path, [_fail(), _ok(), _fail(), _ok()])
    caplog.set_level(logging.INFO, logger="claudebar")
    for _ in range(4):
        collector._fetch_usage_limits([])

    assert len(_records(caplog, logging.WARNING)) == 2


def test_failure_reason_travels_to_the_snapshot(monkeypatch, tmp_path):
    collector = _collector(monkeypatch, tmp_path, [_fail("OAuth token expired at 08:00")])
    errors = []
    data = collector._fetch_usage_limits(errors)

    assert data.is_valid is False
    assert errors == ["OAuth: OAuth token expired at 08:00"]


def test_format_duration_reads_as_elapsed_time():
    now = datetime.now()
    assert format_duration(None) == "unknown"
    assert format_duration(now - timedelta(seconds=30)) == "30s"
    assert format_duration(now - timedelta(minutes=5)) == "5m"
    assert format_duration(now - timedelta(hours=3, minutes=20)) == "3h 20m"
    assert format_duration(now - timedelta(days=2, hours=1)) == "2d 1h"
