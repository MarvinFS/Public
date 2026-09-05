"""Input validation for ClaudeBar."""


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
