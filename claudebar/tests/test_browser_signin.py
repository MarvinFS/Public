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
