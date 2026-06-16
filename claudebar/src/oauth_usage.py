"""Fetch Claude usage data via OAuth API."""

import json
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from pathlib import Path

from retry import with_retry
from validation import safe_get_float


@dataclass
class UsageWindow:
    """Usage window with utilization and reset time."""
    utilization: float  # API returns percentage directly (0-100)
    resets_at: Optional[datetime] = None
    raw_reset_text: Optional[str] = None  # Direct text from CLI PTY parsing

    @property
    def percent(self) -> float:
        """Get utilization as percentage (0-100)."""
        # Anthropic OAuth API returns utilization already as percentage (0-100)
        # Capped at 100 for display purposes (can exceed with extra usage)
        return min(100.0, self.utilization)

    @property
    def reset_str(self) -> Optional[str]:
        """Get human-readable reset time."""
        if self.raw_reset_text:
            return self.raw_reset_text
        if not self.resets_at:
            return None

        now = datetime.now(self.resets_at.tzinfo) if self.resets_at.tzinfo else datetime.now()
        diff = self.resets_at - now

        if diff.total_seconds() <= 0:
            return "soon"

        hours = int(diff.total_seconds() // 3600)
        minutes = int((diff.total_seconds() % 3600) // 60)

        if hours >= 24:
            days = hours // 24
            hours = hours % 24
            return f"in {days}d {hours}h"
        elif hours > 0:
            return f"in {hours}h {minutes}m"
        else:
            return f"in {minutes}m"


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
    plan_type: Optional[str] = None  # subscription tier
    error: Optional[str] = None

    @property
    def is_valid(self) -> bool:
        """Check if we have valid usage data."""
        return self.error is None and (self.session is not None or self.weekly is not None)


def get_credentials_path() -> Path:
    """Get path to Claude credentials file."""
    return Path.home() / ".claude" / ".credentials.json"


def load_credentials() -> tuple[Optional[str], Optional[float]]:
    """Load OAuth credentials from file.

    Returns:
        Tuple of (access_token, expires_at_ms). expires_at_ms is epoch
        milliseconds when present and numeric, else None.
    """
    creds_path = get_credentials_path()

    if not creds_path.exists():
        return None, None

    try:
        with open(creds_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        oauth = data.get("claudeAiOauth")
        if not isinstance(oauth, dict):
            oauth = {}

        access_token = oauth.get("accessToken")

        # Only treat expiresAt as expiry when it is a real number (a JSON bool is
        # an int subclass in Python, so exclude it); tolerate None/string/missing.
        expires_at = oauth.get("expiresAt")
        if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
            expires_at = None

        return access_token, expires_at
    except (json.JSONDecodeError, OSError, TypeError):
        return None, None


# Skew before expiry: don't hand out a token that could die mid-request.
SKEW_MS = 30_000


def load_access_token() -> Optional[str]:
    """Return the current OAuth access token, or None if absent/expired.

    ClaudeBar is a passive reader - Claude Code owns token refresh on the shared
    credentials file. We re-read fresh each call and skip a token within SKEW_MS
    of expiry; on a token gap the caller falls back to cached usage data.
    """
    access_token, expires_at_ms = load_credentials()

    if not access_token:
        return None

    if expires_at_ms is not None and time.time() * 1000 >= (expires_at_ms - SKEW_MS):
        return None

    return access_token


def parse_reset_time(resets_at_str: Optional[str]) -> Optional[datetime]:
    """Parse ISO8601 reset time string."""
    if not resets_at_str:
        return None

    try:
        # Handle ISO8601 format with Z suffix
        if resets_at_str.endswith("Z"):
            resets_at_str = resets_at_str[:-1] + "+00:00"
        return datetime.fromisoformat(resets_at_str)
    except (ValueError, TypeError):
        return None


@with_retry(max_attempts=3, base_delay=1.0)
def fetch_oauth_usage(access_token: Optional[str] = None, debug: bool = False) -> OAuthUsageData:
    """Fetch usage data from Anthropic OAuth API."""
    if access_token is None:
        access_token = load_access_token()

    if not access_token:
        return OAuthUsageData(error="No OAuth token available")

    url = "https://api.anthropic.com/api/oauth/usage"

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "ClaudeBar/1.0",
    }

    try:
        req = urllib.request.Request(url, headers=headers, method="GET")

        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))

        # Debug logging to verify API response format
        if debug:
            print(f"DEBUG OAuth API response: {json.dumps(data, indent=2)}")
            if "five_hour" in data:
                print(f"DEBUG five_hour.utilization = {data['five_hour'].get('utilization')}")
            if "seven_day" in data:
                print(f"DEBUG seven_day.utilization = {data['seven_day'].get('utilization')}")

        # Parse session (five_hour)
        session = None
        if "five_hour" in data:
            fh = data["five_hour"]
            session = UsageWindow(
                utilization=fh.get("utilization", 0.0),
                resets_at=parse_reset_time(fh.get("resets_at")),
            )

        # Parse weekly (seven_day)
        weekly = None
        if "seven_day" in data:
            sd = data["seven_day"]
            weekly = UsageWindow(
                utilization=sd.get("utilization", 0.0),
                resets_at=parse_reset_time(sd.get("resets_at")),
            )

        # Parse extra usage
        # API returns monthly_limit and used_credits in CENTS, divide by 100
        extra = None
        if "extra_usage" in data:
            eu = data["extra_usage"]
            extra = ExtraUsage(
                enabled=eu.get("is_enabled") or False,
                monthly_limit=safe_get_float(eu, "monthly_limit") / 100.0,  # cents → currency
                used_credits=safe_get_float(eu, "used_credits") / 100.0,    # cents → currency
                utilization=safe_get_float(eu, "utilization"),              # API returns percentage
                currency=eu.get("currency") or "usd",
            )

        # Extract plan type from various possible fields
        plan_type = (
            data.get("plan_type") or
            data.get("plan") or
            data.get("subscription_type") or
            data.get("tier")
        )

        return OAuthUsageData(
            session=session,
            weekly=weekly,
            extra=extra,
            plan_type=plan_type,
        )

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
