"""Command line handling and the start line's engine list."""

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
