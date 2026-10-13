"""The Anthropic usage call: one request that also asks for banked resets.

With the app's own user-agent the `cedar_ember` block comes back as
`eligible: false, reason: surface`; with the Claude CLI's it carries the real
grants. So the single usage call carries `?cedar_ember=1` and the CLI agent,
and on a 400 or 403 it is repeated once as the old plain call so the quota
windows are never lost to the new query.
"""

import io
import json
import urllib.error
from datetime import datetime, timezone
from unittest.mock import patch

import oauth_usage

USAGE = {
    "five_hour": {"utilization": 12.0, "resets_at": "2026-10-12T22:00:00+00:00"},
    "seven_day": {"utilization": 39.0, "resets_at": "2026-10-16T09:00:00+00:00"},
}
GRANT = {"id": "opus55-launch-promax-20260921", "resets_total": 1, "resets_left": 1,
         "ends_at": "2026-10-22T16:00:00+00:00", "clears": ["five_hour"],
         "paused": False, "usable_now": True, "use_requires_limit": False}
CEDAR = {"eligible": True, "grants": [GRANT], "next_grant_id": GRANT["id"], "at_limit": False}


class _Resp:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(code, headers=None):
    return urllib.error.HTTPError("https://api.anthropic.com/api/oauth/usage", code,
                                  "reason", headers or {}, io.BytesIO(b""))


class FakeUrlopen:
    """Replays one scripted answer per call and records every request."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return _Resp(answer)


def fetch(*answers):
    fake = FakeUrlopen(*answers)
    with patch("urllib.request.urlopen", fake):
        data = oauth_usage.fetch_oauth_usage(access_token="token")
    return data, fake


def agent(request):
    return request.get_header("User-agent")


class TestTheOneCall:
    def test_it_asks_for_banked_resets_as_the_cli(self):
        _, fake = fetch(dict(USAGE, cedar_ember=CEDAR))
        assert len(fake.requests) == 1
        request = fake.requests[0]
        assert request.full_url == "https://api.anthropic.com/api/oauth/usage?cedar_ember=1"
        assert agent(request) == oauth_usage.CLAUDE_CLI_USER_AGENT
        assert agent(request).startswith("claude-cli/") and agent(request).endswith("(external, cli)")

    def test_the_bearer_and_beta_headers_are_kept(self):
        _, fake = fetch(dict(USAGE, cedar_ember=CEDAR))
        request = fake.requests[0]
        assert request.get_header("Authorization") == "Bearer token"
        assert request.get_header("Anthropic-beta") == "oauth-2025-04-20"

    def test_the_windows_still_parse(self):
        data, _ = fetch(dict(USAGE, cedar_ember=CEDAR))
        assert data.is_valid
        assert data.weekly.percent == 39.0
        assert data.session.percent == 12.0

    def test_banked_resets_are_parsed_from_the_same_response(self):
        data, _ = fetch(dict(USAGE, cedar_ember=CEDAR))
        assert data.banked.provider == "claude" and data.banked.state == "ok"
        assert data.banked.available == 1
        assert data.banked.credits[0].id == GRANT["id"]

    def test_a_response_without_the_block_has_no_banked_data(self):
        data, _ = fetch(USAGE)
        assert data.is_valid and data.banked is None

    def test_an_ineligible_account_is_remembered(self):
        data, _ = fetch(dict(USAGE, cedar_ember={"eligible": False,
                                                 "ineligible_reason": "surface"}))
        assert data.banked.state == "ineligible"
        assert data.banked.available == 0

    def test_a_null_block_has_no_banked_data(self):
        data, _ = fetch(dict(USAGE, cedar_ember=None))
        assert data.banked is None


class TestFallbackToThePlainCall:
    def test_a_400_repeats_the_old_call_once(self):
        data, fake = fetch(_http_error(400), USAGE)
        assert len(fake.requests) == 2
        plain = fake.requests[1]
        assert plain.full_url == "https://api.anthropic.com/api/oauth/usage"
        assert agent(plain) == "ClaudeBar/1.0"
        assert data.is_valid and data.weekly.percent == 39.0

    def test_a_403_repeats_the_old_call_once(self):
        data, fake = fetch(_http_error(403), USAGE)
        assert len(fake.requests) == 2
        assert data.is_valid

    def test_the_fallback_marks_banked_data_as_failed_to_load(self):
        data, _ = fetch(_http_error(400), USAGE)
        assert data.banked.state == "failed"
        assert data.banked.available == 0

    def test_the_fallback_is_tried_only_once(self):
        data, fake = fetch(_http_error(400), _http_error(403), USAGE)
        assert len(fake.requests) == 2
        assert not data.is_valid
        assert data.error == "HTTP 403: reason"

    def test_401_is_not_retried(self):
        data, fake = fetch(_http_error(401), USAGE)
        assert len(fake.requests) == 1
        assert data.error == "OAuth token expired or invalid"

    def test_429_is_not_retried_so_the_endpoint_is_never_hammered(self):
        data, fake = fetch(_http_error(429, {"Retry-After": "0"}), USAGE)
        assert len(fake.requests) == 1
        assert data.error == "API access restricted (429)"

    def test_a_server_error_is_not_retried(self):
        data, fake = fetch(_http_error(500), USAGE)
        assert len(fake.requests) == 1
        assert data.error == "HTTP 500: reason"

    def test_a_network_failure_is_not_retried(self):
        data, fake = fetch(urllib.error.URLError("offline"), USAGE)
        assert len(fake.requests) == 1
        assert data.error.startswith("Network error")
