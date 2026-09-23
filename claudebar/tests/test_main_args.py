"""Command line handling and the start line's engine list."""

import subprocess
import sys
from pathlib import Path

import pytest

from config import Config
from main import describe_engines, parse_args


def test_debug_flag_is_read_after_the_program_name():
    assert parse_args(["ClaudeBar.exe"]) is False
    assert parse_args(["ClaudeBar.exe", "--debug"]) is True
    assert parse_args(["ClaudeBar.exe", "--something", "--debug"]) is True


def test_engines_list_follows_the_config():
    assert describe_engines(Config()) == "claude, codex, deepseek"
    assert describe_engines(Config(codex_enabled=False)) == "claude, deepseek"
    assert describe_engines(
        Config(claude_enabled=False, codex_enabled=False, deepseek_enabled=False)
    ) == "none"


@pytest.mark.skipif(sys.platform != "win32", reason="named mutex is Windows only")
def test_a_second_instance_sees_the_first():
    """Both in child processes, so the test does not hold the app's mutex itself."""
    src = str(Path(__file__).resolve().parent.parent / "src")
    check = f"import sys; sys.path.insert(0, {src!r}); import main; print(main.another_instance_running(), flush=True)"
    first = subprocess.Popen([sys.executable, "-c", check + "; sys.stdin.read()"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        first.stdout.readline()               # the first one holds the mutex now
        second = subprocess.run([sys.executable, "-c", check], capture_output=True,
                                text=True, timeout=30)
        assert second.stdout.strip() == "True"
    finally:
        first.communicate("", timeout=30)
