"""Spending a banked reset.

A reset is spent by one irreversible request, so this module is built around
not sending it by accident and not sending it twice:

* the clients sit behind a small transport interface, so they are tested with
  a fake that replays recorded response shapes and no test ever reaches a real
  endpoint;
* every id is checked against the documented pattern before anything is sent;
* a request is never retried here. A network failure or a server error leaves
  the outcome unknown, and the caller must say so rather than try again;
* RedeemFlow is the confirm, idempotency and in-flight rules as a plain state
  machine: nothing is sent before an explicit confirmation, one confirmation
  yields one attempt, and the idempotency key is reused only when the same
  reset is retried after an unknown result;
* error text is a status and a fixed sentence. Tokens and response bodies are
  never put in a message or a log line.

OAuth tokens are only read here, never refreshed: refreshing would rotate the
Claude CLI's own tokens, so a 401 asks for a fresh sign-in instead.
"""

import json
import logging
import re
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Optional, Protocol

import banked_resets
from banked_resets import Claim

logger = logging.getLogger("claudebar")

TIMEOUT_SECONDS = 15

PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
CLAUDE_RESET_URL = "https://api.anthropic.com/api/organizations/{org}/reset_rate_limits"
CODEX_CONSUME_URL = "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits/consume"

# What each id may look like before it is allowed into a URL or a body. They are
# used with fullmatch: `$` would also accept a trailing newline.
GRANT_ID = re.compile(r"[a-z0-9_-]{1,40}")
REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
ORG_ID = re.compile(r"[A-Za-z0-9-]{1,128}")
CREDIT_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}")

KINDS = ("success", "nothing", "unavailable", "cooldown", "rate_limited", "auth",
         "ineligible", "error", "invalid", "unknown")

# The kinds that are the server's answer about the reset itself. Anything else
# (sign-in, a refused id, a rate limit, a plain refusal) says nothing about
# whether an earlier unknown request went through.
ANSWERED = ("success", "nothing", "unavailable", "cooldown")


class TransportError(Exception):
    """The request failed on the way (no answer): its effect is unknown."""


@dataclass(frozen=True)
class HttpResult:
    status: int
    data: Any = None          # the parsed JSON body, or None


class Transport(Protocol):
    def request(self, method: str, url: str, headers: dict, body: Optional[dict],
                timeout: float) -> HttpResult: ...


class UrllibTransport:
    """The real transport. An HTTP error status is an answer; anything else
    that goes wrong is a TransportError carrying only the exception's type."""

    def request(self, method, url, headers, body, timeout=TIMEOUT_SECONDS) -> HttpResult:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return HttpResult(response.status, self._parse(response.read()))
        except urllib.error.HTTPError as e:
            try:
                return HttpResult(e.code, self._parse(e.read()))
            except OSError:
                return HttpResult(e.code, None)
        except Exception as e:     # URLError, timeouts, resets, malformed responses
            raise TransportError(type(e).__name__) from None

    @staticmethod
    def _parse(raw: bytes):
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            return None


@dataclass(frozen=True)
class RedeemOutcome:
    kind: str                      # one of KINDS
    message: str                   # a fixed sentence: safe to show and to log
    windows: int = 0               # limits the reset refreshed
    consumed: Optional[bool] = None  # True spent, False not spent, None unknown


def new_request_id() -> str:
    return str(uuid.uuid4())


def _success(windows: int) -> RedeemOutcome:
    detail = "" if windows <= 0 else f" · {windows} limit{'s' if windows != 1 else ''} refreshed"
    return RedeemOutcome("success", "Reset applied" + detail, windows, True)


NOTHING = RedeemOutcome("nothing", "Nothing to reset right now · the reset stays available",
                        0, False)
UNAVAILABLE = RedeemOutcome("unavailable", "No banked reset is available", 0, False)
COOLDOWN = RedeemOutcome("cooldown", "Cooling down · try again later", 0, False)
RATE_LIMITED = RedeemOutcome("rate_limited", "Rate limited · try again in a few minutes", 0, False)
INELIGIBLE = RedeemOutcome("ineligible", "This account can't use banked resets", 0, False)
UNKNOWN = RedeemOutcome("unknown", "Result unknown · refresh before trying again", 0, None)
INVALID = RedeemOutcome("invalid", "Can't send · unexpected reset id", 0, False)
NOT_SENT = RedeemOutcome("error", "Couldn't send · nothing was sent", 0, False)


def _auth(provider: str) -> RedeemOutcome:
    text = ("Codex sign-in expired · run 'codex login', then refresh" if provider == "codex"
            else "Sign in to Claude Code again, then refresh")
    return RedeemOutcome("auth", text, 0, False)


def _refused(code: int) -> RedeemOutcome:
    return RedeemOutcome("error", f"Refused (HTTP {code}) · nothing was spent", 0, False)


def _failure(result: HttpResult, provider: str) -> Optional[RedeemOutcome]:
    """The outcome for a response that is not an answer to the question asked,
    or None when it is a 2xx whose body has to be read."""
    if 200 <= result.status < 300:
        return None
    if result.status == 401:
        return _auth(provider)
    if result.status == 403:
        return INELIGIBLE
    if result.status == 429:
        return RATE_LIMITED
    if result.status >= 500 or result.status in (408, 409, 425) or 300 <= result.status < 400:
        return UNKNOWN              # the server may have acted: timeout, conflict, early, redirect
    return _refused(result.status)


def log_outcome(provider: str, outcome: RedeemOutcome) -> None:
    """One line: the provider and the kind. No ids, tokens or bodies."""
    logger.info("Banked reset (%s): %s", provider, outcome.kind)


def redeem_claude(transport: Transport, access_token: str, claim: Claim,
                  request_id: str) -> RedeemOutcome:
    """GET the organisation, then POST the reset once."""
    from oauth_usage import claude_headers      # the one place the CLI user-agent lives

    grant_id = claim.credit_id
    if not isinstance(grant_id, str) or not GRANT_ID.fullmatch(grant_id) \
            or not REQUEST_ID.fullmatch(request_id):
        return INVALID
    headers = claude_headers(access_token)

    try:
        profile = transport.request("GET", PROFILE_URL, headers, None, TIMEOUT_SECONDS)
    except TransportError:
        return RedeemOutcome("error", "Couldn't reach Anthropic · nothing was sent", 0, False)
    if profile.status == 401:
        return _auth("claude")
    if not 200 <= profile.status < 300:
        return RedeemOutcome("error", "Couldn't read the account · nothing was sent", 0, False)
    organization = profile.data.get("organization") if isinstance(profile.data, dict) else None
    org = organization.get("uuid") if isinstance(organization, dict) else None
    if not isinstance(org, str):
        return RedeemOutcome("error", "Couldn't read the account · nothing was sent", 0, False)
    if not ORG_ID.fullmatch(org):
        return INVALID

    body = {"program": "cedar_ember", "grant_id": grant_id, "request_id": request_id}
    try:
        result = transport.request("POST", CLAUDE_RESET_URL.format(org=org), headers, body,
                                   TIMEOUT_SECONDS)
    except TransportError:
        return UNKNOWN
    failure = _failure(result, "claude")
    if failure is not None:
        return failure

    answer = result.data.get("result") if isinstance(result.data, dict) else None
    if answer in ("reset", "already_used"):
        cleared = result.data.get("cleared")
        return _success(len(cleared) if isinstance(cleared, list) else 0)
    if answer == "not_limited":
        return NOTHING
    if answer in ("ineligible", "unavailable"):
        return UNAVAILABLE
    if answer == "cooldown":
        return COOLDOWN
    return UNKNOWN


def redeem_codex(transport: Transport, access_token: str, account_id: Optional[str],
                 claim: Claim, request_id: str) -> RedeemOutcome:
    """POST the consume request once."""
    from openai_usage import chatgpt_headers

    credit_id = claim.credit_id
    if (credit_id is not None and (not isinstance(credit_id, str) or not CREDIT_ID.fullmatch(credit_id))) \
            or not REQUEST_ID.fullmatch(request_id):
        return INVALID
    body = {"redeem_request_id": request_id}
    if credit_id is not None:
        body["credit_id"] = credit_id
    try:
        result = transport.request("POST", CODEX_CONSUME_URL,
                                   chatgpt_headers(access_token, account_id), body, TIMEOUT_SECONDS)
    except TransportError:
        return UNKNOWN
    failure = _failure(result, "codex")
    if failure is not None:
        return failure

    code = result.data.get("code") if isinstance(result.data, dict) else None
    if code in ("reset", "already_redeemed"):
        return _success(banked_resets._whole(result.data.get("windows_reset")) or 0)
    if code == "nothing_to_reset":
        return NOTHING
    if code == "no_credit":
        return UNAVAILABLE
    return UNKNOWN


@dataclass(frozen=True)
class Attempt:
    """One confirmed request: what to spend and the idempotency key to send."""
    claim: Claim
    request_id: str

    @property
    def provider(self) -> str:
        return self.claim.provider


def run_attempt(attempt: Attempt, transport: Optional[Transport] = None) -> RedeemOutcome:
    """Read the credentials fresh and send the attempt. Blocking: call it from a
    worker thread. With no usable token nothing is sent."""
    transport = transport or UrllibTransport()
    if attempt.provider == "claude":
        import oauth_usage
        token, _ = oauth_usage.access_token_or_reason()
        outcome = (redeem_claude(transport, token, attempt.claim, attempt.request_id)
                   if token else _auth("claude"))
    elif attempt.provider == "codex":
        import openai_usage
        token = openai_usage.load_codex_token()
        outcome = (redeem_codex(transport, token, openai_usage.load_codex_account_id(),
                                attempt.claim, attempt.request_id)
                   if token else _auth("codex"))
    else:
        outcome = INVALID
    log_outcome(attempt.provider, outcome)
    return outcome


class RedeemFlow:
    """Confirm, idempotency and in-flight rules for spending a reset.

    idle -> confirming (the user pressed Use) -> sending (they confirmed) ->
    done, or unknown when the result could not be learned. Only confirm() hands
    out an Attempt, and only from "confirming", so one confirmation is at most
    one request. After an unknown result the same reset keeps its key, so a
    retry the user chooses to make cannot spend a second one. The key is
    forgotten only when the server answers about that same reset: a retry that
    fails before an answer (sign-in, a refused id, a rate limit) leaves the
    first request's fate as unknown as it was.
    """

    def __init__(self, new_key: Callable[[], str] = new_request_id):
        self.state = "idle"
        self._new_key = new_key
        self._claim: Optional[Claim] = None
        self._key: Optional[str] = None
        self._unknown: dict = {}    # (provider, credit_id) -> key of a request whose result is unlearned

    @property
    def in_flight(self) -> bool:
        return self.state == "sending"

    @property
    def needs_refresh(self) -> bool:
        """The last attempt's result is unknown: refresh before trying again."""
        return bool(self._unknown)

    def request(self, claim: Optional[Claim]) -> bool:
        """The user pressed Use. True when a confirmation should be shown."""
        if claim is None or self.in_flight:
            return False
        self._claim = claim
        self._key = self._unknown.get((claim.provider, claim.credit_id)) or self._new_key()
        self.state = "confirming"
        return True

    def confirm(self, current: Optional[Claim]) -> Optional[Attempt]:
        """The user confirmed. `current` is what is on offer now: if it is not
        what was shown in the confirmation, nothing is sent."""
        if self.state != "confirming":
            return None
        if current is None or current != self._claim:
            self.state = "idle"
            return None
        self.state = "sending"
        return Attempt(self._claim, self._key)

    def cancel(self) -> None:
        if self.state == "confirming":
            self.state = "idle"

    def finish(self, outcome: RedeemOutcome) -> None:
        if self.state != "sending":
            return
        reset = (self._claim.provider, self._claim.credit_id)
        if outcome.kind == "unknown":
            self._unknown[reset] = self._key
            self.state = "unknown"
        else:
            if outcome.kind in ANSWERED:
                self._unknown.pop(reset, None)
            self.state = "done"


def _clears_text(clears: tuple) -> str:
    parts = []
    if "five_hour" in clears:
        parts.append("the 5-hour limit")
    if "seven_day" in clears:
        parts.append("the weekly limit")
    elif "seven_day_overage_included" in clears:
        parts.append("the weekly limit (extra usage included)")
    return "Clears " + " and ".join(parts) if parts else "Clears your usage limits"


def confirm_lines(claim: Claim, usage_text: str, now: datetime, tz=None) -> list:
    """What the confirmation says: which reset, when it expires, what it
    clears, the current usage, and that it cannot be undone."""
    lines = [claim.label or "Banked reset"]
    if claim.expires_at is not None:
        local = claim.expires_at.astimezone(tz)
        lines.append(f"Expires {local:%b} {local.day}, {local:%H:%M} "
                     f"(in {banked_resets._remaining_text(claim.expires_at - now)})")
    lines.append("Refreshes your Codex usage limits" if claim.provider == "codex"
                 else _clears_text(claim.clears))
    lines.append(f"Current usage: {usage_text}")
    lines.append("This uses the reset now and cannot be undone.")
    return lines
