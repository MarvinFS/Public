"""DeepSeek credential discovery: precedence and storage."""

from pathlib import Path

import secret_store
import deepseek_auth as a


class TestUserTokenNormalisation:
    """localStorage holds an object, so the obvious copy-paste is JSON."""

    def test_the_bare_token_passes_through(self):
        assert a.normalize_user_token("abc123+/=") == "abc123+/="

    def test_the_localstorage_object_is_unwrapped(self):
        assert a.normalize_user_token('{"value":"abc123","__version":"0"}') == "abc123"

    def test_surrounding_whitespace_and_quotes_are_trimmed(self):
        assert a.normalize_user_token('  "abc123"  ') == "abc123"

    def test_a_malformed_object_falls_back_to_the_raw_text(self):
        # Better to try the literal than to silently produce an empty token.
        assert a.normalize_user_token('{"value": broken') == '{"value": broken'

    def test_an_empty_object_yields_nothing(self):
        assert a.normalize_user_token('{"__version":"0"}') is None

    def test_empty_input_yields_nothing(self):
        assert a.normalize_user_token("   ") is None

    def test_alternative_keys_are_accepted(self):
        assert a.normalize_user_token('{"userToken":"xyz"}') == "xyz"


class TestPrecedence:
    """The platform token is the only DeepSeek credential there is."""

    def _isolate(self, monkeypatch, tmp_path, saved=None):
        monkeypatch.setattr(secret_store, "get_config_dir", lambda: tmp_path)
        monkeypatch.delenv("DEEPSEEK_USER_TOKEN", raising=False)
        monkeypatch.delenv("DEEPSEEK_PLATFORM_TOKEN", raising=False)
        if saved:
            secret_store.save_secret(a.USER_TOKEN_SECRET, saved)

    def test_saved_secret_beats_environment(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path, saved="tok-saved")
        monkeypatch.setenv("DEEPSEEK_USER_TOKEN", "tok-env")
        found = a.discover_user_token()
        assert found.value == "tok-saved"
        assert found.source == "saved"

    def test_environment_is_used_when_nothing_is_saved(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        monkeypatch.setenv("DEEPSEEK_USER_TOKEN", "tok-env")
        assert a.discover_user_token().source == "env"

    def test_nothing_configured_is_none(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        assert a.discover_user_token() is None

    def test_an_empty_environment_variable_is_ignored(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        monkeypatch.setenv("DEEPSEEK_USER_TOKEN", "   ")
        assert a.discover_user_token() is None

    def test_a_pasted_object_is_stored_unwrapped(self, monkeypatch, tmp_path):
        """localStorage holds an object, so the obvious paste is JSON."""
        self._isolate(monkeypatch, tmp_path)
        assert a.save_user_token('{"value":"the-real-token","__version":"0"}')
        assert a.discover_user_token().value == "the-real-token"

    def test_another_application_credential_store_is_not_read(self, monkeypatch, tmp_path):
        """The DeepSeek Harness store used to be read here, which produced a
        balance with nothing signed in and nothing on screen explaining it."""
        self._isolate(monkeypatch, tmp_path)
        harness = tmp_path / ".dsh"
        harness.mkdir()
        (harness / ".credentials.yaml").write_text(
            chr(10).join(["version: 1", "refs:"]) + chr(10)
            + "  DEEPSEEK_USER_TOKEN: tok-from-harness" + chr(10),
            encoding="utf-8")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        assert a.discover_user_token() is None

    def test_clear_removes_the_token(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        a.save_user_token("tok")
        a.clear_credentials()
        assert a.discover_user_token() is None
