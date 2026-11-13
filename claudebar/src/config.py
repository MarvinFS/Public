"""Configuration management for ClaudeBar."""

import json
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass, asdict, fields
from pathlib import Path
from typing import Optional, Union, get_args, get_origin

from ui_scale import PANEL_MIN_SCALE, PANEL_MAX_SCALE

logger = logging.getLogger("claudebar")


# Every refresh calls the usage APIs; faster than this only invites rate limits.
MIN_REFRESH_INTERVAL = 30  # seconds
# Event.wait raises OverflowError above threading.TIMEOUT_MAX (about 50 days on Windows), and
# a refresh slower than a day is a typo more than a choice.
MAX_REFRESH_INTERVAL = 24 * 60 * 60  # seconds

# A config file that fails to open is tried this many times, this far apart: 0.3 s at most,
# because load_config runs while the app starts.
_READ_ATTEMPTS = 4
_READ_PAUSE = 0.1  # seconds

# True from a load that could not read an existing config file until a load that can: what is
# in memory is then the defaults, and save_config leaves the file alone.
_unreadable = False
_unreadable_noted = False   # the skipped save has been said once

# The two halves of the panel below the limits: today's costs, or the forecast.
PANEL_VIEWS = ("cost", "forecast")


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
    # Which half of the panel shows below the limits; kept across providers and restarts
    panel_view: str = "cost"
    # The panel's own size factor, on top of the monitor's DPI scale; None = by the monitor's height
    panel_scale: Optional[float] = None
    # The tallest the panel may grow, in design pixels (at scale 1.0); None = as tall as it needs
    panel_max_height: Optional[int] = None


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


def _interval_seconds(value) -> int:
    """`value` as whole seconds between MIN_REFRESH_INTERVAL and MAX_REFRESH_INTERVAL. Raises
    TypeError, ValueError or OverflowError (what int() makes of infinity) for what is no finite
    number; a JSON true is not one."""
    if isinstance(value, bool):
        raise TypeError("a boolean is not a number of seconds")
    return min(MAX_REFRESH_INTERVAL, max(MIN_REFRESH_INTERVAL, int(value)))


def clamp_refresh_interval(value) -> int:
    """`value` as whole seconds within the interval's limits, or the default interval when it
    is no finite number."""
    try:
        return _interval_seconds(value)
    except (TypeError, ValueError, OverflowError):
        return Config().refresh_interval


def _has_type(value, declared) -> bool:
    """Whether `value` is what the dataclass declares for a field. A JSON true is a bool and not
    an int, a JSON string is never a bool."""
    if declared is type(None):
        return value is None
    if declared is int:
        return isinstance(value, int) and not isinstance(value, bool)
    if declared is float:                               # a hand-written 2 is as good as 2.0
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if get_origin(declared) is Union:                   # Optional[int]
        return any(_has_type(value, arg) for arg in get_args(declared))
    return isinstance(value, get_origin(declared) or declared)


def _supported_currency(code) -> bool:
    from currency import CURRENCIES         # which imports this module, so not at the top
    return code in CURRENCIES


# What a field's value has to satisfy beyond its declared type. A field with no entry is
# checked by its type alone, so one added to Config later is checked without being listed here.
_RANGES = {
    "warning_threshold": lambda percent: 0 <= percent <= 100,
    "critical_threshold": lambda percent: 0 <= percent <= 100,
    "currency": _supported_currency,
    "panel_view": lambda view: view in PANEL_VIEWS,
    "panel_scale": lambda scale: scale is None or PANEL_MIN_SCALE <= scale <= PANEL_MAX_SCALE,
    "panel_max_height": lambda height: height is None or height > 0,
}
# Fields whose number is made to fit rather than refused: a refresh interval is clamped.
_COERCIONS = {"refresh_interval": _interval_seconds}


def _usable(field, value) -> bool:
    """Whether a dataclass field of Config may hold `value`, as the file gave it."""
    if field.name in _COERCIONS:
        try:
            _COERCIONS[field.name](value)
        except (TypeError, ValueError, OverflowError):
            return False
        return True
    return _has_type(value, field.type) and _RANGES.get(field.name, lambda _: True)(value)


def _config_from(data: dict) -> tuple:
    """A Config from the JSON object in the file, and the names of the fields it had to reset.

    One field at a time: a value that cannot be used (a string where a number goes, a percentage
    of 150, a currency the app does not have) is that field's default, and the user's other
    values stay. A field the file leaves out is its default without being counted as reset."""
    values, reset = {}, []
    for field in fields(Config):
        if field.name not in data:
            continue
        value = data[field.name]
        if _usable(field, value):
            values[field.name] = _COERCIONS.get(field.name, lambda kept: kept)(value)
        else:
            reset.append(field.name)
    config = Config(**values)
    # A warning above the critical level would colour a limit red before it is amber.
    if config.warning_threshold > config.critical_threshold:
        defaults = Config()
        config.warning_threshold, config.critical_threshold = defaults.warning_threshold, defaults.critical_threshold
        reset += [name for name in ("warning_threshold", "critical_threshold") if name not in reset]
    return config, reset


def _read_text(path: Path) -> str:
    """The file's text. A read that fails with OSError (a virus scan or a sync client holds the
    file for a moment) is tried again a few times, well under a second in all."""
    for attempt in range(_READ_ATTEMPTS):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except OSError:
            if attempt + 1 == _READ_ATTEMPTS:
                raise
            time.sleep(_READ_PAUSE)


def load_config() -> Config:
    """Load configuration from file, creating defaults if needed."""
    global _unreadable, _unreadable_noted
    config_path = get_config_path()
    _unreadable = _unreadable_noted = False

    if config_path.exists():
        try:
            data = json.loads(_read_text(config_path))
            if not isinstance(data, dict):
                raise ValueError("not a JSON object")
            config, reset = _config_from(data)
            if reset:
                # Names only: the values are the user's own, and a path or a token typed into
                # the wrong field must not reach the log.
                logger.warning("Config file %s has unusable values for %s, using the defaults "
                               "for those", config_path.name,
                               ", ".join(f.name for f in fields(Config) if f.name in reset))
            return config
        except OSError as e:
            # The file may be fine and only held for longer than the retries. What it says is
            # unknown, so nothing may be saved over it (save_config) until a load reads it.
            _unreadable = True
            logger.warning("Config file %s could not be read (%s), using the defaults and "
                           "not saving over it", config_path.name, type(e).__name__)
        except (ValueError, TypeError, KeyError) as e:
            # ValueError is bad JSON and bad UTF-8 alike. The file is left as it is: the
            # user may be mid-edit, and the next save writes it whole.
            logger.warning("Config file %s is unusable (%s), using the defaults",
                           config_path.name, type(e).__name__)

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
            # On disk before the name points at it: replacing a file with one that
            # was never flushed can leave an empty file after a power cut.
            f.flush()
            os.fsync(f.fileno())
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
    """Save configuration to file, unless the file could not be read at load: `config` is then
    the defaults plus whatever changed since, and would replace the user's real settings."""
    global _unreadable_noted
    if _unreadable:
        if not _unreadable_noted:
            _unreadable_noted = True
            logger.warning("Config file %s could not be read at load, so it is not saved over "
                           "this session", get_config_path().name)
        return
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
