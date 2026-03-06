"""Check Claude Code installation and authentication status."""

import json
import logging
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from config import find_claude_cli, get_claude_dir

logger = logging.getLogger("claudebar")


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


def check_claude_status_via_cli(cli_path: Optional[str] = None) -> Optional[ClaudeStatus]:
    """Check status by running 'claude auth status' subprocess.

    Returns ClaudeStatus on success, None if CLI unavailable or fails.
    """
    if cli_path is None:
        cli_path = find_claude_cli()
    if not cli_path:
        return None

    try:
        env = os.environ.copy()
        env.pop("CLAUDECODE", None)
        result = subprocess.run(
            [cli_path, "auth", "status"],
            capture_output=True, text=True, timeout=10,
            env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )

        if result.returncode != 0:
            logger.debug("claude auth status returned %d: %s", result.returncode, result.stderr.strip())
            return None

        data = json.loads(result.stdout)
        status = ClaudeStatus(
            installed=True,
            cli_path=cli_path,
            authenticated=data.get("loggedIn", False),
            email=data.get("email"),
            organization=data.get("orgName"),
            plan=data.get("subscriptionType"),
        )
        logger.info("CLI auth status: authenticated=%s, plan=%s", status.authenticated, status.plan)
        return status

    except subprocess.TimeoutExpired:
        logger.warning("claude auth status timed out")
        return None
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("claude auth status failed: %s", e)
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

        if creds.get("apiKey"):
            return True, creds, None

        oauth_data = creds.get("claudeAiOauth")
        if not oauth_data:
            return False, creds, "No OAuth credentials found"

        access_token = oauth_data.get("accessToken")
        if not access_token:
            return False, creds, "No access token"

        expires_at = oauth_data.get("expiresAt", 0)
        if expires_at > 0:
            expires_dt = datetime.fromtimestamp(expires_at / 1000)
            buffer_time = timedelta(minutes=5)

            if datetime.now() > (expires_dt - buffer_time):
                refresh_token = oauth_data.get("refreshToken")
                if not refresh_token:
                    return False, creds, "Token expired, no refresh token"

                try:
                    from oauth_usage import refresh_access_token, save_credentials
                    result = refresh_access_token(refresh_token)
                    if result:
                        new_access, new_refresh, new_expires = result
                        save_credentials(new_access, new_refresh, new_expires)
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
    """Perform a comprehensive check of Claude Code status.

    Prefers CLI subprocess (claude auth status) for reliable status,
    falls back to credentials file check.
    """
    cli_path = find_claude_cli()
    if cli_path:
        cli_status = check_claude_status_via_cli(cli_path)
        if cli_status:
            return cli_status

    # Fallback: check credentials file directly
    status = ClaudeStatus()
    status.cli_path = cli_path
    status.installed = cli_path is not None

    claude_dir = get_claude_dir()
    if not claude_dir.exists():
        status.error = "Claude data directory not found (~/.claude)"
        return status

    auth_ok, creds, _error = check_credentials()
    status.authenticated = auth_ok

    if not auth_ok:
        status.error = "Not logged in to Claude Code"
        return status

    if auth_ok and creds and creds.get("claudeAiOauth"):
        status.installed = True

    if creds:
        oauth = creds.get("claudeAiOauth", {})
        if isinstance(oauth, dict):
            status.organization = oauth.get("organizationName") or oauth.get("organization_name")
            status.email = oauth.get("email")
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

    return status


def get_status_message(status: ClaudeStatus) -> str:
    """Get a human-readable status message."""
    if not status.installed:
        return "Claude CLI not installed"

    if not status.authenticated:
        return "Not logged in"

    parts = ["Connected"]
    if status.plan:
        parts.append(f"({status.plan})")
    if status.organization:
        parts.append(f"- {status.organization}")

    return " ".join(parts)
