"""Check Claude Code installation and authentication status."""

import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass
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
        is_valid is True only if a usable (apiKey or unexpired OAuth) token exists.
        Claude Code owns token refresh; an expired OAuth token reports "Token expired".
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
        if not isinstance(oauth_data, dict):
            return False, creds, "No OAuth credentials found"

        access_token = oauth_data.get("accessToken")
        if not access_token:
            return False, creds, "No access token"

        # Only treat expiresAt as expiry when it is a real number (a JSON bool is
        # an int subclass, so exclude it). 5-minute skew matches the OAuth reader.
        expires_at = oauth_data.get("expiresAt")
        if not isinstance(expires_at, bool) and isinstance(expires_at, (int, float)):
            if time.time() * 1000 >= (expires_at - 300_000):
                return False, creds, "Token expired"

        return True, creds, None
    except (json.JSONDecodeError, IOError) as e:
        return False, None, f"Error reading credentials: {str(e)}"


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
