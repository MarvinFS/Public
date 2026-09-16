"""The four ways a Claude OAuth token can be unusable, and what the log says.

"No OAuth token available" used to cover all of them, which made the most
frequent line in the log the least useful one.
"""

import json
import time

import oauth_usage


def _patch_creds(monkeypatch, tmp_path, payload=None, raw=None):
    creds = tmp_path / ".credentials.json"
    creds.parent.mkdir(parents=True, exist_ok=True)
    if raw is not None:
        creds.write_text(raw, encoding="utf-8")
    elif payload is not None:
        creds.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(oauth_usage, "get_credentials_path", lambda: creds)
    return creds


def test_missing_file_names_the_path(monkeypatch, tmp_path):
    creds = _patch_creds(monkeypatch, tmp_path)
    token, reason = oauth_usage.access_token_or_reason()

    assert token is None
    assert "no credentials file" in reason
    assert str(creds) in reason


def test_unreadable_file_says_so(monkeypatch, tmp_path):
    _patch_creds(monkeypatch, tmp_path, raw="{not json")
    token, reason = oauth_usage.access_token_or_reason()

    assert token is None
    assert "could not be read" in reason


def test_missing_oauth_block_says_so(monkeypatch, tmp_path):
    _patch_creds(monkeypatch, tmp_path, {"apiKey": "sk-xxx"})
    token, reason = oauth_usage.access_token_or_reason()

    assert token is None
    assert "no Claude OAuth token" in reason


def test_missing_access_token_field_says_so(monkeypatch, tmp_path):
    _patch_creds(monkeypatch, tmp_path,
                 {"claudeAiOauth": {"expiresAt": time.time() * 1000 + 3_600_000}})
    token, reason = oauth_usage.access_token_or_reason()

    assert token is None
    assert "no Claude OAuth token" in reason


def test_expired_token_names_the_expiry_and_the_fix(monkeypatch, tmp_path):
    expired_ms = (time.time() - 3_600) * 1000
    _patch_creds(monkeypatch, tmp_path,
                 {"claudeAiOauth": {"accessToken": "tok", "expiresAt": expired_ms}})
    token, reason = oauth_usage.access_token_or_reason()

    assert token is None
    assert "expired at" in reason
    assert "run Claude Code" in reason


def test_valid_token_has_no_reason(monkeypatch, tmp_path):
    _patch_creds(monkeypatch, tmp_path,
                 {"claudeAiOauth": {"accessToken": "tok",
                                    "expiresAt": time.time() * 1000 + 3_600_000}})
    token, reason = oauth_usage.access_token_or_reason()

    assert token == "tok"
    assert reason is None


def test_non_dict_document_does_not_raise(monkeypatch, tmp_path):
    _patch_creds(monkeypatch, tmp_path, raw='["unexpected"]')
    token, reason = oauth_usage.access_token_or_reason()

    assert token is None
    assert reason


def test_fetch_reports_the_specific_reason(monkeypatch, tmp_path):
    _patch_creds(monkeypatch, tmp_path, {"apiKey": "sk-xxx"})
    data = oauth_usage.fetch_oauth_usage()

    assert data.is_valid is False
    assert "no Claude OAuth token" in data.error
