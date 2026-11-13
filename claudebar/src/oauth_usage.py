"""Fetch Claude usage data via OAuth API."""

import json
import logging
import time
import urllib.error
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from pathlib import Path

import http_json
from banked_resets import BankedResets, CLAUDE, parse_claude_banked
from models import reset_words
from validation import safe_get_float

logger = logging.getLogger("claudebar")

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"

# Anthropic only hands the banked-reset data (`cedar_ember`) to the Claude CLI's
# own user-agent; with ours it answers `eligible: false, reason: surface`. The
# user approved sending it. This is the one place the version lives (the profile
# and redeem calls import it): the CLI's version is not read anywhere cheap, and
# running `claude --version` on every refresh is not.
CLAUDE_CLI_VERSION = "2.1.296"
CLAUDE_CLI_USER_AGENT = f"claude-cli/{CLAUDE_CLI_VERSION} (external, cli)"
LEGACY_USER_AGENT = "ClaudeBar/1.0"


@dataclass
class UsageWindow:
    """Usage window with utilization and reset time."""
    utilization: float  # API returns percentage directly (0-100)
    resets_at: Optional[datetime] = None

    @property
    def percent(self) -> float:
        """Get utilization as percentage (0-100)."""
        # Anthropic OAuth API returns utilization already as percentage (0-100)
        # Capped at 100 for display purposes (can exceed with extra usage)
        return min(100.0, self.utilization)

    @property
    def reset_str(self) -> Optional[str]:
        """Get human-readable reset time."""
        return reset_words(self.resets_at)


@dataclass
class ExtraUsage:
    """Extra usage (paid overage) information."""
    enabled: bool = False
    monthly_limit: float = 0.0  # In actual currency (already divided from cents)
    used_credits: float = 0.0   # In actual currency (already divided from cents)
    utilization: float = 0.0    # Percentage from API (0-100)
    currency: str = "usd"

    @property
    def percent(self) -> float:
        """Get utilization as percentage (0-100)."""
        return min(100.0, self.utilization)


@dataclass
class OAuthUsageData:
    """Complete OAuth usage data."""
    session: Optional[UsageWindow] = None  # five_hour
    weekly: Optional[UsageWindow] = None   # seven_day
    extra: Optional[ExtraUsage] = None
    banked: Optional[BankedResets] = None  # saved one-time resets, when the response has them
    error: Optional[str] = None

    @property
    def is_valid(self) -> bool:
        """Check if we have valid usage data."""
        return self.error is None and (self.session is not None or self.weekly is not None)


def get_credentials_path() -> Path:
    """Get path to Claude credentials file."""
    return Path.home() / ".claude" / ".credentials.json"


# Skew before expiry: don't hand out a token that could die mid-request.
SKEW_MS = 30_000


def access_token_or_reason() -> tuple[Optional[str], Optional[str]]:
    """Return the access token, or None plus the reason it is unusable.

    ClaudeBar only reads the credentials file Claude Code owns, so "no token"
    covers four situations that need different fixes: no file at all, a file
    without the claudeAiOauth block, a block without an accessToken, and a
    token past its expiry. Each gets its own sentence so the log says which.
    """
    creds_path = get_credentials_path()

    if not creds_path.exists():
        return None, f"no credentials file at {creds_path}"

    try:
        with open(creds_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError, TypeError):
        return None, f"credentials file at {creds_path} could not be read"

    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if not isinstance(oauth, dict) or not oauth.get("accessToken"):
        return None, "credentials file holds no Claude OAuth token"

    # A JSON bool is an int subclass, so a non-numeric expiresAt means "no
    # expiry" rather than a bogus date.
    expires_at_ms = oauth.get("expiresAt")
    if isinstance(expires_at_ms, bool) or not isinstance(expires_at_ms, (int, float)):
        expires_at_ms = None

    if expires_at_ms is not None and time.time() * 1000 >= (expires_at_ms - SKEW_MS):
        expired = datetime.fromtimestamp(expires_at_ms / 1000)
        return None, (f"OAuth token expired at {expired:%Y-%m-%d %H:%M}, "
                      "run Claude Code once to refresh it")

    return oauth["accessToken"], None


def parse_reset_time(resets_at_str: Optional[str]) -> Optional[datetime]:
    """Parse ISO8601 reset time string."""
    if not resets_at_str or not isinstance(resets_at_str, str):
        return None

    try:
        # Handle ISO8601 format with Z suffix
        if resets_at_str.endswith("Z"):
            resets_at_str = resets_at_str[:-1] + "+00:00"
        return datetime.fromisoformat(resets_at_str)
    except (ValueError, TypeError):
        return None


def claude_headers(access_token: str, user_agent: str = CLAUDE_CLI_USER_AGENT) -> dict:
    """Headers for a call to Anthropic's OAuth API (usage, profile, redeem)."""
    return {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": user_agent,
    }


def _get_usage_json(url: str, access_token: str, user_agent: str) -> dict:
    return http_json.request_json(url, claude_headers(access_token, user_agent), timeout=10)


def _window(raw) -> Optional[UsageWindow]:
    """One window of the reply, or None. The API sends null for a window the plan has
    none of, and a window can carry a null utilization: neither is a reading, and a None
    percent would raise wherever it is formatted."""
    if not isinstance(raw, dict):
        return None
    utilization = raw.get("utilization", 0.0)
    if isinstance(utilization, bool) or not isinstance(utilization, (int, float)):
        return None
    return UsageWindow(utilization=utilization, resets_at=parse_reset_time(raw.get("resets_at")))


def parse_usage_response(data: dict, banked_failed: bool = False) -> OAuthUsageData:
    """The usage reply as windows, extra usage and banked resets. The same JSON arrives from
    the endpoint and from the Claude CLI's saved copy of it, so both read it here."""
    session = _window(data.get("five_hour"))
    weekly = _window(data.get("seven_day"))

    # Parse extra usage
    # API returns monthly_limit and used_credits in CENTS, divide by 100
    extra = None
    eu = data.get("extra_usage")
    if isinstance(eu, dict):
        extra = ExtraUsage(
            enabled=eu.get("is_enabled") or False,
            monthly_limit=safe_get_float(eu, "monthly_limit") / 100.0,  # cents → currency
            used_credits=safe_get_float(eu, "used_credits") / 100.0,    # cents → currency
            utilization=safe_get_float(eu, "utilization"),              # API returns percentage
            currency=eu.get("currency") or "usd",
        )

    if banked_failed:
        banked = BankedResets(provider=CLAUDE, state="failed")
    else:
        banked = parse_claude_banked(data.get("cedar_ember"), datetime.now(timezone.utc))

    return OAuthUsageData(
        session=session,
        weekly=weekly,
        extra=extra,
        banked=banked,
        # Every window null is a reply that says nothing: say so, so the log reads
        # "OAuth: <reason>" and not "OAuth: None".
        error=None if session is not None or weekly is not None
        else "The usage reply holds no session or weekly window",
    )


def fetch_oauth_usage(access_token: Optional[str] = None) -> OAuthUsageData:
    """Fetch usage data from Anthropic OAuth API.

    One request, which also asks for the banked resets. A 400 or 403 repeats it
    once as the old plain call, so the quota windows survive a refusal of the
    new query; the banked data is then "failed to load". Any other failure
    (401, 429, a server error, no network) is not retried, so a struggling
    endpoint is never called twice per refresh.
    """
    if access_token is None:
        access_token, reason = access_token_or_reason()
        if not access_token:
            return OAuthUsageData(error=reason or "No OAuth token available")

    try:
        banked_failed = False
        try:
            data = _get_usage_json(f"{USAGE_URL}?cedar_ember=1", access_token, CLAUDE_CLI_USER_AGENT)
        except urllib.error.HTTPError as e:
            if e.code not in (400, 403):
                raise
            logger.debug("Usage call with banked resets answered HTTP %d, repeating it plain", e.code)
            data = _get_usage_json(USAGE_URL, access_token, LEGACY_USER_AGENT)
            banked_failed = True

        return parse_usage_response(data, banked_failed)

    except urllib.error.HTTPError as e:
        if e.code == 401:
            return OAuthUsageData(error="OAuth token expired or invalid")
        if e.code == 429:
            # Check Retry-After header: 0 means permanent block (fingerprinting),
            # non-zero means transient rate limit
            retry_after = e.headers.get("Retry-After", "") if e.headers else ""
            if retry_after == "0":
                return OAuthUsageData(error="API access restricted (429)")
            return OAuthUsageData(error=f"Rate limited (retry after {retry_after}s)")
        return OAuthUsageData(error=f"HTTP {e.code}: {e.reason}")
    except urllib.error.URLError as e:
        return OAuthUsageData(error=f"Network error: {e.reason}")
    except json.JSONDecodeError:
        return OAuthUsageData(error="Invalid JSON response")
    except Exception as e:
        return OAuthUsageData(error=str(e))
