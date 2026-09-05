"""Logging configuration for ClaudeBar."""

import logging
import sys
import re
from logging.handlers import RotatingFileHandler
from config import get_config_dir

LOGGER_NAME = "claudebar"

SENSITIVE_PATTERNS = [
    (r'Bearer [A-Za-z0-9._-]+', 'Bearer [REDACTED]'),
    (r'accessToken":\s*"[^"]+', 'accessToken": "[REDACTED]'),
    (r'refreshToken":\s*"[^"]+', 'refreshToken": "[REDACTED]'),
    (r'access_token":\s*"[^"]+', 'access_token": "[REDACTED]'),
    (r'refresh_token":\s*"[^"]+', 'refresh_token": "[REDACTED]'),
]


class RedactingFormatter(logging.Formatter):
    """Formatter that redacts sensitive data."""

    def format(self, record):
        message = super().format(record)
        for pattern, replacement in SENSITIVE_PATTERNS:
            message = re.sub(pattern, replacement, message)
        return message


def setup_logging(debug: bool = False) -> logging.Logger:
    """Configure logging for ClaudeBar.

    Args:
        debug: If True, set log level to DEBUG.

    Returns:
        Configured logger instance.
    """
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG if debug else logging.INFO)
    logger.handlers.clear()

    # Console handler (warnings and above only)
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.WARNING)
    console.setFormatter(RedactingFormatter('%(levelname)s: %(message)s'))
    logger.addHandler(console)

    # File handler (rotating, in config dir)
    try:
        log_dir = get_config_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_dir / "claudebar.log",
            maxBytes=1_000_000,
            backupCount=3,
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(RedactingFormatter(
            '%(asctime)s %(levelname)s [%(module)s] %(message)s'
        ))
        logger.addHandler(file_handler)
    except Exception:
        pass

    return logger


def get_logger() -> logging.Logger:
    """Get the ClaudeBar logger."""
    return logging.getLogger(LOGGER_NAME)
