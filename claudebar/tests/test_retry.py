"""Tests for retry module."""

import pytest
import urllib.error
from retry import with_retry, RETRYABLE_EXCEPTIONS


class TestWithRetry:
    """Tests for with_retry decorator."""

    def test_success_first_try(self):
        call_count = 0

        @with_retry(max_attempts=3)
        def succeeds():
            nonlocal call_count
            call_count += 1
            return "success"

        result = succeeds()
        assert result == "success"
        assert call_count == 1

    def test_success_after_retry(self):
        call_count = 0

        @with_retry(max_attempts=3, base_delay=0.01)
        def fails_then_succeeds():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise ConnectionError("temporary failure")
            return "success"

        result = fails_then_succeeds()
        assert result == "success"
        assert call_count == 2

    def test_max_attempts_exceeded(self):
        call_count = 0

        @with_retry(max_attempts=3, base_delay=0.01)
        def always_fails():
            nonlocal call_count
            call_count += 1
            raise ConnectionError("permanent failure")

        with pytest.raises(ConnectionError):
            always_fails()
        assert call_count == 3

    def test_non_retryable_exception(self):
        call_count = 0

        @with_retry(max_attempts=3, base_delay=0.01)
        def raises_value_error():
            nonlocal call_count
            call_count += 1
            raise ValueError("not retryable")

        with pytest.raises(ValueError):
            raises_value_error()
        assert call_count == 1

    def test_custom_exceptions(self):
        call_count = 0

        @with_retry(max_attempts=3, base_delay=0.01, exceptions=(ValueError,))
        def raises_value_error():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise ValueError("retryable")
            return "success"

        result = raises_value_error()
        assert result == "success"
        assert call_count == 2

    def test_preserves_function_metadata(self):
        @with_retry()
        def documented_function():
            """This is the docstring."""
            pass

        assert documented_function.__name__ == "documented_function"
        assert documented_function.__doc__ == "This is the docstring."


class TestRetryableExceptions:
    """Tests for RETRYABLE_EXCEPTIONS tuple."""

    def test_url_error_is_retryable(self):
        assert urllib.error.URLError in RETRYABLE_EXCEPTIONS

    def test_connection_error_is_retryable(self):
        assert ConnectionError in RETRYABLE_EXCEPTIONS

    def test_timeout_error_is_retryable(self):
        assert TimeoutError in RETRYABLE_EXCEPTIONS
