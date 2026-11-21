"""Tests for Google Antigravity auth, quota fetching, local DB reader, pricing, cache, and UI."""

import io
import json
import sqlite3
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import antigravity_auth
import antigravity_usage
import model_catalog
import snapshot_cache
import ui_window
from config import Config
from models import AntigravitySnapshot, CombinedSnapshot, Engine, ModelUsage, QuotaBucket, TokenUsage


UTC = timezone.utc

# Exact captured retrieveUserQuotaSummary payload structure from live probe.
LIVE_SUMMARY_FIXTURE = {
    "groups": [
        {
            "displayName": "Gemini",
            "buckets": [
                {
                    "bucketId": "gemini-5h",
                    "displayName": "5-hour",
                    "remainingFraction": 0.252,
                    "resetTime": "2026-11-20T21:15:00Z",
                    "window": "5h",
                    "disabled": False,
                },
                {
                    "bucketId": "gemini-weekly",
                    "displayName": "Weekly",
                    "remainingFraction": 0.281,
                    "resetTime": "2026-11-26T18:00:00Z",
                    "window": "weekly",
                    "disabled": False,
                },
            ],
        },
        {
            "displayName": "Gemini 4 Argon",
            "buckets": [
                {
                    "bucketId": "gemini-argon-weekly",
                    "displayName": "Weekly",
                    "remainingFraction": 1.0,
                    "resetTime": "2026-11-27T20:00:00Z",
                    "window": "weekly",
                    "disabled": False,
                }
            ],
        },
        {
            "displayName": "Claude & GPT",
            "buckets": [
                {
                    "bucketId": "3p-5h",
                    "displayName": "5-hour",
                    "remainingFraction": 1.0,
                    "resetTime": "2026-11-21T01:00:00Z",
                    "window": "5h",
                    "disabled": False,
                },
                {
                    "bucketId": "3p-weekly",
                    "displayName": "Weekly",
                    "remainingFraction": 1.0,
                    "resetTime": "2026-11-27T20:00:00Z",
                    "window": "weekly",
                    "disabled": False,
                },
            ],
        },
    ]
}


# ---- Protobuf builder helpers for fixture SQLite DBs -----------------------

def _pb_varint(val: int) -> bytes:
    out = bytearray()
    while True:
        b = val & 0x7F
        val >>= 7
        if val:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    return bytes(out)


def _pb_tag(field_num: int, wire_type: int) -> bytes:
    return _pb_varint((field_num << 3) | wire_type)


def _pb_f_varint(field_num: int, val: int) -> bytes:
    return _pb_tag(field_num, 0) + _pb_varint(val)


def _pb_f_bytes(field_num: int, payload: bytes) -> bytes:
    return _pb_tag(field_num, 2) + _pb_varint(len(payload)) + payload


def _pb_f_str(field_num: int, text: str) -> bytes:
    return _pb_f_bytes(field_num, text.encode("utf-8"))


def _pb_timestamp(secs: int) -> bytes:
    return _pb_f_varint(1, secs)


def _make_usage_blob(
    model_id: int = 0,
    uncached: int = 0,
    output: int = 0,
    cache_create: int = 0,
    cached: int = 0,
    reasoning: int = 0,
    visible_out: int = 0,
    key7: str = "",
    key11: str = "",
    key12: str = "",
) -> bytes:
    buf = bytearray()
    if model_id:
        buf += _pb_f_varint(1, model_id)
    if uncached:
        buf += _pb_f_varint(2, uncached)
    if output:
        buf += _pb_f_varint(3, output)
    if cache_create:
        buf += _pb_f_varint(4, cache_create)
    if cached:
        buf += _pb_f_varint(5, cached)
    if key7:
        buf += _pb_f_str(7, key7)
    if reasoning:
        buf += _pb_f_varint(9, reasoning)
    if visible_out:
        buf += _pb_f_varint(10, visible_out)
    if key11:
        buf += _pb_f_str(11, key11)
    if key12:
        buf += _pb_f_str(12, key12)
    return bytes(buf)


def _make_gen_metadata_blob(
    model_name: str,
    model_id: int,
    ts_secs: int,
    usage_blob: bytes,
    retries: list[bytes] = (),
) -> bytes:
    inner = bytearray()
    if model_id:
        inner += _pb_f_varint(3, model_id)
    if usage_blob:
        inner += _pb_f_bytes(4, usage_blob)
    if ts_secs:
        inner += _pb_f_bytes(9, _pb_f_bytes(4, _pb_timestamp(ts_secs)))
    for r in retries:
        inner += _pb_f_bytes(17, _pb_f_bytes(2, r))
    if model_name:
        inner += _pb_f_str(19, model_name)
    return _pb_f_bytes(1, bytes(inner))


def _make_step_metadata_blob(
    model_name: str,
    model_id: int,
    ts_secs: int,
    usage_blob: bytes,
) -> bytes:
    buf = bytearray()
    if ts_secs:
        buf += _pb_f_bytes(8, _pb_timestamp(ts_secs))
    if usage_blob:
        buf += _pb_f_bytes(9, usage_blob)
    model_msg = bytearray()
    if model_id:
        model_msg += _pb_f_varint(1, model_id)
    if model_name:
        model_msg += _pb_f_str(12, model_name)
    if model_msg:
        buf += _pb_f_bytes(24, bytes(model_msg))
    return bytes(buf)


# ---- Static guard: zero Google OAuth client secrets in src/ ----------------

def test_no_google_oauth_client_secrets_in_src():
    src_dir = Path(__file__).resolve().parent.parent / "src"
    for path in src_dir.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "GOCSPX-" not in text, f"Hardcoded Google client secret found in {path.name}"


# ---- antigravity_auth tests ------------------------------------------------

class TestAntigravityAuth:
    def test_uses_strictly_claudebar_profile_never_other_apps(self):
        now = datetime(2026, 11, 20, 20, 0, tzinfo=UTC)
        future = (now + timedelta(minutes=30)).isoformat()

        assert antigravity_auth.discover_credential(now=now) is None
        assert antigravity_auth.has_claudebar_profile_token() is False
        assert antigravity_auth.claudebar_account_status(now=now) == (False, None, None)

        cb_path = antigravity_auth.get_claudebar_token_path()
        cb_path.parent.mkdir(parents=True, exist_ok=True)
        cb_path.write_text(json.dumps({
            "token": {"access_token": "valid-cb", "expiry": future},
            "auth_method": "oauth-personal",
            "id_token_email": "cb@example.com",
        }), encoding="utf-8")

        cred = antigravity_auth.discover_credential(now=now)
        assert cred is not None and cred.access_token == "valid-cb"
        assert cred.email == "cb@example.com" and cred.source == "claudebar"
        assert antigravity_auth.claudebar_account_status(now=now) == (True, "cb@example.com", "claudebar")

        assert antigravity_auth.sign_out_claudebar_profile() is True
        assert antigravity_auth.discover_credential(now=now) is None
        assert antigravity_auth.has_claudebar_profile_token() is False

    def test_expired_token_triggers_silent_agy_refresh(self, monkeypatch):
        now = datetime.now(UTC)
        cb_path = antigravity_auth.get_claudebar_token_path()
        cb_path.parent.mkdir(parents=True, exist_ok=True)
        cb_path.write_text(json.dumps({
            "token": {"access_token": "expired-tok", "expiry": (now - timedelta(minutes=5)).isoformat()},
            "auth_method": "oauth-personal",
            "id_token_email": "cb@example.com",
        }), encoding="utf-8")

        refreshed = antigravity_auth.AntigravityCredential(
            access_token="fresh-tok",
            expires_at=now + timedelta(hours=1),
            auth_method="oauth-personal",
            email="cb@example.com",
            source="claudebar",
        )
        refresh_calls = []
        monkeypatch.setattr(antigravity_auth, "refresh_claudebar_token", lambda: refresh_calls.append(1) or refreshed)
        seen_auth = []

        def fake_req(url, headers, **kw):
            seen_auth.append(headers.get("Authorization"))
            if url.endswith("loadCodeAssist"):
                return {"paidTier": {"id": "g1-pro-tier", "name": "Google AI Pro"}}
            return LIVE_SUMMARY_FIXTURE

        monkeypatch.setattr(antigravity_usage.http_json, "request_json", fake_req)
        data = antigravity_usage.fetch_antigravity_usage()
        assert data.is_valid and refresh_calls == [1]
        assert all(h == "Bearer fresh-tok" for h in seen_auth)

    def test_validate_authorization_url_and_login_code(self):
        good = (
            "https://accounts.google.com/o/oauth2/v2/auth?"
            "response_type=code&redirect_uri=https%3A%2F%2Fantigravity.google%2Foauth-callback"
        )
        assert antigravity_auth.validate_authorization_url(good) == good
        assert antigravity_auth.validate_authorization_url("https://evil.example/o/oauth2/auth?response_type=code") is None
        assert antigravity_auth.validate_authorization_url(
            "https://accounts.google.com/o/oauth2/auth?response_type=token&redirect_uri=https://antigravity.google/oauth-callback"
        ) is None

        assert antigravity_auth.normalize_login_code("4/0AbC_123-xyz") == "4/0AbC_123-xyz"
        cb_url = "https://antigravity.google/oauth-callback?code=4%2F0AbC&state=ok"
        assert antigravity_auth.normalize_login_code(cb_url) == cb_url
        assert antigravity_auth.normalize_login_code("https://evil.example/oauth-callback?code=123") is None
        assert antigravity_auth.normalize_login_code("line1\nline2") is None

    def test_open_url_preserves_ampersands_without_cmd(self, monkeypatch):
        import os
        opened = []
        if hasattr(ui_window, "open_uri"):
            monkeypatch.setattr(ui_window.open_uri, "open_url", lambda url: opened.append(url))
        else:
            monkeypatch.setattr(os, "startfile", lambda url: opened.append(url), raising=False)
        url = (
            "https://accounts.google.com/o/oauth2/auth?"
            "access_type=offline&client_id=123&response_type=code&state=xyz"
        )
        ui_window._open_url(url)
        assert opened == [url]

    def test_install_agy_windows_x64_and_linux_tar_gz(self, monkeypatch, tmp_path):
        import hashlib
        import tarfile

        # 1. Windows x64 binary download & SHA-512 check (real manifest uses cli_windows_x64.exe)
        win_bytes = b"MZ-fake-agy-exe"
        win_sha = hashlib.sha512(win_bytes).hexdigest()
        win_url = (
            "https://storage.googleapis.com/antigravity-public/antigravity-cli/"
            "1.5.13-5999302598852608/windows-x64/cli_windows_x64.exe"
        )
        win_dest = tmp_path / "win" / "bin" / "agy.exe"
        monkeypatch.setattr(antigravity_auth, "_cli_target",
                            lambda: ("windows_amd64", "windows-x64", "cli_windows_x64.exe", False))
        monkeypatch.setattr(antigravity_auth, "get_managed_agy_path", lambda: win_dest)
        monkeypatch.setattr(antigravity_auth.http_json, "request_json",
                            lambda url, headers, **kw: {"version": "1.5.13", "url": win_url, "sha512": win_sha})
        monkeypatch.setattr(antigravity_auth.urllib.request, "urlopen",
                            lambda req, timeout=300: io.BytesIO(win_bytes))

        got_win = antigravity_auth.install_agy_executable()
        assert got_win == win_dest and win_dest.read_bytes() == win_bytes

        # 2. Linux x64 .tar.gz download, SHA-512 check, and binary extraction
        tar_buf = io.BytesIO()
        linux_bytes = b"#!/bin/sh\necho ok\n"
        with tarfile.open(fileobj=tar_buf, mode="w:gz") as tf:
            info = tarfile.TarInfo("antigravity")
            info.size = len(linux_bytes)
            info.mode = 0o755
            tf.addfile(info, io.BytesIO(linux_bytes))
        tar_bytes = tar_buf.getvalue()
        tar_sha = hashlib.sha512(tar_bytes).hexdigest()
        linux_url = (
            "https://storage.googleapis.com/antigravity-public/antigravity-cli/"
            "1.5.13-5999302598852608/linux-x64/cli_linux_x64.tar.gz"
        )
        linux_dest = tmp_path / "linux" / "bin" / "antigravity"
        monkeypatch.setattr(antigravity_auth, "_cli_target",
                            lambda: ("linux_amd64", "linux-x64", "cli_linux_x64.tar.gz", True))
        monkeypatch.setattr(antigravity_auth, "get_managed_agy_path", lambda: linux_dest)
        monkeypatch.setattr(antigravity_auth.http_json, "request_json",
                            lambda url, headers, **kw: {"version": "1.5.13", "url": linux_url, "sha512": tar_sha})
        monkeypatch.setattr(antigravity_auth.urllib.request, "urlopen",
                            lambda req, timeout=300: io.BytesIO(tar_bytes))

        got_linux = antigravity_auth.install_agy_executable()
        assert got_linux == linux_dest and linux_dest.read_bytes() == linux_bytes

        # 3. Corrupted payload fails SHA-512 verification and leaves no partial file
        bad_dest = tmp_path / "bad" / "bin" / "agy.exe"
        monkeypatch.setattr(antigravity_auth, "get_managed_agy_path", lambda: bad_dest)
        monkeypatch.setattr(antigravity_auth.urllib.request, "urlopen",
                            lambda req, timeout=300: io.BytesIO(b"tampered"))
        with pytest.raises(RuntimeError, match="SHA-512"):
            antigravity_auth.install_agy_executable()
        assert not bad_dest.exists()


# ---- antigravity_usage quota tests -----------------------------------------

class TestAntigravityQuota:
    def test_parse_live_summary_fixture_and_untouched_reset_suppression(self):
        req_at = datetime(2026, 11, 20, 20, 0, 0, tzinfo=UTC)
        buckets = antigravity_usage.parse_summary_buckets(LIVE_SUMMARY_FIXTURE, requested_at=req_at)
        five_h, weekly, extra = antigravity_usage._split_primary_and_extra(buckets)

        assert five_h is not None and five_h.percent == pytest.approx(74.8)
        assert five_h.resets_at == datetime(2026, 11, 20, 21, 15, 0, tzinfo=UTC)
        assert weekly is not None and weekly.percent == pytest.approx(71.9)
        assert [b.bucket_id for b in extra] == ["gemini-argon-weekly", "3p-5h", "3p-weekly"]
        assert [b.label for b in extra] == [
            "Gemini 4 Argon · Weekly",
            "Claude & GPT · 5-hour",
            "Claude & GPT · Weekly",
        ]
        # All three untouched buckets had rolling full-window resetTime == req_at + window -> suppressed!
        assert all(b.resets_at is None and b.reset_str is None for b in extra)

    def test_first_host_wins_and_tier_cached_once(self, monkeypatch):
        now = datetime.now(UTC)
        cred = antigravity_auth.AntigravityCredential(
            access_token="tok-123",
            expires_at=now + timedelta(hours=1),
            auth_method="oauth-personal",
            email="u@example.com",
            source="claudebar",
        )
        calls = []

        def fake_request_json(url, headers, timeout=10, method="GET", body=None, **kw):
            calls.append((url, headers.get("User-Agent", "")))
            if url.endswith("loadCodeAssist"):
                return {
                    "paidTier": {"id": "g1-pro-tier", "name": "Google AI Pro", "usesGcpTos": False},
                    "cloudaicompanionProject": "proj-1",
                }
            if "daily-cloudcode-pa" in url:
                return LIVE_SUMMARY_FIXTURE
            # Prod host reports 100% untouched; daily host (first) must win for every overlapping bucketId.
            return {
                "groups": [
                    {
                        "displayName": "Gemini",
                        "buckets": [
                            {"bucketId": "gemini-5h", "remainingFraction": 1.0, "window": "5h"},
                            {"bucketId": "gemini-weekly", "remainingFraction": 1.0, "window": "weekly"},
                        ],
                    }
                ]
            }

        monkeypatch.setattr(antigravity_usage.http_json, "request_json", fake_request_json)

        first = antigravity_usage.fetch_antigravity_usage(credential=cred)
        second = antigravity_usage.fetch_antigravity_usage(credential=cred)

        assert first.is_valid and first.plan_type == "AI Pro"
        assert first.session_percent == pytest.approx(74.8)
        assert second.session_percent == pytest.approx(74.8)
        # loadCodeAssist called once (cached for 24h across both fetches).
        assert sum(1 for u, _ in calls if u.endswith("loadCodeAssist")) == 1
        assert all(f"antigravity/hub/{antigravity_usage.ANTIGRAVITY_HUB_VERSION}" in ua for _, ua in calls)

    def test_empty_summary_falls_back_to_fetch_available_models(self, monkeypatch):
        now = datetime.now(UTC)
        cred = antigravity_auth.AntigravityCredential(
            access_token="tok", expires_at=now + timedelta(hours=1), source="claudebar"
        )

        def fake_request_json(url, headers, **kw):
            if url.endswith("loadCodeAssist"):
                return {"currentTier": {"id": "free-tier", "name": "Antigravity"}}
            if url.endswith("retrieveUserQuotaSummary"):
                return {"groups": []}
            if url.endswith("fetchAvailableModels"):
                return {
                    "models": {
                        "gemini-3.1-pro": {
                            "modelProvider": "MODEL_PROVIDER_GOOGLE",
                            "weeklyQuotaInfo": {"remainingFraction": 0.4, "resetTime": (now + timedelta(days=2)).isoformat()},
                        },
                        "claude-sonnet-4-6": {
                            "modelProvider": "MODEL_PROVIDER_ANTHROPIC",
                            "quotaInfo": {"remainingFraction": 0.75},
                        },
                    }
                }
            return {}

        monkeypatch.setattr(antigravity_usage.http_json, "request_json", fake_request_json)
        data = antigravity_usage.fetch_antigravity_usage(credential=cred)
        assert data.is_valid
        assert data.plan_type == "Free"
        assert data.weekly is not None and data.weekly.percent == pytest.approx(60.0)
        assert any(b.label == "Claude · Quota" and b.used_percent == pytest.approx(25.0) for b in data.extra_buckets)

    def test_403_terms_trips_circuit_breaker(self, monkeypatch):
        now = datetime.now(UTC)
        cred = antigravity_auth.AntigravityCredential(
            access_token="tok", expires_at=now + timedelta(hours=1), source="claudebar"
        )
        attempts = []

        def fake_request_json(url, headers, **kw):
            attempts.append(url)
            if url.endswith("loadCodeAssist"):
                return {}
            raise urllib.error.HTTPError(
                url, 403, "Forbidden", {}, io.BytesIO(b'{"error":"terms violation"}')
            )

        monkeypatch.setattr(antigravity_usage.http_json, "request_json", fake_request_json)
        first = antigravity_usage.fetch_antigravity_usage(credential=cred)
        assert not first.is_valid and "403" in (first.error or "")
        before = len(attempts)
        second = antigravity_usage.fetch_antigravity_usage(credential=cred)
        assert not second.is_valid and "403" in (second.error or "")
        assert len(attempts) == before  # circuit-breaker stopped further HTTP calls


# ---- Local SQLite/protobuf reader & pricing tests --------------------------

class TestAntigravityLocalDbAndPricing:
    def test_reads_dedups_learns_models_normalizes_effort_and_caches(self, tmp_path):
        now = datetime(2026, 11, 20, 15, 0, 0)
        ts_today = int(now.timestamp())
        ts_prev = int((now - timedelta(days=2)).timestamp())

        # DB 1: has both steps and gen_metadata sharing key 11 ("req-dup") plus a retry,
        # and teaches model_id 1500 -> "gemini-4-neon-xhigh".
        db1 = tmp_path / "conv1.db"
        conn = sqlite3.connect(db1)
        conn.execute("CREATE TABLE gen_metadata (idx INTEGER, data BLOB)")
        conn.execute("CREATE TABLE steps (idx INTEGER, metadata BLOB)")
        conn.execute("CREATE TABLE trajectory_metadata_blob (data BLOB)")

        u_main = _make_usage_blob(model_id=1500, uncached=100_000, output=10_000, cached=50_000, key11="req-dup")
        u_step_smaller = _make_usage_blob(model_id=1500, uncached=80_000, output=10_000, cached=50_000, key11="req-dup")
        u_retry = _make_usage_blob(model_id=1500, uncached=50_000, output=5_000, key11="req-retry")

        conn.execute(
            "INSERT INTO gen_metadata VALUES (?, ?)",
            (0, _make_gen_metadata_blob("gemini-4-neon-xhigh", 1500, ts_today, u_main, retries=[u_retry])),
        )
        conn.execute(
            "INSERT INTO steps VALUES (?, ?)",
            (0, _make_step_metadata_blob("", 1500, ts_today, u_step_smaller)),
        )
        conn.commit()
        conn.close()

        # DB 2: references model_id 1500 with NO name -> must learn "gemini-4-neon-xhigh" from DB 1!
        # Also has an unknown model_id 9999 -> honest fallback "antigravity-model-9999" ($0.00).
        db2 = tmp_path / "conv2.db"
        conn2 = sqlite3.connect(db2)
        conn2.execute("CREATE TABLE gen_metadata (idx INTEGER, data BLOB)")
        u_learned = _make_usage_blob(model_id=1500, uncached=20_000, output=2_000, key12="req-db2")
        u_unknown = _make_usage_blob(model_id=9999, uncached=1_000, output=500, key12="req-unk")
        conn2.execute("INSERT INTO gen_metadata VALUES (?, ?)", (0, _make_gen_metadata_blob("", 1500, ts_prev, u_learned)))
        conn2.execute("INSERT INTO gen_metadata VALUES (?, ?)", (1, _make_gen_metadata_blob("", 9999, ts_prev, u_unknown)))
        conn2.commit()
        conn2.close()

        # Unrelated summary DB with no gen_metadata/steps -> cleanly skipped.
        summary_db = tmp_path / "conversation_summaries.db"
        conn3 = sqlite3.connect(summary_db)
        conn3.execute("CREATE TABLE summaries (id TEXT, text TEXT)")
        conn3.commit()
        conn3.close()

        usage = antigravity_usage.get_antigravity_local_usage([tmp_path], days=31, end=now)
        assert usage.logs_available is True
        # Deduped today: (100k + 50k cached + 10k out) + (50k + 5k out) = 215k tokens (NOT 355k!)
        assert usage.today_tokens.total_tokens == 215_000
        # Effort suffix "-xhigh" normalized to "gemini-4-neon"
        model_names = [m.model for m in usage.models_used]
        assert "gemini-4-neon" in model_names
        assert "antigravity-model-9999" in model_names
        assert "gemini-2.5-pro" not in model_names
        assert usage.today_cost_usd > 0
        assert len(usage.daily_costs) == 31

        # Second call hits DB cache (no re-read if files unchanged).
        cached_usage = antigravity_usage.get_antigravity_local_usage([tmp_path], days=31, end=now)
        assert cached_usage.today_tokens.total_tokens == usage.today_tokens.total_tokens

    def test_pricing_long_context_claude_gpt_and_catalog_overlay(self):
        # Standard <=200k Gemini 3.1 Pro: $2/1M in, $12/1M out, $0.20/1M cached
        c_short = antigravity_usage.calculate_antigravity_cost("gemini-3.1-pro", 100_000, 10_000, 50_000)
        assert c_short == pytest.approx((100_000 * 2.0 + 10_000 * 12.0 + 50_000 * 0.20) / 1_000_000)
        # >200k prompt switches to long-context tier ($4/1M in, $18/1M out, $0.40/1M cached)
        c_long = antigravity_usage.calculate_antigravity_cost("gemini-3.1-pro", 150_000, 10_000, 100_000)
        assert c_long == pytest.approx((150_000 * 4.0 + 10_000 * 18.0 + 100_000 * 0.40) / 1_000_000)
        # Effort suffix xhigh/max strips cleanly
        assert antigravity_usage.antigravity_rate_model("gemini-4-neon-xhigh") == "gemini-4-neon"
        assert antigravity_usage.antigravity_rate_model("gemini-4-argon-max") == "gemini-4-argon"
        # Delegates Claude & GPT models cleanly
        assert antigravity_usage.calculate_antigravity_cost("claude-sonnet-4-6", 100_000, 10_000) > 0
        assert antigravity_usage.calculate_antigravity_cost("gpt-5", 100_000, 10_000) > 0
        # Unknown non-gemini returns 0.0
        assert antigravity_usage.calculate_antigravity_cost("antigravity-model-9999", 100_000, 10_000) == 0.0

        # models.dev "google" overlay updates GEMINI_PRICING
        assert "google" in model_catalog.PROVIDERS
        antigravity_usage.load_catalog({
            "gemini-9-test": {"cost": {"input": 1.0, "output": 5.0, "cache_read": 0.1}}
        })
        try:
            assert antigravity_usage.calculate_antigravity_cost("gemini-9-test", 1_000_000, 0) == pytest.approx(1.0)
        finally:
            antigravity_usage.GEMINI_PRICING.pop("gemini-9-test", None)


# ---- Cache & UI tests ------------------------------------------------------

def test_antigravity_snapshot_cache_roundtrip():
    now = datetime(2026, 11, 20, 20, 0, tzinfo=UTC)
    snap = AntigravitySnapshot(
        timestamp=datetime(2026, 11, 20, 20, 0),
        session_percent=74.8,
        session_resets_at=now + timedelta(hours=1),
        weekly_percent=71.9,
        weekly_resets_at=now + timedelta(days=5),
        extra_buckets=[
            QuotaBucket("gemini-argon-weekly", "Gemini 4 Argon · Weekly", "weekly", 0.0, None, None, 168.0),
            QuotaBucket("3p-5h", "Claude & GPT · 5-hour", "5h", 12.0, now + timedelta(hours=3), "in 3h", 5.0),
        ],
        plan_type="AI Pro",
        email="user@example.com",
        today_cost_usd=4.5,
        month_cost_usd=19.2,
        today_tokens=TokenUsage(input_tokens=500_000, output_tokens=20_000),
        models_used=[ModelUsage("gemini-4-neon", cost_usd=19.2, message_count=10)],
        available=True,
        logs_available=True,
    )
    snapshot_cache.save_cache(antigravity=snap)
    loaded, cached_at = snapshot_cache.load_antigravity_cache()
    assert loaded is not None and cached_at is not None
    assert loaded.is_stale is True
    assert loaded.plan_type == "AI Pro"
    assert loaded.session_percent == pytest.approx(74.8)
    assert len(loaded.extra_buckets) == 2
    assert loaded.extra_buckets[1].label == "Claude & GPT · 5-hour"
    assert loaded.models_used[0].model == "gemini-4-neon"


def test_panel_renders_antigravity_view_and_buckets(tk_root, monkeypatch):
    monkeypatch.setattr(ui_window.ClaudeBarWindow, "_load_initial_data", lambda self: None)
    monkeypatch.setattr(ui_window, "save_config", lambda cfg: None)
    now = datetime.now(UTC)
    ag = AntigravitySnapshot(
        timestamp=datetime.now(),
        session_percent=74.8,
        session_resets_at=now + timedelta(hours=1, minutes=10),
        weekly_percent=71.9,
        weekly_resets_at=now + timedelta(days=5, hours=22),
        extra_buckets=[
            QuotaBucket("gemini-argon-weekly", "Gemini 4 Argon · Weekly", "weekly", 0.0, None, None, 168.0),
            QuotaBucket("3p-5h", "Claude & GPT · 5-hour", "5h", 15.0, now + timedelta(hours=3), "in 3h", 5.0),
            QuotaBucket("3p-weekly", "Claude & GPT · Weekly", "weekly", 0.0, None, None, 168.0),
        ],
        plan_type="AI Pro",
        today_cost_usd=14.20,
        month_cost_usd=88.50,
        last31_cost_usd=101.00,
        last31_tokens=90_000_000,
        today_tokens=TokenUsage(input_tokens=10_000_000),
        month_tokens=TokenUsage(input_tokens=60_000_000),
        daily_costs=[1.0] * 31,
        models_used=[ModelUsage("gemini-4-neon", cost_usd=88.50, message_count=50)],
        available=True,
        logs_available=True,
    )
    panel = ui_window.ClaudeBarWindow(
        on_refresh=lambda: None, on_settings=lambda: None, on_exit=lambda: None, config=Config()
    )
    panel._create_window()
    try:
        panel.update_combined(CombinedSnapshot(antigravity=ag))
        panel._apply_snapshot_update()
        panel._on_engine_select(Engine.ANTIGRAVITY)

        assert panel._engine_name.cget("text") == "Google"
        assert panel._status_text.cget("text") == "AI Pro"
        assert panel._limits["session"]["title"].cget("text") == "Gemini (5-hour)"
        assert panel._limits["weekly"]["title"].cget("text") == "Gemini (Weekly)"
        assert panel._extra_bucket_count == 3
        assert panel._extra_bucket_rows[0]["name"].cget("text") == "Gemini 4 Argon · Weekly"
        assert panel._extra_bucket_rows[0]["reset"].cget("text") == ""
        assert panel._extra_bucket_rows[1]["name"].cget("text") == "Claude & GPT · 5-hour"
        assert panel._extra_bucket_rows[1]["reset"].cget("text") != ""
        assert "gemini-4-neon" in panel._top_model.cget("text")
        assert panel._source_label.cget("text") == "Estimated from local Antigravity logs"
    finally:
        panel.destroy()
