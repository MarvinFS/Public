"""A windowed exe has no stderr, so an exception nothing catches must reach the log file.

Without these hooks a thread that died, a Tk callback that raised or a tray loop that
failed left no trace: the panel stopped updating, or the icon was gone and the process
could not be reached.
"""

import importlib.util
import logging
import sys
import threading
import time
from pathlib import Path

import pytest

import main
import tray
import ui_window
from config import Config
from logging_config import LOG_FILENAME, setup_logging
from models import CombinedSnapshot
from tests.test_refresh_threads import BlockingCollector, make_app


@pytest.fixture
def log_file(tmp_path, monkeypatch):
    """The real file handler and formatter, in a temp folder, with the three hooks restored
    afterwards so nothing leaks into the next test."""
    import tkinter
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    monkeypatch.setattr(tkinter.Tk, "report_callback_exception", tkinter.Tk.report_callback_exception)
    logger = setup_logging(log_dir=tmp_path)
    path = tmp_path / LOG_FILENAME

    def text() -> str:
        for handler in logger.handlers:
            handler.flush()
        return path.read_text(encoding="utf-8") if path.exists() else ""

    yield text
    for handler in logger.handlers:
        handler.close()
    logger.handlers.clear()


def raised(exc):
    try:
        raise exc
    except BaseException as e:
        return type(e), e, e.__traceback__


class TestTheThreeHooks:
    def test_an_uncaught_exception_in_the_main_thread_is_logged_with_its_traceback(self, log_file):
        main.install_crash_logging()
        sys.excepthook(*raised(ValueError("the main thread broke")))
        text = log_file()
        assert "Uncaught exception" in text and "ValueError: the main thread broke" in text
        assert "Traceback (most recent call last)" in text

    def test_ctrl_c_is_not_logged_as_a_crash(self, log_file, monkeypatch):
        passed = []
        monkeypatch.setattr(sys, "__excepthook__", lambda *args: passed.append(args[0]))
        main.install_crash_logging()
        sys.excepthook(*raised(KeyboardInterrupt()))
        assert passed == [KeyboardInterrupt]
        assert "Uncaught" not in log_file()

    def test_an_uncaught_exception_in_a_thread_is_logged_with_the_thread_name(self, log_file):
        main.install_crash_logging()

        def work():
            raise RuntimeError("the worker broke")

        worker = threading.Thread(target=work, name="refresh-worker")
        worker.start()
        worker.join(5)
        text = log_file()
        assert "Uncaught exception in thread refresh-worker" in text
        assert "RuntimeError: the worker broke" in text and "Traceback" in text

    def test_a_thread_that_exits_itself_is_not_a_crash(self, log_file):
        main.install_crash_logging()
        worker = threading.Thread(target=lambda: sys.exit(0))
        worker.start()
        worker.join(5)
        assert "Uncaught" not in log_file()

    def test_an_exception_in_a_tk_callback_is_logged(self, log_file, tk_root):
        main.install_crash_logging()
        tk_root.after(0, lambda: 1 / 0)
        tk_root.update()
        text = log_file()
        assert "Uncaught exception in a Tk callback" in text
        assert "ZeroDivisionError" in text and "Traceback" in text

    def test_the_traceback_is_redacted_like_any_other_line(self, log_file):
        """The formatter runs over the whole record, traceback included: an exception that
        carries a token in its message must not put it in the file."""
        main.install_crash_logging()
        sys.excepthook(*raised(RuntimeError("refused with Bearer abc+def/ghi== for userToken=s3cr3t")))
        text = log_file()
        assert "RuntimeError: refused with Bearer [REDACTED]" in text
        assert "abc+def" not in text and "ghi==" not in text and "s3cr3t" not in text


def test_a_failed_refresh_is_logged_with_its_traceback(caplog):
    class Failing(BlockingCollector):
        def collect_combined(self):
            raise RuntimeError("collect broke")

    app = make_app(Failing())
    with caplog.at_level(logging.ERROR, logger="claudebar"):
        app._refresh()
    (record,) = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert record.exc_info and "collect broke" in str(record.exc_info[1])


class FailingIcon:
    """pystray.Icon whose loop raises, as a broken shell integration can make it."""

    def __init__(self, *args, **kwargs):
        pass

    def run(self):
        raise OSError("the shell refused the icon")

    def stop(self):
        pass


class TestTheTrayLoopFailing:
    def test_it_is_logged_and_the_app_leaves_by_the_exit_path_on_the_main_thread(self, monkeypatch, caplog):
        monkeypatch.setattr(tray.pystray, "Icon", FailingIcon)
        exits = []
        manager = tray.TrayManager(on_refresh=lambda: None, on_exit=lambda: exits.append(1),
                                   use_premium_ui=False)
        with caplog.at_level(logging.ERROR, logger="claudebar"):
            manager.start()                               # pystray's thread, as start_detached runs it

        (record,) = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert record.exc_info and isinstance(record.exc_info[1], OSError)
        assert exits == [], "stop() touches Tk and must not run on pystray's thread"
        manager.run_pending()                             # the main thread's next tick
        assert exits == [1]
        assert not manager.is_running

    def test_a_failure_to_build_the_icon_is_the_same(self, monkeypatch, caplog):
        def refuses(*args, **kwargs):
            raise RuntimeError("no tray")

        monkeypatch.setattr(tray.pystray, "Icon", refuses)
        exits = []
        manager = tray.TrayManager(on_refresh=lambda: None, on_exit=lambda: exits.append(1),
                                   use_premium_ui=False)
        with caplog.at_level(logging.ERROR, logger="claudebar"):
            manager.start()
        assert [r for r in caplog.records if r.levelno == logging.ERROR]
        manager.run_pending()
        assert exits == [1]


def test_a_missing_ui_module_is_logged_when_the_tray_is_made(monkeypatch, caplog):
    """tray.py is imported before the log exists, so it keeps the ImportError and says it
    when the manager is built, instead of an icon with no panel and nothing written."""
    monkeypatch.setitem(sys.modules, "ui_window", None)    # `from ui_window import ...` now fails
    spec = importlib.util.spec_from_file_location(
        "tray_without_panel", Path(tray.__file__))
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    assert not fresh.HAS_UI_WINDOW

    with caplog.at_level(logging.ERROR, logger="claudebar"):
        fresh.TrayManager(on_refresh=lambda: None, on_exit=lambda: None)

    (record,) = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert "details panel is unavailable" in record.getMessage()
    assert record.exc_info and isinstance(record.exc_info[1], ImportError)


class TestTheInitialLoadFailing:
    @staticmethod
    def window():
        return ui_window.ClaudeBarWindow(on_refresh=lambda: None, on_settings=lambda: None,
                                         on_exit=lambda: None, config=Config())

    def wait_for_flag(self, window):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with window._lock:
                if window._status_needs_update:
                    return True
            time.sleep(0.01)
        return False

    def test_the_status_row_is_still_told_to_update(self, monkeypatch, caplog):
        def broken():
            raise RuntimeError("rates endpoint broke")

        monkeypatch.setattr(ui_window, "get_exchange_rates", broken)
        monkeypatch.setattr(ui_window, "check_claude_status", lambda: None)
        window = self.window()
        with caplog.at_level(logging.ERROR, logger="claudebar"):
            window._load_initial_data()
            assert self.wait_for_flag(window), "an exception in the loader froze the status row"
        (record,) = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert record.exc_info and "rates endpoint broke" in str(record.exc_info[1])

    def test_a_failing_status_check_is_the_same(self, monkeypatch, caplog):
        def broken():
            raise UnicodeDecodeError("utf-8", b"\x98", 0, 1, "invalid start byte")

        monkeypatch.setattr(ui_window, "get_exchange_rates", lambda: None)
        monkeypatch.setattr(ui_window, "check_claude_status", broken)
        window = self.window()
        with caplog.at_level(logging.ERROR, logger="claudebar"):
            window._load_initial_data()
            assert self.wait_for_flag(window)
        assert [r for r in caplog.records if r.levelno == logging.ERROR]
