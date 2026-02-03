"""Input validation for ClaudeBar."""

from typing import Any, Optional
from dataclasses import dataclass


@dataclass
class ValidationError:
    """Validation error details."""
    field: str
    message: str
    value: Any = None


def validate_jsonl_entry(entry: dict) -> Optional[ValidationError]:
    """Validate a JSONL log entry has expected structure.

    Args:
        entry: Parsed JSON entry from JSONL file.

    Returns:
        ValidationError if invalid, None if valid.
    """
    if not isinstance(entry, dict):
        return ValidationError("entry", "Not a dictionary", type(entry))

    entry_type = entry.get("type")
    if entry_type == "assistant":
        message = entry.get("message")
        if message is not None and not isinstance(message, dict):
            return ValidationError("message", "Not a dictionary", type(message))

    return None


def safe_get_int(data: dict, key: str, default: int = 0) -> int:
    """Safely get an integer from a dict.

    Args:
        data: Dictionary to read from.
        key: Key to look up.
        default: Default value if key missing or invalid.

    Returns:
        Integer value or default.
    """
    value = data.get(key)
    if value is None:
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def safe_get_float(data: dict, key: str, default: float = 0.0) -> float:
    """Safely get a float from a dict.

    Args:
        data: Dictionary to read from.
        key: Key to look up.
        default: Default value if key missing or invalid.

    Returns:
        Float value or default.
    """
    value = data.get(key)
    if value is None:
        return default
    try:
        return float(value)
    except (ValueError, TypeError):
        return default
