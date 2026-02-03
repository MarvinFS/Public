# ClaudeBar Hardening Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Implement P0 and P2 security/stability fixes from the code review synthesis.

**Architecture:** Add defensive layers (size limits, validation, retry, logging) around existing well-structured code without changing core functionality.

**Tech Stack:** Python 3.12, tkinter, pystray, urllib, logging, pytest

---

## Scope Decisions

### Issue 1 - Credential Storage: OUT OF SCOPE

Per user decision: The OAuth tokens in `~/.claude/.credentials.json` and `~/.codex/auth.json` are created and managed by Claude CLI and OpenAI Codex respectively. ClaudeBar only reads these files. The plaintext storage is an upstream decision by Anthropic and OpenAI.

ClaudeBar does write back to `~/.claude/.credentials.json` after token refresh (oauth_usage.py:135), but this maintains compatibility with Claude CLI's expected format.

**Decision:** No changes to credential handling. Document this as a known limitation.

### Already Implemented (No Action Needed)

- **Threading Architecture:** RLock-based thread-safe UI updates (ui_window.py:351, 1176-1195, 1396-1418)
- **Network Timeouts:** All urllib calls have 5-10s timeouts
- **Context Managers:** All file I/O uses `with` blocks

---

## Phase 1: P0 Critical Fixes

### Task 1: Global Exception Handler

**Files:**
- Modify: `claudebar/src/main.py:128-135`

**Step 1: Add exception handler to main()**

```python
def main():
    """Entry point."""
    try:
        app = ClaudeBar()
        app.run()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        import traceback
        error_msg = f"ClaudeBar crashed: {e}\n{traceback.format_exc()}"
        print(error_msg, file=sys.stderr)
        if sys.platform == "win32":
            try:
                ctypes.windll.user32.MessageBoxW(0, str(e), "ClaudeBar Error", 0x10)
            except Exception:
                pass
```

**Step 2: Verify**

Run: `python src/main.py`
Expected: App starts normally. Force crash with code change, verify dialog appears.

---

### Task 2: JSONL Parser Size Limits

**Files:**
- Modify: `claudebar/src/log_parser.py:69-86`
- Modify: `claudebar/src/codex_log_parser.py` (similar pattern)

**Step 1: Add constants at top of log_parser.py**

```python
# Parser safety limits
MAX_JSONL_FILE_SIZE_MB = 100
MAX_LINES_PER_FILE = 100_000
```

**Step 2: Update parse_jsonl_file()**

```python
def parse_jsonl_file(file_path: Path) -> Iterator[tuple[str, TokenUsage, datetime]]:
    """Parse a single JSONL file and yield usage entries."""
    # Check file size first
    try:
        file_size_mb = file_path.stat().st_size / (1024 * 1024)
        if file_size_mb > MAX_JSONL_FILE_SIZE_MB:
            return
    except OSError:
        return

    line_count = 0
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if line_count >= MAX_LINES_PER_FILE:
                    break
                line_count += 1

                line = line.strip()
                if not line:
                    continue

                try:
                    entry = json.loads(line)
                    result = extract_usage_from_entry(entry)
                    if result:
                        yield result
                except json.JSONDecodeError:
                    continue
    except (IOError, OSError):
        pass
```

**Step 3: Apply same pattern to codex_log_parser.py**

**Step 4: Verify**

Run app, check JSONL files are still parsed. Create oversized test file to verify it's skipped.

---

## Phase 2: P2 High Priority Fixes

### Task 3: Retry Logic with Exponential Backoff

**Files:**
- Create: `claudebar/src/retry.py`
- Modify: `claudebar/src/oauth_usage.py`
- Modify: `claudebar/src/openai_usage.py`
- Modify: `claudebar/src/currency.py`

**Step 1: Create retry.py**

```python
"""Retry utilities with exponential backoff."""

import time
from functools import wraps
from typing import Callable, TypeVar
import urllib.error

T = TypeVar('T')

RETRYABLE_EXCEPTIONS = (
    urllib.error.URLError,
    ConnectionError,
    TimeoutError,
)


def with_retry(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    exceptions: tuple = RETRYABLE_EXCEPTIONS,
) -> Callable:
    """Decorator for retry with exponential backoff."""
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        def wrapper(*args, **kwargs) -> T:
            last_exception = None
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt < max_attempts - 1:
                        delay = min(base_delay * (2 ** attempt), max_delay)
                        time.sleep(delay)
            raise last_exception
        return wrapper
    return decorator
```

**Step 2: Add @with_retry to oauth_usage.py functions**

Add import and decorator to `refresh_access_token()` and the inner network call in `fetch_oauth_usage()`.

**Step 3: Add @with_retry to openai_usage.py and currency.py**

**Step 4: Verify**

Disconnect network briefly, verify app retries and recovers.

---

### Task 4: Structured Logging

**Files:**
- Create: `claudebar/src/logging_config.py`
- Modify: `claudebar/src/main.py`
- Modify: Files with print statements

**Step 1: Create logging_config.py**

```python
"""Logging configuration for ClaudeBar."""

import logging
import sys
import re
from pathlib import Path
from logging.handlers import RotatingFileHandler
from config import get_config_dir

LOGGER_NAME = "claudebar"

SENSITIVE_PATTERNS = [
    (r'Bearer [A-Za-z0-9._-]+', 'Bearer [REDACTED]'),
    (r'accessToken":\s*"[^"]+', 'accessToken": "[REDACTED]'),
    (r'refreshToken":\s*"[^"]+', 'refreshToken": "[REDACTED]'),
]


class RedactingFormatter(logging.Formatter):
    """Formatter that redacts sensitive data."""

    def format(self, record):
        message = super().format(record)
        for pattern, replacement in SENSITIVE_PATTERNS:
            message = re.sub(pattern, replacement, message)
        return message


def setup_logging(debug: bool = False) -> logging.Logger:
    """Configure logging for ClaudeBar."""
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG if debug else logging.INFO)
    logger.handlers.clear()

    # Console (warnings only)
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.WARNING)
    console.setFormatter(RedactingFormatter('%(levelname)s: %(message)s'))
    logger.addHandler(console)

    # File (rotating, in config dir)
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
```

**Step 2: Initialize in main.py**

```python
from logging_config import setup_logging, get_logger

def main():
    logger = setup_logging()
    logger.info("ClaudeBar starting")
    # ... rest of main
```

**Step 3: Replace print statements**

- main.py:51: `print(f"Refresh error: {e}")` → `logger.error(f"Refresh error: {e}")`
- Similar replacements in data_collector.py, oauth_usage.py

**Step 4: Verify**

Check `%LOCALAPPDATA%\ClaudeBar\claudebar.log` exists and contains no plaintext tokens.

---

### Task 5: Input Validation Layer

**Files:**
- Create: `claudebar/src/validation.py`
- Modify: `claudebar/src/log_parser.py`
- Modify: `claudebar/src/oauth_usage.py`

**Step 1: Create validation.py**

```python
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
    """Validate a JSONL log entry has expected structure."""
    if not isinstance(entry, dict):
        return ValidationError("entry", "Not a dictionary", type(entry))

    entry_type = entry.get("type")
    if entry_type == "assistant":
        message = entry.get("message")
        if message is not None and not isinstance(message, dict):
            return ValidationError("message", "Not a dictionary", type(message))

    return None


def safe_get_int(data: dict, key: str, default: int = 0) -> int:
    """Safely get an integer from a dict."""
    value = data.get(key)
    if value is None:
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def safe_get_float(data: dict, key: str, default: float = 0.0) -> float:
    """Safely get a float from a dict."""
    value = data.get(key)
    if value is None:
        return default
    try:
        return float(value)
    except (ValueError, TypeError):
        return default
```

**Step 2: Update log_parser.py extract_usage_from_entry()**

Use `safe_get_int` for token fields instead of direct `.get()`.

**Step 3: Update oauth_usage.py**

Use `safe_get_float` for numeric API response fields.

**Step 4: Verify**

Create malformed JSONL test file, verify no crashes.

---

## Phase 3: Test Infrastructure

### Task 6: Create Test Framework

**Files:**
- Create: `claudebar/pytest.ini`
- Create: `claudebar/tests/__init__.py`
- Create: `claudebar/tests/conftest.py`
- Create: `claudebar/tests/test_validation.py`
- Create: `claudebar/tests/test_retry.py`
- Update: `claudebar/requirements.txt`

**Step 1: Create pytest.ini**

```ini
[pytest]
testpaths = tests
python_files = test_*.py
python_functions = test_*
addopts = -v --tb=short
```

**Step 2: Create tests directory with conftest.py**

```python
import pytest
import tempfile
import json
from pathlib import Path

@pytest.fixture
def temp_jsonl_file():
    def _create(entries):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            for entry in entries:
                f.write(json.dumps(entry) + '\n')
            return Path(f.name)
    return _create
```

**Step 3: Add pytest to requirements.txt**

```
pytest>=8.0.0
pytest-cov>=4.1.0
```

**Step 4: Run tests**

Run: `cd claudebar && pytest`
Expected: All tests pass.

---

## Verification Checklist

After implementation, verify:

- [ ] App starts without errors: `python src/main.py`
- [ ] App handles network failures (disconnect WiFi, reconnect)
- [ ] App handles corrupted JSONL files without crashing
- [ ] Log file exists at `%LOCALAPPDATA%\ClaudeBar\claudebar.log`
- [ ] No tokens visible in log file (grep for "Bearer", "accessToken")
- [ ] pytest passes: `cd claudebar && pytest`

---

## Files to Modify Summary

| File | Action | Description |
|------|--------|-------------|
| src/main.py | Modify | Add global exception handler, logging init |
| src/log_parser.py | Modify | Add size/line limits, use validation |
| src/codex_log_parser.py | Modify | Add size/line limits |
| src/oauth_usage.py | Modify | Add @with_retry, use safe_get_float |
| src/openai_usage.py | Modify | Add @with_retry |
| src/currency.py | Modify | Add @with_retry |
| src/retry.py | Create | Exponential backoff decorator |
| src/logging_config.py | Create | Structured logging with redaction |
| src/validation.py | Create | Input validation helpers |
| tests/ | Create | Test infrastructure |
| pytest.ini | Create | pytest configuration |
| requirements.txt | Modify | Add pytest dependencies |
