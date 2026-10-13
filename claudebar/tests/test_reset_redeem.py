"""Spending a banked reset: the two redeem clients, their outcomes, and the
confirm / idempotency / in-flight rules around them.

Spending is irreversible, so nothing here uses a real endpoint: a fake
transport replays the response shapes recorded from the live accounts, and
asserts exactly which requests would have been sent.
"""

import re
from datetime import datetime, timedelta, timezone

import pytest

import banked_resets as br
import oauth_usage
import reset_redeem as rr

UTC = timezone.utc
NOW = datetime(2026, 10, 12, 18, 30, tzinfo=UTC)
TOKEN = "tok-SECRET-123"
ORG = "11111111-2222-3333-4444-555555555555"
PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
RESET_URL = f"https://api.anthropic.com/api/organizations/{ORG}/reset_rate_limits"
CONSUME_URL = "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits/consume"
BODY_TEXT = "BODY-MUST-NOT-LEAK"

KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class FakeTransport:
    """Replays scripted answers in order and records every request."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def request(self, method, url, headers, body, timeout):
        self.calls.append({"method": method, "url": url, "headers": headers, "body": body})
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def posts(self):
        return [c for c in self.calls if c["method"] == "POST"]


def ok(data):
    return rr.HttpResult(200, data)


def status(code, data=None):
    return rr.HttpResult(code, data if data is not None else {"error": BODY_TEXT})


PROFILE = {"account": {"uuid": "acct"}, "organization": {"uuid": ORG, "name": "Org"}}

CLAIM_CLAUDE = br.Claim("claude", "opus55-launch-promax-20260921", "Opus launch reset",
                        datetime(2026, 10, 22, 16, 0, tzinfo=UTC),
                        ("five_hour", "seven_day", "seven_day_overage_included"))
CLAIM_CODEX = br.Claim("codex", "c-oct22", "Full reset (Monthly)",
                       datetime(2026, 10, 22, 9, 0, tzinfo=UTC))
KEY = "7d444840-9dc0-11d1-b245-5ffdce74fad2"


def claude(*answers, claim=CLAIM_CLAUDE, key=KEY, token=TOKEN):
    transport = FakeTransport(*answers)
    return rr.redeem_claude(transport, token, claim, key), transport


def codex(*answers, claim=CLAIM_CODEX, key=KEY, account="acct-1"):
    transport = FakeTransport(*answers)
    return rr.redeem_codex(transport, TOKEN, account, claim, key), transport


class TestClaudeRedeem:
    def test_it_looks_up_the_organisation_then_posts_once(self):
        outcome, t = claude(ok(PROFILE), ok({"result": "reset", "cleared": ["five_hour", "seven_day"]}))
        assert [(c["method"], c["url"]) for c in t.calls] == [("GET", PROFILE_URL), ("POST", RESET_URL)]
        assert t.posts()[0]["body"] == {"program": "cedar_ember",
                                        "grant_id": "opus55-launch-promax-20260921",
                                        "request_id": KEY}
        assert outcome.kind == "success" and outcome.windows == 2 and outcome.consumed is True

    def test_both_calls_carry_the_bearer_the_beta_header_and_the_cli_agent(self):
        _, t = claude(ok(PROFILE), ok({"result": "reset", "cleared": []}))
        for call in t.calls:
            assert call["headers"]["Authorization"] == f"Bearer {TOKEN}"
            assert call["headers"]["anthropic-beta"] == "oauth-2025-04-20"
            assert call["headers"]["User-Agent"] == oauth_usage.CLAUDE_CLI_USER_AGENT

    @pytest.mark.parametrize("result", ["reset", "already_used"])
    def test_reset_and_already_used_are_success(self, result):
        outcome, _ = claude(ok(PROFILE), ok({"result": result, "cleared": ["five_hour"]}))
        assert outcome.kind == "success" and outcome.windows == 1

    def test_not_limited_means_nothing_was_reset_and_nothing_spent(self):
        outcome, _ = claude(ok(PROFILE), ok({"result": "not_limited"}))
        assert outcome.kind == "nothing" and outcome.consumed is False

    @pytest.mark.parametrize("result", ["ineligible", "unavailable"])
    def test_ineligible_and_unavailable_mean_none_is_available(self, result):
        outcome, _ = claude(ok(PROFILE), ok({"result": result}))
        assert outcome.kind == "unavailable" and outcome.consumed is False

    def test_cooldown_is_an_error_that_says_so(self):
        outcome, _ = claude(ok(PROFILE), ok({"result": "cooldown"}))
        assert outcome.kind == "cooldown" and "cooling down" in outcome.message.lower()
        assert outcome.consumed is False

    @pytest.mark.parametrize("body", [{"result": "teleported"}, {}, {"result": None}, {"result": 7}])
    def test_an_unrecognised_answer_is_an_unknown_outcome_not_a_guess(self, body):
        outcome, _ = claude(ok(PROFILE), ok(body))
        assert outcome.kind == "unknown" and outcome.consumed is None

    def test_a_non_object_answer_is_unknown(self):
        outcome, _ = claude(ok(PROFILE), rr.HttpResult(200, None))
        assert outcome.kind == "unknown"

    def test_http_429_is_rate_limited(self):
        outcome, _ = claude(ok(PROFILE), status(429))
        assert outcome.kind == "rate_limited" and outcome.consumed is False

    def test_http_401_asks_to_sign_in_again_and_never_refreshes_tokens(self):
        outcome, t = claude(ok(PROFILE), status(401))
        assert outcome.kind == "auth" and outcome.consumed is False
        assert len(t.calls) == 2                       # no refresh call, no retry

    def test_http_403_is_ineligible(self):
        outcome, _ = claude(ok(PROFILE), status(403))
        assert outcome.kind == "ineligible" and outcome.consumed is False

    @pytest.mark.parametrize("code", [500, 502, 503, 504, 408, 409, 425, 301, 307])
    def test_a_status_that_may_have_reached_the_server_leaves_the_outcome_unknown(self, code):
        outcome, _ = claude(ok(PROFILE), status(code))
        assert outcome.kind == "unknown" and outcome.consumed is None

    @pytest.mark.parametrize("code", [400, 404, 422])
    def test_another_client_error_is_a_plain_refusal(self, code):
        outcome, _ = claude(ok(PROFILE), status(code))
        assert outcome.kind == "error" and outcome.consumed is False
        assert str(code) in outcome.message

    def test_a_network_failure_on_the_post_is_unknown_and_is_not_retried(self):
        outcome, t = claude(ok(PROFILE), rr.TransportError("timed out"), ok({"result": "reset"}))
        assert outcome.kind == "unknown" and outcome.consumed is None
        assert len(t.posts()) == 1
        assert len(t.answers) == 1                       # the third answer was never asked for

    def test_a_profile_failure_sends_nothing(self):
        for answer in (status(401), status(500), rr.TransportError("offline")):
            outcome, t = claude(answer)
            assert t.posts() == []
            assert outcome.consumed is False

    def test_a_profile_401_asks_to_sign_in_again(self):
        outcome, _ = claude(status(401))
        assert outcome.kind == "auth"

    def test_a_profile_without_an_organisation_sends_nothing(self):
        for profile in ({}, {"organization": {}}, {"organization": None}, {"organization": {"uuid": 5}}):
            outcome, t = claude(ok(profile))
            assert t.posts() == [] and outcome.kind == "error" and outcome.consumed is False

    @pytest.mark.parametrize("org", ["../evil", "a/b", "x" * 129, "", "ab cd", "ORG?x=1", "org\n"])
    def test_an_organisation_id_that_could_alter_the_url_sends_nothing(self, org):
        outcome, t = claude(ok({"organization": {"uuid": org}}))
        assert t.posts() == [] and outcome.kind == "invalid"

    @pytest.mark.parametrize("grant", ["Has Upper", "with space", "a" * 41, "", "semi;colon", "a/b", "grant\n"])
    def test_a_grant_id_outside_the_pattern_sends_nothing_at_all(self, grant):
        claim = br.Claim("claude", grant, "x", None)
        outcome, t = claude(claim=claim)
        assert t.calls == [] and outcome.kind == "invalid" and outcome.consumed is False

    @pytest.mark.parametrize("key", ["", "has space", "a" * 65, "semi;colon", "a/b", "key\n"])
    def test_a_request_id_outside_the_pattern_sends_nothing_at_all(self, key):
        outcome, t = claude(key=key)
        assert t.calls == [] and outcome.kind == "invalid"

    def test_a_claim_without_a_grant_id_sends_nothing(self):
        outcome, t = claude(claim=br.Claim("claude", None, "x", None))
        assert t.calls == [] and outcome.kind == "invalid"

    def test_the_key_a_flow_generates_passes_the_pattern(self):
        assert KEY_PATTERN.match(rr.new_request_id())


class TestCodexRedeem:
    def test_one_post_with_the_credit_and_the_request_id(self):
        outcome, t = codex(ok({"code": "reset", "windows_reset": 1}))
        assert [(c["method"], c["url"]) for c in t.calls] == [("POST", CONSUME_URL)]
        assert t.calls[0]["body"] == {"redeem_request_id": KEY, "credit_id": "c-oct22"}
        assert outcome.kind == "success" and outcome.windows == 1 and outcome.consumed is True

    def test_the_bearer_and_the_account_id_are_sent(self):
        _, t = codex(ok({"code": "reset", "windows_reset": 1}))
        headers = t.calls[0]["headers"]
        assert headers["Authorization"] == f"Bearer {TOKEN}"
        assert headers["ChatGPT-Account-ID"] == "acct-1"

    def test_no_account_id_is_simply_not_sent(self):
        _, t = codex(ok({"code": "reset"}), account=None)
        assert "ChatGPT-Account-ID" not in t.calls[0]["headers"]

    def test_without_a_credit_id_the_server_picks(self):
        claim = br.Claim("codex", None, "Banked reset", None)
        _, t = codex(ok({"code": "reset"}), claim=claim)
        assert t.calls[0]["body"] == {"redeem_request_id": KEY}

    @pytest.mark.parametrize("code", ["reset", "already_redeemed"])
    def test_reset_and_already_redeemed_are_success(self, code):
        outcome, _ = codex(ok({"code": code, "windows_reset": 2}))
        assert outcome.kind == "success"

    def test_the_number_of_windows_is_reported(self):
        outcome, _ = codex(ok({"code": "reset", "windows_reset": 3}))
        assert outcome.windows == 3 and "3" in outcome.message

    def test_nothing_to_reset_is_not_consumed(self):
        outcome, _ = codex(ok({"code": "nothing_to_reset"}))
        assert outcome.kind == "nothing" and outcome.consumed is False
        assert "stays available" in outcome.message

    def test_no_credit_means_none_is_left(self):
        outcome, _ = codex(ok({"code": "no_credit"}))
        assert outcome.kind == "unavailable" and outcome.consumed is False

    @pytest.mark.parametrize("body", [{"code": "mystery"}, {}, {"code": None}, {"code": ["reset"]}])
    def test_an_unrecognised_answer_is_unknown(self, body):
        outcome, _ = codex(ok(body))
        assert outcome.kind == "unknown" and outcome.consumed is None

    def test_401_means_the_sign_in_expired(self):
        outcome, _ = codex(status(401))
        assert outcome.kind == "auth" and "codex login" in outcome.message

    def test_403_is_ineligible(self):
        assert codex(status(403))[0].kind == "ineligible"

    def test_429_is_rate_limited(self):
        assert codex(status(429))[0].kind == "rate_limited"

    @pytest.mark.parametrize("code", [500, 502, 503, 408, 409, 425, 302, 308])
    def test_a_status_that_may_have_reached_the_server_leaves_the_outcome_unknown(self, code):
        assert codex(status(code))[0].kind == "unknown"

    def test_a_network_failure_is_unknown_and_is_not_retried(self):
        outcome, t = codex(rr.TransportError("reset by peer"), ok({"code": "reset"}))
        assert outcome.kind == "unknown" and outcome.consumed is None
        assert len(t.calls) == 1

    @pytest.mark.parametrize("credit", ["has space", "a/b", "x" * 129, "semi;colon", "credit\n"])
    def test_a_credit_id_outside_the_pattern_sends_nothing(self, credit):
        claim = br.Claim("codex", credit, "x", None)
        outcome, t = codex(claim=claim)
        assert t.calls == [] and outcome.kind == "invalid"

    @pytest.mark.parametrize("key", ["", "has space", "a" * 65, "key\n"])
    def test_a_request_id_outside_the_pattern_sends_nothing(self, key):
        outcome, t = codex(key=key)
        assert t.calls == [] and outcome.kind == "invalid"


class TestNothingLeaks:
    """Error text carries a status and a fixed sentence: never a token or a body."""

    def everything(self):
        outcomes = []
        for answers in ([status(401)], [status(500)], [status(403)], [status(429)], [status(400)],
                        [rr.TransportError(f"boom {TOKEN} {BODY_TEXT}")],
                        [ok(PROFILE), status(500)], [ok(PROFILE), status(400)],
                        [ok(PROFILE), rr.TransportError(f"boom {TOKEN}")],
                        [ok(PROFILE), ok({"result": f"{BODY_TEXT}"})],
                        [ok(PROFILE), ok({"result": "reset", "cleared": [BODY_TEXT]})]):
            outcomes.append(claude(*answers)[0])
        for answers in ([status(401)], [status(500)], [status(403)], [status(400)],
                        [rr.TransportError(f"boom {TOKEN} {BODY_TEXT}")],
                        [ok({"code": BODY_TEXT})], [ok({"code": "reset", "windows_reset": BODY_TEXT})]):
            outcomes.append(codex(*answers)[0])
        return outcomes

    def test_no_message_contains_the_token_or_a_response_body(self):
        for outcome in self.everything():
            assert TOKEN not in outcome.message
            assert BODY_TEXT not in outcome.message

    def test_every_outcome_has_a_message_and_a_known_kind(self):
        for outcome in self.everything():
            assert outcome.message and outcome.kind in rr.KINDS

    def test_the_log_line_for_an_outcome_has_no_secrets(self, caplog):
        import logging
        with caplog.at_level(logging.DEBUG, logger="claudebar"):
            for outcome in self.everything():
                rr.log_outcome("claude", outcome)
        assert TOKEN not in caplog.text and BODY_TEXT not in caplog.text


class TestFlow:
    def flow(self):
        keys = iter(f"key-{i}" for i in range(100))
        return rr.RedeemFlow(new_key=lambda: next(keys))

    def test_nothing_is_sent_until_the_user_confirms(self):
        flow = self.flow()
        assert flow.state == "idle"
        assert flow.confirm(CLAIM_CLAUDE) is None
        assert flow.request(CLAIM_CLAUDE) is True
        assert flow.state == "confirming"

    def test_one_confirmation_gives_exactly_one_attempt(self):
        flow = self.flow()
        flow.request(CLAIM_CLAUDE)
        attempt = flow.confirm(CLAIM_CLAUDE)
        assert attempt is not None and flow.state == "sending" and flow.in_flight
        assert flow.confirm(CLAIM_CLAUDE) is None            # a double click sends nothing more

    def test_the_attempt_names_what_to_spend(self):
        flow = self.flow()
        flow.request(CLAIM_CODEX)
        attempt = flow.confirm(CLAIM_CODEX)
        assert (attempt.claim, attempt.request_id) == (CLAIM_CODEX, "key-0")

    def test_cancelling_sends_nothing(self):
        flow = self.flow()
        flow.request(CLAIM_CLAUDE)
        flow.cancel()
        assert flow.state == "idle"
        assert flow.confirm(CLAIM_CLAUDE) is None

    def test_a_claim_that_changed_while_the_dialog_was_open_is_refused(self):
        flow = self.flow()
        flow.request(CLAIM_CLAUDE)
        assert flow.confirm(CLAIM_CODEX) is None             # what is on offer is not what was shown
        assert flow.state == "idle"

    def test_no_claim_at_confirmation_is_refused(self):
        flow = self.flow()
        flow.request(CLAIM_CLAUDE)
        assert flow.confirm(None) is None
        assert flow.state == "idle"

    def test_a_request_with_nothing_to_spend_is_ignored(self):
        flow = self.flow()
        assert flow.request(None) is False and flow.state == "idle"

    def test_no_new_request_while_one_is_in_flight(self):
        flow = self.flow()
        flow.request(CLAIM_CLAUDE)
        flow.confirm(CLAIM_CLAUDE)
        assert flow.request(CLAIM_CLAUDE) is False
        assert flow.state == "sending"

    def test_a_finished_attempt_frees_the_flow(self):
        flow = self.flow()
        flow.request(CLAIM_CLAUDE)
        flow.confirm(CLAIM_CLAUDE)
        flow.finish(rr.RedeemOutcome("success", "done", 1, True))
        assert flow.state == "done" and not flow.in_flight
        assert flow.request(CLAIM_CLAUDE) is True

    def test_a_new_attempt_after_a_definite_result_gets_a_new_key(self):
        flow = self.flow()
        flow.request(CLAIM_CLAUDE)
        first = flow.confirm(CLAIM_CLAUDE)
        flow.finish(rr.RedeemOutcome("nothing", "nothing to reset", 0, False))
        flow.request(CLAIM_CLAUDE)
        assert flow.confirm(CLAIM_CLAUDE).request_id != first.request_id

    def test_after_an_unknown_result_a_retry_of_the_same_reset_reuses_the_key(self):
        flow = self.flow()
        flow.request(CLAIM_CLAUDE)
        first = flow.confirm(CLAIM_CLAUDE)
        flow.finish(rr.RedeemOutcome("unknown", "result unknown", 0, None))
        assert flow.state == "unknown" and flow.needs_refresh
        flow.request(CLAIM_CLAUDE)
        assert flow.confirm(CLAIM_CLAUDE).request_id == first.request_id

    def test_a_different_reset_after_an_unknown_result_is_a_new_attempt(self):
        flow = self.flow()
        flow.request(CLAIM_CLAUDE)
        first = flow.confirm(CLAIM_CLAUDE)
        flow.finish(rr.RedeemOutcome("unknown", "result unknown", 0, None))
        other = br.Claim("claude", "another-grant", "x", None)
        flow.request(other)
        assert flow.confirm(other).request_id != first.request_id

    def test_the_other_provider_after_an_unknown_result_is_a_new_attempt(self):
        flow = self.flow()
        flow.request(CLAIM_CLAUDE)
        first = flow.confirm(CLAIM_CLAUDE)
        flow.finish(rr.RedeemOutcome("unknown", "result unknown", 0, None))
        flow.request(CLAIM_CODEX)
        assert flow.confirm(CLAIM_CODEX).request_id != first.request_id

    def test_cancelling_after_an_unknown_result_keeps_the_key_for_the_retry(self):
        flow = self.flow()
        flow.request(CLAIM_CLAUDE)
        first = flow.confirm(CLAIM_CLAUDE)
        flow.finish(rr.RedeemOutcome("unknown", "result unknown", 0, None))
        flow.request(CLAIM_CLAUDE)
        flow.cancel()
        flow.request(CLAIM_CLAUDE)
        assert flow.confirm(CLAIM_CLAUDE).request_id == first.request_id

    def test_a_definite_result_clears_the_unknown_memory(self):
        flow = self.flow()
        flow.request(CLAIM_CLAUDE)
        first = flow.confirm(CLAIM_CLAUDE)
        flow.finish(rr.RedeemOutcome("unknown", "result unknown", 0, None))
        flow.request(CLAIM_CLAUDE)
        flow.confirm(CLAIM_CLAUDE)
        flow.finish(rr.RedeemOutcome("success", "ok", 1, True))
        flow.request(CLAIM_CLAUDE)
        assert flow.confirm(CLAIM_CLAUDE).request_id != first.request_id
        assert not flow.needs_refresh

    @pytest.mark.parametrize("kind", ["auth", "invalid", "error", "rate_limited", "ineligible"])
    def test_a_retry_that_never_got_an_answer_keeps_the_key_for_the_next_retry(self, kind):
        flow = self.flow()
        flow.request(CLAIM_CLAUDE)
        first = flow.confirm(CLAIM_CLAUDE)
        flow.finish(rr.RedeemOutcome("unknown", "result unknown", 0, None))
        flow.request(CLAIM_CLAUDE)
        flow.confirm(CLAIM_CLAUDE)
        flow.finish(rr.RedeemOutcome(kind, "no answer about the reset", 0, False))
        assert flow.needs_refresh
        flow.request(CLAIM_CLAUDE)
        assert flow.confirm(CLAIM_CLAUDE).request_id == first.request_id

    @pytest.mark.parametrize("kind", ["success", "nothing", "unavailable", "cooldown"])
    def test_a_server_answer_about_the_same_reset_ends_the_unknown(self, kind):
        flow = self.flow()
        flow.request(CLAIM_CLAUDE)
        first = flow.confirm(CLAIM_CLAUDE)
        flow.finish(rr.RedeemOutcome("unknown", "result unknown", 0, None))
        flow.request(CLAIM_CLAUDE)
        flow.confirm(CLAIM_CLAUDE)
        flow.finish(rr.RedeemOutcome(kind, "answered", 0, False))
        assert not flow.needs_refresh
        flow.request(CLAIM_CLAUDE)
        assert flow.confirm(CLAIM_CLAUDE).request_id != first.request_id

    def test_another_resets_answer_does_not_end_the_unknown(self):
        flow = self.flow()
        flow.request(CLAIM_CLAUDE)
        first = flow.confirm(CLAIM_CLAUDE)
        flow.finish(rr.RedeemOutcome("unknown", "result unknown", 0, None))
        flow.request(CLAIM_CODEX)
        flow.confirm(CLAIM_CODEX)
        flow.finish(rr.RedeemOutcome("success", "ok", 1, True))
        assert flow.needs_refresh
        flow.request(CLAIM_CLAUDE)
        assert flow.confirm(CLAIM_CLAUDE).request_id == first.request_id

    def test_two_unknown_resets_are_both_remembered(self):
        flow = self.flow()
        flow.request(CLAIM_CLAUDE)
        claude_key = flow.confirm(CLAIM_CLAUDE).request_id
        flow.finish(rr.RedeemOutcome("unknown", "result unknown", 0, None))
        flow.request(CLAIM_CODEX)
        codex_key = flow.confirm(CLAIM_CODEX).request_id
        flow.finish(rr.RedeemOutcome("unknown", "result unknown", 0, None))
        flow.request(CLAIM_CLAUDE)
        assert flow.confirm(CLAIM_CLAUDE).request_id == claude_key
        flow.finish(rr.RedeemOutcome("unknown", "result unknown", 0, None))
        flow.request(CLAIM_CODEX)
        assert flow.confirm(CLAIM_CODEX).request_id == codex_key

    def test_finish_when_nothing_is_in_flight_changes_nothing(self):
        flow = self.flow()
        flow.finish(rr.RedeemOutcome("success", "ok", 1, True))
        assert flow.state == "idle"

    def test_the_default_keys_are_unique_and_valid(self):
        keys = {rr.new_request_id() for _ in range(50)}
        assert len(keys) == 50 and all(KEY_PATTERN.match(k) for k in keys)


class TestConfirmText:
    def test_claude(self):
        lines = rr.confirm_lines(CLAUDE_CLAIM := CLAIM_CLAUDE, "session 12%, weekly 39%", NOW,
                                 tz=UTC)
        assert lines[0] == "Opus launch reset"
        assert "Expires Oct 22, 16:00 (in 10 d)" in lines
        assert "Clears the 5-hour limit and the weekly limit" in lines
        assert "Current usage: session 12%, weekly 39%" in lines
        assert lines[-1] == "This uses the reset now and cannot be undone."

    def test_codex(self):
        lines = rr.confirm_lines(CLAIM_CODEX, "weekly 4%", NOW, tz=UTC)
        assert lines[0] == "Full reset (Monthly)"
        assert "Expires Oct 22, 09:00 (in 10 d)" in lines
        assert "Refreshes your Codex usage limits" in lines
        assert "Current usage: weekly 4%" in lines

    def test_no_expiry_known(self):
        lines = rr.confirm_lines(br.Claim("codex", None, "Banked reset", None), "weekly 4%", NOW, tz=UTC)
        assert all(not line.startswith("Expires") for line in lines)

    def test_the_expiry_is_in_the_zone_asked_for(self):
        lines = rr.confirm_lines(CLAIM_CLAUDE, "x", NOW, tz=timezone(timedelta(hours=3)))
        assert "Expires Oct 22, 19:00 (in 10 d)" in lines

    @pytest.mark.parametrize("clears,text", [
        (("five_hour",), "Clears the 5-hour limit"),
        (("seven_day",), "Clears the weekly limit"),
        (("five_hour", "seven_day"), "Clears the 5-hour limit and the weekly limit"),
        (("seven_day_overage_included",), "Clears the weekly limit (extra usage included)"),
        (("five_hour", "seven_day", "seven_day_overage_included"),
         "Clears the 5-hour limit and the weekly limit"),
        (("something_new",), "Clears your usage limits"),
        ((), "Clears your usage limits")])
    def test_what_it_clears(self, clears, text):
        claim = br.Claim("claude", "g", "x", None, clears)
        assert text in rr.confirm_lines(claim, "u", NOW, tz=UTC)


class TestRunAttempt:
    """run_attempt reads the token fresh, then hands over to the right client."""

    def attempt(self, claim):
        return rr.Attempt(claim, KEY)

    def test_claude_without_a_token_sends_nothing_and_says_to_sign_in(self, monkeypatch):
        monkeypatch.setattr(oauth_usage, "access_token_or_reason", lambda: (None, "token expired"))
        transport = FakeTransport()
        outcome = rr.run_attempt(self.attempt(CLAIM_CLAUDE), transport)
        assert outcome.kind == "auth" and transport.calls == []

    def test_codex_without_a_token_sends_nothing_and_says_to_sign_in(self, monkeypatch):
        import openai_usage
        monkeypatch.setattr(openai_usage, "load_codex_token", lambda: None)
        transport = FakeTransport()
        outcome = rr.run_attempt(self.attempt(CLAIM_CODEX), transport)
        assert outcome.kind == "auth" and "codex login" in outcome.message and transport.calls == []

    def test_claude_goes_to_the_claude_client(self, monkeypatch):
        monkeypatch.setattr(oauth_usage, "access_token_or_reason", lambda: (TOKEN, None))
        transport = FakeTransport(ok(PROFILE), ok({"result": "reset", "cleared": ["five_hour"]}))
        outcome = rr.run_attempt(self.attempt(CLAIM_CLAUDE), transport)
        assert outcome.kind == "success" and len(transport.posts()) == 1

    def test_codex_goes_to_the_codex_client_with_the_account_id(self, monkeypatch):
        import openai_usage
        monkeypatch.setattr(openai_usage, "load_codex_token", lambda: TOKEN)
        monkeypatch.setattr(openai_usage, "load_codex_account_id", lambda: "acct-9")
        transport = FakeTransport(ok({"code": "reset", "windows_reset": 1}))
        outcome = rr.run_attempt(self.attempt(CLAIM_CODEX), transport)
        assert outcome.kind == "success"
        assert transport.calls[0]["headers"]["ChatGPT-Account-ID"] == "acct-9"

    def test_an_unknown_provider_sends_nothing(self):
        transport = FakeTransport()
        outcome = rr.run_attempt(self.attempt(br.Claim("mars", "x", "x", None)), transport)
        assert outcome.kind == "invalid" and transport.calls == []
