"""Check Claude Code installation and authentication status."""

import json
import shutil
from pathlib import Path
from dataclasses import dataclass
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


def check_credentials() -> tuple[bool, Optional[dict]]:
    """Check if Claude credentials exist and are valid."""
    creds_path = get_claude_dir() / ".credentials.json"

    if not creds_path.exists():
        return False, None

    try:
        with open(creds_path, "r", encoding="utf-8") as f:
            creds = json.load(f)

        # Check for required fields
        if creds.get("claudeAiOauth"):
            return True, creds
        if creds.get("apiKey"):
            return True, creds

        return False, creds
    except (json.JSONDecodeError, IOError):
        return False, None


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
    auth_ok, creds = check_credentials()
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
