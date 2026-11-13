"""Codex usage windows and the cost period they are reported alongside.

OpenAI used to always put the 5-hour window in `primary_window` and the weekly
one in `secondary_window`. On some plans (prolite) it now returns the weekly
window as `primary` with `secondary` null, so the parser has to go by
`limit_window_seconds` instead of position.
"""

import json
from unittest.mock import patch

from openai_usage import fetch_openai_usage


def _fetch(rate_limit):
    payload = json.dumps({"plan_type": "prolite", "rate_limit": rate_limit}).encode()

    class _Resp:
        def read(self):
            return payload

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    with patch("http_json.open_url", return_value=_Resp()):
        return fetch_openai_usage(access_token="token")


def test_weekly_only_plan_fills_the_weekly_window():
    data = _fetch({
        "primary_window": {"used_percent": 2, "limit_window_seconds": 604800,
                           "reset_at": 1789225516},
        "secondary_window": None,
    })
    assert data.weekly_percent == 2
    assert data.session_percent == 0
    assert data.has_session_window is False


def test_classic_two_window_plan_still_maps_by_position():
    data = _fetch({
        "primary_window": {"used_percent": 44, "limit_window_seconds": 18000,
                           "reset_at": 1789225516},
        "secondary_window": {"used_percent": 7, "limit_window_seconds": 604800,
                             "reset_at": 1789825516},
    })
    assert data.session_percent == 44
    assert data.weekly_percent == 7
    assert data.has_session_window is True


def test_the_quota_view_charts_the_session_where_there_is_one():
    from datetime import datetime
    from models import OpenAISnapshot, UsageSnapshot
    reset = datetime(2026, 10, 13, 15, 0)
    codex = OpenAISnapshot(timestamp=reset, session_percent=44, session_resets_at=reset,
                           session_window_hours=5, weekly_percent=7)
    assert codex.charted_window() == (True, 44, reset, 5)
    claude = UsageSnapshot(timestamp=reset, session_percent=30, session_resets_at=reset, weekly_percent=9)
    assert claude.charted_window() == (True, 30, reset, 5)


def test_a_plan_with_no_session_charts_its_long_window():
    from datetime import datetime
    from models import OpenAISnapshot
    reset = datetime(2026, 11, 5, 15, 0)
    codex = OpenAISnapshot(timestamp=reset, session_available=False, weekly_percent=4,
                           weekly_resets_at=reset, weekly_window_hours=720)
    assert codex.charted_window() == (False, 4, reset, 720)
