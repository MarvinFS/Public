"""`claude auth status`: its output is read as UTF-8, whatever the system code page is."""

import subprocess
import sys

import claude_check

STATUS = b'{"loggedIn": true, "email": "a\x98b@example.com", "orgName": "Org", "subscriptionType": "max"}'


def fake_cli(tmp_path):
    """An executable that prints `STATUS`, a byte that is no valid UTF-8 among it (0x98 is
    unassigned in the Russian code page, so the old text=True read raised there)."""
    script = tmp_path / "status.py"
    script.write_text(f"import sys\nsys.stdout.buffer.write({STATUS!r})\n", encoding="utf-8")
    if sys.platform == "win32":
        shim = tmp_path / "claude.cmd"
        shim.write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="ascii")
    else:
        shim = tmp_path / "claude"
        shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="ascii")
        shim.chmod(0o755)
    return str(shim)


def test_a_byte_that_is_not_utf8_does_not_break_the_status_check(tmp_path):
    status = claude_check.check_claude_status_via_cli(fake_cli(tmp_path))
    assert status is not None and status.authenticated
    assert status.plan == "max"
    assert status.email == "a�b@example.com"


def test_the_output_is_decoded_as_utf8_with_replacement(monkeypatch):
    seen = {}

    def run(cmd, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, stdout='{"loggedIn": false}', stderr="")

    monkeypatch.setattr(claude_check.subprocess, "run", run)
    claude_check.check_claude_status_via_cli("claude")
    assert seen["encoding"] == "utf-8" and seen["errors"] == "replace"
    assert not seen.get("text")


class TestJsonThatIsNotAnObject:
    """`data.get` on a list or a number raised AttributeError, which nothing caught."""

    @staticmethod
    def run_with(monkeypatch, stdout):
        monkeypatch.setattr(claude_check.subprocess, "run",
                            lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr=""))
        return claude_check.check_claude_status_via_cli("claude")

    def test_an_auth_status_that_is_a_list_is_no_status(self, monkeypatch):
        assert self.run_with(monkeypatch, "[1, 2]") is None

    def test_an_auth_status_that_is_a_number_or_null_is_no_status(self, monkeypatch):
        assert self.run_with(monkeypatch, "42") is None
        assert self.run_with(monkeypatch, "null") is None

    def test_credentials_that_are_not_an_object_are_not_valid(self, monkeypatch, tmp_path):
        monkeypatch.setattr(claude_check, "get_claude_dir", lambda: tmp_path)
        for body in ("[]", "7", "null", '"text"'):
            (tmp_path / ".credentials.json").write_text(body, encoding="utf-8")
            ok, creds, error = claude_check.check_credentials()
            assert ok is False and creds is None and "not a JSON object" in error

    def test_credentials_that_are_not_utf8_are_not_valid(self, monkeypatch, tmp_path):
        monkeypatch.setattr(claude_check, "get_claude_dir", lambda: tmp_path)
        (tmp_path / ".credentials.json").write_bytes(b'{"apiKey": "a\x98b"}')
        ok, creds, error = claude_check.check_credentials()
        assert ok is False and creds is None and error.startswith("Error reading credentials")
