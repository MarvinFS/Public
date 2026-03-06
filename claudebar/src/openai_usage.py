"""Fetch OpenAI/Codex usage data via OAuth API."""

import json
import os
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional
from pathlib import Path

from retry import with_retry


@dataclass
class OpenAIUsageWindow:
    """Usage window with utilization and reset time."""
    percent: float = 0.0  # 0-100
    resets_at: Optional[datetime] = None

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
            return f"in {days}d {hours}h"
        elif hours > 0:
            return f"in {hours}h {minutes}m"
        else:
            return f"in {minutes}m"


@dataclass
class OpenAIUsageData:
    """Complete OpenAI/Codex usage data."""
    five_hour: Optional[OpenAIUsageWindow] = None
    weekly: Optional[OpenAIUsageWindow] = None
    credits_remaining: Optional[float] = None
    plan_type: Optional[str] = None  # "plus", "pro", "enterprise", "free"
    error: Optional[str] = None

    @property
    def is_valid(self) -> bool:
        """Check if we have valid usage data."""
        return self.error is None and (self.five_hour is not None or self.weekly is not None)

    @property
    def session_percent(self) -> float:
        """Get session (5-hour) usage percentage."""
        return self.five_hour.percent if self.five_hour else 0.0

    @property
    def weekly_percent(self) -> float:
        """Get weekly usage percentage."""
        return self.weekly.percent if self.weekly else 0.0

    @property
    def session_reset(self) -> Optional[str]:
        """Get session reset time string."""
        return self.five_hour.reset_str if self.five_hour else None

    @property
    def weekly_reset(self) -> Optional[str]:
        """Get weekly reset time string."""
        return self.weekly.reset_str if self.weekly else None


def get_codex_auth_path() -> Path:
    """Get path to Codex auth file."""
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        return Path(codex_home) / "auth.json"
    return Path.home() / ".codex" / "auth.json"


def load_codex_token() -> Optional[str]:
    """Load OAuth access token from Codex CLI credentials.

    The Codex auth.json structure after `codex login`:
    {
        "OPENAI_API_KEY": null,
        "tokens": {
            "id_token": "eyJ...",
            "access_token": "eyJ...",  // OAuth token for ChatGPT API
            "refresh_token": "rt_...",
            "account_id": "..."
        },
        "last_refresh": "2026-01-16T21:24:05.762Z"
    }
    """
    auth_path = get_codex_auth_path()

    if not auth_path.exists():
        return None

    try:
        with open(auth_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        # Token is nested under "tokens" object
        tokens = data.get("tokens")
        if not tokens:
            return None

        access_token = tokens.get("access_token")
        if not access_token:
            return None

        # Check if refresh is too old (8 days = token likely expired)
        last_refresh = data.get("last_refresh")
        if last_refresh:
            try:
                if last_refresh.endswith("Z"):
                    last_refresh = last_refresh[:-1] + "+00:00"
                refresh_dt = datetime.fromisoformat(last_refresh)
                now = datetime.now(refresh_dt.tzinfo) if refresh_dt.tzinfo else datetime.now()
                if (now - refresh_dt) > timedelta(days=8):
                    return None  # Token likely expired
            except (ValueError, TypeError):
                pass

        return access_token
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def parse_reset_time(reset_info: dict) -> Optional[datetime]:
    """Parse reset time from API response."""
    if not reset_info:
        return None

    # Try parsing ISO timestamp
    timestamp = reset_info.get("timestamp") or reset_info.get("resets_at")
    if timestamp:
        try:
            if isinstance(timestamp, str):
                if timestamp.endswith("Z"):
                    timestamp = timestamp[:-1] + "+00:00"
                return datetime.fromisoformat(timestamp)
        except (ValueError, TypeError):
            pass

    # Try parsing seconds until reset
    seconds = reset_info.get("seconds") or reset_info.get("seconds_until_reset")
    if seconds and isinstance(seconds, (int, float)):
        return datetime.now() + timedelta(seconds=seconds)

    return None


@with_retry(max_attempts=3, base_delay=1.0)
def fetch_openai_usage(access_token: Optional[str] = None) -> OpenAIUsageData:
    """Fetch usage data from ChatGPT/Codex API.

    Endpoint: GET https://chatgpt.com/backend-api/wham/usage
    Auth: Authorization: Bearer <token>

    Response format:
    {
        "plan_type": "plus",
        "rate_limit": {
            "primary_window": {
                "used_percent": 1,
                "limit_window_seconds": 18000,
                "reset_after_seconds": 17779,
                "reset_at": 1768617365
            },
            "secondary_window": {
                "used_percent": 0,
                "limit_window_seconds": 604800,
                "reset_after_seconds": 604579,
                "reset_at": 1769204165
            }
        }
    }
    """
    if access_token is None:
        access_token = load_codex_token()

    if not access_token:
        return OpenAIUsageData(error="No Codex OAuth token. Run 'codex login' first.")

    url = "https://chatgpt.com/backend-api/wham/usage"

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "ClaudeBar/1.0",
    }

    try:
        req = urllib.request.Request(url, headers=headers, method="GET")

        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))

        # Parse plan type
        plan_type = data.get("plan_type")

        # Parse rate_limit structure
        rate_limit = data.get("rate_limit", {})

        # Parse primary window (5-hour / session limit)
        five_hour = None
        primary = rate_limit.get("primary_window")
        if primary:
            reset_at = None
            reset_timestamp = primary.get("reset_at")
            if reset_timestamp:
                reset_at = datetime.fromtimestamp(reset_timestamp)
            five_hour = OpenAIUsageWindow(
                percent=float(primary.get("used_percent", 0)),
                resets_at=reset_at,
            )

        # Parse secondary window (weekly limit)
        weekly = None
        secondary = rate_limit.get("secondary_window")
        if secondary:
            reset_at = None
            reset_timestamp = secondary.get("reset_at")
            if reset_timestamp:
                reset_at = datetime.fromtimestamp(reset_timestamp)
            weekly = OpenAIUsageWindow(
                percent=float(secondary.get("used_percent", 0)),
                resets_at=reset_at,
            )

        # Parse credits
        credits_data = data.get("credits", {})
        credits_balance = None
        if credits_data.get("has_credits"):
            try:
                credits_balance = float(credits_data.get("balance", 0))
            except (ValueError, TypeError):
                pass

        return OpenAIUsageData(
            five_hour=five_hour,
            weekly=weekly,
            credits_remaining=credits_balance,
            plan_type=plan_type,
        )

    except urllib.error.HTTPError as e:
        if e.code == 401:
            return OpenAIUsageData(error="Codex token expired. Run 'codex login' again.")
        if e.code == 403:
            return OpenAIUsageData(error="Access denied. Ensure Codex is authenticated.")
        return OpenAIUsageData(error=f"HTTP {e.code}: {e.reason}")
    except urllib.error.URLError as e:
        return OpenAIUsageData(error=f"Network error: {e.reason}")
    except json.JSONDecodeError:
        return OpenAIUsageData(error="Invalid JSON response from OpenAI")
    except Exception as e:
        return OpenAIUsageData(error=str(e))


def is_codex_configured() -> bool:
    """Check if Codex CLI is configured with OAuth tokens."""
    return load_codex_token() is not None
