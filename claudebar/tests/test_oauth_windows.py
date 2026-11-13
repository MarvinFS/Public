"""The usage reply with windows the API has nothing for.

The endpoint sends `null` for a window the plan does not have, and a window can come
with a `null` utilization. A null window is no window and a null utilization is no
reading; neither may reach the code that formats a percent.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

import claude_cli_snapshot as ccs
import data_collector as dc
import oauth_usage
from oauth_usage import OAuthUsageData, parse_usage_response

RESET = "2099-10-12T22:00:00+00:00"
WEEKLY = {"utilization": 39.0, "resets_at": RESET}


def test_a_null_window_is_no_window():
    data = parse_usage_response({"five_hour": None, "seven_day": WEEKLY})
    assert data.session is None
    assert data.weekly.percent == 39.0
    assert data.is_valid


def test_a_reply_of_null_windows_is_not_valid_and_does_not_raise():
    data = parse_usage_response({"five_hour": None, "seven_day": None, "extra_usage": None})
    assert data.session is None and data.weekly is None and data.extra is None
    assert not data.is_valid


@pytest.mark.parametrize("utilization", [None, "12", True, [], {}])
def test_a_utilization_that_is_not_a_number_is_not_a_reading(utilization):
    data = parse_usage_response({"five_hour": {"utilization": utilization, "resets_at": RESET},
                                 "seven_day": WEEKLY})
    assert data.session is None
    assert data.weekly.percent == 39.0


def test_a_window_without_the_key_still_reads_as_nothing_used():
    """The one old default that stays: a window object that says nothing about use."""
    assert parse_usage_response({"five_hour": {"resets_at": RESET}}).session.percent == 0.0


def test_null_extra_usage_is_no_extra_usage():
    data = parse_usage_response({"five_hour": {"utilization": 5.0}, "extra_usage": None})
    assert data.extra is None
    assert data.session.percent == 5.0


def test_a_null_utilization_does_not_take_the_collector_down(monkeypatch):
    """collect() read `.percent` of a window whose utilization was None and raised, so the
    whole refresh failed (and, before the providers were isolated, every provider with it)."""
    reply = parse_usage_response({"five_hour": {"utilization": None, "resets_at": RESET},
                                  "seven_day": WEEKLY})
    monkeypatch.setattr(dc, "fetch_oauth_usage", lambda: reply)
    monkeypatch.setattr(dc, "collect_entries", lambda projects_dir: {})

    snapshot = dc.DataCollector().collect()

    assert snapshot.weekly_percent == 39.0
    assert snapshot.session_percent == 0.0      # nothing recorded for it
    assert snapshot.cli_available


def test_the_cli_copy_with_a_null_utilization_is_no_reading():
    now = datetime.now(timezone.utc)
    config = {"oauthAccount": {"accountUuid": "u"},
              "cachedUsageUtilization": {
                  "fetchedAtMs": int((now - timedelta(minutes=1)).timestamp() * 1000),
                  "accountUuid": "u",
                  "utilization": {"five_hour": {"utilization": None, "resets_at": RESET},
                                  "seven_day": None}}}
    path = ccs.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config), encoding="utf-8")

    assert ccs.read(now) is None


@pytest.mark.parametrize("resets_at", [1791000000, 1.5, True, [], {}, ["2099-01-01T00:00:00Z"]])
def test_a_reset_time_that_is_not_a_string_is_no_reset_time(resets_at):
    """.endswith on an int raised AttributeError, past the handler for a malformed string."""
    assert oauth_usage.parse_reset_time(resets_at) is None
    data = parse_usage_response({"five_hour": {"utilization": 5.0, "resets_at": resets_at}})
    assert data.session.percent == 5.0 and data.session.resets_at is None


def test_a_reply_of_null_windows_says_why_it_failed():
    data = parse_usage_response({"five_hour": None, "seven_day": None, "extra_usage": None})
    assert data.error and "no session or weekly window" in data.error
    assert not data.is_valid


def test_a_reply_with_a_window_has_no_error():
    assert parse_usage_response({"seven_day": WEEKLY}).error is None


def test_the_log_line_for_an_empty_reply_has_a_reason(monkeypatch, caplog):
    """The collector wrote "OAuth usage unavailable ...: None" for it."""
    import logging
    monkeypatch.setattr(dc, "fetch_oauth_usage",
                        lambda: parse_usage_response({"five_hour": None, "seven_day": None}))
    collector = dc.DataCollector()
    errors = []
    with caplog.at_level(logging.WARNING, logger="claudebar"):
        collector._fetch_usage_limits(errors)
    assert errors == ["OAuth: The usage reply holds no session or weekly window"]
    assert "None" not in caplog.text
