"""Pytest fixtures for ClaudeBar tests."""

import pytest
import ctypes
import os
import threading
import sys
from ctypes import wintypes
from pathlib import Path

# Add src to path for imports
src_dir = Path(__file__).parent.parent / "src"
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))


_GENERIC_ALL, _DESKTOP_ENUMERATE = 0x10000000, 0x0040
_WINDOW_VISITOR = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


class HiddenDesktop:
    """A desktop of its own, which every window of the test run lives on.

    A window shown there appears on no monitor, takes no keyboard focus from the user, cannot be
    clicked, and leaves the user's foreground window alone: Tk's focus, raise, grab and topmost calls
    act on this desktop only, so the tests keep using them as they are."""

    def __init__(self):
        u = self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        k = ctypes.WinDLL("kernel32")
        u.CreateDesktopW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p, wintypes.DWORD,
                                     wintypes.DWORD, ctypes.c_void_p]
        u.CreateDesktopW.restype = u.OpenDesktopW.restype = u.GetThreadDesktop.restype = wintypes.HANDLE
        u.OpenDesktopW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        u.GetThreadDesktop.argtypes = [wintypes.DWORD]
        u.SetThreadDesktop.argtypes = u.CloseDesktop.argtypes = [wintypes.HANDLE]
        u.EnumDesktopWindows.argtypes = [wintypes.HANDLE, _WINDOW_VISITOR, wintypes.LPARAM]
        u.GetForegroundWindow.restype = wintypes.HWND
        u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self._original = u.GetThreadDesktop(k.GetCurrentThreadId())
        self.handle = u.CreateDesktopW(f"ClaudeBarTests{os.getpid()}", None, None, 0, _GENERIC_ALL, None)
        # No fallback to the user's own desktop: a run that cannot hide its windows must not show them.
        if not self.handle or not u.SetThreadDesktop(self.handle):
            raise OSError(ctypes.get_last_error(), "the test run could not move to a desktop of its own")

    def leave(self):
        """Back to the user's desktop, if Windows lets this thread (it does not while Tcl keeps a window)."""
        if self._user32.SetThreadDesktop(self._original):
            self._user32.CloseDesktop(self.handle)

    def user_foreground(self):
        """The window the user is working in, asked from a thread that is on the user's desktop."""
        answer = []
        thread = threading.Thread(target=lambda: answer.append(self._user32.GetForegroundWindow()))
        thread.start()
        thread.join()
        return answer[0] or 0

    def windows(self, on_user_desktop):
        """Every top-level window on the user's desktop, or on this run's own."""
        user32 = self._user32
        desktop = user32.OpenDesktopW("Default", 0, False, _DESKTOP_ENUMERATE) if on_user_desktop else self.handle
        found = []
        visit = _WINDOW_VISITOR(lambda hwnd, _: found.append(hwnd or 0) or True)
        user32.EnumDesktopWindows(desktop, visit, 0)
        if on_user_desktop:
            user32.CloseDesktop(desktop)
        return found

    def owner_pid(self, hwnd):
        pid = wintypes.DWORD()
        self._user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value


@pytest.fixture(scope="session", autouse=True)
def hidden_desktop():
    """The run's windows live on a desktop of their own (see HiddenDesktop), so no test, old or new,
    can show a window or take focus where the user works. It is made before the first Tk root and
    outlives the last one."""
    desktop = HiddenDesktop()
    yield desktop
    desktop.leave()


@pytest.fixture(scope="session")
def tk_root(hidden_desktop):
    """The one hidden Tk root the whole run shares; a test that draws a panel or a
    chart asks for it.

    Every extra Tcl interpreter is another chance for Tcl's own init to fail
    ("tcl_findLibrary", "Can't find a usable tk.tcl"), roughly one run in two while
    each module made its own. A failure here is an error, never a skip: a skipped
    panel test hides the regression it was written to catch. It is also the default
    root, which `tk.Toplevel()` with no master (the panel's window) attaches to, so
    no test may destroy it."""
    import tkinter as tk
    root = tk.Tk()
    root.withdraw()
    yield root
    root.destroy()


@pytest.fixture(autouse=True)
def _isolated_appdata(tmp_path, monkeypatch):
    """Point %LOCALAPPDATA% at a temp dir, so no test reads or writes the real
    snapshot cache, config or quota history, and the Claude CLI's config (which a
    refused refresh reads) at a file that does not exist. Claude's log folder is
    empty too: the panel asks when a log was last written, and the answer must not
    depend on the Claude Code sessions running on the machine that runs the tests."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    import claude_cli_snapshot
    monkeypatch.setattr(claude_cli_snapshot, "config_path", lambda: tmp_path / "claude_home" / ".claude.json")
    import log_parser
    monkeypatch.setattr(log_parser, "get_claude_projects_dir", lambda: tmp_path / "claude_home" / "projects")


@pytest.fixture(autouse=True)
def _log_looks_end_with_the_test(_isolated_appdata):
    """The panel looks at the Claude Code logs on a thread of its own. One still running when
    the test ends would walk the real folder, once this test's stand-in for it is undone: wait
    for it first (this fixture is torn down before _isolated_appdata's patch is)."""
    yield
    import threading
    for thread in threading.enumerate():
        if thread.name == "claudebar-log-look":
            thread.join(10)


@pytest.fixture(autouse=True)
def pictures(monkeypatch):
    """The panel takes a picture of itself before it changes (PrintWindow) and lays a layered window over
    itself (Win32). No test grabs the screen or calls Windows for either: the picture is a small plain
    image, `size` is its size (None: Windows refuses), `calls` the window handles asked for, `layered`
    the handles made layered."""
    import ui_window
    from types import SimpleNamespace
    from PIL import Image
    state = SimpleNamespace(calls=[], size=(40, 60), layered=[])

    def picture(hwnd):
        state.calls.append(hwnd)
        return Image.new("RGB", state.size, "#1c1c1e") if state.size else None

    monkeypatch.setattr(ui_window, "window_picture", picture)
    monkeypatch.setattr(ui_window, "make_layered", lambda hwnd: state.layered.append(hwnd) or True)
    return state


@pytest.fixture(autouse=True)
def _fresh_log_caches():
    """The log parsers keep what they read of a file between calls. Forget it around every
    test, so a test never sees what another one wrote under a path of its own."""
    import codex_log_parser
    import log_parser
    log_parser.reset_cache()
    codex_log_parser.reset_cache()
    yield
    log_parser.reset_cache()
    codex_log_parser.reset_cache()


@pytest.fixture(autouse=True)
def _no_outside_network(monkeypatch):
    """No test reaches a host outside this machine. An attempt is refused like a dead
    host (the fetchers swallow that, as they would in the field) and, because a swallowed
    refusal would let a test pass anyway, it fails the test when it ends: a test that
    forgot to fake its transport is caught here instead of calling a real endpoint (one
    of them spends a real, irreversible banked reset). Loopback stays open: the redirect
    and DevTools tests run their own little servers."""
    import socket
    local = {None, "", "localhost", "127.0.0.1", "::1", "0.0.0.0"}
    real_getaddrinfo, real_connect, real_connect_ex = (
        socket.getaddrinfo, socket.socket.connect, socket.socket.connect_ex)
    attempts = []

    def refuse(host):
        attempts.append(str(host))
        raise OSError(f"the test network guard refused {host!r}")

    def getaddrinfo(host, *args, **kwargs):
        if host not in local:
            refuse(host)
        return real_getaddrinfo(host, *args, **kwargs)

    def connect(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6) and address[0] not in local:
            refuse(address[0])
        return real_connect(self, address)

    def connect_ex(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6) and address[0] not in local:
            refuse(address[0])
        return real_connect_ex(self, address)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    yield
    if attempts:
        pytest.fail("a test tried to reach a host outside this machine: "
                    + ", ".join(sorted(set(attempts))) + " (fake the transport)", pytrace=False)


@pytest.fixture(autouse=True)
def _fresh_failure_memory():
    """A download that failed in one test is remembered for ten minutes: forget it, so
    no test depends on which one ran before it."""
    import currency
    import model_catalog
    currency._fetch_failures.clear()
    model_catalog._fetch_failures.clear()
