"""Resolve the DeepSeek platform session for ClaudeBar.

One credential, one surface. The signed-in platform ``userToken`` unlocks the
account summary (balance and lifetime spend) and the usage endpoints (today,
this month and the last 7 days of cost, tokens and requests). ClaudeBar never
holds an account password: the user signs in on DeepSeek's own page and only
the resulting session token is kept, encrypted by secret_store.

A saved secret wins over the environment, so an explicit choice is never
silently overridden.
"""

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import secret_store

# secret_store names
# Legacy: the Settings field is gone, but a stored key from an older
# build is still cleared so it cannot linger on disk.
API_KEY_SECRET = "deepseek_api_key"
USER_TOKEN_SECRET = "deepseek_user_token"

# Environment overrides, in priority order
USER_TOKEN_ENV = ("DEEPSEEK_USER_TOKEN", "DEEPSEEK_PLATFORM_TOKEN")

@dataclass(frozen=True)
class Credential:
    """A resolved secret plus where it came from, for the Settings hint."""
    value: str
    source: str  # "saved" | "env"


def normalize_user_token(value: str) -> Optional[str]:
    """Accept either the bare platform token or the JSON that localStorage holds.

    `localStorage.getItem("userToken")` returns an object such as
    {"value":"...","__version":"0"}, and pasting that verbatim would put the
    whole blob in the Authorization header and fail. Both forms are accepted so
    the obvious copy-paste works.
    """
    text = (value or "").strip()
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return _clean(text)  # malformed: let the API give the real verdict
        if isinstance(data, dict):
            for key in ("value", "userToken", "token"):
                inner = data.get(key)
                if isinstance(inner, str) and inner.strip():
                    return _clean(inner)
        # Valid JSON carrying no token is not a token; no header is better than
        # a bogus one. (A real token is base64-ish and never starts with "{").
        return None
    return _clean(text)


def _clean(raw: str) -> Optional[str]:
    """Trim whitespace and one layer of matching quotes; None when empty."""
    value = (raw or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    return value or None


def _from_env(names: tuple[str, ...]) -> Optional[str]:
    for name in names:
        value = _clean(os.environ.get(name, ""))
        if value:
            return value
    return None


def discover_user_token() -> Optional[Credential]:
    """Saved secret, then environment. Never auto-detected from a browser."""
    saved = normalize_user_token(secret_store.load_secret(USER_TOKEN_SECRET) or "")
    if saved:
        return Credential(saved, "saved")

    from_env = normalize_user_token(_from_env(USER_TOKEN_ENV) or "")
    if from_env:
        return Credential(from_env, "env")

    return None


def save_user_token(value: str) -> bool:
    """Persist a platform userToken, unwrapping the localStorage JSON if that
    is what was pasted. False when DPAPI could not store it."""
    token = normalize_user_token(value)
    if not token:
        return False
    return secret_store.save_secret(USER_TOKEN_SECRET, token)


def clear_credentials() -> None:
    """Forget both stored secrets. An environment value still applies."""
    secret_store.delete_secret(API_KEY_SECRET)
    secret_store.delete_secret(USER_TOKEN_SECRET)
