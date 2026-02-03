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
    """Decorator for retry with exponential backoff.

    Args:
        max_attempts: Maximum number of attempts before giving up.
        base_delay: Initial delay between retries in seconds.
        max_delay: Maximum delay between retries in seconds.
        exceptions: Tuple of exception types to catch and retry.

    Returns:
        Decorated function that will retry on specified exceptions.
    """
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
