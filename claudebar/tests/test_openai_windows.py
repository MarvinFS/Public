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

    with patch("urllib.request.urlopen", return_value=_Resp()):
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


def test_month_usage_covers_the_calendar_month_only():
    """The "This month" figure must reset on the 1st, not roll 30 days back."""
    from datetime import datetime
    from pathlib import Path
    import codex_log_parser

    scanned = []
    with patch.object(codex_log_parser, "get_sessions_for_date",
                      side_effect=lambda d, date: scanned.append(date.date()) or []):
        with patch.object(codex_log_parser, "datetime") as fake:
            fake.now.return_value = datetime(2026, 9, 5, 18, 30)
            codex_log_parser.get_month_codex_usage(sessions_dir=Path("."))

    assert scanned[0] == datetime(2026, 9, 1).date()
    assert scanned[-1] == datetime(2026, 9, 5).date()
    assert len(scanned) == 5
