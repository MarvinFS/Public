"""Tests for validation module."""

import pytest
from validation import safe_get_int, safe_get_float, validate_jsonl_entry


class TestSafeGetInt:
    """Tests for safe_get_int function."""

    def test_valid_int(self):
        data = {"count": 42}
        assert safe_get_int(data, "count") == 42

    def test_missing_key(self):
        data = {"other": 1}
        assert safe_get_int(data, "count") == 0

    def test_missing_key_with_default(self):
        data = {"other": 1}
        assert safe_get_int(data, "count", default=10) == 10

    def test_none_value(self):
        data = {"count": None}
        assert safe_get_int(data, "count") == 0

    def test_string_value(self):
        data = {"count": "invalid"}
        assert safe_get_int(data, "count") == 0

    def test_string_number(self):
        data = {"count": "42"}
        assert safe_get_int(data, "count") == 42

    def test_float_value(self):
        data = {"count": 42.7}
        assert safe_get_int(data, "count") == 42


class TestSafeGetFloat:
    """Tests for safe_get_float function."""

    def test_valid_float(self):
        data = {"amount": 3.14}
        assert safe_get_float(data, "amount") == 3.14

    def test_valid_int(self):
        data = {"amount": 42}
        assert safe_get_float(data, "amount") == 42.0

    def test_missing_key(self):
        data = {"other": 1}
        assert safe_get_float(data, "amount") == 0.0

    def test_missing_key_with_default(self):
        data = {"other": 1}
        assert safe_get_float(data, "amount", default=1.5) == 1.5

    def test_none_value(self):
        data = {"amount": None}
        assert safe_get_float(data, "amount") == 0.0

    def test_string_value(self):
        data = {"amount": "invalid"}
        assert safe_get_float(data, "amount") == 0.0

    def test_string_number(self):
        data = {"amount": "3.14"}
        assert safe_get_float(data, "amount") == 3.14


class TestValidateJsonlEntry:
    """Tests for validate_jsonl_entry function."""

    def test_valid_assistant_entry(self):
        entry = {
            "type": "assistant",
            "message": {"model": "claude-3", "usage": {}},
        }
        assert validate_jsonl_entry(entry) is None

    def test_valid_non_assistant_entry(self):
        entry = {"type": "user", "content": "hello"}
        assert validate_jsonl_entry(entry) is None

    def test_non_dict_entry(self):
        error = validate_jsonl_entry("not a dict")
        assert error is not None
        assert error.field == "entry"

    def test_non_dict_message(self):
        entry = {"type": "assistant", "message": "string instead of dict"}
        error = validate_jsonl_entry(entry)
        assert error is not None
        assert error.field == "message"
