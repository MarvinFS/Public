"""DeepSeek credential discovery: precedence and the harness store reader."""

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


HARNESS_BLOCK = """version: 1
records:
  {
    client-connection/browser-session:
      {
        kind: client-connection,
        payload: abc
      }
  }
refs:
  DEEPSEEK_API_KEY: sk-harness-key
"""


class TestHarnessReader:
    def test_reads_a_scalar_under_refs(self, tmp_path):
        path = tmp_path / ".credentials.yaml"
        path.write_text(HARNESS_BLOCK, encoding="utf-8")
        assert a.read_harness_ref("DEEPSEEK_API_KEY", path) == "sk-harness-key"

    def test_strips_surrounding_quotes(self, tmp_path):
        path = tmp_path / ".credentials.yaml"
        path.write_text('refs:\n  DEEPSEEK_API_KEY: "sk-quoted"\n', encoding="utf-8")
        assert a.read_harness_ref("DEEPSEEK_API_KEY", path) == "sk-quoted"

    def test_flow_mapping_on_one_line(self, tmp_path):
        path = tmp_path / ".credentials.yaml"
        path.write_text("refs: {DEEPSEEK_API_KEY: sk-flow}\n", encoding="utf-8")
        assert a.read_harness_ref("DEEPSEEK_API_KEY", path) == "sk-flow"

    def test_stops_at_the_next_top_level_key(self, tmp_path):
        path = tmp_path / ".credentials.yaml"
        path.write_text("refs:\n  OTHER: x\nDEEPSEEK_API_KEY: sk-too-late\n", encoding="utf-8")
        assert a.read_harness_ref("DEEPSEEK_API_KEY", path) is None

    def test_missing_file_is_none(self, tmp_path):
        assert a.read_harness_ref("DEEPSEEK_API_KEY", tmp_path / "nope.yaml") is None

    def test_unknown_ref_is_none(self, tmp_path):
        path = tmp_path / ".credentials.yaml"
        path.write_text(HARNESS_BLOCK, encoding="utf-8")
        assert a.read_harness_ref("DEEPSEEK_USER_TOKEN", path) is None

    def test_a_changed_format_degrades_instead_of_raising(self, tmp_path):
        path = tmp_path / ".credentials.yaml"
        path.write_text("\x00\x01 not: [valid\n  yaml at all\n", encoding="utf-8")
        assert a.read_harness_ref("DEEPSEEK_API_KEY", path) is None


class TestPrecedence:
    def _isolate(self, monkeypatch, tmp_path, saved=None):
        monkeypatch.setattr(secret_store, "get_config_dir", lambda: tmp_path)
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.delenv("DEEPSEEK_KEY", raising=False)
        monkeypatch.delenv("DEEPSEEK_USER_TOKEN", raising=False)
        monkeypatch.delenv("DEEPSEEK_PLATFORM_TOKEN", raising=False)
        monkeypatch.setattr(a, "HARNESS_CREDENTIALS", tmp_path / "absent.yaml")
        if saved:
            secret_store.save_secret(a.API_KEY_SECRET, saved)

    def test_saved_secret_beats_environment_and_harness(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path, saved="sk-saved")
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-env")
        harness = tmp_path / ".credentials.yaml"
        harness.write_text(HARNESS_BLOCK, encoding="utf-8")
        monkeypatch.setattr(a, "HARNESS_CREDENTIALS", harness)

        found = a.discover_api_key()
        assert found.value == "sk-saved"
        assert found.source == "saved"

    def test_environment_beats_harness(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-env")
        harness = tmp_path / ".credentials.yaml"
        harness.write_text(HARNESS_BLOCK, encoding="utf-8")
        monkeypatch.setattr(a, "HARNESS_CREDENTIALS", harness)

        found = a.discover_api_key()
        assert found.value == "sk-env"
        assert found.source == "env"

    def test_harness_is_used_when_nothing_else_is_set(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        harness = tmp_path / ".credentials.yaml"
        harness.write_text(HARNESS_BLOCK, encoding="utf-8")
        monkeypatch.setattr(a, "HARNESS_CREDENTIALS", harness)

        found = a.discover_api_key()
        assert found.value == "sk-harness-key"
        assert found.source == "harness"

    def test_nothing_configured_is_none(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        assert a.discover_api_key() is None

    def test_an_empty_environment_variable_is_ignored(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        monkeypatch.setenv("DEEPSEEK_API_KEY", "   ")
        assert a.discover_api_key() is None

    def test_user_token_is_never_auto_detected(self, monkeypatch, tmp_path):
        """The platform token only ever comes from the user or the environment."""
        self._isolate(monkeypatch, tmp_path)
        harness = tmp_path / ".credentials.yaml"
        harness.write_text(HARNESS_BLOCK, encoding="utf-8")
        monkeypatch.setattr(a, "HARNESS_CREDENTIALS", harness)
        assert a.discover_user_token() is None

    def test_saved_user_token_is_returned(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        a.save_user_token("platform-token")
        found = a.discover_user_token()
        assert found.value == "platform-token"
        assert found.source == "saved"

    def test_clear_removes_both_secrets(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        a.save_api_key("sk-x")
        a.save_user_token("tok")
        a.clear_credentials()
        assert a.discover_api_key() is None
        assert a.discover_user_token() is None

    def test_a_pasted_object_is_stored_unwrapped(self, monkeypatch, tmp_path):
        """The whole localStorage blob must never reach the Authorization header."""
        self._isolate(monkeypatch, tmp_path)
        assert a.save_user_token('{"value":"the-real-token","__version":"0"}')
        found = a.discover_user_token()
        assert found.value == "the-real-token"
