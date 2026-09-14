"""Secret storage: encrypted round trip, and graceful failure without DPAPI."""

import secret_store
import deepseek_auth as a


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(secret_store, "get_config_dir", lambda: tmp_path)


def test_round_trip(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    assert secret_store.save_secret("k", "sk-value")
    assert secret_store.load_secret("k") == "sk-value"


def test_missing_secret_is_none(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    assert secret_store.load_secret("never-written") is None


def test_delete_removes_it(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    secret_store.save_secret("k", "v")
    secret_store.delete_secret("k")
    assert secret_store.load_secret("k") is None


def test_delete_of_a_missing_secret_is_silent(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    secret_store.delete_secret("never-written")


def test_the_blob_is_not_plaintext(monkeypatch, tmp_path):
    """The whole point of DPAPI: the file must not carry the secret."""
    _isolate(monkeypatch, tmp_path)
    secret_store.save_secret(a.USER_TOKEN_SECRET, "SUPERSECRET-TOKEN")
    blob = secret_store.secret_path(a.USER_TOKEN_SECRET).read_bytes()
    assert b"SUPERSECRET-TOKEN" not in blob


def test_the_secret_never_lands_in_config_json(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    secret_store.save_secret(a.USER_TOKEN_SECRET, "SUPERSECRET-TOKEN")
    written = [p.name for p in tmp_path.rglob("*") if p.is_file()]
    assert written and all("config.json" not in name for name in written)


def test_saving_fails_cleanly_when_dpapi_is_unavailable(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(secret_store, "protect", lambda text: None)
    assert secret_store.save_secret("k", "v") is False
    assert secret_store.load_secret("k") is None


def test_an_unreadable_blob_is_none(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    path = secret_store.secret_path("broken")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not base64 !!")
    assert secret_store.load_secret("broken") is None


def test_unprotect_of_foreign_bytes_is_none(monkeypatch, tmp_path):
    """Bytes from another account or machine must not decode into a token."""
    _isolate(monkeypatch, tmp_path)
    assert secret_store.unprotect(b"\x00\x01\x02not-a-dpapi-blob") is None
