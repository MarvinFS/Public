"""Fetch Google Antigravity subscription limits and local conversation DB usage.

Quota collection uses read-only access tokens discovered by `antigravity_auth`
(zero hardcoded OAuth client secrets, never refreshes OAuth tokens directly).
Local token and API-equivalent cost statistics are decoded from Antigravity's
SQLite conversation databases (ported from T3 Code's `antigravityUsageReader.ts`).
"""

import json
import logging
import math
import os
import platform
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import antigravity_auth
import codex_pricing
import http_json
import pricing
from models import (
    DAILY_HISTORY_DAYS,
    ModelUsage,
    QuotaBucket,
    SESSION_WINDOW_HOURS,
    TokenUsage,
    WEEKLY_WINDOW_HOURS,
    reset_words,
)

logger = logging.getLogger("claudebar")

# Named version constants (mirrors CLAUDE_CLI_VERSION in oauth_usage.py).
ANTIGRAVITY_HUB_VERSION = "2.24.0"
ANTIGRAVITY_HUB_CL = "963137146"

DAILY_ENDPOINT = "https://daily-cloudcode-pa.googleapis.com"
PROD_ENDPOINT = "https://cloudcode-pa.googleapis.com"
_ENDPOINTS = {
    "oauth-personal": (DAILY_ENDPOINT, PROD_ENDPOINT),
    "oauth-business": (PROD_ENDPOINT, DAILY_ENDPOINT),
}
_SUMMARY_WINDOW_HOURS = {
    "5h": float(SESSION_WINDOW_HOURS),
    "weekly": float(WEEKLY_WINDOW_HOURS),
}

# Tier metadata from loadCodeAssist changes rarely; cache per token/account for 24 h.
_TIER_CACHE_TTL_SECONDS = 24 * 3600
_tier_cache: dict[str, tuple[float, Optional[str], Optional[str], bool]] = {}
_tier_lock = threading.Lock()

# Circuit-breaker: if Google returns HTTP 403 indicating terms/service disabled,
# stop polling for the rest of the session instead of repeating every 5 minutes.
_service_disabled_reason: Optional[str] = None
_state_lock = threading.Lock()


def antigravity_user_agent() -> str:
    """User-Agent matching T3 Code's `antigravityUsageLimits.ts`."""
    os_name = "win32" if sys.platform == "win32" else ("darwin" if sys.platform == "darwin" else "linux")
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
    return (
        f"antigravity/hub/{ANTIGRAVITY_HUB_VERSION} "
        f"(aidev_client; os_type={os_name}; arch={arch}; cl={ANTIGRAVITY_HUB_CL})"
    )


def reset_runtime_state() -> None:
    """Clear in-memory tier cache, circuit-breaker, and DB cache (for tests / sign-out)."""
    global _service_disabled_reason
    with _state_lock:
        _service_disabled_reason = None
    with _tier_lock:
        _tier_cache.clear()
    reset_db_cache()


@dataclass
class AntigravityUsageWindow:
    """Primary rate-limit window (5-hour session or weekly)."""
    percent: float = 0.0
    resets_at: Optional[datetime] = None
    window_seconds: int = 0
    bucket_id: str = ""
    label: str = ""

    @property
    def reset_str(self) -> Optional[str]:
        return reset_words(self.resets_at)


@dataclass
class AntigravityUsageData:
    """Rate-limit and subscription tier data from Google Antigravity."""
    five_hour: Optional[AntigravityUsageWindow] = None
    weekly: Optional[AntigravityUsageWindow] = None
    extra_buckets: list[QuotaBucket] = field(default_factory=list)
    plan_type: Optional[str] = None
    email: Optional[str] = None
    error: Optional[str] = None

    @property
    def is_valid(self) -> bool:
        return self.error is None and (
            self.five_hour is not None or self.weekly is not None or bool(self.extra_buckets)
        )

    @property
    def has_session_window(self) -> bool:
        return self.five_hour is not None

    @property
    def session_percent(self) -> float:
        return self.five_hour.percent if self.five_hour else 0.0

    @property
    def weekly_percent(self) -> float:
        return self.weekly.percent if self.weekly else 0.0

    @property
    def session_reset(self) -> Optional[str]:
        return self.five_hour.reset_str if self.five_hour else None

    @property
    def weekly_reset(self) -> Optional[str]:
        return self.weekly.reset_str if self.weekly else None


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _summary_reset_time(
    bucket: dict,
    window_hours: Optional[float],
    requested_at: datetime,
) -> Optional[datetime]:
    """Suppress rolling full-window reset times on untouched buckets (T3 `summaryResetTime`)."""
    resets_at = _parse_iso(bucket.get("resetTime"))
    if resets_at is None:
        return None
    rem = bucket.get("remainingFraction")
    if (
        rem == 1
        and bucket.get("description") is None
        and window_hours is not None
        and abs((resets_at - requested_at).total_seconds() - window_hours * 3600) <= 60
    ):
        return None
    return resets_at


def parse_summary_buckets(data: dict, requested_at: Optional[datetime] = None) -> list[QuotaBucket]:
    """Parse `retrieveUserQuotaSummary` JSON into normalized `QuotaBucket` rows."""
    if not isinstance(data, dict):
        return []
    requested_at = requested_at or datetime.now(timezone.utc)
    raw_groups = data.get("groups")
    if isinstance(raw_groups, list) and any(
        isinstance(g, dict) and isinstance(g.get("buckets"), list) and g["buckets"]
        for g in raw_groups
    ):
        groups = [g for g in raw_groups if isinstance(g, dict)]
    elif isinstance(data.get("buckets"), list):
        groups = [{"buckets": data["buckets"]}]
    else:
        return []

    by_id: dict[str, QuotaBucket] = {}
    for group in groups:
        group_name = str(group.get("displayName") or "").strip()
        for bucket in group.get("buckets") or []:
            if not isinstance(bucket, dict) or bucket.get("disabled"):
                continue
            bid = bucket.get("bucketId")
            if not isinstance(bid, str) or not bid.strip():
                continue
            bid = bid.strip()
            rem = bucket.get("remainingFraction")
            if isinstance(rem, bool) or not isinstance(rem, (int, float)) or not math.isfinite(rem):
                continue
            used = (1.0 - max(0.0, min(1.0, float(rem)))) * 100.0
            window_raw = str(bucket.get("window") or "").strip()
            window_hours = _SUMMARY_WINDOW_HOURS.get(window_raw)

            if bid.startswith("gemini-argon-"):
                family = group_name or "Gemini Argon"
            elif bid.startswith("gemini-carbon-"):
                family = group_name or "Gemini Carbon"
            elif bid.startswith("gemini-"):
                family = "Gemini"
            elif bid.startswith("3p-"):
                family = "Claude & GPT"
            else:
                family = group_name or bid

            if window_raw == "weekly":
                cadence = "Weekly"
                window_key = "weekly"
            elif window_raw == "5h":
                cadence = "5-hour"
                window_key = "5h"
            else:
                cadence = str(bucket.get("displayName") or "").strip() or "Quota"
                window_key = "other"

            resets_at = _summary_reset_time(bucket, window_hours, requested_at)
            by_id[bid] = QuotaBucket(
                bucket_id=bid,
                label=f"{family} \u00b7 {cadence}",
                window=window_key,
                used_percent=used,
                resets_at=resets_at,
                reset_str=reset_words(resets_at),
                window_hours=window_hours or 0.0,
            )
    return list(by_id.values())


def parse_models_fallback(data: dict) -> list[QuotaBucket]:
    """Parse `fetchAvailableModels` when `retrieveUserQuotaSummary` returns no buckets."""
    if not isinstance(data, dict) or not isinstance(data.get("models"), dict):
        return []
    by_id: dict[str, QuotaBucket] = {}
    for model_id, info in data["models"].items():
        if not isinstance(info, dict):
            continue
        provider = str(info.get("modelProvider") or "")
        mid = str(model_id)
        if provider == "MODEL_PROVIDER_GOOGLE" or mid.startswith("gemini-"):
            family = "Gemini"
        elif provider == "MODEL_PROVIDER_ANTHROPIC" or mid.startswith("claude-"):
            family = "Claude"
        elif provider == "MODEL_PROVIDER_OPENAI" or mid.startswith("gpt-"):
            family = "GPT"
        else:
            continue
        for suffix, key, window_key, window_hours in (
            ("quota", "quotaInfo", "other", 0.0),
            ("weekly", "weeklyQuotaInfo", "weekly", float(WEEKLY_WINDOW_HOURS)),
        ):
            quota = info.get(key)
            if not isinstance(quota, dict):
                continue
            rem = quota.get("remainingFraction")
            if isinstance(rem, bool) or not isinstance(rem, (int, float)) or not math.isfinite(rem):
                continue
            used = (1.0 - max(0.0, min(1.0, float(rem)))) * 100.0
            resets_at = _parse_iso(quota.get("resetTime"))
            bid = f"{family.lower()}-{suffix}"
            prev = by_id.get(bid)
            if prev is None or used > prev.used_percent:
                by_id[bid] = QuotaBucket(
                    bucket_id=bid,
                    label=f"{family} \u00b7 {'Weekly' if suffix == 'weekly' else 'Quota'}",
                    window=window_key,
                    used_percent=used,
                    resets_at=resets_at,
                    reset_str=reset_words(resets_at),
                    window_hours=window_hours,
                )
    return list(by_id.values())


def _split_primary_and_extra(
    buckets: list[QuotaBucket],
) -> tuple[Optional[AntigravityUsageWindow], Optional[AntigravityUsageWindow], list[QuotaBucket]]:
    """Separate primary Gemini 5h/weekly windows from secondary buckets."""
    five_hour: Optional[AntigravityUsageWindow] = None
    weekly: Optional[AntigravityUsageWindow] = None
    extra: list[QuotaBucket] = []

    for b in buckets:
        is_base_gemini = (
            b.bucket_id.startswith("gemini-")
            and not b.bucket_id.startswith("gemini-argon-")
            and not b.bucket_id.startswith("gemini-carbon-")
        )
        if is_base_gemini and b.window == "5h" and five_hour is None:
            five_hour = AntigravityUsageWindow(
                percent=b.used_percent,
                resets_at=b.resets_at,
                window_seconds=int(b.window_hours * 3600) or SESSION_WINDOW_HOURS * 3600,
                bucket_id=b.bucket_id,
                label=b.label,
            )
        elif is_base_gemini and b.window == "weekly" and weekly is None:
            weekly = AntigravityUsageWindow(
                percent=b.used_percent,
                resets_at=b.resets_at,
                window_seconds=int(b.window_hours * 3600) or WEEKLY_WINDOW_HOURS * 3600,
                bucket_id=b.bucket_id,
                label=b.label,
            )
        else:
            extra.append(b)

    # Sort secondary buckets cleanly: Argon/Carbon first, then 3P 5h before 3P weekly, then others.
    def _order(b: QuotaBucket) -> tuple[int, int, str]:
        if b.bucket_id.startswith("gemini-argon-"):
            grp = 0
        elif b.bucket_id.startswith("gemini-carbon-"):
            grp = 1
        elif b.bucket_id.startswith("3p-"):
            grp = 2
        else:
            grp = 3
        win = 0 if b.window == "5h" else (1 if b.window == "weekly" else 2)
        return grp, win, b.label

    extra.sort(key=_order)
    return five_hour, weekly, extra


def _parse_tier_plan(data: dict) -> tuple[Optional[str], Optional[str], bool]:
    """Extract (plan_badge, project_id, uses_gcp_tos) from `loadCodeAssist`."""
    if not isinstance(data, dict):
        return None, None, False
    paid = data.get("paidTier") if isinstance(data.get("paidTier"), dict) else None
    current = data.get("currentTier") if isinstance(data.get("currentTier"), dict) else None
    tier = paid or current or {}
    raw_name = str(tier.get("name") or "").strip()
    raw_id = str(tier.get("id") or "").strip().lower()
    if "ultra" in raw_id or "ultra" in raw_name.lower():
        plan = "AI Ultra"
    elif "pro" in raw_id or "pro" in raw_name.lower():
        plan = "AI Pro"
    elif "free" in raw_id or raw_name.lower() == "antigravity":
        plan = "Free"
    elif raw_name:
        plan = raw_name
    else:
        plan = None
    project = data.get("cloudaicompanionProject")
    project_id = project.strip() if isinstance(project, str) and project.strip() else None
    uses_gcp_tos = bool(tier.get("usesGcpTos"))
    return plan, project_id, uses_gcp_tos


def _headers(access_token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": antigravity_user_agent(),
    }


def _ccpa_post(endpoint: str, operation: str, access_token: str, body: dict) -> dict:
    url = f"{endpoint}/v1internal:{operation}"
    data = http_json.request_json(url, _headers(access_token), timeout=10, method="POST", body=body)
    return data if isinstance(data, dict) else {}


def _check_403_disable(exc: urllib.error.HTTPError) -> Optional[str]:
    """Inspect a 403 error body for terms/disabled signals and trip circuit-breaker if found."""
    global _service_disabled_reason
    if exc.code != 403:
        return None
    try:
        body = exc.read().decode("utf-8", errors="ignore").lower()
    except Exception:
        body = ""
    if any(needle in body for needle in ("terms", "disabled", "suspended", "violation", "subscription_required")):
        msg = "Antigravity access restricted (HTTP 403); polling paused"
        with _state_lock:
            _service_disabled_reason = msg
        logger.warning("%s", msg)
        return msg
    return None


def _load_cached_tier(
    cred: antigravity_auth.AntigravityCredential,
) -> tuple[Optional[str], Optional[str], bool]:
    cache_key = cred.email or cred.source or "default"
    now_mono = time.monotonic()
    with _tier_lock:
        hit = _tier_cache.get(cache_key)
        if hit and now_mono - hit[0] < _TIER_CACHE_TTL_SECONDS:
            return hit[1], hit[2], hit[3]

    endpoint = _ENDPOINTS.get(cred.auth_method, _ENDPOINTS["oauth-personal"])[0]
    body = {
        "metadata": {
            "ideType": "ANTIGRAVITY",
            "platform": "WINDOWS_AMD64" if sys.platform == "win32" else "LINUX_AMD64",
            "pluginType": "GEMINI",
        }
    }
    try:
        resp = _ccpa_post(endpoint, "loadCodeAssist", cred.access_token, body)
        plan, project_id, uses_gcp_tos = _parse_tier_plan(resp)
        with _tier_lock:
            _tier_cache[cache_key] = (now_mono, plan, project_id, uses_gcp_tos)
        return plan, project_id, uses_gcp_tos
    except urllib.error.HTTPError as exc:
        _check_403_disable(exc)
        return None, None, False
    except Exception:
        return None, None, False


def fetch_antigravity_usage(
    credential: Optional[antigravity_auth.AntigravityCredential] = None,
) -> AntigravityUsageData:
    """Fetch Antigravity rate limits and plan tier using a read-only access token."""
    with _state_lock:
        if _service_disabled_reason:
            return AntigravityUsageData(error=_service_disabled_reason)

    cred = credential if credential is not None else antigravity_auth.discover_credential()
    if cred is None:
        return AntigravityUsageData(error="Sign in to Google in Settings")
    if not cred.is_valid():
        if credential is None:
            renewed = antigravity_auth.refresh_claudebar_token()
            if renewed is not None and renewed.is_valid():
                cred = renewed
        if not cred.is_valid():
            return AntigravityUsageData(
                email=cred.email,
                error="Google sign-in expired - sign in again in Settings",
            )

    plan_type, project_id, uses_gcp_tos = _load_cached_tier(cred)
    with _state_lock:
        if _service_disabled_reason:
            return AntigravityUsageData(email=cred.email, error=_service_disabled_reason)

    auth_key = "oauth-business" if (cred.auth_method == "oauth-business" or uses_gcp_tos) else "oauth-personal"
    endpoints = _ENDPOINTS[auth_key]
    body = {"project": project_id} if project_id else {}

    merged: dict[str, QuotaBucket] = {}
    last_error: Optional[str] = None
    for ep in endpoints:
        try:
            requested_at = datetime.now(timezone.utc)
            resp = _ccpa_post(ep, "retrieveUserQuotaSummary", cred.access_token, body)
            for b in parse_summary_buckets(resp, requested_at=requested_at):
                if b.bucket_id not in merged:
                    merged[b.bucket_id] = b
        except urllib.error.HTTPError as exc:
            disabled = _check_403_disable(exc)
            if disabled:
                return AntigravityUsageData(email=cred.email, error=disabled)
            if exc.code == 401:
                return AntigravityUsageData(
                    email=cred.email,
                    error="Antigravity token expired or invalid",
                )
            last_error = f"HTTP {exc.code}: {exc.reason}"
        except urllib.error.URLError as exc:
            last_error = f"Network error: {exc.reason}"
        except Exception as exc:
            last_error = str(exc)

    # Match upstream antigravityUsageLimits.ts: fall back to fetchAvailableModels
    # only when retrieveUserQuotaSummary returned zero buckets.
    if not merged:
        for ep in endpoints:
            try:
                resp = _ccpa_post(ep, "fetchAvailableModels", cred.access_token, body)
                for b in parse_models_fallback(resp):
                    if b.bucket_id not in merged:
                        merged[b.bucket_id] = b
            except urllib.error.HTTPError as exc:
                disabled = _check_403_disable(exc)
                if disabled:
                    return AntigravityUsageData(email=cred.email, error=disabled)
                if exc.code == 401:
                    return AntigravityUsageData(
                        email=cred.email, error="Antigravity token expired or invalid"
                    )
                last_error = f"HTTP {exc.code}: {exc.reason}"
            except Exception as exc:
                last_error = str(exc)

    if not merged:
        return AntigravityUsageData(
            email=cred.email,
            plan_type=plan_type,
            error=last_error or "Antigravity returned no quota data",
        )

    five_hour, weekly, extra = _split_primary_and_extra(list(merged.values()))
    return AntigravityUsageData(
        five_hour=five_hour,
        weekly=weekly,
        extra_buckets=extra,
        plan_type=plan_type,
        email=cred.email,
    )


# ---------------------------------------------------------------------------
# Local Antigravity SQLite/protobuf conversation usage reader & pricing
# Ported cleanly from pingdotgg/t3code `antigravityUsageReader.ts`.
# ---------------------------------------------------------------------------

MODEL_IDS: dict[int, str] = {
    246: "gemini-2.5-pro",
    312: "gemini-2.5-flash",
    313: "gemini-2.5-flash-thinking",
    329: "gemini-2.5-flash-thinking",
    330: "gemini-2.5-flash-lite",
    281: "claude-sonnet-4",
    282: "claude-sonnet-4",
    290: "claude-opus-4",
    291: "claude-opus-4",
    333: "claude-sonnet-4-5",
    334: "claude-sonnet-4-5",
    340: "claude-haiku-4-5",
    341: "claude-haiku-4-5",
    342: "gpt-oss-120b-medium",
    1016: "gemini-3.1-pro",
    1018: "gemini-3-flash-preview",
    1020: "gemini-3.5-flash-medium",
    1021: "gemini-3.1-flash-image",
    1026: "claude-opus-4-6",
    1035: "claude-sonnet-4-6",
    1036: "gemini-3.1-pro",
    1037: "gemini-3.1-pro",
    1047: "gemini-3-flash-preview",
    1050: "gemini-3.1-flash-lite",
    1071: "gemini-3.6-flash-high",
    1072: "gemini-3.6-flash-medium",
    1073: "gemini-3.6-flash-low",
    1084: "gemini-3-flash-preview",
    1132: "gemini-3.5-flash-high",
    1133: "gemini-3.5-flash-high",
    1187: "gemini-3.5-flash-extra-low",
    1196: "gemini-3.6-flash",
    1266: "gemini-3.6-flash",
    1298: "gemini-3.7-flash-high",
    1299: "gemini-3.7-flash-medium",
    1300: "gemini-3.7-flash-low",
    1301: "gemini-3.7-flash-tiered",
    1318: "gemini-3.8-flash-high",
    1319: "gemini-3.8-flash-medium",
    1320: "gemini-3.8-flash-low",
    1322: "gemini-3.8-flash-tiered",
    1402: "claude-opus-5-5",
    1417: "gemini-4-argon",
    1418: "gemini-4-argon",
    1419: "gemini-4-argon",
}

_EFFORT_SUFFIX_RE = re.compile(r"-(?:extra-low|low|medium|high|xhigh|max|tiered|control|thinking|agent)$")
_FLASH_AGENT_RE = re.compile(r"^gemini-3-flash-(?:agent(?:-[ab])?|[ab])$")
_CONCRETE_MODEL_RE = re.compile(r"^(?:gemini-\d|claude-(?:sonnet|opus|haiku)-\d|gpt-)")

# USD per 1M tokens: (input, output, cache_read, cache_write, long_input, long_output, long_cache_read)
# Overlays from models.dev ("google" provider) update this table at runtime.
GEMINI_PRICING: dict[str, tuple[float, float, float, float, float, float, float]] = {
    "gemini-4-argon": (2.00, 12.00, 0.20, 2.00, 4.00, 18.00, 0.40),
    "gemini-4-neon": (2.00, 12.00, 0.20, 2.00, 4.00, 18.00, 0.40),
    "gemini-4-hydrogen": (1.25, 10.00, 0.125, 1.25, 2.50, 15.00, 0.25),
    "gemini-3.8-flash": (0.50, 3.00, 0.05, 0.50, 0.50, 3.00, 0.05),
    "gemini-3.7-flash": (0.50, 3.00, 0.05, 0.50, 0.50, 3.00, 0.05),
    "gemini-3.6-flash": (0.50, 3.00, 0.05, 0.50, 0.50, 3.00, 0.05),
    "gemini-3.5-flash": (0.50, 3.00, 0.05, 0.50, 0.50, 3.00, 0.05),
    "gemini-3.1-pro": (2.00, 12.00, 0.20, 2.00, 4.00, 18.00, 0.40),
    "gemini-3.1-flash-lite": (0.25, 1.50, 0.025, 0.25, 0.25, 1.50, 0.025),
    "gemini-3-flash-preview": (0.50, 3.00, 0.05, 0.50, 0.50, 3.00, 0.05),
    "gemini-2.5-pro": (1.25, 10.00, 0.125, 1.25, 2.50, 15.00, 0.25),
    "gemini-2.5-flash": (0.30, 2.50, 0.03, 0.30, 0.30, 2.50, 0.03),
    "gemini-2.5-flash-lite": (0.10, 0.40, 0.01, 0.10, 0.10, 0.40, 0.01),
}
_LONG_CONTEXT_THRESHOLD = 200_000


def load_catalog(models: dict) -> None:
    """Overlay models.dev Google rates onto `GEMINI_PRICING`."""
    if not isinstance(models, dict):
        return
    for model_id, entry in models.items():
        if not isinstance(model_id, str) or "gemini" not in model_id.lower():
            continue
        cost = entry.get("cost") if isinstance(entry, dict) else None
        if not isinstance(cost, dict):
            continue
        try:
            inp = float(cost["input"])
            out = float(cost["output"])
            cr = float(cost.get("cache_read", inp * 0.1))
            cw = float(cost.get("cache_write", inp))
            long_inp, long_out, long_cr = inp, out, cr
            over = cost.get("context_over_200k")
            if isinstance(over, dict):
                long_inp = float(over.get("input", inp))
                long_out = float(over.get("output", out))
                long_cr = float(over.get("cache_read", cr))
            GEMINI_PRICING[model_id.lower()] = (inp, out, cr, cw, long_inp, long_out, long_cr)
        except (KeyError, TypeError, ValueError):
            continue


def antigravity_rate_model(model: str) -> str:
    """Normalize effort/routing suffixes to the base model used for pricing."""
    base = _EFFORT_SUFFIX_RE.sub("", model)
    if _FLASH_AGENT_RE.match(model):
        return "gemini-3.5-flash"
    if model == "gemini-3.7-flash-safety-le":
        return "gemini-3.7-flash"
    if base == "gemini-3-flash":
        return "gemini-3-flash-preview"
    return base


def _normalize_model_name(name: str) -> str:
    if not name:
        return ""
    norm = re.sub(r"\s*\(([^)]*)\)\s*$", r"-\1", name.strip().lower())
    norm = re.sub(r"\s+", "-", norm)
    if norm.startswith("claude-"):
        norm = re.sub(r"^claude-(\d+(?:\.\d+)?)-(sonnet|opus|haiku)", r"claude-\2-\1", norm)
        norm = norm.replace(".", "-")
    return norm


def _is_concrete_model(model: str) -> bool:
    return bool(_CONCRETE_MODEL_RE.match(model)) and not bool(_FLASH_AGENT_RE.match(model))


def calculate_antigravity_cost(
    model: str,
    uncached_input: int,
    output: int,
    cached_input: int = 0,
    cache_creation: int = 0,
) -> float:
    """Calculate API-equivalent USD cost for a turn run inside Antigravity."""
    rate_model = antigravity_rate_model(model.lower())
    if rate_model.startswith("claude-"):
        return pricing.calculate_cost(
            rate_model,
            TokenUsage(
                input_tokens=uncached_input,
                output_tokens=output,
                cache_read_input_tokens=cached_input,
                cache_creation_input_tokens=cache_creation,
            ),
        )
    if rate_model.startswith("gpt-"):
        return codex_pricing.calculate_cost(
            model=rate_model,
            input_tokens=uncached_input + cached_input,
            output_tokens=output,
            cached_input_tokens=cached_input,
            cache_write_input_tokens=cache_creation,
        )
    rates = GEMINI_PRICING.get(rate_model) or pricing.longest_prefix(GEMINI_PRICING, rate_model)
    if rates is None:
        if "gemini" not in rate_model:
            return 0.0
        rates = GEMINI_PRICING["gemini-3.8-flash"] if "flash" in rate_model else GEMINI_PRICING["gemini-3.1-pro"]
    inp, out, cr, cw, l_inp, l_out, l_cr = rates
    total_prompt = uncached_input + cached_input + cache_creation
    if total_prompt > _LONG_CONTEXT_THRESHOLD:
        inp, out, cr = l_inp, l_out, l_cr
    return (
        uncached_input * inp
        + output * out
        + cached_input * cr
        + cache_creation * cw
    ) / 1_000_000.0


def _pb_fields(data: bytes) -> dict[int, list]:
    """Minimal protobuf wire decoder (varint, 64-bit, length-delimited, 32-bit)."""
    result: dict[int, list] = {}
    offset = 0
    n = len(data)

    def _varint() -> int:
        nonlocal offset
        value = 0
        for shift in range(0, 70, 7):
            if offset >= n:
                raise ValueError("Truncated protobuf varint")
            b = data[offset]
            offset += 1
            if shift == 63 and b > 1:
                raise ValueError("Invalid protobuf varint")
            value |= (b & 0x7F) << shift
            if b < 0x80:
                return value
        raise ValueError("Invalid protobuf varint")

    while offset < n:
        tag = _varint()
        number = tag >> 3
        wire = tag & 7
        if number == 0:
            raise ValueError("Invalid protobuf field number")
        if wire == 0:
            val = _varint()
        elif wire == 2:
            length = _varint()
            if length > n - offset:
                raise ValueError("Truncated protobuf field")
            val = data[offset:offset + length]
            offset += length
        elif wire == 1:
            if 8 > n - offset:
                raise ValueError("Truncated fixed64")
            offset += 8
            continue
        elif wire == 5:
            if 4 > n - offset:
                raise ValueError("Truncated fixed32")
            offset += 4
            continue
        else:
            raise ValueError(f"Unsupported wire type {wire}")
        result.setdefault(number, []).append(val)
    return result


def _pb_int(fields: dict[int, list], key: int) -> int:
    vals = fields.get(key)
    return vals[0] if vals and isinstance(vals[0], int) else 0


def _pb_bytes(fields: dict[int, list], key: int) -> Optional[bytes]:
    vals = fields.get(key)
    return vals[0] if vals and isinstance(vals[0], bytes) else None


def _pb_nested(fields: dict[int, list], key: int) -> dict[int, list]:
    b = _pb_bytes(fields, key)
    return _pb_fields(b) if b is not None else {}


def _pb_text(fields: dict[int, list], key: int) -> str:
    b = _pb_bytes(fields, key)
    if b is None:
        return ""
    try:
        return b.decode("utf-8").strip()
    except UnicodeDecodeError:
        return ""


def _pb_timestamp_ms(fields: dict[int, list]) -> Optional[int]:
    secs = _pb_int(fields, 1)
    if secs <= 0:
        return None
    return secs * 1000 + (_pb_int(fields, 2) // 1_000_000)


@dataclass
class _Metadata:
    model: str
    model_id: int
    timestamp_ms: Optional[int]
    usages: list[dict[int, list]]


@dataclass
class _Candidate:
    session_id: str
    timestamp_ms: int
    model: str
    model_id: int
    model_quality: int
    timestamp_quality: int
    keys: tuple[str, ...]
    uncached_input: int
    cached_input: int
    cache_creation: int
    output: int
    reasoning: int


def _decode_metadata(blob: bytes, is_step: bool) -> _Metadata:
    root = _pb_fields(blob)
    if not is_step and _pb_bytes(root, 1) is None:
        raise ValueError("Missing Antigravity generation metadata")
    data = root if is_step else _pb_nested(root, 1)
    model_msg = _pb_nested(data, 24) if is_step else data
    usage_bytes = _pb_bytes(data, 9 if is_step else 4)
    primary_usage = _pb_fields(usage_bytes) if usage_bytes is not None else None
    usages: list[dict[int, list]] = [primary_usage] if primary_usage is not None else []
    for retry in data.get(28 if is_step else 17) or []:
        if not isinstance(retry, bytes):
            continue
        retry_usage = _pb_bytes(_pb_fields(retry), 2)
        if retry_usage is not None:
            usages.append(_pb_fields(retry_usage))
    names = [
        _normalize_model_name(_pb_text(model_msg, 12 if is_step else 19)),
        _normalize_model_name(_pb_text(model_msg, 8 if is_step else 21)),
    ]
    chosen = next((n for n in names if _is_concrete_model(n)), None)
    if chosen is None:
        chosen = next((n for n in names if n and not re.match(r"^model[_-]", n)), "")
    model_id = _pb_int(model_msg, 1 if is_step else 3) or (
        _pb_int(primary_usage, 1) if primary_usage is not None else 0
    )
    if is_step:
        ts_ms = _pb_timestamp_ms(_pb_nested(data, 8)) or _pb_timestamp_ms(_pb_nested(data, 1))
    else:
        ts_ms = _pb_timestamp_ms(_pb_nested(_pb_nested(data, 9), 4))
    return _Metadata(model=chosen, model_id=model_id, timestamp_ms=ts_ms, usages=usages)


def _remember_model(models: dict[int, Optional[str]], model_id: int, model: Optional[str]) -> None:
    if model_id not in models:
        models[model_id] = model
    elif models[model_id] != model:
        models[model_id] = None


def _read_database(db_path: Path, fallback_ts_ms: int) -> Optional[tuple[list[_Candidate], dict[int, Optional[str]]]]:
    uri = f"file:{urllib.request.pathname2url(str(db_path))}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=0.5)
    except sqlite3.Error:
        return None
    try:
        conn.execute("PRAGMA busy_timeout = 100")
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        if "gen_metadata" not in tables and "steps" not in tables:
            return None

        generations: list[tuple[int, _Metadata]] = []
        if "gen_metadata" in tables:
            for idx, data in conn.execute("SELECT idx, data FROM gen_metadata ORDER BY rowid"):
                if isinstance(idx, int) and isinstance(data, (bytes, memoryview)):
                    try:
                        generations.append((idx, _decode_metadata(bytes(data), False)))
                    except ValueError:
                        continue
            generations.sort(key=lambda pair: pair[0])

        traj_ts_ms: Optional[int] = None
        if "trajectory_metadata_blob" in tables:
            for (data,) in conn.execute("SELECT data FROM trajectory_metadata_blob"):
                if isinstance(data, (bytes, memoryview)):
                    try:
                        ts = _pb_timestamp_ms(_pb_nested(_pb_fields(bytes(data)), 2))
                        if ts is not None:
                            traj_ts_ms = ts
                            break
                    except ValueError:
                        continue

        steps: list[tuple[int, _Metadata]] = []
        if "steps" in tables:
            for idx, data in conn.execute("SELECT idx, metadata FROM steps ORDER BY rowid"):
                if data is not None and isinstance(idx, int) and isinstance(data, (bytes, memoryview)):
                    try:
                        steps.append((idx, _decode_metadata(bytes(data), True)))
                    except ValueError:
                        continue
            steps.sort(key=lambda pair: pair[0])
    except sqlite3.Error:
        return None
    finally:
        conn.close()

    learned: dict[int, Optional[str]] = {}
    for _, entry in (*generations, *steps):
        if entry.model_id > 0 and _is_concrete_model(entry.model):
            _remember_model(learned, entry.model_id, entry.model)

    gen_by_idx = {idx: entry for idx, entry in generations}
    session_id = db_path.stem
    candidates: list[_Candidate] = []
    for source, entries in (("step", steps), ("generation", generations)):
        for _, (idx, entry) in enumerate(entries):
            for usage in entry.usages:
                model_id = _pb_int(usage, 1) or entry.model_id
                gen = gen_by_idx.get(idx) if source == "step" else None
                matches = lambda m: model_id == 0 or m.model_id == model_id
                if matches(entry) and entry.model:
                    model = entry.model
                elif gen is not None and matches(gen):
                    model = gen.model
                else:
                    model = ""
                output = max(_pb_int(usage, 3), _pb_int(usage, 9) + _pb_int(usage, 10))
                uncached = _pb_int(usage, 2)
                cached = _pb_int(usage, 5)
                cache_create = _pb_int(usage, 4)
                if uncached + cached + cache_create + output == 0:
                    continue
                reasoning = min(output, _pb_int(usage, 9))
                keys = tuple(
                    f"antigravity:{k}:{tid}"
                    for k in (11, 12, 7)
                    if (tid := _pb_text(usage, k))
                )
                ts_ms = entry.timestamp_ms if entry.timestamp_ms is not None else (
                    traj_ts_ms if traj_ts_ms is not None else fallback_ts_ms
                )
                ts_quality = 2 if entry.timestamp_ms is not None else (1 if traj_ts_ms is not None else 0)
                model_quality = 4 if _is_concrete_model(model) else (1 if model else 0)
                candidates.append(_Candidate(
                    session_id=session_id,
                    timestamp_ms=ts_ms,
                    model=model,
                    model_id=model_id,
                    model_quality=model_quality,
                    timestamp_quality=ts_quality,
                    keys=keys,
                    uncached_input=uncached,
                    cached_input=cached,
                    cache_creation=cache_create,
                    output=output,
                    reasoning=reasoning,
                ))
    return candidates, learned


# Incremental per-DB cache: canonical_path -> (fingerprint, (candidates, learned_models))
_db_cache: dict[str, tuple[str, Optional[tuple[list[_Candidate], dict[int, Optional[str]]]]]] = {}
_db_lock = threading.Lock()


def reset_db_cache() -> None:
    """Forget cached conversation DB parses."""
    with _db_lock:
        _db_cache.clear()


def _stat_part(path: Path) -> Optional[tuple[os.stat_result, str]]:
    try:
        st = path.stat()
        return st, f"{st.st_size}:{st.st_mtime_ns}:{st.st_ctime_ns}"
    except OSError:
        return None


def get_conversation_dirs(home: Optional[Path] = None) -> list[Path]:
    """Return verified Antigravity conversation DB directories (deduplicated)."""
    home = home or Path.home()
    env_dirs = os.environ.get("ANTIGRAVITY_DATA_DIR", "").strip()
    raw_roots: list[Path] = []
    if env_dirs:
        for part in env_dirs.split(","):
            part = part.strip()
            if part:
                p = Path(part).expanduser()
                raw_roots.append(p / "conversations" if (p / "conversations").is_dir() else p)
    else:
        raw_roots.append(home / ".gemini" / "antigravity-cli" / "conversations")
        t3_base = home / ".t3" / "userdata" / "providers" / "antigravity"
        if t3_base.is_dir():
            try:
                for child in sorted(t3_base.iterdir()):
                    conv = child / "antigravity-acp" / "conversations"
                    if conv.is_dir():
                        raw_roots.append(conv)
            except OSError:
                pass
        cb_conv = antigravity_auth.get_antigravity_profile_dir() / "antigravity-acp" / "conversations"
        if cb_conv.is_dir():
            raw_roots.append(cb_conv)

    seen: set[str] = set()
    result: list[Path] = []
    for root in raw_roots:
        if not root.is_dir():
            continue
        try:
            key = str(root.resolve()).lower() if sys.platform == "win32" else str(root.resolve())
        except OSError:
            key = str(root)
        if key not in seen:
            seen.add(key)
            result.append(root)
    return result


@dataclass
class AntigravityLocalUsage:
    """Aggregated token and API-equivalent cost stats from local Antigravity DBs."""
    today_tokens: TokenUsage = field(default_factory=TokenUsage)
    month_tokens: TokenUsage = field(default_factory=TokenUsage)
    today_cost_usd: float = 0.0
    month_cost_usd: float = 0.0
    last31_cost_usd: float = 0.0
    last31_tokens: int = 0
    daily_costs: list[float] = field(default_factory=list)
    models_used: list[ModelUsage] = field(default_factory=list)
    logs_available: bool = False


def get_antigravity_local_usage(
    conversation_dirs: Optional[list[Path]] = None,
    days: int = DAILY_HISTORY_DAYS,
    end: Optional[datetime] = None,
) -> AntigravityLocalUsage:
    """Read, deduplicate, and price local Antigravity conversation DBs."""
    dirs = conversation_dirs if conversation_dirs is not None else get_conversation_dirs()
    last_day = (end or datetime.now()).date()
    first_day = last_day - timedelta(days=days - 1)
    month_start = last_day.replace(day=1)
    scan_start = min(first_day, month_start)
    since_ms = int(datetime(scan_start.year, scan_start.month, scan_start.day).timestamp() * 1000)

    db_files: list[Path] = []
    for d in dirs:
        if not d.is_dir():
            continue
        try:
            for p in sorted(d.rglob("*.db")):
                if p.is_file() and not p.is_symlink():
                    db_files.append(p)
        except OSError:
            continue

    if not db_files:
        return AntigravityLocalUsage(daily_costs=[0.0] * days, logs_available=False)

    learned_models: dict[int, Optional[str]] = {}
    pending: list[list[_Candidate]] = []
    visited: set[str] = set()

    with _db_lock:
        for db_path in db_files:
            try:
                canon = str(db_path.resolve())
            except OSError:
                continue
            canon_key = canon.lower() if sys.platform == "win32" else canon
            if canon_key in visited:
                continue
            visited.add(canon_key)
            main_stat = _stat_part(db_path)
            if main_stat is None:
                continue
            wal_stat = _stat_part(Path(f"{db_path}-wal"))
            fp = f"{main_stat[1]}/{wal_stat[1] if wal_stat else '-'}"
            cached = _db_cache.get(canon_key)
            if cached is not None and cached[0] == fp:
                parsed = cached[1]
            else:
                fallback_ms = int(main_stat[0].st_mtime * 1000)
                parsed = _read_database(db_path, fallback_ms)
                _db_cache[canon_key] = (fp, parsed)
            if parsed is None:
                continue
            cands, models = parsed
            for mid, mname in models.items():
                _remember_model(learned_models, mid, mname)
            pending.append(cands)

        for stale in [k for k in _db_cache if k not in visited]:
            del _db_cache[stale]

    # Union-find deduplication across steps, gen_metadata, and retries.
    groups: list[dict] = []
    identities: dict[str, int] = {}

    def _find(i: int) -> int:
        root = i
        while groups[root]["parent"] != root:
            root = groups[root]["parent"]
        while i != root:
            nxt = groups[i]["parent"]
            groups[i]["parent"] = root
            i = nxt
        return root

    def _merge(left: int, right: int) -> int:
        a, b = _find(left), _find(right)
        if a == b:
            return a
        if groups[a]["size"] < groups[b]["size"]:
            a, b = b, a
        t, s = groups[a], groups[b]
        first = t if t["owner"] < s["owner"] else s
        best_model = (
            first if s["model_quality"] == t["model_quality"]
            else (s if s["model_quality"] > t["model_quality"] else t)
        )
        best_time = (
            s if (
                s["timestamp_quality"] > t["timestamp_quality"]
                or (s["timestamp_quality"] == t["timestamp_quality"] and s["timestamp_ms"] < t["timestamp_ms"])
            ) else t
        )
        t["model"] = best_model["model"]
        t["model_quality"] = best_model["model_quality"]
        t["timestamp_ms"] = best_time["timestamp_ms"]
        t["timestamp_quality"] = best_time["timestamp_quality"]
        t["uncached_input"] = max(t["uncached_input"], s["uncached_input"])
        t["cached_input"] = max(t["cached_input"], s["cached_input"])
        t["cache_creation"] = max(t["cache_creation"], s["cache_creation"])
        t["output"] = max(t["output"], s["output"])
        t["owner"] = first["owner"]
        t["size"] += s["size"]
        s["parent"] = a
        return a

    for cands in pending:
        for cand in cands:
            learned = learned_models.get(cand.model_id)
            fallback = None if (cand.model_id in learned_models and learned is None) else MODEL_IDS.get(cand.model_id)
            model = (
                (cand.model if cand.model_quality == 4 else "")
                or learned
                or fallback
                or cand.model
                or (f"antigravity-model-{cand.model_id}" if cand.model_id > 0 else "antigravity-unknown")
            )
            mq = 4 if cand.model_quality == 4 else (3 if learned else (2 if fallback else cand.model_quality))
            idx = len(groups)
            groups.append({
                "parent": idx,
                "size": 1,
                "owner": idx,
                "model": model,
                "model_quality": mq,
                "timestamp_ms": cand.timestamp_ms,
                "timestamp_quality": cand.timestamp_quality,
                "uncached_input": cand.uncached_input,
                "cached_input": cand.cached_input,
                "cache_creation": cand.cache_creation,
                "output": cand.output,
            })
            for k in cand.keys:
                existing = identities.get(k)
                if existing is not None:
                    _merge(idx, existing)
                identities[k] = idx

    daily_cost_map: dict[date, float] = {first_day + timedelta(days=i): 0.0 for i in range(days)}
    today_tokens = TokenUsage()
    month_tokens = TokenUsage()
    today_cost = 0.0
    month_cost = 0.0
    last31_cost = 0.0
    last31_tokens = 0
    by_model: dict[str, ModelUsage] = {}

    for idx, g in enumerate(groups):
        if g["parent"] != idx or g["timestamp_ms"] < since_ms:
            continue
        turn_day = datetime.fromtimestamp(g["timestamp_ms"] / 1000.0).date()
        if turn_day > last_day:
            continue
        cost = calculate_antigravity_cost(
            g["model"], g["uncached_input"], g["output"], g["cached_input"], g["cache_creation"]
        )
        tok = TokenUsage(
            input_tokens=g["uncached_input"],
            output_tokens=g["output"],
            cache_read_input_tokens=g["cached_input"],
            cache_creation_input_tokens=g["cache_creation"],
        )
        if turn_day in daily_cost_map:
            daily_cost_map[turn_day] += cost
            last31_cost += cost
            last31_tokens += tok.total_tokens
        if turn_day == last_day:
            today_tokens = today_tokens + tok
            today_cost += cost
        if month_start <= turn_day <= last_day:
            month_tokens = month_tokens + tok
            month_cost += cost
            rate_name = antigravity_rate_model(g["model"])
            entry = by_model.get(rate_name)
            if entry is None:
                by_model[rate_name] = ModelUsage(model=rate_name, tokens=tok, cost_usd=cost, message_count=1)
            else:
                entry.tokens = entry.tokens + tok
                entry.cost_usd += cost
                entry.message_count += 1

    models_used = sorted(by_model.values(), key=lambda m: m.cost_usd, reverse=True)
    return AntigravityLocalUsage(
        today_tokens=today_tokens,
        month_tokens=month_tokens,
        today_cost_usd=today_cost,
        month_cost_usd=month_cost,
        last31_cost_usd=last31_cost,
        last31_tokens=last31_tokens,
        daily_costs=[daily_cost_map[first_day + timedelta(days=i)] for i in range(days)],
        models_used=models_used,
        logs_available=True,
    )
