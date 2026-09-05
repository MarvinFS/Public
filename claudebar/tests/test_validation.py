"""Tests for validation module."""

from validation import safe_get_int, safe_get_float


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
