"""Fetch Claude usage data via OAuth API."""

import json
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from pathlib import Path


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
            return f"{days}d {hours}h"
        elif hours > 0:
            return f"{hours}h {minutes}m"
        else:
            return f"{minutes}m"


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


def load_access_token() -> Optional[str]:
    """Load OAuth access token from credentials file."""
    creds_path = get_credentials_path()

    if not creds_path.exists():
        return None

    try:
        with open(creds_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        oauth_data = data.get("claudeAiOauth", {})
        access_token = oauth_data.get("accessToken")

        # Check if token is expired
        expires_at = oauth_data.get("expiresAt", 0)
        if expires_at > 0:
            # expiresAt is in milliseconds
            expires_dt = datetime.fromtimestamp(expires_at / 1000)
            if datetime.now() > expires_dt:
                return None  # Token expired

        return access_token
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


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
                enabled=eu.get("is_enabled", False),
                monthly_limit=eu.get("monthly_limit", 0.0) / 100.0,  # cents → currency
                used_credits=eu.get("used_credits", 0.0) / 100.0,    # cents → currency
                utilization=eu.get("utilization", 0.0),              # API returns percentage
                currency=eu.get("currency", "usd"),
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
        return OAuthUsageData(error=f"HTTP {e.code}: {e.reason}")
    except urllib.error.URLError as e:
        return OAuthUsageData(error=f"Network error: {e.reason}")
    except json.JSONDecodeError:
        return OAuthUsageData(error="Invalid JSON response")
    except Exception as e:
        return OAuthUsageData(error=str(e))
