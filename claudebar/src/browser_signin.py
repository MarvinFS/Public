"""Sign in to the DeepSeek platform through a browser ClaudeBar owns.

The platform's sign-in sits behind an AWS WAF JavaScript challenge, so a plain
HTTP client cannot authenticate: `POST /auth-api/v0/users/login` answers with
the challenge page instead of reaching the application. A real browser engine
passes it, so ClaudeBar opens one, lets the user sign in on DeepSeek's own
page, and reads the resulting session token out of the profile it created.

The password never passes through this app, which is the whole point: only the
session token is persisted, and it expires on its own. Everything after
sign-in is plain HTTP with that token, so no browser stays open.

The browser keeps its own profile under the ClaudeBar config directory, so the
session survives restarts and the user's everyday browser is never touched.
"""

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional

import browser_cdp

from config import get_config_dir

SIGNIN_URL = "https://platform.deepseek.com/sign_in"

# Where Chromium keeps web storage inside a profile
STORAGE_GLOBS = ("*/Local Storage/leveldb", "Local Storage/leveldb")
STORAGE_SUFFIXES = ("*.log", "*.ldb")

# localStorage stores the token as {"value":"<token>","__version":"0"}, encoded
# either Latin-1 or UTF-16LE, so both byte patterns are matched.
_TOKEN_ASCII = re.compile(rb'\{"value":"([A-Za-z0-9+/=_-]{20,})"')
_TOKEN_UTF16 = re.compile(r'\{"value":"([A-Za-z0-9+/=_-]{20,})"')
_KEY = b"userToken"
_WINDOW_BYTES = 512

BROWSER_EXE_NAMES = ("msedge.exe", "chrome.exe", "brave.exe",
                     "vivaldi.exe", "opera.exe", "thorium.exe")

# Roots that hold browser installs, expanded from the environment.
_INSTALL_ROOTS = ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")

# Chromium browsers only, and only ones whose localStorage is the same LevelDB
# shape we already read. Order is a preference, not a requirement: the default
# browser is irrelevant because we launch our own instance with our own
# profile, so this picks whichever engine is present and reliable.
_BROWSER_RELATIVE = (
    "Microsoft/Edge/Application/msedge.exe",
    "Microsoft/EdgeCore/Application/msedge.exe",
    "Google/Chrome/Application/chrome.exe",
    "Google/Chrome Beta/Application/chrome.exe",
    "BraveSoftware/Brave-Browser/Application/brave.exe",
    "Chromium/Application/chrome.exe",
    "Vivaldi/Application/vivaldi.exe",
    "Thorium/Application/thorium.exe",
    "Opera/opera.exe",
    # Per-user installs, which is how these usually land on a home machine
    "Programs/Google/Chrome/Application/chrome.exe",
    "Programs/BraveSoftware/Brave-Browser/Application/brave.exe",
    "Programs/Microsoft/Edge/Application/msedge.exe",
    "Programs/Opera/opera.exe",
    "Programs/Vivaldi/Application/vivaldi.exe",
)


def _candidate_paths():
    """Every plausible Chromium browser path, most preferred first.

    Browser preference is the outer loop, install root the inner one, so an
    Edge install anywhere beats a Chrome install anywhere.
    """
    seen = []
    for relative in _BROWSER_RELATIVE:
        for root_name in _INSTALL_ROOTS:
            root = os.environ.get(root_name)
            if not root:
                continue
            # Normalise separators so every candidate looks the same; Windows
            # accepts forward slashes, and it keeps comparisons predictable.
            candidate = f"{root.replace(chr(92), '/')}/{relative}"
            if candidate not in seen:
                seen.append(candidate)
    return seen


def find_browser() -> Optional[str]:
    """Path to an installed Chromium browser, or None.

    Only Chromium engines qualify: the Gecko family stores localStorage in a
    SQLite database with snappy-compressed values, which this app cannot read.
    """
    for candidate in _candidate_paths():
        if Path(candidate).exists():
            return candidate
    # Edge keeps versioned install directories, so glob as a last resort.
    for root in (r"C:/Program Files (x86)/Microsoft/Edge/Application",
                 r"C:/Program Files/Microsoft/Edge/Application"):
        found = sorted(Path(root).glob("*/msedge.exe"))
        if found:
            return str(found[-1])
    return None


def profile_dir() -> Path:
    """ClaudeBar's own browser profile. Separate from the user's browser."""
    return get_config_dir() / "browser"


# DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP, so the browser outlives the call
# that started it and never inherits our console.
_DETACHED = 0x00000008 | 0x00000200

#: Which browser the current sign-in is running in: "chromium" or "firefox".
_engine: Optional[str] = None


def engine() -> Optional[str]:
    """Which browser this sign-in is using, or None before one is launched."""
    return _engine


def launch(url: str = SIGNIN_URL) -> Optional[subprocess.Popen]:
    """Open the sign-in page in a browser running against our own profile.

    Chromium first, because that is what Windows has and the live page can be
    read through it. Firefox second: a machine with only Firefox installed used
    to have no way to sign in at all, and there is no reason for that - Firefox
    writes the same session to disk in plain SQLite, readable while it is open.

    Which engine actually started is recorded in `_engine`, because reading the
    token back depends on it.
    """
    global _engine

    browser = find_browser()
    if browser:
        _engine = "chromium"
        profile = profile_dir()
        profile.mkdir(parents=True, exist_ok=True)
        # Debugging is switched on so the token can be read from the live page.
        # Waiting for it to reach disk instead means waiting for Chromium to
        # flush its storage, which measured about a minute after the user had
        # visibly finished signing in. The port is chosen by the browser, bound
        # to 127.0.0.1, and lives no longer than the window.
        args = [browser, f"--app={url}", f"--user-data-dir={profile}",
                "--remote-debugging-port=0",
                "--no-first-run", "--no-default-browser-check", "--new-window"]
        kwargs = {"close_fds": True}
        if sys.platform == "win32":
            kwargs["creationflags"] = _DETACHED
        return subprocess.Popen(args, **kwargs)

    firefox = find_firefox()
    if firefox:
        _engine = "firefox"
        return _launch_firefox(firefox, url)

    _engine = None
    return None


def extract_token(data: bytes) -> Optional[str]:
    """Pull the DeepSeek session token out of raw localStorage bytes.

    Pure, so it is unit-tested without a browser. Returns None when the key is
    absent, which is the normal state before the user signs in.
    """
    for match in re.finditer(re.escape(_KEY), data):
        window = data[match.start(): match.start() + _WINDOW_BYTES]
        hit = _TOKEN_ASCII.search(window)
        if hit:
            return hit.group(1).decode("ascii", "replace")
        # UTF-16LE: the value has interleaved NULs, so decode before matching
        hit = _TOKEN_UTF16.search(window.decode("utf-16-le", "ignore"))
        if hit:
            return hit.group(1)
    return None


def _storage_roots(profile: Path) -> list:
    roots = []
    for pattern in STORAGE_GLOBS:
        roots.extend(sorted(profile.glob(pattern)))
    return roots


def read_token(profile: Optional[Path] = None) -> Optional[str]:
    """Read the session token from the browser profile, if it is there yet.

    Both engines are checked, because which profile holds it depends on which
    browser ran. The Firefox profile is read first when Firefox is the engine
    that started, since that is the one that will have it.
    """
    firefox_token = read_firefox_token()
    if _engine == "firefox":
        return firefox_token

    profile = profile or profile_dir()
    for root in _storage_roots(profile):
        for suffix in STORAGE_SUFFIXES:
            for path in sorted(root.glob(suffix)):
                try:
                    data = path.read_bytes()
                except OSError:
                    continue  # locked by the running browser, try again later
                token = extract_token(data)
                if token:
                    return token
    return firefox_token


# --- Firefox, when no Chromium browser is installed -------------------------
#
# Windows always has Edge, so this is a fallback rather than the usual path -
# but "always" is doing more work than it can carry: Edge is absent on Server
# and LTSC images, can be removed by policy, and a machine with only Firefox
# installed had no way to sign in at all before this existed.
#
# The reader below is the same one the Linux build uses, because Firefox's
# storage layout is Firefox's own and does not vary by platform. What differs is
# only where the browser is and how it is closed.

#: Executables to look for, and the keys that identify them as ours.
FIREFOX_EXE_NAMES = ("firefox.exe", "librewolf.exe", "waterfox.exe")

#: The key DeepSeek's own page writes its session token under.
FIREFOX_KEY = "userToken"

#: Where Firefox keeps its record of which profile was last used, so a profile
#: we created can be recognised as ours rather than guessed at.
_FIREFOX_INSTALL_ROOTS = ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")
_FIREFOX_RELATIVE = (
    "Mozilla Firefox/firefox.exe",
    "Firefox Developer Edition/firefox.exe",
    "LibreWolf/LibreWolf.exe",
    "Waterfox/Waterfox.exe",
)


def find_firefox() -> Optional[str]:
    """Path to an installed Firefox-family browser, or None.

    The registry first, because that is what Firefox itself records and it
    survives being installed anywhere. The usual directories second, for a copy
    that was unpacked rather than installed.
    """
    if sys.platform != "win32":
        return None

    try:
        import winreg
    except ImportError:
        winreg = None

    if winreg is not None:
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(hive, r"SOFTWARE\Mozilla\Mozilla Firefox") as key:
                    version = winreg.QueryValueEx(key, "CurrentVersion")[0]
                with winreg.OpenKey(
                        hive,
                        rf"SOFTWARE\Mozilla\Mozilla Firefox\{version}\Main") as key:
                    path = winreg.QueryValueEx(key, "PathToExe")[0]
            except (OSError, IndexError, TypeError):
                continue
            if path and Path(path).is_file():
                return path

    for root in _FIREFOX_INSTALL_ROOTS:
        base = os.environ.get(root)
        if not base:
            continue
        for relative in _FIREFOX_RELATIVE:
            candidate = Path(base) / relative
            if candidate.is_file():
                return str(candidate)
    return None


def firefox_profile_dir() -> Path:
    """ClaudeBar's own Firefox profile. The user's browser is never touched."""
    return get_config_dir() / "firefox"


def _launch_firefox(browser: str, url: str) -> Optional[subprocess.Popen]:
    """Open the sign-in page in Firefox, with no automation switched on.

    ``--no-remote`` keeps this from handing the URL to an instance the user
    already has open, which would put the page in their profile instead of ours
    and leave nothing for us to read. ``--profile`` is what guarantees the
    storage we read afterwards is one we created.

    Deliberately not driven: no WebDriver, no Marionette, nothing that sets
    navigator.webdriver. Driven Firefox is served a human-verification page
    instead of the sign-in form, so the ordinary browser is the one that works.
    """
    profile = firefox_profile_dir()
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "user.js").write_text(
        'user_pref("browser.shell.checkDefaultBrowser", false);\n'
        'user_pref("browser.startup.homepage_override.mstone", "ignore");\n'
        'user_pref("toolkit.telemetry.enabled", false);\n'
        'user_pref("datareporting.policy.dataSubmissionEnabled", false);\n'
        'user_pref("browser.aboutwelcome.enabled", false);\n',
        encoding="utf-8")

    args = [browser, "--no-remote", "--profile", str(profile), url]
    kwargs = {"close_fds": True,
              "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        kwargs["creationflags"] = _DETACHED
    return subprocess.Popen(args, **kwargs)


def _firefox_storage_files(profile: Path) -> list:
    """Every SQLite file those versions of Firefox might have put the token in.

    Two layouts exist and both are cheap to check. Older versions keep one
    directory per origin (``storage/default/<origin>/ls/data.sqlite``); newer
    ones also consolidate into ``storage/ls-archive.sqlite``.
    """
    found = []
    storage = profile / "storage"
    if not storage.is_dir():
        return found

    archive = storage / "ls-archive.sqlite"
    if archive.is_file():
        found.append(archive)

    for pattern in ("default/*deepseek*/ls/data.sqlite",
                    "permanent/*deepseek*/ls/data.sqlite",
                    "default/https+++*/ls/data.sqlite"):
        for match in sorted(storage.glob(pattern)):
            if match not in found:
                found.append(match)
    return found


def unwrap_firefox_value(raw: str) -> Optional[str]:
    """The token inside what the page stores.

    DeepSeek wraps it: ``{"value":"<token>","__version":"0"}``. A bare string is
    accepted too rather than assumed away, because that is what a simpler page
    would store and the cost of checking is one startswith.
    """
    text = (raw or "").strip()
    if not text:
        return None
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return None
        inner = data.get("value") if isinstance(data, dict) else None
        return inner.strip() if isinstance(inner, str) and inner.strip() else None
    return text


def _firefox_value_text(value) -> list:
    """Plausible strings for one stored value, best guess first.

    Order matters. ASCII written as UTF-16 is half NUL bytes, and those decode
    perfectly well as UTF-8 into a string that is not the value, so UTF-16 is
    tried first when the bytes look like it. Anything that decodes with
    replacement characters is dropped rather than offered as a candidate.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value]

    raw = bytes(value)
    candidates = []
    if b"\x00" in raw[:64]:
        try:
            candidates.append(raw.decode("utf-16-le"))
        except UnicodeDecodeError:
            pass
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    if text is not None and "\ufffd" not in text:
        candidates.append(text)
    return candidates


def _looks_like_a_firefox_token(text: str) -> bool:
    """Whether a decoded value could be a session token at all.

    A sanity check, not a format check: the point is to keep binary that happened
    to decode from being stored as a credential. Length is deliberately not part
    of it - whether a token is real is settled by the API call that validates it
    before anything is stored.
    """
    if not text or len(text) > 4096:
        return False
    return all(character.isprintable() and character != "\ufffd"
               for character in text)


def _firefox_read_from(path: Path) -> Optional[str]:
    """Look for the token key in one SQLite file, whatever its table is called."""
    try:
        # Read-only and immutable: Firefox may be holding the file, and this must
        # never be able to change anything in a browser profile.
        connection = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    except sqlite3.Error:
        return None

    try:
        tables = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        for table in tables:
            columns = [row[1] for row in connection.execute(
                f"PRAGMA table_info({table})")]
            if "key" not in columns or "value" not in columns:
                continue
            for row in connection.execute(
                    f"SELECT * FROM {table} WHERE key = ?", (FIREFOX_KEY,)):
                record = dict(zip(columns, row))
                if record.get("compression_type"):
                    # Snappy. No decoder is shipped, and decompressing binary
                    # noise into a string would hand a garbage credential to the
                    # rest of the application, so this refuses rather than
                    # guesses. Measured on the value that matters - DeepSeek's
                    # token is 92 bytes, stored compression_type=0 - so this
                    # guards against a different Firefox, not a limitation today.
                    continue
                for candidate in _firefox_value_text(record.get("value")):
                    token = unwrap_firefox_value(candidate)
                    if token and _looks_like_a_firefox_token(token):
                        return token
    except sqlite3.Error:
        pass
    finally:
        connection.close()
    return None


def read_firefox_token(profile: Optional[Path] = None) -> Optional[str]:
    """The token Firefox has written for us, or None.

    Safe to call while Firefox is running, which is the point: it writes the row
    within about twenty seconds of the page loading and updates it on sign-in, so
    polling this is how the sign-in is detected at all.

    Returns None while the row holds null, which is the state before anyone has
    signed in, and None if the value is compressed rather than guessed at.
    """
    for candidate in _firefox_storage_files(profile or firefox_profile_dir()):
        token = _firefox_read_from(candidate)
        if token:
            return token
    return None


def wait_for_token(timeout: float = 300.0, interval: float = 2.0,
                   should_stop: Optional[Callable[[], bool]] = None,
                   profile: Optional[Path] = None) -> Optional[str]:
    """Poll until sign-in completes, the deadline passes, or stopped.

    The live page is asked first, because it knows the token the moment the
    user is signed in. The profile on disk is the fallback: it lags, since
    Chromium flushes its storage in its own time, but it still holds the token
    if the page has already gone.
    """
    target = profile or profile_dir()
    # The live page is asked first for Chromium, because it knows the token the
    # moment the user is signed in. Firefox has no such channel - it is
    # deliberately not driven - so its profile on disk is the only source, and
    # it is written within about twenty seconds.
    live = (lambda: None) if _engine == "firefox" else (
        lambda: browser_cdp.token_from_page(target))
    deadline = time.time() + timeout
    while time.time() < deadline:
        if should_stop and should_stop():
            return None
        token = live() or read_token(target)
        if token:
            return token
        time.sleep(interval)
    return live() or read_token(target)


def _iter_processes():
    """Yield (pid, executable name) for every process on the machine."""
    import ctypes
    from ctypes import wintypes

    TH32CS_SNAPPROCESS = 0x00000002
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class PROCESSENTRY32(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD),
                    ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                    ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_char * 260)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == INVALID_HANDLE_VALUE:
        return
    try:
        entry = PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
        if not kernel32.Process32First(snapshot, ctypes.byref(entry)):
            return
        while True:
            yield entry.th32ProcessID, entry.szExeFile.decode("ascii", "replace").lower()
            if not kernel32.Process32Next(snapshot, ctypes.byref(entry)):
                return
    finally:
        kernel32.CloseHandle(snapshot)


def _command_line(pid: int) -> str:
    """Full command line of a process, or '' when it cannot be read."""
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    ProcessCommandLineInformation = 60
    STATUS_INFO_LENGTH_MISMATCH = 0xC0000004

    class UNICODE_STRING(ctypes.Structure):
        _fields_ = [("Length", ctypes.c_ushort),
                    ("MaximumLength", ctypes.c_ushort),
                    ("Buffer", ctypes.c_void_p)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll")
    # Without restype, ctypes truncates the 64-bit HANDLE to a 32-bit int and
    # every query against it fails.
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    ntdll.NtQueryInformationProcess.restype = ctypes.c_long
    ntdll.NtQueryInformationProcess.argtypes = [
        wintypes.HANDLE, ctypes.c_ulong, ctypes.c_void_p,
        ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)]
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = ctypes.c_ulong(1024)
        buf = ctypes.create_string_buffer(size.value)
        status = ntdll.NtQueryInformationProcess(
            wintypes.HANDLE(handle), ProcessCommandLineInformation,
            buf, size.value, ctypes.byref(size))
        if status == STATUS_INFO_LENGTH_MISMATCH:
            buf = ctypes.create_string_buffer(size.value)
            status = ntdll.NtQueryInformationProcess(
                wintypes.HANDLE(handle), ProcessCommandLineInformation,
                buf, size.value, ctypes.byref(size))
        if status != 0:
            return ""
        text = ctypes.cast(buf, ctypes.POINTER(UNICODE_STRING)).contents
        if not text.Buffer or not text.Length:
            return ""
        return ctypes.wstring_at(text.Buffer, text.Length // 2)
    except Exception:
        return ""
    finally:
        kernel32.CloseHandle(handle)


def pids_using_profile(profile: Optional[Path] = None) -> list:
    """Browser PIDs whose command line names our profile directory.

    Matching on the profile is what makes closing reliable: a second launch of
    the same browser with the same profile hands off to the running instance
    and exits, so the process we spawned is not the one owning the window.
    """
    if sys.platform != "win32":
        return []
    names = tuple(name.lower() for name in BROWSER_EXE_NAMES + FIREFOX_EXE_NAMES)
    found = set()
    for target in (str(profile or profile_dir()).lower(),
                   str(firefox_profile_dir()).lower()):
        found.update(pid for pid, exe in _iter_processes()
                     if exe in names and target in _command_line(pid).lower())
    return sorted(found)


def close(process: Optional[subprocess.Popen] = None) -> int:
    """Close every browser window running against our profile.

    Only our own profile is matched, so the user's everyday browser is never
    touched. Returns the number of processes stopped.
    """
    if sys.platform != "win32":
        if process is not None and process.poll() is None:
            process.terminate()
        return 0

    pids = pids_using_profile()
    stopped = 0
    for pid in pids:
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=20)
            stopped += 1
        except (OSError, subprocess.SubprocessError):
            pass
    # A delegated launch exits on its own; nothing to do for it.
    return stopped


# Paths that belong to a real browser. Nothing here is ever deleted.
# Locations that belong to a real browser. Nothing here is ever deleted.
# Compared with separators normalised to '/', so no escapes are involved.
_REAL_BROWSER_MARKERS = (
    "/google/chrome/user data",
    "/microsoft/edge/user data",
    "/brave-browser/user data",
    "/chromium/user data",
    "/mozilla/firefox",
    "/opera software/",
    "/vivaldi/user data",
)



def is_our_profile(path: Optional[Path] = None) -> bool:
    """Whether `path` is the profile ClaudeBar created, and nothing else.

    Deleting a browser profile is destructive: a real Edge or Chrome profile
    holds bookmarks, passwords and history, and is gigabytes wide. ClaudeBar's
    own profile lives directly under its config directory and is only ever
    pointed at platform.deepseek.com, so removing it can only remove that.
    This guard makes the difference explicit rather than assumed, so a future
    bug in profile_dir() cannot turn into deleted user data.
    """
    try:
        resolved = (path or profile_dir()).resolve()
        allowed = {(get_config_dir() / "browser").resolve(),
                   firefox_profile_dir().resolve()}
        config = get_config_dir().resolve()
    except OSError:
        return False

    # One of exactly two directories, each directly under the config dir.
    if resolved not in allowed:
        return False
    if any(candidate.parent != config for candidate in allowed):
        return False
    lowered = str(resolved).lower().replace(chr(92), '/')
    return not any(marker in lowered for marker in _REAL_BROWSER_MARKERS)


def forget() -> bool:
    """Delete ClaudeBar's sign-in profile, dropping the stored session.

    Returns False, and deletes nothing, unless the target is provably the
    profile ClaudeBar created.
    """
    profile = profile_dir()
    if not is_our_profile(profile):
        return False
    shutil.rmtree(profile, ignore_errors=True)
    # Both, because either could hold the session. The guard is checked for each
    # separately rather than assumed from the first.
    firefox = firefox_profile_dir()
    if is_our_profile(firefox):
        shutil.rmtree(firefox, ignore_errors=True)
    return not profile.exists() and not firefox.exists()
