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

import os
import re
import shutil
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


def launch(url: str = SIGNIN_URL) -> Optional[subprocess.Popen]:
    """Open the sign-in page in a browser running against our own profile.

    Debugging is switched on so the token can be read from the live page.
    Waiting for it to reach disk instead means waiting for Chromium to flush
    its storage, which measured about a minute after the user had visibly
    finished signing in. The port is chosen by the browser, bound to
    127.0.0.1, and lives no longer than the window.
    """
    browser = find_browser()
    if not browser:
        return None
    profile = profile_dir()
    profile.mkdir(parents=True, exist_ok=True)
    args = [browser, f"--app={url}", f"--user-data-dir={profile}",
            "--remote-debugging-port=0",
            "--no-first-run", "--no-default-browser-check", "--new-window"]
    kwargs = {"close_fds": True}
    if sys.platform == "win32":
        kwargs["creationflags"] = _DETACHED
    return subprocess.Popen(args, **kwargs)


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
    """Read the session token from the browser profile, if it is there yet."""
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
    deadline = time.time() + timeout
    while time.time() < deadline:
        if should_stop and should_stop():
            return None
        token = browser_cdp.token_from_page(target) or read_token(target)
        if token:
            return token
        time.sleep(interval)
    return browser_cdp.token_from_page(target) or read_token(target)


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
    target = str(profile or profile_dir()).lower()
    exes = tuple(name.lower() for name in BROWSER_EXE_NAMES)
    return [pid for pid, exe in _iter_processes()
            if exe in exes and target in _command_line(pid).lower()]


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
    candidate = path or profile_dir()
    try:
        resolved = candidate.resolve()
        expected = (get_config_dir() / "browser").resolve()
    except OSError:
        return False

    if resolved != expected:
        return False
    if expected.parent != get_config_dir().resolve():
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
    return not profile.exists()
