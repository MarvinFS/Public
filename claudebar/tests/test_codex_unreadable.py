"""A Codex session file that cannot be read is named in the log once, not every refresh."""

import logging
from datetime import datetime

import codex_log_parser as parser


def sessions_with_a_broken_file(tmp_path):
    day = tmp_path / "2026" / "11" / "12"
    day.mkdir(parents=True)
    (day / "rollout-ok.jsonl").write_text("", encoding="utf-8")
    broken = day / "rollout-broken.jsonl"
    broken.mkdir()                      # opening a directory as a file raises OSError on every platform
    return tmp_path, broken


def refresh(sessions):
    return parser.get_codex_daily_usage(sessions, days=3, end=datetime(2026, 11, 12, 12, 0))


def test_a_file_that_cannot_be_read_is_logged_once_per_process(tmp_path, caplog):
    sessions, broken = sessions_with_a_broken_file(tmp_path)
    with caplog.at_level(logging.WARNING, logger="claudebar"):
        first = refresh(sessions)
        for _ in range(3):                                   # the next three refreshes
            refresh(sessions)
    assert len(first) == 3                                   # the rest of the series is still built
    lines = [r for r in caplog.records if "rollout-broken.jsonl" in r.getMessage()]
    assert len(lines) == 1 and lines[0].levelno == logging.WARNING


def test_the_line_names_the_file_and_nothing_of_its_path_or_contents(tmp_path, caplog):
    sessions, broken = sessions_with_a_broken_file(tmp_path)
    with caplog.at_level(logging.WARNING, logger="claudebar"):
        refresh(sessions)
    (record,) = [r for r in caplog.records if "rollout-broken.jsonl" in r.getMessage()]
    assert str(tmp_path) not in record.getMessage()


def test_each_broken_file_is_named_once(tmp_path, caplog):
    sessions, broken = sessions_with_a_broken_file(tmp_path)
    second = broken.parent / "rollout-broken-too.jsonl"
    second.mkdir()
    with caplog.at_level(logging.WARNING, logger="claudebar"):
        refresh(sessions)
        refresh(sessions)
    names = sorted(r.getMessage().split()[3] for r in caplog.records if "could not be read" in r.getMessage())
    assert names == ["rollout-broken-too.jsonl", "rollout-broken.jsonl"]


def test_a_file_outside_the_date_layout_is_named_too(tmp_path, caplog):
    odd = tmp_path / "x" / "y" / "z"
    odd.mkdir(parents=True)
    (odd / "rollout-odd.jsonl").write_text("", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="claudebar"):
        refresh(tmp_path)
    assert [r for r in caplog.records if "rollout-odd.jsonl" in r.getMessage()]
