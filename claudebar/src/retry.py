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

# Exceptions that should never be retried (permanent failures)
NON_RETRYABLE_EXCEPTIONS = (
    urllib.error.HTTPError,
)


def with_retry(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    exceptions: tuple = RETRYABLE_EXCEPTIONS,
) -> Callable:
    """Decorator for retry with exponential backoff.

    Args:
        max_attempts: Maximum number of attempts before giving up.
        base_delay: Initial delay between retries in seconds.
        max_delay: Maximum delay between retries in seconds.
        exceptions: Tuple of exception types to catch and retry.

    Note: HTTPError is excluded from retries since the decorated functions
    handle HTTP errors internally and retrying permanent errors (429 with
    Retry-After: 0, 401, etc.) wastes time.
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        def wrapper(*args, **kwargs) -> T:
            last_exception = None
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except NON_RETRYABLE_EXCEPTIONS:
                    # HTTPError and other permanent failures - don't retry
                    raise
                except exceptions as e:
                    last_exception = e
                    if attempt < max_attempts - 1:
                        delay = min(base_delay * (2 ** attempt), max_delay)
                        time.sleep(delay)
            raise last_exception
        return wrapper
    return decorator
