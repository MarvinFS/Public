"""Configuration management for ClaudeBar."""

import json
import os
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional


@dataclass
class Config:
    """ClaudeBar configuration."""
    refresh_interval: int = 300  # seconds
    cli_timeout: int = 30  # seconds
    warning_threshold: int = 80  # percent
    critical_threshold: int = 95  # percent
    show_notifications: bool = True
    start_minimized: bool = True
    currency: str = "USD"  # USD, EUR, RUB, RON


def get_config_dir() -> Path:
    """Get the configuration directory path in AppData/Local."""
    # Use LOCALAPPDATA environment variable (works for any Windows user)
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "ClaudeBar"
    # Fallback for non-Windows or missing env var
    return Path.home() / ".claudebar"


def get_config_path() -> Path:
    """Get the configuration file path."""
    return get_config_dir() / "config.json"


def ensure_config_exists() -> Config:
    """Ensure config file exists with defaults, create if missing."""
    config_path = get_config_path()

    if not config_path.exists():
        # First run - create config with USD default
        config = Config(currency="USD")
        save_config(config)
        return config

    return load_config()


def get_claude_dir() -> Path:
    """Get the Claude Code data directory."""
    return Path.home() / ".claude"


def get_claude_projects_dir() -> Path:
    """Get the Claude Code projects directory with JSONL logs."""
    return get_claude_dir() / "projects"


def load_config() -> Config:
    """Load configuration from file, creating defaults if needed."""
    config_path = get_config_path()

    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return Config(**{k: v for k, v in data.items() if k in Config.__dataclass_fields__})
        except (json.JSONDecodeError, TypeError, KeyError):
            pass

    # Return defaults
    return Config()


def save_config(config: Config) -> None:
    """Save configuration to file."""
    config_dir = get_config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)

    config_path = get_config_path()
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(asdict(config), f, indent=2)


def find_claude_cli() -> Optional[str]:
    """Find the claude CLI executable."""
    # Check common locations
    possible_paths = [
        "claude",  # In PATH
        "claude.exe",  # Windows in PATH
    ]

    # Check if claude is in PATH
    import shutil
    for cmd in possible_paths:
        path = shutil.which(cmd)
        if path:
            return path

    # Common installation locations on Windows
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if local_app_data:
        npm_global = Path(local_app_data) / "npm" / "claude.cmd"
        if npm_global.exists():
            return str(npm_global)

    # User profile npm location
    user_profile = os.environ.get("USERPROFILE", "")
    if user_profile:
        npm_user = Path(user_profile) / "AppData" / "Roaming" / "npm" / "claude.cmd"
        if npm_user.exists():
            return str(npm_user)

    return None
