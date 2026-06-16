"""Passive token reader + credential hardening checks (no network).

Covers the Codex round 1-3 edge cases: claudeAiOauth None/non-dict/truthy
non-dict, expiresAt None/string/past/future, apiKey-only creds, and an expired
OAuth token reporting "Token expired" rather than falling through to authed.
"""

import json
import time

import oauth_usage
import claude_check


def _write_creds(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _patch_creds(monkeypatch, tmp_path, payload):
    """Point both readers at a temp .credentials.json with the given payload."""
    creds = tmp_path / ".credentials.json"
    _write_creds(creds, payload)
    monkeypatch.setattr(oauth_usage, "get_credentials_path", lambda: creds)
    monkeypatch.setattr(claude_check, "get_claude_dir", lambda: tmp_path)
    return creds


def _future_ms():
    return time.time() * 1000 + 3_600_000  # +1h


def _past_ms():
    return time.time() * 1000 - 3_600_000  # -1h


# --- load_access_token: valid / expired / missing -------------------------

def test_token_valid_returned(monkeypatch, tmp_path):
    _patch_creds(monkeypatch, tmp_path,
                 {"claudeAiOauth": {"accessToken": "tok", "expiresAt": _future_ms()}})
    assert oauth_usage.load_access_token() == "tok"


def test_token_expired_returns_none(monkeypatch, tmp_path):
    _patch_creds(monkeypatch, tmp_path,
                 {"claudeAiOauth": {"accessToken": "tok", "expiresAt": _past_ms()}})
    assert oauth_usage.load_access_token() is None


def test_token_missing_returns_none(monkeypatch, tmp_path):
    _patch_creds(monkeypatch, tmp_path, {"claudeAiOauth": {"expiresAt": _future_ms()}})
    assert oauth_usage.load_access_token() is None


def test_token_no_expiry_returned(monkeypatch, tmp_path):
    # No usable expiresAt -> treat as non-expiring, hand the token over.
    _patch_creds(monkeypatch, tmp_path, {"claudeAiOauth": {"accessToken": "tok"}})
    assert oauth_usage.load_access_token() == "tok"


# --- load_credentials hardening (Codex r1 #1 / r2 #1) ---------------------

def test_oauth_none_does_not_crash(monkeypatch, tmp_path):
    _patch_creds(monkeypatch, tmp_path, {"claudeAiOauth": None})
    assert oauth_usage.load_credentials() == (None, None)


def test_oauth_truthy_non_dict_does_not_crash(monkeypatch, tmp_path):
    _patch_creds(monkeypatch, tmp_path, {"claudeAiOauth": "bad"})
    assert oauth_usage.load_credentials() == (None, None)


def test_expiresat_string_treated_as_no_expiry(monkeypatch, tmp_path):
    _patch_creds(monkeypatch, tmp_path,
                 {"claudeAiOauth": {"accessToken": "tok", "expiresAt": "soon"}})
    token, exp = oauth_usage.load_credentials()
    assert token == "tok" and exp is None
    assert oauth_usage.load_access_token() == "tok"


def test_expiresat_bool_not_treated_as_number(monkeypatch, tmp_path):
    _patch_creds(monkeypatch, tmp_path,
                 {"claudeAiOauth": {"accessToken": "tok", "expiresAt": True}})
    _, exp = oauth_usage.load_credentials()
    assert exp is None


# --- check_credentials (Codex r1 #7 / r2 #2) ------------------------------

def test_apikey_only_stays_authenticated(monkeypatch, tmp_path):
    _patch_creds(monkeypatch, tmp_path, {"apiKey": "sk-xxx"})
    ok, _creds, err = claude_check.check_credentials()
    assert ok is True and err is None


def test_expired_oauth_reports_token_expired(monkeypatch, tmp_path):
    _patch_creds(monkeypatch, tmp_path,
                 {"claudeAiOauth": {"accessToken": "tok", "expiresAt": _past_ms()}})
    ok, _creds, err = claude_check.check_credentials()
    assert ok is False and err == "Token expired"


def test_valid_oauth_authenticated(monkeypatch, tmp_path):
    _patch_creds(monkeypatch, tmp_path,
                 {"claudeAiOauth": {"accessToken": "tok", "expiresAt": _future_ms()}})
    ok, _creds, err = claude_check.check_credentials()
    assert ok is True and err is None
