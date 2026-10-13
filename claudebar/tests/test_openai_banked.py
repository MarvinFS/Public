"""The Codex usage call: banked reset credits, and the window length from data.

`wham/usage` reports a count (`rate_limit_reset_credits`); when it is above
zero one more GET lists the credits. The window length matters because the
free plan on this account has a single 30-day window.
"""

import io
import json
import urllib.error
from datetime import datetime, timezone
from unittest.mock import patch

import openai_usage

UTC = timezone.utc
USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
DETAILS_URL = "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits"

FREE_30_DAY = {
    "plan_type": "free",
    "rate_limit": {"primary_window": {"used_percent": 4, "limit_window_seconds": 2592000,
                                      "reset_at": 1792684800},
                   "secondary_window": None},
    "rate_limit_reset_credits": {"available_count": 3, "applicable_available_count": 0},
}
DETAILS = {
    "available_count": 3, "total_earned_count": 9, "history_enabled": True,
    "credits": [
        {"id": "c-nov6", "reset_type": "codex_rate_limits", "status": "available",
         "title": "Full reset (Monthly)", "expires_at": "2099-11-06T09:00:00Z"},
        {"id": "c-oct22", "reset_type": "codex_rate_limits", "status": "available",
         "title": "Full reset (Monthly)", "expires_at": "2099-10-22T09:00:00Z"},
        {"id": "c-oct29", "reset_type": "codex_rate_limits", "status": "available",
         "title": "Full reset (Monthly)", "expires_at": "2099-10-29T09:00:00Z"},
    ],
}


class _Resp:
    def __init__(self, payload):
        self._body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeUrlopen:
    """Answers by URL, and records every request."""

    def __init__(self, routes):
        self.routes = routes
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        answer = self.routes[request.full_url]
        if isinstance(answer, Exception):
            raise answer
        return _Resp(answer)

    def urls(self):
        return [r.full_url for r in self.requests]


def fetch(routes, account_id="acct-1"):
    fake = FakeUrlopen(routes)
    with patch("urllib.request.urlopen", fake), \
            patch.object(openai_usage, "load_codex_account_id", lambda: account_id):
        data = openai_usage.fetch_openai_usage(access_token="token")
    return data, fake


class TestWindowLength:
    def test_a_30_day_window_keeps_its_length(self):
        data, _ = fetch({USAGE_URL: FREE_30_DAY, DETAILS_URL: DETAILS})
        assert data.weekly.window_seconds == 2592000
        assert data.weekly.window_seconds / 3600 == 720
        assert data.weekly_percent == 4
        assert data.has_session_window is False

    def test_a_weekly_window_is_168_hours(self):
        usage = {"plan_type": "plus", "rate_limit": {
            "primary_window": {"used_percent": 1, "limit_window_seconds": 18000, "reset_at": 1},
            "secondary_window": {"used_percent": 2, "limit_window_seconds": 604800, "reset_at": 2}}}
        data, _ = fetch({USAGE_URL: usage})
        assert data.five_hour.window_seconds == 18000
        assert data.weekly.window_seconds == 604800


class TestBankedCredits:
    def test_the_count_comes_from_the_usage_call_and_the_list_from_the_details(self):
        data, fake = fetch({USAGE_URL: FREE_30_DAY, DETAILS_URL: DETAILS})
        assert fake.urls() == [USAGE_URL, DETAILS_URL]
        assert data.banked.provider == "codex"
        assert data.banked.available == 3 and data.banked.applicable == 0
        assert [c.id for c in data.banked.credits] == ["c-oct22", "c-oct29", "c-nov6"]

    def test_the_details_call_carries_the_bearer_and_the_account_id(self):
        _, fake = fetch({USAGE_URL: FREE_30_DAY, DETAILS_URL: DETAILS})
        request = fake.requests[1]
        assert request.get_header("Authorization") == "Bearer token"
        assert request.get_header("Chatgpt-account-id") == "acct-1"

    def test_no_account_id_means_no_header(self):
        _, fake = fetch({USAGE_URL: FREE_30_DAY, DETAILS_URL: DETAILS}, account_id=None)
        assert fake.requests[1].get_header("Chatgpt-account-id") is None

    def test_nothing_banked_never_asks_for_the_list(self):
        usage = dict(FREE_30_DAY, rate_limit_reset_credits={"available_count": 0,
                                                            "applicable_available_count": 0})
        data, fake = fetch({USAGE_URL: usage})
        assert fake.urls() == [USAGE_URL]
        assert data.banked.available == 0

    def test_a_response_without_the_block_has_no_banked_data_and_no_second_call(self):
        usage = {k: v for k, v in FREE_30_DAY.items() if k != "rate_limit_reset_credits"}
        data, fake = fetch({USAGE_URL: usage})
        assert fake.urls() == [USAGE_URL]
        assert data.banked is None

    def test_a_failing_details_call_degrades_to_the_count(self):
        down = urllib.error.HTTPError(DETAILS_URL, 500, "boom", {}, io.BytesIO(b""))
        data, fake = fetch({USAGE_URL: FREE_30_DAY, DETAILS_URL: down})
        assert data.is_valid
        assert data.banked.available == 3 and data.banked.credits == []

    def test_a_garbled_details_body_degrades_to_the_count(self):
        data, _ = fetch({USAGE_URL: FREE_30_DAY, DETAILS_URL: b"<html>nope</html>"})
        assert data.banked.available == 3 and data.banked.credits == []

    def test_a_network_error_on_the_details_call_degrades_to_the_count(self):
        data, _ = fetch({USAGE_URL: FREE_30_DAY,
                         DETAILS_URL: urllib.error.URLError("offline")})
        assert data.is_valid and data.banked.available == 3

    def test_the_usage_windows_do_not_depend_on_the_details_call(self):
        down = urllib.error.HTTPError(DETAILS_URL, 403, "no", {}, io.BytesIO(b""))
        data, _ = fetch({USAGE_URL: FREE_30_DAY, DETAILS_URL: down})
        assert data.weekly_percent == 4
