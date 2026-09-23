"""Configuration management for ClaudeBar."""

import json
import os
import sys
import threading
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional


# Every refresh calls the usage APIs; faster than this only invites rate limits.
MIN_REFRESH_INTERVAL = 30  # seconds


@dataclass
class Config:
    """ClaudeBar configuration."""
    refresh_interval: int = 300  # seconds
    warning_threshold: int = 80  # percent
    critical_threshold: int = 95  # percent
    currency: str = "USD"  # USD, EUR, RUB, RON
    # Engine toggles
    claude_enabled: bool = True
    codex_enabled: bool = True
    deepseek_enabled: bool = True
    # Where the user last dragged the panel; None = default spot near the tray
    window_x: Optional[int] = None
    window_y: Optional[int] = None


def get_resources_path() -> Path:
    """Get the resources path, works both in dev and the bundled exe."""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "resources"
    return Path(__file__).parent.parent / "resources"


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
            config = Config(**{k: v for k, v in data.items() if k in Config.__dataclass_fields__})
            # A hand-edited file can hold 0, which made the refresh loop spin.
            try:
                config.refresh_interval = max(MIN_REFRESH_INTERVAL, int(config.refresh_interval))
            except (TypeError, ValueError):
                config.refresh_interval = Config().refresh_interval
            return config
        except (json.JSONDecodeError, TypeError, KeyError):
            pass

    # Return defaults
    return Config()


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Replace `path` with `data` in one step.

    The data goes to a temp file beside it, then os.replace swaps it in, so a
    crash mid-write leaves the old file whole rather than truncated. The temp
    name is per process and thread, so two writers never share one.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def atomic_write_text(path: Path, text: str) -> None:
    """atomic_write_bytes for UTF-8 text."""
    atomic_write_bytes(path, text.encode("utf-8"))


def save_config(config: Config) -> None:
    """Save configuration to file."""
    atomic_write_text(get_config_path(), json.dumps(asdict(config), indent=2))


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
