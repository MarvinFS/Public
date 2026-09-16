"""Logging configuration for ClaudeBar.

The log is a failure trail, not a heartbeat. A routine refresh repeats every
few minutes and says nothing worth keeping, so it stays at DEBUG. What earns a
line is a transition (the first failure, the recovery, a reminder while
something stays broken) plus lifecycle events.
"""

import logging
import os
import re
import sys
import time
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Optional

from config import get_config_dir

LOGGER_NAME = "claudebar"

LOG_FILENAME = "claudebar.log"
#: Ceiling for one day's file. A normal day is kilobytes; this only exists so a
#: debug session or a retry storm cannot fill the disk.
MAX_BYTES_PER_DAY = 1_000_000
#: How long rotated files stay on disk.
RETENTION_DAYS = 14

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


class DailyRotatingHandler(TimedRotatingFileHandler):
    """Daily log file, with a per-day byte ceiling and age-based pruning.

    TimedRotatingFileHandler alone is not enough here. It has no size limit, so
    one loud day grows without bound, and its rollover names the archive after
    the start of the interval and deletes whatever already holds that name, so a
    second rollover inside the same day would destroy the first archive. This
    handler adds a size trigger and gives a repeated rollover a counter suffix.

    Pruning walks the directory by mtime instead of using the inherited
    extension matching, because that pattern cannot match the numbered names the
    size trigger produces.

    Archives are named for the interval they cover: at midnight that is the day
    that just ended, and mid-interval it is the day being logged.
    """

    def __init__(self, filename, maxBytes: int = MAX_BYTES_PER_DAY,
                 retention_days: int = RETENTION_DAYS, **kwargs):
        super().__init__(
            filename, when="midnight", interval=1, backupCount=0,
            encoding="utf-8", errors="backslashreplace", delay=True, **kwargs
        )
        self.maxBytes = maxBytes
        self.retention_days = retention_days

    def shouldRollover(self, record) -> bool:
        if super().shouldRollover(record):
            return True
        if not self.maxBytes or self.stream is None:
            return False
        self.stream.seek(0, os.SEEK_END)
        pending = len(self.format(record).encode("utf-8"))
        return self.stream.tell() + pending >= self.maxBytes

    def doRollover(self) -> None:
        if self.stream:
            self.stream.close()
            self.stream = None

        when = self.rolloverAt - self.interval
        time_tuple = time.gmtime(when) if self.utc else time.localtime(when)
        stamp = time.strftime(self.suffix, time_tuple)
        archive = f"{self.baseFilename}.{stamp}"
        index = 1
        while os.path.exists(archive):
            archive = f"{self.baseFilename}.{stamp}.{index}"
            index += 1
        self.rotate(self.baseFilename, self.rotation_filename(archive))

        self._prune()

        if not self.delay:
            self.stream = self._open()
        self.rolloverAt = self.computeRollover(int(time.time()))

    def _prune(self) -> None:
        """Delete archives older than retention_days."""
        if not self.retention_days:
            return
        directory = os.path.dirname(self.baseFilename) or "."
        prefix = os.path.basename(self.baseFilename) + "."
        cutoff = time.time() - self.retention_days * 86_400
        try:
            names = os.listdir(directory)
        except OSError:
            return
        for name in names:
            if not name.startswith(prefix):
                continue
            path = os.path.join(directory, name)
            try:
                if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except OSError:
                pass


def get_log_path() -> Path:
    """Path of the active log file. It exists once the app has logged anything."""
    return get_config_dir() / LOG_FILENAME


def setup_logging(debug: bool = False, log_dir: Optional[Path] = None) -> logging.Logger:
    """Configure logging for ClaudeBar.

    Args:
        debug: If True, keep DEBUG records (routine refresh successes, probe
            output) in the file. Without it the file holds INFO and above.
        log_dir: Directory for the log file. Defaults to the config directory.

    Returns:
        Configured logger instance.
    """
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG if debug else logging.INFO)
    logger.handlers.clear()

    # Console handler (warnings and above only). A windowed PyInstaller build
    # has no stderr to write to, so skip it rather than hold a dead stream.
    if sys.stderr is not None:
        console = logging.StreamHandler(sys.stderr)
        console.setLevel(logging.WARNING)
        console.setFormatter(RedactingFormatter('%(levelname)s: %(message)s'))
        logger.addHandler(console)

    # File handler (rotating, in config dir)
    try:
        target_dir = log_dir or get_config_dir()
        target_dir.mkdir(parents=True, exist_ok=True)
        file_handler = DailyRotatingHandler(target_dir / LOG_FILENAME)
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
