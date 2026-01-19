"""Check Claude Code installation and authentication status."""

import json
import shutil
from pathlib import Path
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from config import get_claude_dir


@dataclass
class ClaudeStatus:
    """Status of Claude Code installation and authentication."""
    installed: bool = False
    cli_path: Optional[str] = None
    authenticated: bool = False
    organization: Optional[str] = None
    email: Optional[str] = None
    plan: Optional[str] = None
    version: Optional[str] = None
    error: Optional[str] = None


def find_claude_cli() -> Optional[str]:
    """Find the Claude CLI executable."""
    # Check if in PATH
    cli = shutil.which("claude") or shutil.which("claude.exe")
    if cli:
        return cli

    # Check common npm locations on Windows
    import os
    locations = [
        Path(os.environ.get("APPDATA", "")) / "npm" / "claude.cmd",
        Path(os.environ.get("LOCALAPPDATA", "")) / "npm" / "claude.cmd",
        Path.home() / "AppData" / "Roaming" / "npm" / "claude.cmd",
    ]

    for loc in locations:
        if loc.exists():
            return str(loc)

    return None


def check_credentials() -> tuple[bool, Optional[dict], Optional[str]]:
    """Check if Claude credentials exist and are valid.

    Returns:
        Tuple of (is_valid, credentials_dict, error_message).
        is_valid is True only if we can obtain a working access token.
    """
    creds_path = get_claude_dir() / ".credentials.json"

    if not creds_path.exists():
        return False, None, "Credentials file not found"

    try:
        with open(creds_path, "r", encoding="utf-8") as f:
            creds = json.load(f)

        # Check for API key (always valid if present)
        if creds.get("apiKey"):
            return True, creds, None

        # Check for OAuth credentials
        oauth_data = creds.get("claudeAiOauth")
        if not oauth_data:
            return False, creds, "No OAuth credentials found"

        # Check if access token exists
        access_token = oauth_data.get("accessToken")
        if not access_token:
            return False, creds, "No access token"

        # Check if token is expired
        expires_at = oauth_data.get("expiresAt", 0)
        if expires_at > 0:
            expires_dt = datetime.fromtimestamp(expires_at / 1000)
            buffer_time = timedelta(minutes=5)

            if datetime.now() > (expires_dt - buffer_time):
                # Token expired, try to refresh
                refresh_token = oauth_data.get("refreshToken")
                if not refresh_token:
                    return False, creds, "Token expired, no refresh token"

                # Try to refresh using oauth_usage module
                try:
                    from oauth_usage import refresh_access_token, save_credentials
                    result = refresh_access_token(refresh_token)
                    if result:
                        new_access, new_refresh, new_expires = result
                        save_credentials(new_access, new_refresh, new_expires)
                        # Reload credentials after refresh
                        with open(creds_path, "r", encoding="utf-8") as f:
                            creds = json.load(f)
                        return True, creds, None
                    else:
                        return False, creds, "Token refresh failed"
                except Exception as e:
                    return False, creds, f"Token refresh error: {str(e)}"

        return True, creds, None
    except (json.JSONDecodeError, IOError) as e:
        return False, None, f"Error reading credentials: {str(e)}"


def get_settings_info() -> dict:
    """Get info from Claude settings."""
    settings_path = get_claude_dir() / "settings.json"

    if not settings_path.exists():
        return {}

    try:
        with open(settings_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}


def check_claude_status() -> ClaudeStatus:
    """Perform a comprehensive check of Claude Code status."""
    status = ClaudeStatus()

    # Check CLI installation (optional - OAuth works without CLI in PATH)
    cli_path = find_claude_cli()
    status.cli_path = cli_path
    status.installed = cli_path is not None

    # Check Claude directory exists
    claude_dir = get_claude_dir()
    if not claude_dir.exists():
        status.error = "Claude data directory not found (~/.claude)"
        return status

    # Check authentication - this is what really matters for OAuth API
    auth_ok, creds, _error = check_credentials()
    status.authenticated = auth_ok

    if not auth_ok:
        status.error = "Not logged in to Claude Code"
        return status

    # If authenticated via OAuth, mark as installed even if CLI not in PATH
    # The OAuth API works without the CLI executable
    if auth_ok and creds and creds.get("claudeAiOauth"):
        status.installed = True

    # Get additional info from credentials
    if creds:
        oauth = creds.get("claudeAiOauth", {})
        if isinstance(oauth, dict):
            status.organization = oauth.get("organizationName") or oauth.get("organization_name")
            status.email = oauth.get("email")
            # Try various field names for plan
            status.plan = (
                oauth.get("plan") or
                oauth.get("planType") or
                oauth.get("plan_type") or
                oauth.get("subscriptionType") or
                oauth.get("subscription_type") or
                oauth.get("tier") or
                oauth.get("accountType") or
                oauth.get("account_type")
            )

    # Get settings info
    settings = get_settings_info()
    if settings:
        # Could extract more info from settings
        pass

    return status


def get_status_message(status: ClaudeStatus) -> str:
    """Get a human-readable status message."""
    if not status.installed:
        return "❌ Claude CLI not installed"

    if not status.authenticated:
        return "⚠️ Not logged in"

    parts = ["✓ Connected"]
    if status.plan:
        parts.append(f"({status.plan})")
    if status.organization:
        parts.append(f"- {status.organization}")

    return " ".join(parts)
