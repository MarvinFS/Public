"""A DeepSeek sign-in leaves nothing behind.

The browser ClaudeBar launches is closed however the sign-in ends, and the profile it ran
in (which holds the session token in plaintext) is deleted. Nothing here starts a browser:
processes, the registry and the network are replaced.
"""

import collections
import subprocess
import threading
import time
import tkinter as tk
import types

import pytest

import browser_cdp as cdp
import browser_signin as bs
import deepseek_auth
import deepseek_usage
from ui_window import SettingsDialog


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    """ClaudeBar's config dir with both capture profiles, the stored token and a bystander."""
    monkeypatch.setattr(bs, "get_config_dir", lambda: tmp_path)
    for name in ("browser", "firefox", "secrets", "bystander"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "keep.txt").write_text(name)
    (tmp_path / "browser" / "Local Storage" / "leveldb").mkdir(parents=True)
    (tmp_path / "browser" / "Local Storage" / "leveldb" / "000003.log").write_bytes(b"userToken")
    (tmp_path / "config.json").write_text("{}")
    return tmp_path


@pytest.fixture
def browser(monkeypatch):
    """Records what the sign-in does to the browser and its profile."""
    events = []
    monkeypatch.setattr(bs, "close", lambda process=None: events.append(("close", process)) or 1)
    real_forget = bs.forget

    def forget():
        events.append(("forget",))
        return real_forget()

    monkeypatch.setattr(bs, "forget", forget)
    monkeypatch.setattr(bs.time, "sleep", lambda seconds: None)
    return events


class TestEndSignin:
    def test_it_closes_the_browser_and_deletes_only_the_capture_profiles(self, config_dir, browser):
        process = object()
        bs.end_signin(process)
        assert browser[0] == ("close", process)             # the browser first, then its files
        assert not (config_dir / "browser").exists()
        assert not (config_dir / "firefox").exists()
        for kept in ("secrets", "bystander"):
            assert (config_dir / kept / "keep.txt").read_text() == kept
        assert (config_dir / "config.json").exists()

    def test_a_profile_outside_the_config_dir_is_never_deleted(self, config_dir, browser, monkeypatch, tmp_path):
        foreign = tmp_path.parent / f"{tmp_path.name}-foreign" / "Google" / "Chrome" / "User Data"
        foreign.mkdir(parents=True)
        (foreign / "Login Data").write_text("passwords")
        monkeypatch.setattr(bs, "profile_dir", lambda: foreign)
        monkeypatch.setattr(bs, "firefox_profile_dir", lambda: foreign)
        bs.end_signin()
        assert (foreign / "Login Data").read_text() == "passwords"

    def test_it_retries_while_the_browser_still_holds_files_and_then_stops(self, monkeypatch):
        tries = []
        monkeypatch.setattr(bs, "close", lambda process=None: 0)
        monkeypatch.setattr(bs.time, "sleep", lambda seconds: None)
        monkeypatch.setattr(bs, "forget", lambda: tries.append(1) or len(tries) == 3)
        bs.end_signin()
        assert len(tries) == 3                               # deleted on the third try

        tries.clear()
        monkeypatch.setattr(bs, "forget", lambda: tries.append(1) or False)
        bs.end_signin()
        assert len(tries) == bs._DELETE_ATTEMPTS             # bounded, and it does not raise

    def test_it_never_raises(self, monkeypatch):
        def broken(process=None):
            raise OSError("no such process")
        monkeypatch.setattr(bs, "close", broken)
        bs.end_signin()


class TestTaskkillHasNoConsoleWindow:
    def test_close_passes_create_no_window(self, monkeypatch):
        calls = []
        monkeypatch.setattr(bs.sys, "platform", "win32")
        monkeypatch.setattr(bs, "pids_using_profile", lambda profile=None: [4242])
        monkeypatch.setattr(bs.subprocess, "run", lambda args, **kw: calls.append((args, kw)))
        assert bs.close() == 1
        args, kwargs = calls[0]
        assert args[0] == "taskkill" and "4242" in args
        assert kwargs["creationflags"] & 0x08000000          # CREATE_NO_WINDOW


class TestLaunchStartsFromNoStalePort:
    def test_a_port_file_from_an_earlier_run_is_removed(self, monkeypatch, tmp_path):
        profile = tmp_path / "browser"
        profile.mkdir()
        (profile / "DevToolsActivePort").write_text("51234\n/devtools/browser/abc")
        monkeypatch.setattr(bs, "find_browser", lambda: "C:/fake/msedge.exe")
        monkeypatch.setattr(bs, "profile_dir", lambda: profile)
        monkeypatch.setattr(bs.subprocess, "Popen", lambda *a, **k: object())
        bs.launch()
        assert cdp.debug_port(profile) is None               # nothing is read before the new browser writes it
        assert (profile).is_dir()


class TestTheTokenIsReadOnlyFromTheDeepSeekPlatform:
    PAGE = "https://platform.deepseek.com/sign_in"

    def read(self, monkeypatch, targets):
        asked = []
        monkeypatch.setattr(cdp, "debug_port", lambda profile: 1234)
        monkeypatch.setattr(cdp, "page_targets", lambda port: targets)
        monkeypatch.setattr(cdp, "evaluate", lambda ws, expression: asked.append(ws) or f"token-from-{ws}")
        return cdp.token_from_page("profile"), asked

    def test_other_origins_are_skipped(self, monkeypatch):
        targets = [{"url": url, "webSocketDebuggerUrl": f"ws://{i}"} for i, url in enumerate((
            "https://accounts.google.com/o/oauth2/auth",
            "https://platform.deepseek.com.evil.example/",        # a look-alike host
            "http://platform.deepseek.com/",                      # the wrong scheme
            "https://platform.deepseek.com:8443/",                # the wrong port
            "about:blank",
            self.PAGE))]
        token, asked = self.read(monkeypatch, targets)
        assert asked == ["ws://5"] and token == "token-from-ws://5"

    def test_the_default_port_may_be_written_out(self, monkeypatch):
        token, asked = self.read(monkeypatch, [
            {"url": "https://PLATFORM.deepseek.com:443/usage", "webSocketDebuggerUrl": "ws://0"}])
        assert asked == ["ws://0"]

    def test_a_target_without_a_url_is_skipped(self, monkeypatch):
        token, asked = self.read(monkeypatch, [{"webSocketDebuggerUrl": "ws://0"}])
        assert asked == [] and token is None

    def test_the_sign_in_page_is_on_that_origin(self):
        assert cdp.origin_of(bs.SIGNIN_URL) == cdp.origin_of(cdp.PLATFORM_ORIGIN)


class TestTheWorkerCleansUpOnEveryExit:
    """`_signin_worker` ends with the browser closed and the profile deleted, whatever
    happened. The dialog is built without Tk: the worker touches no widget."""

    @pytest.fixture
    def dialog(self, config_dir, browser, monkeypatch):
        dialog = SettingsDialog.__new__(SettingsDialog)
        dialog._signin_cancel = False
        dialog._signin_result = None
        dialog._signin_thread = None
        monkeypatch.setattr(deepseek_auth, "save_user_token", lambda token: True)
        monkeypatch.setattr(deepseek_usage, "fetch_summary",
                            lambda token: deepseek_usage.AccountSummary(available=True, topped_up=2.0))
        return dialog

    def cleaned_up(self, config_dir, events):
        return ([e for e in events if e[0] == "close"] != []
                and not (config_dir / "browser").exists()
                and not (config_dir / "firefox").exists()
                and (config_dir / "secrets" / "keep.txt").exists())

    def test_success(self, dialog, config_dir, browser, monkeypatch):
        monkeypatch.setattr(bs, "wait_for_token", lambda **kw: "tok")
        dialog._signin_worker(None)
        assert dialog._signin_result[0] == "ok"
        assert self.cleaned_up(config_dir, browser)

    def test_cancelled(self, dialog, config_dir, browser, monkeypatch):
        def cancelled(**kw):
            dialog._signin_cancel = True
            return None
        monkeypatch.setattr(bs, "wait_for_token", cancelled)
        dialog._signin_worker(None)
        assert dialog._signin_result == ("fail", "Cancelled")
        assert self.cleaned_up(config_dir, browser)

    def test_timed_out(self, dialog, config_dir, browser, monkeypatch):
        monkeypatch.setattr(bs, "wait_for_token", lambda **kw: None)
        dialog._signin_worker(None)
        assert dialog._signin_result == ("fail", "Timed out")      # the browser is closed now
        assert self.cleaned_up(config_dir, browser)

    def test_an_error_while_waiting(self, dialog, config_dir, browser, monkeypatch):
        def broken(**kw):
            raise RuntimeError("devtools went away")
        monkeypatch.setattr(bs, "wait_for_token", broken)
        dialog._signin_worker(None)
        assert dialog._signin_result[0] == "fail"
        assert self.cleaned_up(config_dir, browser)

    def test_a_rejected_token(self, dialog, config_dir, browser, monkeypatch):
        monkeypatch.setattr(bs, "wait_for_token", lambda **kw: "tok")
        monkeypatch.setattr(deepseek_usage, "fetch_summary",
                            lambda token: deepseek_usage.AccountSummary(error="Session expired"))
        dialog._signin_worker(None)
        assert dialog._signin_result == ("fail", "Session expired")
        assert self.cleaned_up(config_dir, browser)

    def test_the_browser_is_closed_once_when_the_token_is_kept(self, dialog, config_dir, browser, monkeypatch):
        monkeypatch.setattr(bs, "wait_for_token", lambda **kw: "tok")
        dialog._signin_worker(None)
        assert len([e for e in browser if e[0] == "close"]) == 1

    def test_the_dialog_closed_while_waiting(self, dialog, config_dir, browser, monkeypatch):
        """Closing the dialog sets the flag; the worker, still polling, notices and cleans up."""
        polling = threading.Event()

        def wait_until_told(should_stop=None, **kw):
            polling.set()
            while not should_stop():
                threading.Event().wait(0.01)
            return None

        monkeypatch.setattr(bs, "wait_for_token", wait_until_told)
        dialog._signin_thread = threading.Thread(target=dialog._signin_worker, args=(None,), daemon=True)
        dialog._signin_thread.start()
        assert polling.wait(2)
        dialog._stop_signin(wait=5)              # what _on_dialog_close does, then the worker finishes
        assert not dialog._signin_thread.is_alive()
        assert self.cleaned_up(config_dir, browser)


# ------------------------------------------------- sign-ins that overlap, and Exit during one

class Widget:
    def __init__(self):
        self.calls = []

    def config(self, **kwargs):
        self.calls.append(kwargs)


class Var:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


def make_dialog():
    """A Settings dialog without Tk, as far as sign-in, forget and their workers go. What it
    schedules with after() is kept in `scheduled` and run by `pump`, as the Tk loop would."""
    dialog = SettingsDialog.__new__(SettingsDialog)
    dialog._signin_cancel, dialog._signin_result, dialog._signin_thread = False, None, None
    dialog._forget_result, dialog._forget_thread = None, None
    dialog._signin_btn, dialog._signin_status, dialog._credential_note = Widget(), Widget(), Widget()
    dialog.deepseek_token_var = Var("pasted-token")
    dialog.refreshed = []
    dialog.on_refresh = lambda: dialog.refreshed.append(1)
    dialog.scheduled = []
    dialog.dialog = types.SimpleNamespace(winfo_exists=lambda: True,
                                          after=lambda ms, fn: dialog.scheduled.append((ms, fn)))
    return dialog


def pump(dialog, until, seconds=5.0):
    """Run what the dialog scheduled, as the Tk loop would, until `until()` or the time is up."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        due, dialog.scheduled = dialog.scheduled, []
        for _, fn in due:
            fn()
        if until():
            return True
        time.sleep(0.01)
    return until()


class FakeBrowsers:
    """The browser and its profile directory as the sign-ins see them. Every sign-in shares the
    one profile, so `close` ends every browser on it, as the real one (which matches by profile
    and not by the process it was given) does, and `forget` deletes the profile under them.

    A worker polls for the token until its dialog cancels it. Sign-in number 1 then keeps
    polling until `late[1]` is set: the worker that wakes seconds after its dialog closed
    (inside its 2 s sleep, or a 3 s DevTools probe). Sign-ins are numbered in the order their
    workers start polling."""

    def __init__(self, monkeypatch):
        self.running = []                       # numbers of the browsers open on the profile
        self.launched = 0
        self.events = []
        self.polling = collections.defaultdict(threading.Event)
        self.late = collections.defaultdict(threading.Event)
        self._lock = threading.Lock()
        self._polls = 0
        monkeypatch.setattr(bs, "find_browser", lambda: "C:/fake/msedge.exe")
        monkeypatch.setattr(bs, "find_firefox", lambda: None)
        monkeypatch.setattr(bs, "launch", self.launch)
        monkeypatch.setattr(bs, "close", self.close)
        monkeypatch.setattr(bs, "forget", self.forget)
        monkeypatch.setattr(bs, "wait_for_token", self.wait_for_token)
        for number in (2, 3):
            self.late[number].set()             # only the first one wakes late

    def launch(self, url=None):
        with self._lock:
            self.launched += 1
            self.running.append(self.launched)
            self.events.append(("launch", self.launched))
            return types.SimpleNamespace(number=self.launched)

    def close(self, process=None):
        with self._lock:
            self.events.append(("close", list(self.running)))
            self.running.clear()
            return 1

    def forget(self):
        with self._lock:
            self.events.append(("forget",))
            return True

    def wait_for_token(self, timeout=300.0, interval=2.0, should_stop=None, profile=None):
        with self._lock:
            self._polls += 1
            number = self._polls
        self.polling[number].set()
        while not should_stop():
            time.sleep(0.005)
        self.late[number].wait(5)
        return None


@pytest.fixture
def world(monkeypatch):
    return FakeBrowsers(monkeypatch)


def finish(*dialogs):
    for dialog in dialogs:
        dialog._stop_signin(wait=5)
        assert dialog._signin_thread is None or not dialog._signin_thread.is_alive()


class TestACancelledSignInCannotTearDownTheNextOne:
    """Closing Settings during a sign-in only sets a flag: the worker is inside its poll and
    cleans up when it wakes, seconds later. A sign-in started in between shares its profile
    directory, and the late cleanup closed that browser and deleted that profile."""

    def test_the_new_sign_in_opens_its_browser_after_the_old_one_has_cleaned_up(self, world):
        old, new = make_dialog(), make_dialog()
        old._begin_signin()
        assert world.polling[1].wait(3)
        old._stop_signin()                               # Settings closed, nothing waited for

        started = time.monotonic()
        new._begin_signin()                              # reopened; Sign in clicked
        assert time.monotonic() - started < 1.0, "the Tk thread waited for the old sign-in"

        world.late[1].set()                              # the old worker wakes and cleans up
        old._signin_thread.join(5)
        assert world.polling[2].wait(5), "the new sign-in never got its browser"

        assert world.running == [2], "the old sign-in's cleanup closed the new browser"
        launch_2 = world.events.index(("launch", 2))
        assert ("forget",) in world.events[:launch_2], "the old profile is deleted before the new browser starts"
        assert ("forget",) not in world.events[launch_2:], "a profile deleted under the new browser"
        finish(new)

    def test_a_sign_in_cancelled_while_it_waits_never_opens_a_browser(self, world):
        old, new = make_dialog(), make_dialog()
        old._begin_signin()
        assert world.polling[1].wait(3)
        old._stop_signin()
        new._begin_signin()
        new._stop_signin()                               # reopened and closed again at once

        world.late[1].set()
        old._signin_thread.join(5)
        new._signin_thread.join(5)

        assert world.launched == 1 and world.running == []
        assert new._signin_result == ("fail", "Cancelled")

    def test_sign_ins_one_after_another_each_get_a_browser(self, world):
        first = make_dialog()
        first._begin_signin()
        assert world.polling[1].wait(3)
        world.late[1].set()
        finish(first)
        second = make_dialog()
        second._begin_signin()
        assert world.polling[2].wait(3)
        assert world.running == [2]
        finish(second)
        assert world.running == []

    def test_a_browser_that_will_not_open_is_said_in_the_status_line(self, world, monkeypatch):
        monkeypatch.setattr(bs, "launch", lambda url=None: None)
        dialog = make_dialog()
        dialog._begin_signin()
        dialog._signin_thread.join(5)
        assert dialog._signin_result == ("fail", "Could not open the browser")
        assert world.events == [], "nothing was started, so nothing is closed or deleted"

    def test_a_launch_that_raises_is_reported_and_leaves_the_next_sign_in_able_to_start(self, world, monkeypatch):
        def broken(url=None):
            raise OSError("no such file")

        monkeypatch.setattr(bs, "launch", broken)
        world.late[1].set()
        dialog = make_dialog()
        dialog._begin_signin()
        dialog._signin_thread.join(5)
        assert dialog._signin_result == ("fail", "OSError: no such file")

        monkeypatch.setattr(bs, "launch", world.launch)
        again = make_dialog()
        again._begin_signin()
        assert world.polling[1].wait(3)          # the session was released by the failure
        finish(again)


class TestOneSignInAtATime:
    def test_a_second_sign_in_waits_for_the_first_to_be_finished(self):
        order, release = [], threading.Event()

        def first():
            with bs.session():
                order.append("first in")
                release.wait(5)
                order.append("first out")

        def second():
            with bs.session():
                order.append("second in")

        one = threading.Thread(target=first)
        one.start()
        while "first in" not in order:
            time.sleep(0.005)
        two = threading.Thread(target=second)
        two.start()
        time.sleep(0.1)
        assert order == ["first in"], "the second one got in while the first was running"
        release.set()
        one.join(5)
        two.join(5)
        assert order == ["first in", "first out", "second in"]

    def test_the_session_is_released_when_the_sign_in_raises(self):
        with pytest.raises(RuntimeError):
            with bs.session():
                raise RuntimeError("the sign-in broke")
        with bs.session():                           # would block for ever if it were still held
            pass


class TestAbandonSignin:
    """What Exit does about a sign-in: its worker is a daemon thread that ends with the process
    before its own cleanup, which left the browser open and the plaintext profile on disk."""

    def test_it_closes_the_browser_and_deletes_the_capture_profiles_of_a_sign_in_under_way(
            self, config_dir, browser):
        with bs.session():
            bs.abandon_signin()
        assert browser[0] == ("close", None)
        assert not (config_dir / "browser").exists()
        assert not (config_dir / "firefox").exists()
        for kept in ("secrets", "bystander"):
            assert (config_dir / kept / "keep.txt").read_text() == kept

    def test_it_does_nothing_when_no_sign_in_is_running(self, config_dir, browser):
        """Exit stays instant, and a profile that is not a running sign-in's is left alone."""
        bs.abandon_signin()
        assert browser == []
        assert (config_dir / "browser" / "keep.txt").exists()

    def test_it_does_not_wait_for_the_sign_in_it_ends(self, config_dir, browser):
        """The worker may be minutes into a poll and holds the session for all of it."""
        outcome = []
        with bs.session():
            thread = threading.Thread(target=lambda: (bs.abandon_signin(), outcome.append("done")),
                                      daemon=True)
            thread.start()
            thread.join(3)
        assert outcome == ["done"]

    def test_its_retries_are_short(self, monkeypatch):
        """A profile the browser still holds is tried again, but Exit must not wait for it."""
        tries, pauses = [], []
        monkeypatch.setattr(bs, "close", lambda process=None: 0)
        monkeypatch.setattr(bs, "forget", lambda: tries.append(1) or False)
        monkeypatch.setattr(bs.time, "sleep", pauses.append)
        with bs.session():
            bs.abandon_signin()
        assert len(tries) == bs._EXIT_DELETE_ATTEMPTS
        assert sum(pauses) <= 0.5

    def test_it_never_raises(self, monkeypatch):
        def broken(process=None):
            raise OSError("no such process")
        monkeypatch.setattr(bs, "close", broken)
        with bs.session():
            bs.abandon_signin()


class TestForgetDoesNotFreezeTheDialog:
    """Forget used to join the sign-in worker for up to 2.5 s on the Tk thread. The stopping and
    the forgetting now run on a thread of their own, behind the same one-sign-in-at-a-time
    session, and the dialog's labels follow when they are done."""

    @pytest.fixture
    def forgotten(self, monkeypatch, world):
        """deepseek_auth.clear_credentials, recorded in the same list as the browsers' events."""
        monkeypatch.setattr(deepseek_auth, "clear_credentials", lambda: world.events.append(("clear",)))
        return world.events

    def test_the_tk_thread_returns_at_once_while_a_sign_in_runs_and_the_forget_still_completes(
            self, world, forgotten):
        dialog = make_dialog()
        dialog._begin_signin()
        assert world.polling[1].wait(3) and world.running == [1]

        started = time.monotonic()
        dialog._forget_signin()
        assert time.monotonic() - started < 0.3, "the Tk thread waited for the sign-in worker"
        assert ("clear",) not in world.events, "nothing may be cleared while the worker can still store a token"
        assert dialog._signin_cancel, "the running sign-in is told to stop"
        assert dialog._signin_btn.calls[-1] == {"state": tk.DISABLED}, "no sign-in may start mid-forget"
        before = dialog._signin_thread
        dialog._begin_signin()                           # a click that gets through anyway
        assert dialog._signin_thread is before and world.launched == 1

        world.late[1].set()                              # the worker wakes, sees the flag, cleans up
        assert pump(dialog, lambda: dialog._credential_note.calls), "the labels never followed"

        assert world.running == [], "the browser is still open"
        cleanup = world.events.index(("close", [1]))
        assert world.events.index(("clear",)) > cleanup, "cleared while the worker was still running"
        assert world.events.count(("forget",)) == 2      # the worker's cleanup, then the forget's own
        assert dialog._credential_note.calls[-1] == {"text": "Signed out"}
        assert dialog._signin_btn.calls[-1] == {"state": tk.NORMAL}
        assert dialog._signin_status.calls[-1] == {"text": ""}
        assert dialog.deepseek_token_var.get() == ""
        assert dialog.refreshed == [1]

    def test_the_cancelled_sign_in_does_not_overwrite_the_cleared_status(self, world, forgotten):
        """The sign-in forget stopped reports "Cancelled": the user did that, it is no news, and
        it must not land after the forget has cleared the status line."""
        dialog = make_dialog()
        dialog._begin_signin()
        assert world.polling[1].wait(3)
        dialog._forget_signin()
        world.late[1].set()
        assert pump(dialog, lambda: dialog._credential_note.calls)
        pump(dialog, lambda: False, seconds=0.5)         # let any leftover poll run
        texts = [call["text"] for call in dialog._signin_status.calls if "text" in call]
        assert texts[-1] == "", f"the status line ends as {texts[-1]!r}"
        assert not dialog.scheduled, "the sign-in poll is still running after both are done"

    def test_forget_with_no_sign_in_running(self, world, forgotten):
        dialog = make_dialog()
        started = time.monotonic()
        dialog._forget_signin()
        assert time.monotonic() - started < 0.3
        assert pump(dialog, lambda: dialog._credential_note.calls)
        assert sorted(world.events) == [("clear",), ("forget",)]
        assert dialog._credential_note.calls[-1] == {"text": "Signed out"}

    def test_a_profile_that_could_not_be_deleted_says_credentials_cleared(self, world, forgotten, monkeypatch):
        monkeypatch.setattr(bs, "forget", lambda: False)
        dialog = make_dialog()
        dialog._forget_signin()
        assert pump(dialog, lambda: dialog._credential_note.calls)
        assert dialog._credential_note.calls[-1] == {"text": "Credentials cleared"}

    def test_a_second_click_while_it_runs_does_nothing(self, world, forgotten):
        dialog = make_dialog()
        dialog._begin_signin()
        assert world.polling[1].wait(3)
        dialog._forget_signin()
        dialog._forget_signin()
        world.late[1].set()
        assert pump(dialog, lambda: dialog._credential_note.calls)
        assert world.events.count(("clear",)) == 1

    def test_a_forget_that_fails_is_said_and_not_reported_as_done(self, world, monkeypatch):
        def broken():
            raise RuntimeError("the credential store is gone")
        monkeypatch.setattr(deepseek_auth, "clear_credentials", broken)
        dialog = make_dialog()
        dialog._forget_signin()
        assert pump(dialog, lambda: dialog._credential_note.calls)
        assert dialog._credential_note.calls[-1] == {"text": "Could not forget the sign-in"}
        assert dialog._signin_btn.calls[-1] == {"state": tk.NORMAL}
        assert dialog.refreshed == []
        assert dialog.deepseek_token_var.get() == "pasted-token"

    def test_a_new_sign_in_after_a_forget_gets_its_own_browser(self, world, forgotten):
        dialog = make_dialog()
        dialog._begin_signin()
        assert world.polling[1].wait(3)
        dialog._forget_signin()
        world.late[1].set()
        assert pump(dialog, lambda: dialog._credential_note.calls)
        dialog._begin_signin()
        assert world.polling[2].wait(3) and world.running == [2]
        finish(dialog)
