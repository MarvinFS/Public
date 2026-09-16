"""Log handler behavior: rotation, size ceiling, retention, encoding, redaction.

The log is the only diagnostic trail a windowed build leaves, so these cover the
properties that make it usable: it rotates daily, it cannot grow without bound
inside one day, old files go away, non-ASCII survives, and tokens do not.
"""

import logging
import os
import time

import pytest

from logging_config import (
    LOG_FILENAME,
    RedactingFormatter,
    DailyRotatingHandler,
    setup_logging,
)


def _record(message: str) -> logging.LogRecord:
    return logging.LogRecord("claudebar", logging.INFO, __file__, 1, message, None, None)


def _handler(tmp_path, **kwargs) -> DailyRotatingHandler:
    handler = DailyRotatingHandler(tmp_path / LOG_FILENAME, **kwargs)
    handler.setFormatter(RedactingFormatter('%(message)s'))
    return handler


def _archives(tmp_path):
    return sorted(p.name for p in tmp_path.glob(f"{LOG_FILENAME}.*"))


def test_writes_the_active_file(tmp_path):
    handler = _handler(tmp_path)
    handler.emit(_record("first line"))
    handler.close()

    assert (tmp_path / LOG_FILENAME).read_text(encoding="utf-8").strip() == "first line"
    assert _archives(tmp_path) == []


def test_size_ceiling_rotates_and_keeps_every_line(tmp_path):
    handler = _handler(tmp_path, maxBytes=200)
    for i in range(12):
        handler.emit(_record(f"line {i:02d} " + "x" * 40))
    handler.close()

    assert _archives(tmp_path), "a day that exceeds the ceiling must roll over"

    kept = (tmp_path / LOG_FILENAME).read_text(encoding="utf-8")
    kept += "".join((tmp_path / name).read_text(encoding="utf-8") for name in _archives(tmp_path))
    for i in range(12):
        assert f"line {i:02d}" in kept, f"line {i:02d} was lost during rollover"


def test_repeated_rollover_in_one_day_does_not_overwrite(tmp_path):
    """The archive names collide within a day, so each gets its own counter."""
    handler = _handler(tmp_path, maxBytes=200)
    for i in range(40):
        handler.emit(_record(f"line {i:02d} " + "x" * 40))
    handler.close()

    assert len(_archives(tmp_path)) > 1, "expected more than one archive for one day"

    kept = "".join((tmp_path / name).read_text(encoding="utf-8") for name in _archives(tmp_path))
    assert "line 00" in kept, "the oldest archive was overwritten"


def test_prunes_archives_past_the_retention_window(tmp_path):
    stale = tmp_path / f"{LOG_FILENAME}.2020-01-01"
    stale.write_text("ancient\n", encoding="utf-8")
    old = time.time() - 30 * 86_400
    os.utime(stale, (old, old))

    recent = tmp_path / f"{LOG_FILENAME}.2020-01-02"
    recent.write_text("yesterday\n", encoding="utf-8")

    handler = _handler(tmp_path, maxBytes=100, retention_days=14)
    for i in range(6):
        handler.emit(_record(f"line {i} " + "x" * 40))
    handler.close()

    assert not stale.exists(), "archives past the retention window must be deleted"
    assert recent.exists(), "an archive inside the window must survive"
    assert (tmp_path / LOG_FILENAME).exists(), "the active file is not an archive"


def test_non_ascii_survives(tmp_path):
    """The file is UTF-8, not the machine's locale encoding (cp1251 here)."""
    handler = _handler(tmp_path)
    handler.emit(_record("путь D:\\данные\\claude — ok"))
    handler.close()

    text = (tmp_path / LOG_FILENAME).read_text(encoding="utf-8")
    assert "путь D:\\данные\\claude" in text


def test_redacts_tokens():
    formatter = RedactingFormatter('%(message)s')
    for message, secret in [
        ("request with Bearer sk-ant-oat01-abc.def-ghi", "sk-ant-oat01-abc.def-ghi"),
        ('{"accessToken": "abc123"}', "abc123"),
        ('{"refresh_token": "def456"}', "def456"),
    ]:
        out = formatter.format(_record(message))
        assert secret not in out
        assert "[REDACTED]" in out


def test_setup_logging_hides_debug_without_the_flag(tmp_path):
    logger = setup_logging(debug=False, log_dir=tmp_path)
    try:
        logger.info("visible line")
        logger.debug("hidden line")
        for handler in logger.handlers:
            handler.flush()
        text = (tmp_path / LOG_FILENAME).read_text(encoding="utf-8")
        assert "visible line" in text
        assert "hidden line" not in text
    finally:
        for handler in logger.handlers:
            handler.close()
        logger.handlers.clear()


def test_setup_logging_keeps_debug_with_the_flag(tmp_path):
    logger = setup_logging(debug=True, log_dir=tmp_path)
    try:
        logger.debug("hidden no more")
        for handler in logger.handlers:
            handler.flush()
        assert "hidden no more" in (tmp_path / LOG_FILENAME).read_text(encoding="utf-8")
    finally:
        for handler in logger.handlers:
            handler.close()
        logger.handlers.clear()


@pytest.mark.parametrize("bad_bytes", [None, 0])
def test_no_ceiling_means_no_size_rollover(tmp_path, bad_bytes):
    handler = _handler(tmp_path, maxBytes=bad_bytes)
    for i in range(5):
        handler.emit(_record(f"line {i} " + "x" * 200))
    handler.close()

    assert _archives(tmp_path) == []
