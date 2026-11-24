"""Google / Antigravity authentication via the official `agy` CLI.

ClaudeBar never stores or uses any Google OAuth client ID or client secret, and
never reads credentials belonging to other applications (such as T3 Code or the
system keyring). Signing in from Settings runs Google's official `agy` binary
(`agy acp --login`) against ClaudeBar's own isolated profile directory
(`%LOCALAPPDATA%\\ClaudeBar\\antigravity` on Windows,
`~/.local/share/claudebar/antigravity` on Linux). When ClaudeBar's short-lived
access token expires (~1 hour), `refresh_claudebar_token()` lets `agy acp`
renew ClaudeBar's own token file silently without opening a browser.
"""

import hashlib
import json
import logging
import os
import platform
import queue
import re
import shutil
import subprocess
import sys
import tarfile
import threading
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

import config as _config
import http_json

logger = logging.getLogger("claudebar")

_TOKEN_SKEW = timedelta(seconds=60)

# Official Antigravity CLI release manifest (from pingdotgg/t3code antigravityCliRelease.ts).
ANTIGRAVITY_CLI_MANIFEST_BASE = (
    "https://antigravity-cli-auto-updater-974169037036.us-central1.run.app/manifests"
)
_ANTIGRAVITY_RELEASE_PREFIX = "https://storage.googleapis.com/antigravity-public/antigravity-cli/"
_CODE_REDIRECT_URI = "https://antigravity.google/oauth-callback"
_AUTH_URL_RE = re.compile(r"https://accounts\.google\.com/o/oauth2/(?:v2/)?auth\S+")

# Environment variables stripped before launching `agy` so external API keys or
# GCP project overrides never hijack ClaudeBar's isolated Google-account flow.
_STRIPPED_ENV_KEYS = frozenset({
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_CLOUD_LOCATION",
    "GOOGLE_CLOUD_QUOTA_PROJECT",
    "GOOGLE_GENAI_USE_VERTEXAI",
    "GCLOUD_PROJECT",
    "CLOUDSDK_CORE_PROJECT",
    "AGY_ACP_CCPA_PROJECT",
    "AGY_ACP_ENABLE_OAUTH",
    "GEMINI_HOME",
    "AGY_ACP_FORCE_FILE_STORAGE",
    "AGY_CLI_DISABLE_AUTO_UPDATE",
})


@dataclass
class AntigravityCredential:
    """OAuth access token stored inside ClaudeBar's own Antigravity profile."""
    access_token: str = ""
    expires_at: Optional[datetime] = None
    auth_method: str = "oauth-personal"  # "oauth-personal" | "oauth-business"
    email: Optional[str] = None
    source: str = "claudebar"

    def is_valid(self, now: Optional[datetime] = None) -> bool:
        if not self.access_token:
            return False
        if self.expires_at is None:
            return True
        now = now or datetime.now(timezone.utc)
        return self.expires_at > now + _TOKEN_SKEW


def get_antigravity_profile_dir() -> Path:
    """ClaudeBar's own isolated Antigravity GEMINI_HOME directory."""
    base = _config.get_data_dir() if hasattr(_config, "get_data_dir") else _config.get_config_dir()
    return base / "antigravity"


def get_claudebar_token_path() -> Path:
    """Token file written by `agy acp --login` inside ClaudeBar's profile."""
    return get_antigravity_profile_dir() / "antigravity-acp" / "antigravity-oauth-token"


def get_managed_agy_path() -> Path:
    """Where ClaudeBar stores a downloaded `agy` executable if the user has none."""
    name = "agy.exe" if sys.platform == "win32" else "antigravity"
    return get_antigravity_profile_dir() / "bin" / name


def find_agy_executable() -> Optional[Path]:
    """Locate Google's official `agy` CLI binary if installed."""
    for candidate in ("agy", "antigravity"):
        found = shutil.which(candidate)
        if found:
            return Path(found)
    if sys.platform == "win32":
        local_app = os.environ.get("LOCALAPPDATA")
        if local_app:
            std = Path(local_app) / "agy" / "bin" / "agy.exe"
            if std.is_file():
                return std
    else:
        home = Path.home()
        for std in (
            home / ".local" / "bin" / "agy",
            home / ".local" / "bin" / "antigravity",
            home / ".agy" / "bin" / "agy",
            home / ".antigravity" / "bin" / "antigravity",
        ):
            if std.is_file() and os.access(std, os.X_OK):
                return std
    managed = get_managed_agy_path()
    if managed.is_file() and (sys.platform == "win32" or os.access(managed, os.X_OK)):
        return managed
    return None


def _cli_target() -> Optional[tuple[str, str, str, bool]]:
    """Return (manifest_target, folder, file_name, is_tar_gz) for this machine, or None."""
    machine = platform.machine().lower()
    if machine in ("amd64", "x86_64"):
        manifest_arch, folder_arch, file_arch = "amd64", "x64", "x64"
    elif machine in ("arm64", "aarch64"):
        manifest_arch, folder_arch, file_arch = "arm64", "arm", "arm64"
    else:
        return None
    if sys.platform == "win32":
        return (
            f"windows_{manifest_arch}",
            f"windows-{folder_arch}",
            f"cli_windows_{file_arch}.exe",
            False,
        )
    if sys.platform.startswith("linux"):
        return (
            f"linux_{manifest_arch}",
            f"linux-{folder_arch}",
            f"cli_linux_{file_arch}.tar.gz",
            True,
        )
    return None


def _extract_tar_binary(archive_path: Path, dest: Path) -> None:
    """Extract the `antigravity` executable from a verified `.tar.gz` into `dest`."""
    bin_tmp = dest.with_name(f"{dest.name}.{os.getpid()}.{threading.get_ident()}.bin.tmp")
    try:
        with tarfile.open(archive_path, "r:gz") as tf:
            member = next(
                (
                    m for m in tf.getmembers()
                    if m.isfile() and Path(m.name).name in ("antigravity", "agy")
                ),
                None,
            )
            if member is None:
                raise RuntimeError("Downloaded Antigravity archive contained no executable")
            src = tf.extractfile(member)
            if src is None:
                raise RuntimeError("Could not read executable from Antigravity archive")
            with src, open(bin_tmp, "wb") as out:
                shutil.copyfileobj(src, out, length=256 * 1024)
                out.flush()
                os.fsync(out.fileno())
        os.chmod(bin_tmp, 0o755)
        os.replace(bin_tmp, dest)
    except Exception:
        try:
            bin_tmp.unlink()
        except OSError:
            pass
        raise


def install_agy_executable(
    on_progress: Optional[Callable[[str], None]] = None,
) -> Path:
    """Download and SHA-512 verify the official `agy` binary into ClaudeBar's profile."""
    target = _cli_target()
    if target is None:
        raise RuntimeError("Automatic Antigravity CLI download is only supported on Windows and Linux x64/arm64")
    manifest_target, folder, file_name, is_tar_gz = target
    if on_progress:
        on_progress("Checking latest Antigravity release...")
    manifest_url = f"{ANTIGRAVITY_CLI_MANIFEST_BASE}/{manifest_target}.json"
    manifest = http_json.request_json(
        manifest_url, {"User-Agent": "ClaudeBar"}, timeout=20, follow_redirects=True
    )
    version = str(manifest.get("version") or "").strip()
    url = str(manifest.get("url") or "").strip()
    expected_sha512 = str(manifest.get("sha512") or "").strip().lower()
    if (
        not re.fullmatch(r"\d+\.\d+\.\d+", version)
        or not re.fullmatch(r"[0-9a-f]{128}", expected_sha512)
        or not url.startswith(f"{_ANTIGRAVITY_RELEASE_PREFIX}{version}-")
    ):
        raise RuntimeError("Antigravity release manifest failed validation")
    suffix = url[len(f"{_ANTIGRAVITY_RELEASE_PREFIX}{version}-"):]
    expected_tail = f"{folder}/{file_name}"
    if not re.fullmatch(rf"\d+/{re.escape(expected_tail)}", suffix):
        raise RuntimeError("Antigravity release URL failed validation")

    if on_progress:
        on_progress(f"Downloading Antigravity v{version}...")
    dest = get_managed_agy_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dl_tmp = dest.with_name(f"{dest.name}.{os.getpid()}.{threading.get_ident()}.dl.tmp")
    req = urllib.request.Request(url, headers={"User-Agent": "ClaudeBar"})
    hasher = hashlib.sha512()
    try:
        with urllib.request.urlopen(req, timeout=300) as resp, open(dl_tmp, "wb") as out:
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                hasher.update(chunk)
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        if hasher.hexdigest().lower() != expected_sha512:
            raise RuntimeError("Downloaded Antigravity binary failed SHA-512 verification")
        if is_tar_gz:
            if on_progress:
                on_progress(f"Extracting Antigravity v{version}...")
            _extract_tar_binary(dl_tmp, dest)
        else:
            os.replace(dl_tmp, dest)
    finally:
        try:
            if dl_tmp.exists():
                dl_tmp.unlink()
        except OSError:
            pass
    return dest


def _parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
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


def _normalize_auth_method(raw: Optional[str]) -> str:
    if isinstance(raw, str) and raw.strip().lower() in ("oauth-business", "business", "enterprise"):
        return "oauth-business"
    return "oauth-personal"


def parse_oauth_token_payload(text: str, source: str = "claudebar") -> Optional[AntigravityCredential]:
    """Parse an Antigravity 1.5.x `antigravity-oauth-token` JSON payload."""
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    tok = data.get("token")
    if not isinstance(tok, dict):
        return None
    access_token = tok.get("access_token")
    if not isinstance(access_token, str) or not access_token.strip():
        return None
    expiry = _parse_iso_datetime(tok.get("expiry"))
    email = data.get("id_token_email")
    return AntigravityCredential(
        access_token=access_token.strip(),
        expires_at=expiry,
        auth_method=_normalize_auth_method(data.get("auth_method")),
        email=email.strip() if isinstance(email, str) and email.strip() else None,
        source=source,
    )


def _read_token_file(path: Path, source: str = "claudebar") -> Optional[AntigravityCredential]:
    try:
        if not path.is_file():
            return None
        return parse_oauth_token_payload(path.read_text(encoding="utf-8"), source=source)
    except OSError:
        return None


def discover_credential(
    now: Optional[datetime] = None,
) -> Optional[AntigravityCredential]:
    """Read ClaudeBar's own Antigravity profile token (never reads other apps' tokens)."""
    del now  # kept for signature compatibility with callers/tests
    return _read_token_file(get_claudebar_token_path(), "claudebar")


def claudebar_account_status(now: Optional[datetime] = None) -> tuple[bool, Optional[str], Optional[str]]:
    """Return (signed_in, email, source) for Settings display."""
    cred = discover_credential(now=now)
    if cred is None:
        return False, None, None
    return True, cred.email, cred.source


def has_claudebar_profile_token() -> bool:
    """Whether ClaudeBar owns a signed-in token file in its own profile."""
    return get_claudebar_token_path().is_file()


def sign_out_claudebar_profile() -> bool:
    """Delete ClaudeBar's own Antigravity profile token. Returns True if removed."""
    path = get_claudebar_token_path()
    try:
        if path.is_file():
            path.unlink()
            return True
    except OSError as exc:
        logger.warning("Could not remove Antigravity token file: %s", type(exc).__name__)
    return False


def _agy_env(profile_dir: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k.upper() not in _STRIPPED_ENV_KEYS}
    # Setting SSH_CONNECTION tells agy 1.5.x to use isolated file storage inside
    # GEMINI_HOME instead of touching the shared OS keyring, and prevents opening a browser
    # during background token renewal.
    env["SSH_CONNECTION"] = os.environ.get("SSH_CONNECTION") or "127.0.0.1 0 127.0.0.1 0"
    env["GEMINI_HOME"] = str(profile_dir)
    env["AGY_ACP_FORCE_FILE_STORAGE"] = "1"
    env["AGY_CLI_DISABLE_AUTO_UPDATE"] = "1"
    return env


def refresh_claudebar_token(
    timeout: float = 15.0,
    profile_dir: Optional[Path] = None,
) -> Optional[AntigravityCredential]:
    """Ask Google's official `agy` binary to renew ClaudeBar's expired access token.

    Runs `agy acp` against ClaudeBar's isolated profile with `SSH_CONNECTION` set,
    sending JSON-RPC `initialize` + `authenticate`. Because `SSH_CONNECTION` is set,
    `agy` never touches the OS keyring and never pops a browser if the refresh token
    was revoked (it immediately returns an RPC error instead).
    """
    prof = Path(profile_dir or get_antigravity_profile_dir())
    token_path = prof / "antigravity-acp" / "antigravity-oauth-token"
    if not token_path.is_file():
        return None
    agy = find_agy_executable()
    if agy is None:
        return None

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    proc: Optional[subprocess.Popen] = None
    try:
        proc = subprocess.Popen(
            [
                str(agy),
                "acp",
                "--gemini-dir",
                str(prof),
                "--app-data-dir",
                "antigravity-acp",
                "--disable-telemetry",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_agy_env(prof),
            creationflags=creationflags,
        )
        assert proc.stdin is not None and proc.stdout is not None
        reqs = (
            json.dumps({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": 1, "clientCapabilities": {}},
            })
            + "\n"
            + json.dumps({
                "jsonrpc": "2.0",
                "id": 2,
                "method": "authenticate",
                "params": {"methodId": "oauth-personal"},
            })
            + "\n"
        )
        proc.stdin.write(reqs)
        proc.stdin.flush()

        # Wait for the id=2 response on a bounded timer so a hung binary cannot block refresh.
        done = threading.Event()
        ok = [False]

        def _drain() -> None:
            try:
                assert proc is not None and proc.stdout is not None
                for line in proc.stdout:
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(msg, dict) and msg.get("id") == 2:
                        ok[0] = "error" not in msg
                        done.set()
                        return
            except Exception:
                pass
            finally:
                done.set()

        threading.Thread(target=_drain, daemon=True).start()
        done.wait(timeout=timeout)
    except Exception as exc:
        logger.debug("Antigravity token renewal skipped: %s", type(exc).__name__)
        return None
    finally:
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    renewed = _read_token_file(token_path, "claudebar")
    return renewed if (renewed is not None and renewed.is_valid()) else None


def validate_authorization_url(url: str) -> Optional[str]:
    """Validate a Google OAuth URL emitted by `agy acp --login`."""
    if not url or len(url) > 16_384 or any(ch.isspace() for ch in url):
        return None
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != "accounts.google.com":
        return None
    if parsed.path not in ("/o/oauth2/auth", "/o/oauth2/v2/auth"):
        return None
    params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    if params.get("response_type") != ["code"]:
        return None
    redirect = params.get("redirect_uri")
    if not redirect or len(redirect) != 1:
        return None
    if redirect[0] != _CODE_REDIRECT_URI and not re.fullmatch(
        r"http://127\.0\.0\.1:[1-9][0-9]{0,4}/", redirect[0]
    ):
        return None
    return url


def normalize_login_code(raw: str) -> Optional[str]:
    """Validate a pasted Antigravity login code or callback URL."""
    value = (raw or "").strip()
    if not value or any(ch in value for ch in ("\r", "\n")):
        return None
    if re.fullmatch(r"[\w./~+=%-]{1,2048}", value):
        return value
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme == "https"
        and parsed.netloc == "antigravity.google"
        and parsed.path == "/oauth-callback"
    ):
        codes = urllib.parse.parse_qs(parsed.query).get("code")
        if codes and len(codes) == 1 and codes[0]:
            return value
    return None


class AntigravityLoginSession:
    """Manages one interactive `agy acp --login` subprocess for ClaudeBar Settings."""

    def __init__(self, agy_path: Path, profile_dir: Optional[Path] = None):
        self.agy_path = Path(agy_path)
        self.profile_dir = Path(profile_dir or get_antigravity_profile_dir())
        self._proc: Optional[subprocess.Popen] = None
        self._url_queue: queue.Queue[Optional[str]] = queue.Queue()
        self._auth_url: Optional[str] = None
        self._output_tail: list[str] = []
        self._closed = False

    def start(self, timeout: float = 30.0) -> str:
        """Spawn `agy acp --login` and wait for its Google sign-in URL."""
        acp_dir = self.profile_dir / "antigravity-acp"
        acp_dir.mkdir(parents=True, exist_ok=True)
        if sys.platform != "win32":
            for directory in (self.profile_dir, acp_dir):
                try:
                    directory.chmod(0o700)
                except OSError:
                    pass
        (acp_dir / "settings.json").write_text(
            json.dumps({"auth": {"type": "oauth-personal"}}) + "\n", encoding="utf-8"
        )
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        self._proc = subprocess.Popen(
            [
                str(self.agy_path),
                "acp",
                "--gemini-dir",
                str(self.profile_dir),
                "--app-data-dir",
                "antigravity-acp",
                "--disable-telemetry",
                "--login",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_agy_env(self.profile_dir),
            creationflags=creationflags,
        )
        threading.Thread(target=self._read_output, daemon=True).start()
        try:
            url = self._url_queue.get(timeout=timeout)
        except queue.Empty:
            self.cancel()
            raise RuntimeError("Timed out waiting for Google sign-in URL")
        if not url:
            self.cancel()
            raise RuntimeError("Antigravity exited before providing a sign-in URL")
        self._auth_url = url
        return url

    def _read_output(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            self._url_queue.put(None)
            return
        sent = False
        try:
            for line in proc.stdout:
                stripped = line.strip()
                if stripped and not any(k in stripped.lower() for k in ("access_token", "refresh_token", "id_token")):
                    self._output_tail.append(stripped[:200])
                    if len(self._output_tail) > 20:
                        del self._output_tail[0]
                m = _AUTH_URL_RE.search(line)
                if m and not sent:
                    valid = validate_authorization_url(m.group(0))
                    if valid:
                        sent = True
                        self._url_queue.put(valid)
        except Exception:
            pass
        finally:
            if not sent:
                self._url_queue.put(None)

    def submit_code(self, code_or_url: str, timeout: float = 60.0) -> AntigravityCredential:
        """Send the pasted code/URL to `agy` and wait for it to store the token."""
        normalized = normalize_login_code(code_or_url)
        if not normalized:
            raise ValueError("Paste the code from the Google callback page, or its full URL")
        proc = self._proc
        if (
            self._closed
            or proc is None
            or proc.stdin is None
            or getattr(proc.stdin, "closed", False)
            or proc.poll() is not None
        ):
            self.cancel()
            raise RuntimeError("Sign-in session ended. Click Sign in to Google again.")
        try:
            proc.stdin.write(normalized + "\n")
            proc.stdin.flush()
            proc.stdin.close()
        except (OSError, ValueError) as exc:
            self.cancel()
            raise RuntimeError("Sign-in session ended. Click Sign in to Google again.") from exc

        try:
            exit_code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.cancel()
            raise RuntimeError("Timed out waiting for Google to finish sign-in")
        finally:
            self._closed = True

        if exit_code != 0:
            joined = " ".join(self._output_tail).lower()
            if "invalid_grant" in joined:
                raise RuntimeError("Code expired or did not match this sign-in. Click Sign in to Google again.")
            raise RuntimeError(f"Google sign-in failed (exit code {exit_code}). Click Sign in to Google again.")

        token_path = self.profile_dir / "antigravity-acp" / "antigravity-oauth-token"
        cred = _read_token_file(token_path, "claudebar")
        if cred is None or not cred.access_token:
            raise RuntimeError("Sign-in finished but no token was saved")
        return cred

    def cancel(self) -> None:
        if self._closed:
            return
        self._closed = True
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
