"""Reading the DeepSeek session token out of ClaudeBar's own browser profile."""

import browser_signin as bs


TOKEN = "QOdEKnsm8ByIxG4aU62U3+tpiWPyAOw4cMGY5w0xR8jOyzlJzbixXigIUavD8+L+"


def latin1_store(key=b"userToken", value=TOKEN, pad_before=b"", pad_after=b""):
    """Chromium stores Latin-1 localStorage values behind a 0x01 prefix byte."""
    return (pad_before + b"_https://platform.deepseek.com\x00\x01" + key
            + b"\x01" + b'{"value":"' + value.encode() + b'","__version":"0"}'
            + pad_after)


def utf16_store(key=b"userToken", value=TOKEN):
    payload = b'{"value":"' + value.encode() + b'","__version":"0"}'
    return (b"_https://platform.deepseek.com\x00\x01" + key
            + b"\x00" + payload.decode().encode("utf-16-le"))


class TestExtractToken:
    def test_reads_a_latin1_value(self):
        assert bs.extract_token(latin1_store()) == TOKEN

    def test_reads_a_utf16_value(self):
        assert bs.extract_token(utf16_store()) == TOKEN

    def test_finds_the_key_among_other_storage(self):
        noise_before = b"_https://platform.deepseek.com\x00\x01theme\x01dark"
        noise_after = b"_https://chat.deepseek.com\x00\x01other\x01thing"
        assert bs.extract_token(latin1_store(pad_before=noise_before,
                                             pad_after=noise_after)) == TOKEN

    def test_absent_key_is_none(self):
        assert bs.extract_token(b"_https://platform.deepseek.com\x00\x01theme\x01dark") is None

    def test_empty_data_is_none(self):
        assert bs.extract_token(b"") is None

    def test_a_truncated_token_is_rejected(self):
        """A partial write must not be mistaken for a session."""
        assert bs.extract_token(latin1_store(value="short")) is None

    def test_the_key_alone_is_not_a_token(self):
        assert bs.extract_token(b"userToken") is None

    def test_another_origin_key_is_not_confused(self):
        """The key name is what matters, not which origin wrote it; a token is
        still a token, but a non-JSON neighbour must not match."""
        assert bs.extract_token(b"userToken\x01not-json-at-all-ever") is None


class TestReadToken:
    def _profile(self, tmp_path, filename="000003.log", data=None):
        root = tmp_path / "Default" / "Local Storage" / "leveldb"
        root.mkdir(parents=True)
        (root / filename).write_bytes(data if data is not None else latin1_store())
        return tmp_path

    def test_reads_from_a_profile_tree(self, tmp_path):
        assert bs.read_token(self._profile(tmp_path)) == TOKEN

    def test_reads_from_a_ldb_file_too(self, tmp_path):
        assert bs.read_token(self._profile(tmp_path, "000005.ldb")) == TOKEN

    def test_profile_without_the_key_is_none(self, tmp_path):
        assert bs.read_token(self._profile(tmp_path, data=b"nothing here")) is None

    def test_absent_profile_is_none(self, tmp_path):
        assert bs.read_token(tmp_path / "does-not-exist") is None


class TestWaitForToken:
    def test_returns_immediately_when_already_present(self, tmp_path):
        root = tmp_path / "Default" / "Local Storage" / "leveldb"
        root.mkdir(parents=True)
        (root / "000003.log").write_bytes(latin1_store())
        assert bs.wait_for_token(timeout=1, interval=0.01, profile=tmp_path) == TOKEN

    def test_times_out_when_absent(self, tmp_path):
        empty = tmp_path / "Default" / "Local Storage" / "leveldb"
        empty.mkdir(parents=True)
        assert bs.wait_for_token(timeout=0.15, interval=0.05, profile=tmp_path) is None

    def test_stop_callback_ends_the_wait(self, tmp_path):
        calls = {"n": 0}

        def stop():
            calls["n"] += 1
            return calls["n"] > 2

        assert bs.wait_for_token(timeout=10, interval=0.01, should_stop=stop,
                                 profile=tmp_path) is None
        assert calls["n"] < 10  # it gave up long before the timeout


class TestBrowserDiscovery:
    def test_find_browser_returns_a_real_path_or_none(self):
        found = bs.find_browser()
        if found is not None:
            from pathlib import Path
            assert Path(found).exists()

    def test_profile_is_inside_the_claude_bar_config_dir(self):
        assert "ClaudeBar" in str(bs.profile_dir())
        assert bs.profile_dir() != bs.profile_dir().parent


class TestSettingsDialogColours:
    """The dialog is a Toplevel, not a ClaudeBarWindow, so it cannot borrow the
    main window's palette. The sign-in handler raised AttributeError on its
    first line because of exactly that, and Tkinter swallowed it, so the button
    simply looked dead."""

    HANDLERS = ("_begin_signin", "_poll_signin", "_signin_worker",
                "_signin_failed", "_signin_succeeded")

    def test_every_colour_the_signin_code_uses_exists_on_the_dialog(self):
        import inspect
        import re
        from ui_window import SettingsDialog

        source = "".join(inspect.getsource(getattr(SettingsDialog, name))
                         for name in self.HANDLERS)
        used = set(re.findall(r"self\.(\w*colou?r\w*)", source))
        assert used, "expected the handlers to use at least one colour"
        missing = sorted(name for name in used if not hasattr(SettingsDialog, name))
        assert not missing, f"SettingsDialog is missing {missing}"

    def test_the_dialog_palette_matches_the_panel(self):
        from ui_window import ClaudeBarWindow, SettingsDialog
        main = ClaudeBarWindow(on_refresh=lambda: None, on_settings=lambda: None,
                              on_exit=lambda: None)
        assert SettingsDialog.warn_color == main.warn_color
        assert SettingsDialog.ok_color == main.ok_color


class TestClosingTheBrowser:
    """Closing must target the profile, not the process we spawned.

    A second launch of the same browser against the same profile hands off to
    the running instance and exits immediately, so the Popen we hold is dead
    while the window it opened is very much alive.
    """

    def test_close_returns_a_count_and_stops_our_windows(self):
        import browser_signin as bs
        stopped = bs.close()
        assert isinstance(stopped, int)
        assert stopped >= 0
        assert bs.pids_using_profile() == []          # nothing of ours left running

    def test_pids_using_profile_is_a_list(self):
        import browser_signin as bs
        assert isinstance(bs.pids_using_profile(), list)

    def test_the_user_browser_is_never_matched(self):
        """Only the ClaudeBar profile path may match, so an everyday Edge or
        Chrome session is never a target."""
        import browser_signin as bs
        target = str(bs.profile_dir()).lower()
        assert "claudebar" in target
        assert target.endswith("browser")
        for pid in bs.pids_using_profile():
            assert target in bs._command_line(pid).lower()

    def test_close_tolerates_a_process_argument(self):
        """Kept for the non-Windows path, where there is no process list."""
        import browser_signin as bs
        assert isinstance(bs.close(None), int)


class TestForgetGuard:
    """Deleting a browser profile is destructive, so the target is proven
    rather than assumed. A real Edge or Chrome profile is gigabytes of
    bookmarks, passwords and history; ours is one site's session."""

    def test_our_own_profile_is_recognised(self):
        import browser_signin as bs
        assert bs.is_our_profile() is True
        assert bs.is_our_profile(bs.profile_dir()) is True

    def test_a_real_browser_profile_is_refused(self, tmp_path, monkeypatch):
        import browser_signin as bs
        fake = tmp_path / "Google" / "Chrome" / "User Data"
        fake.mkdir(parents=True)
        (fake / "Default").mkdir()
        assert bs.is_our_profile(fake) is False

    def test_an_edge_profile_is_refused(self, tmp_path):
        import browser_signin as bs
        fake = tmp_path / "Microsoft" / "Edge" / "User Data"
        fake.mkdir(parents=True)
        assert bs.is_our_profile(fake) is False

    def test_an_arbitrary_directory_is_refused(self, tmp_path):
        import browser_signin as bs
        assert bs.is_our_profile(tmp_path) is False
        assert bs.is_our_profile(tmp_path / "browser") is False

    def test_forget_deletes_only_our_profile(self, tmp_path, monkeypatch):
        import browser_signin as bs
        # Point the config dir at a sandbox and give it a profile to remove
        monkeypatch.setattr(bs, "get_config_dir", lambda: tmp_path)
        profile = tmp_path / "browser"
        profile.mkdir()
        (profile / "marker").write_text("session data")
        bystander = tmp_path / "keepme"
        bystander.mkdir()
        (bystander / "important").write_text("must survive")

        assert bs.forget() is True
        assert not profile.exists()
        assert (bystander / "important").read_text() == "must survive"

    def test_forget_refuses_when_the_path_is_wrong(self, tmp_path, monkeypatch):
        import browser_signin as bs
        monkeypatch.setattr(bs, "get_config_dir", lambda: tmp_path / "elsewhere")
        monkeypatch.setattr(bs, "profile_dir", lambda: tmp_path / "Google" / "Chrome" / "User Data")
        victim = tmp_path / "Google" / "Chrome" / "User Data"
        victim.mkdir(parents=True)
        (victim / "Bookmarks").write_text("precious")
        assert bs.forget() is False
        assert (victim / "Bookmarks").read_text() == "precious"


class TestBrowserDiscovery:
    """The default browser is irrelevant: we launch our own instance with our
    own profile. What matters is finding any Chromium engine, because only
    Chromium's localStorage is the LevelDB shape this app can read."""

    def test_a_browser_is_found_on_this_machine(self):
        import browser_signin as bs
        found = bs.find_browser()
        assert found is not None, "no Chromium browser found to test against"
        from pathlib import Path
        assert Path(found).exists()

    def test_edge_is_preferred_when_present(self):
        """Edge ships with Windows, so it is the most reliable choice and must
        not be displaced by another browser that merely sorts earlier on disk."""
        import browser_signin as bs
        from pathlib import Path
        candidates = [c for c in bs._candidate_paths() if Path(c).exists()]
        if not candidates:
            return
        first = candidates[0].lower()
        assert "msedge" in first or "chrome" in first

    def test_the_gecko_family_is_not_offered(self):
        """Firefox, Zen and Floorp store localStorage in SQLite with snappy
        values, so offering them would promise a token we cannot read."""
        import browser_signin as bs
        joined = " ".join(bs._candidate_paths()).lower()
        for name in ("firefox", "zen", "floorp", "waterfox", "librewolf"):
            assert name not in joined, f"{name} must not be a candidate"

    def test_common_chromium_browsers_are_covered(self):
        import browser_signin as bs
        joined = " ".join(bs._candidate_paths()).lower()
        for name in ("msedge", "chrome", "brave", "chromium", "vivaldi", "opera"):
            assert name in joined, f"{name} should be discoverable"

    def test_per_user_installs_are_covered(self):
        """Chrome and Brave often install under LOCALAPPDATA rather than
        Program Files, which is where a home machine usually has them."""
        import browser_signin as bs
        import os
        local = os.environ.get("LOCALAPPDATA", "").replace(chr(92), "/").lower()
        if not local:
            return
        assert any(c.lower().startswith(local + "/programs") for c in bs._candidate_paths())


class TestLaunch:
    """launch() had no coverage at all, so deleting a module constant it used
    only surfaced when someone clicked the button and got a NameError in the
    status line. These tests exercise the command it builds."""

    def test_it_builds_a_detached_browser_command(self, monkeypatch, tmp_path):
        import browser_signin as bs

        captured = {}

        class FakePopen:
            def __init__(self, args, **kwargs):
                captured["args"] = args
                captured["kwargs"] = kwargs

        monkeypatch.setattr(bs, "find_browser", lambda: "C:/fake/msedge.exe")
        monkeypatch.setattr(bs, "profile_dir", lambda: tmp_path / "browser")
        monkeypatch.setattr(bs.subprocess, "Popen", FakePopen)

        proc = bs.launch("https://example.test/sign_in")
        assert isinstance(proc, FakePopen)

        args = captured["args"]
        assert args[0] == "C:/fake/msedge.exe"
        assert any(a == "--app=https://example.test/sign_in" for a in args)
        assert any(a.startswith("--user-data-dir=") for a in args)
        # The debug port is what lets the token be read from the live page
        # instead of waiting on Chromium to flush it to disk.
        assert "--remote-debugging-port=0" in args

        # The detached flag is what keeps the browser alive independently of
        # the process that started it, so it is asserted rather than assumed.
        assert captured["kwargs"].get("creationflags") == bs._DETACHED

    def test_it_creates_the_profile_directory(self, monkeypatch, tmp_path):
        import browser_signin as bs
        profile = tmp_path / "nested" / "browser"
        monkeypatch.setattr(bs, "find_browser", lambda: "C:/fake/msedge.exe")
        monkeypatch.setattr(bs, "profile_dir", lambda: profile)
        monkeypatch.setattr(bs.subprocess, "Popen", lambda *a, **k: None)

        bs.launch()
        assert profile.is_dir()

    def test_no_browser_means_no_launch(self, monkeypatch):
        import browser_signin as bs
        monkeypatch.setattr(bs, "find_browser", lambda: None)
        assert bs.launch() is None

    def test_the_detached_constant_is_still_defined(self):
        import browser_signin as bs
        assert bs._DETACHED == (0x00000008 | 0x00000200)


class TestTheFirefoxReader:
    """Firefox's storage, read the same way the Linux build reads it.

    There is no WebDriver and no debug port here on purpose: a driven Firefox is
    served a human-verification page instead of the sign-in form, so the ordinary
    browser is the only one that works. What it leaves behind is plain SQLite,
    uncompressed, and readable while it is still open.
    """

    def _profile(self, tmp_path, value, table="data", column_value="value"):
        import sqlite3
        db = (tmp_path / "storage" / "default" / "https+++platform.deepseek.com"
              / "ls" / "data.sqlite")
        db.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(db)
        try:
            connection.execute(
                f"CREATE TABLE {table} (key TEXT, {column_value} BLOB, "
                f"compression_type INTEGER)")
            connection.execute(
                f"INSERT INTO {table} VALUES (?, ?, ?)", ("userToken", value, 0))
            connection.commit()
        finally:
            connection.close()
        return tmp_path

    def test_it_reads_the_wrapped_value(self, tmp_path):
        stored = b'{"value":"a-real-session-token","__version":"0"}'
        assert bs.read_firefox_token(self._profile(tmp_path, stored)) == "a-real-session-token"

    def test_a_signed_out_row_is_not_a_session(self, tmp_path):
        stored = b'{"value":null,"__version":"0"}'
        assert bs.read_firefox_token(self._profile(tmp_path, stored)) is None

    def test_utf16_is_decoded_rather_than_mistaken_for_utf8(self, tmp_path):
        stored = '{"value":"a-real-session-token","__version":"0"}'.encode("utf-16-le")
        assert bs.read_firefox_token(self._profile(tmp_path, stored)) == "a-real-session-token"

    def test_a_compressed_value_is_refused_not_guessed(self, tmp_path):
        import sqlite3
        profile = self._profile(tmp_path, b"\x00\x01\x02binary")
        db = next((profile / "storage").glob("default/*/ls/data.sqlite"))
        connection = sqlite3.connect(db)
        connection.execute("UPDATE data SET compression_type = 1")
        connection.commit()
        connection.close()
        assert bs.read_firefox_token(profile) is None

    def test_an_absent_profile_is_none(self, tmp_path):
        assert bs.read_firefox_token(tmp_path / "nope") is None

    def test_a_profile_without_the_origin_is_none(self, tmp_path):
        (tmp_path / "storage" / "default").mkdir(parents=True)
        assert bs.read_firefox_token(tmp_path) is None

    def test_the_bare_string_is_accepted(self, tmp_path):
        """What a simpler page would store; one startswith is cheaper than a guess."""
        assert bs.read_firefox_token(
            self._profile(tmp_path, b"a-real-session-token")) == "a-real-session-token"

    def test_binary_that_happens_to_decode_is_rejected(self, tmp_path):
        assert bs.read_firefox_token(
            self._profile(tmp_path, b"\x01\x02\x03\x04\x05\x06")) is None


class TestFirefoxAsAFallback:
    def test_the_firefox_profile_is_its_own_directory(self):
        assert bs.firefox_profile_dir() != bs.profile_dir()
        assert "ClaudeBar" in str(bs.firefox_profile_dir())

    def test_both_profiles_are_recognised_as_ours(self):
        assert bs.is_our_profile(bs.profile_dir())
        assert bs.is_our_profile(bs.firefox_profile_dir())

    def test_a_real_firefox_profile_is_never_ours(self):
        """The guard is what stands between a bug and deleted bookmarks."""
        from pathlib import Path
        assert not bs.is_our_profile(
            Path.home() / "AppData" / "Roaming" / "Mozilla" / "Firefox" / "Profiles" / "x")

    def test_engine_is_none_before_a_launch(self, monkeypatch):
        monkeypatch.setattr(bs, "_engine", None)
        assert bs.engine() is None

    def test_launch_falls_back_to_firefox_when_there_is_no_chromium(
            self, monkeypatch, tmp_path):
        """Which is the whole point: a machine with only Firefox could not sign in."""
        monkeypatch.setattr(bs, "find_browser", lambda: None)
        monkeypatch.setattr(bs, "find_firefox", lambda: r"C:\fake\firefox.exe")
        monkeypatch.setattr(bs, "firefox_profile_dir", lambda: tmp_path / "firefox")
        monkeypatch.setattr(bs, "_launch_firefox",
                            lambda browser, url: ("launched", browser))
        assert bs.launch() == ("launched", r"C:\fake\firefox.exe")
        assert bs.engine() == "firefox"

    def test_launch_still_prefers_chromium(self, monkeypatch, tmp_path):
        monkeypatch.setattr(bs, "find_browser", lambda: r"C:\fake\msedge.exe")
        monkeypatch.setattr(bs, "profile_dir", lambda: tmp_path / "browser")
        monkeypatch.setattr(bs.subprocess, "Popen", lambda *a, **k: "launched")
        assert bs.launch() == "launched"
        assert bs.engine() == "chromium"

    def test_the_live_page_is_not_asked_when_firefox_is_the_engine(
            self, monkeypatch, tmp_path):
        """There is no debug port to ask, and asking would only waste a poll."""
        called = {"n": 0}
        monkeypatch.setattr(bs, "_engine", "firefox")
        monkeypatch.setattr(bs.browser_cdp, "token_from_page",
                            lambda target: called.__setitem__("n", called["n"] + 1))
        monkeypatch.setattr(bs, "read_token", lambda profile=None: "from-disk")
        assert bs.wait_for_token(timeout=1, interval=0.01, profile=tmp_path) == "from-disk"
        assert called["n"] == 0


class TestFirefoxDiscovery:
    r"""Where Firefox actually is, without needing Firefox installed to say so.

    There is no Firefox on the machine this was written on, so the two branches
    are exercised directly rather than through a real install: the directory
    branch against a fabricated tree, the registry branch against a fake winreg.
    The registry layout is Mozilla's documented one - CurrentVersion, then
    <version>\Main\PathToExe - and has not been measured against a real install.
    """

    def test_it_finds_a_browser_in_the_usual_directory(self, monkeypatch, tmp_path):
        exe = tmp_path / "Mozilla Firefox" / "firefox.exe"
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"MZ")
        monkeypatch.setattr(bs.sys, "platform", "win32")
        monkeypatch.setenv("ProgramFiles", str(tmp_path))
        monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path / "none"))
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "none"))
        monkeypatch.setitem(__import__("sys").modules, "winreg", None)
        assert bs.find_firefox() == str(exe)

    def test_the_registry_answer_wins(self, monkeypatch, tmp_path):
        exe = tmp_path / "custom" / "firefox.exe"
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"MZ")

        class FakeKey:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        class FakeWinreg:
            HKEY_LOCAL_MACHINE, HKEY_CURRENT_USER = 1, 2

            @staticmethod
            def OpenKey(hive, path):
                if path.endswith(r"\Main"):
                    return FakeKey()
                if path == r"SOFTWARE\Mozilla\Mozilla Firefox":
                    return FakeKey()
                raise FileNotFoundError(path)

            @staticmethod
            def QueryValueEx(key, name):
                if name == "CurrentVersion":
                    return ("128.0", 1)
                return (str(exe), 1)

        monkeypatch.setattr(bs.sys, "platform", "win32")
        monkeypatch.setitem(__import__("sys").modules, "winreg", FakeWinreg)
        assert bs.find_firefox() == str(exe)

    def test_a_registry_path_that_is_gone_falls_through(self, monkeypatch, tmp_path):
        """A stale key is common after an uninstall; it must not be returned."""
        exe = tmp_path / "Mozilla Firefox" / "firefox.exe"
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"MZ")

        class FakeKey:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        class FakeWinreg:
            HKEY_LOCAL_MACHINE, HKEY_CURRENT_USER = 1, 2

            @staticmethod
            def OpenKey(hive, path):
                return FakeKey()

            @staticmethod
            def QueryValueEx(key, name):
                if name == "CurrentVersion":
                    return ("128.0", 1)
                return (str(tmp_path / "uninstalled" / "firefox.exe"), 1)

        monkeypatch.setattr(bs.sys, "platform", "win32")
        monkeypatch.setitem(__import__("sys").modules, "winreg", FakeWinreg)
        monkeypatch.setenv("ProgramFiles", str(tmp_path))
        monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path / "none"))
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "none"))
        assert bs.find_firefox() == str(exe)

    def test_nothing_installed_is_none(self, monkeypatch, tmp_path):
        monkeypatch.setattr(bs.sys, "platform", "win32")
        monkeypatch.setenv("ProgramFiles", str(tmp_path / "a"))
        monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path / "b"))
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "c"))
        monkeypatch.setitem(__import__("sys").modules, "winreg", None)
        assert bs.find_firefox() is None

    def test_not_on_windows_is_none(self, monkeypatch):
        """This module is the Windows one, but the guard keeps it honest."""
        monkeypatch.setattr(bs.sys, "platform", "linux")
        assert bs.find_firefox() is None


class TestTheStatusDot:
    """Drawn, so its position and colour are chosen rather than inherited.

    As a bullet glyph its box was the font's line box, which left the ink low
    against the word beside it and the gap between them set by the glyph's
    advance width rather than by a padding. No display is needed to check that
    the colour goes to a canvas item instead of a label's foreground.
    """

    class FakeCanvas:
        def __init__(self):
            self.items = {}

        def itemconfig(self, item, **kwargs):
            self.items[item] = kwargs

    def test_colouring_it_changes_the_item(self):
        import ui_window
        holder = type("H", (), {})()
        holder._status_indicator = self.FakeCanvas()
        holder._status_dot = 1
        ui_window.ClaudeBarWindow._status_colour(holder, "#22c55e")
        assert holder._status_indicator.items[1]["fill"] == "#22c55e"

    def test_colouring_it_before_it_exists_is_harmless(self):
        import ui_window
        holder = type("H", (), {})()
        holder._status_indicator = None
        holder._status_dot = None
        ui_window.ClaudeBarWindow._status_colour(holder, "#22c55e")  # must not raise


class TestTheDeepSeekBalanceLine:
    """One figure, not three.

    It read "Topped up 22.00 · Granted 0.00 · Spent 21.34": three numbers where
    one is being read, and on most accounts two of them are zero or are already
    on the screen elsewhere.
    """

    @staticmethod
    def _panel():
        import tkinter as tk
        import pytest
        try:
            probe = tk.Tk()
            probe.withdraw()
            probe.destroy()
        except tk.TclError:
            pytest.skip("no display for a Tk window")

        from config import Config
        import ui_window
        # _create_window kicks off a background thread that shells out to the
        # Claude CLI and polls the network. Left running it outlives the test and
        # lands in the next one - including during collection, which it can abort.
        # Neither is anything to do with what this test is checking.
        ui_window.ClaudeBarWindow._load_initial_data = lambda self: None
        panel = ui_window.ClaudeBarWindow(
            on_refresh=lambda: None, on_settings=lambda: None, on_exit=lambda: None,
            config=Config())
        panel._create_window()
        return panel

    def test_it_shows_only_what_was_topped_up(self):
        from models import DeepSeekSnapshot, Engine
        panel = self._panel()
        try:
            panel._on_engine_select(Engine.DEEPSEEK)
            panel.update_deepseek(DeepSeekSnapshot(
                balance_available=True, balance_usable=True, balance_total=25.37,
                balance_topped_up=25.37, balance_granted=0.0,
                total_cost_available=True, total_cost_usd=24.63))
            panel._apply_snapshot_update()
            note = panel._balance_note.cget("text")
            assert "Granted" not in note
            assert "Spent" not in note
            assert note.startswith("Topped up")
        finally:
            panel.destroy()

    def test_the_status_row_comes_from_the_same_snapshot(self):
        """It used to lag one behind: "Not signed in" above a live balance."""
        from models import DeepSeekSnapshot, Engine
        panel = self._panel()
        try:
            panel._on_engine_select(Engine.DEEPSEEK)
            panel.update_deepseek(DeepSeekSnapshot(
                balance_available=True, balance_usable=True, balance_total=25.37,
                usage_available=True, today_cost_usd=1.0, month_cost_usd=2.0))
            panel._apply_snapshot_update()
            assert panel._status_text.cget("text") == "Connected"
        finally:
            panel.destroy()
