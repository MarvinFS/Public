"""Fetch OpenAI/Codex usage data via OAuth API."""

import base64
import json
import os
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from pathlib import Path

from retry import with_retry


def _jwt_exp(token: str) -> Optional[datetime]:
    """Decode a JWT and return its `exp` claim as a tz-aware UTC datetime."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        exp = claims.get("exp")
        if isinstance(exp, (int, float)):
            return datetime.fromtimestamp(exp, timezone.utc)
    except (ValueError, IndexError, json.JSONDecodeError, TypeError, UnicodeDecodeError):
        pass
    return None


# Anything longer than this is a weekly-style window, not a session one.
_SESSION_WINDOW_MAX_SECONDS = 6 * 3600


@dataclass
class OpenAIUsageWindow:
    """Usage window with utilization and reset time."""
    percent: float = 0.0  # 0-100
    resets_at: Optional[datetime] = None
    window_seconds: int = 0  # limit_window_seconds as reported by the API

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
    def has_session_window(self) -> bool:
        """Whether the plan reports a 5-hour window at all (prolite does not)."""
        return self.five_hour is not None

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

        # Check the JWT's actual `exp` claim. Codex CLI refreshes lazily, so
        # `last_refresh` age is NOT a reliable proxy for token validity — the
        # token can be days-old in last_refresh but still valid for hours.
        exp = _jwt_exp(access_token)
        if exp is not None:
            now = datetime.now(timezone.utc)
            if now >= exp - timedelta(seconds=60):
                return None  # Actually expired (or expiring within 60s)

        return access_token
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def _parse_window(raw: Optional[dict]) -> Optional[OpenAIUsageWindow]:
    """Build a usage window from one `rate_limit.*_window` object."""
    if not raw:
        return None
    reset_timestamp = raw.get("reset_at")
    return OpenAIUsageWindow(
        percent=float(raw.get("used_percent", 0)),
        resets_at=datetime.fromtimestamp(reset_timestamp) if reset_timestamp else None,
        window_seconds=int(raw.get("limit_window_seconds") or 0),
    )


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
            }  // may be null - see _parse_window
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

        # `primary_window` used to always be the 5-hour window and
        # `secondary_window` the weekly one. Since 2026 OpenAI returns the
        # weekly window as `primary` with `secondary` null on some plans
        # (e.g. prolite), so trust `limit_window_seconds` over the position
        # and only fall back to the position when the length is missing.
        five_hour = _parse_window(rate_limit.get("primary_window"))
        weekly = _parse_window(rate_limit.get("secondary_window"))
        if five_hour and five_hour.window_seconds > _SESSION_WINDOW_MAX_SECONDS:
            five_hour, weekly = weekly, five_hour

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
