"""Encrypted-at-rest storage for API credentials.

Secrets are encrypted with Windows DPAPI (CryptProtectData), which ties the
ciphertext to the logged-in Windows account: copying the file to another
machine or user profile yields nothing. That is why the DeepSeek platform
token can be persisted at all - config.json stays free of secrets.

Every function degrades instead of raising: on a non-Windows host, or when
DPAPI refuses (a locked-down profile), save_secret reports False and the
caller keeps the value in memory for the session only.
"""

import base64
import ctypes
import sys
from pathlib import Path
from typing import Optional

from config import atomic_write_bytes, get_config_dir

# Win32 flags
CRYPTPROTECT_UI_FORBIDDEN = 0x01

SECRETS_DIRNAME = "secrets"


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _secrets_dir() -> Path:
    return get_config_dir() / SECRETS_DIRNAME


def secret_path(name: str) -> Path:
    """Path of the encrypted blob for `name`."""
    return _secrets_dir() / f"{name}.bin"


def is_available() -> bool:
    """Whether DPAPI encryption is usable on this host."""
    if sys.platform != "win32":
        return False
    try:
        ctypes.windll.crypt32
        return True
    except (AttributeError, OSError):
        return False


def _blob_from(data: bytes) -> _DataBlob:
    buf = ctypes.create_string_buffer(data, len(data))
    return _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _blob_bytes(blob: _DataBlob) -> bytes:
    return ctypes.string_at(blob.pbData, blob.cbData)


def protect(plaintext: str) -> Optional[bytes]:
    """Encrypt `plaintext` under the current Windows account. None on failure."""
    if not is_available():
        return None
    try:
        data_in = _blob_from(plaintext.encode("utf-8"))
        data_out = _DataBlob()
        ok = ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(data_in), None, None, None, None,
            CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(data_out),
        )
        if not ok:
            return None
        try:
            return _blob_bytes(data_out)
        finally:
            ctypes.windll.kernel32.LocalFree(data_out.pbData)
    except (AttributeError, OSError, ValueError):
        return None


def unprotect(blob: bytes) -> Optional[str]:
    """Decrypt a DPAPI blob. None when it was not written by this account."""
    if not is_available() or not blob:
        return None
    try:
        data_in = _blob_from(blob)
        data_out = _DataBlob()
        ok = ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(data_in), None, None, None, None,
            CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(data_out),
        )
        if not ok:
            return None
        try:
            return _blob_bytes(data_out).decode("utf-8")
        finally:
            ctypes.windll.kernel32.LocalFree(data_out.pbData)
    except (AttributeError, OSError, UnicodeDecodeError, ValueError):
        return None


def save_secret(name: str, value: str) -> bool:
    """Persist `value` encrypted. Returns False when it could not be stored."""
    blob = protect(value)
    if blob is None:
        return False
    try:
        # base64 so the file survives an editor or a sync client touching it
        atomic_write_bytes(secret_path(name), base64.b64encode(blob))
        return True
    except OSError:
        return False


def load_secret(name: str) -> Optional[str]:
    """Read and decrypt a stored secret. None when absent or unreadable."""
    try:
        path = secret_path(name)
        if not path.exists():
            return None
        return unprotect(base64.b64decode(path.read_bytes()))
    except (OSError, ValueError):
        return None


def delete_secret(name: str) -> None:
    """Remove a stored secret. Silent when it was never there."""
    try:
        secret_path(name).unlink()
    except OSError:
        pass
