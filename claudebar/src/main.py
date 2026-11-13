"""ClaudeBar - Windows System Tray App for Claude Usage Tracking."""

import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

# Enable DPI awareness on Windows before any GUI imports
# This must be called before creating any tkinter windows
if sys.platform == "win32":
    try:
        import ctypes
        # SetProcessDpiAwareness(2) = Per-Monitor DPI Aware
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            # Fallback for older Windows versions
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

    # Suppress Windows hard-error dialogs (e.g. a child's DLL-init 0xc0000142) so a
    # failed 'claude auth status' spawn can't pop a modal box. Own try (not chained
    # after SetProcessDpiAwareness, which can throw); OR into the current mode to keep
    # existing flags. Children inherit this (subprocess doesn't set CREATE_DEFAULT_ERROR_MODE).
    try:
        import ctypes
        k = ctypes.windll.kernel32
        # SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX | SEM_NOOPENFILEERRORBOX
        k.SetErrorMode(k.GetErrorMode() | 0x0001 | 0x0002 | 0x8000)
    except Exception:
        pass

# Add src directory to path for imports
src_dir = Path(__file__).parent
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))

import browser_signin
from config import ensure_config_exists, Config, MAX_REFRESH_INTERVAL
from data_collector import DataCollector
from tray import TrayManager
from models import Engine
from logging_config import setup_logging, get_logger
from version import __version__


def describe_engines(config: Config) -> str:
    """Names of the enabled engines, for the start line."""
    enabled = [
        name for name, on in (
            ("claude", config.claude_enabled),
            ("codex", config.codex_enabled),
            ("deepseek", config.deepseek_enabled),
        ) if on
    ]
    return ", ".join(enabled) if enabled else "none"


_instance_mutex = None  # held for the life of the process


def another_instance_running() -> bool:
    """Take the app's named mutex; True when another ClaudeBar already holds it.

    Two instances would each write the same config, cache and log files and
    put two icons in the tray.
    """
    global _instance_mutex
    if sys.platform != "win32":
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    _instance_mutex = kernel32.CreateMutexW(None, False, "Local\\ClaudeBar")
    return bool(_instance_mutex) and ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS


def parse_args(argv: list) -> bool:
    """Read the command line. Returns whether debug logging was requested.

    A windowed build has nowhere to print, so --debug is about the log file:
    it keeps the routine per-refresh lines that INFO drops.
    """
    return "--debug" in argv[1:]


def install_crash_logging() -> None:
    """Send an exception nothing caught, from any thread or Tk callback, to the log.

    The exe is windowed, so it has no stderr for Python's own report to land on: an
    exception that escaped a thread or a Tk callback left no trace at all, only a
    panel that stopped updating or an icon that was gone. The log's formatter
    redacts tokens, and that covers the traceback text too.
    """
    import tkinter

    logger = get_logger()

    def report(what: str, exc_type, exc, tb) -> None:
        logger.error(what, exc_info=(exc_type, exc, tb))

    def on_main_thread(exc_type, exc, tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)   # a Ctrl+C in a console is not a crash
            return
        report("Uncaught exception", exc_type, exc, tb)

    def on_other_thread(args) -> None:
        if args.exc_type is SystemExit:             # a thread ending itself, as the default hook treats it
            return
        name = args.thread.name if args.thread else "unknown"
        report(f"Uncaught exception in thread {name}", args.exc_type, args.exc_value, args.exc_traceback)

    def on_tk_callback(self, exc_type, exc, tb) -> None:
        report("Uncaught exception in a Tk callback", exc_type, exc, tb)

    sys.excepthook = on_main_thread
    threading.excepthook = on_other_thread
    tkinter.Tk.report_callback_exception = on_tk_callback


class ClaudeBar:
    """Main application class."""

    def __init__(self):
        self.config: Config = ensure_config_exists()
        # The collector reads the engine switches from this one Config at every refresh,
        # and the Settings dialog changes it in place: no restart to switch an engine back on.
        self.collector = DataCollector(config=self.config)
        self.tray: Optional[TrayManager] = None
        self._running = False
        self._refresh_thread: Optional[threading.Thread] = None
        self._refresh_lock = threading.Lock()
        self._refresh_again = False
        # Set to end the wait between two refreshes early: by Exit, and by a Settings save
        # (the wait then starts again with the interval as saved).
        self._wake = threading.Event()
        # Which engines were on at the last save (or at start), to tell a save that switches
        # one on from every other.
        self._engines_on = self._engine_switches()

    def _load_cached_initial(self) -> None:
        """Load cached data for immediate display on startup."""
        try:
            from snapshot_cache import load_cache, load_deepseek_cache
            from models import CombinedSnapshot, DeepSeekSnapshot, OpenAISnapshot, UsageSnapshot
            cached_claude, cached_openai, cached_at = load_cache()
            cached_deepseek, _ = load_deepseek_cache()
            if cached_claude or cached_openai or cached_deepseek:
                # An engine switched off in Settings is left out, as a refresh leaves it out.
                combined = CombinedSnapshot(
                    claude=(cached_claude or UsageSnapshot(timestamp=datetime.now()))
                    if self.config.claude_enabled else None,
                    openai=(cached_openai or OpenAISnapshot(timestamp=datetime.now()))
                    if self.config.codex_enabled else None,
                    deepseek=(cached_deepseek or DeepSeekSnapshot(timestamp=datetime.now()))
                    if self.config.deepseek_enabled else None,
                    active_engine=self.collector.active_engine,
                    timestamp=datetime.now(),
                )
                if self.tray:
                    self.tray.update_combined(combined)
                get_logger().info("Loaded cached data for initial display")
        except Exception as e:
            get_logger().debug("No cached data available: %s", e)

    def _refresh(self) -> None:
        """Refresh usage data and update tray."""
        # Single-flight: refresh loop, manual button and sign-in all
        # call this on separate threads. A call that finds a collect running leaves
        # the flag set and returns; the holder then collects once more, so a
        # request made mid-collect is served by a collect that starts after it.
        # The outer check runs again after release, for a request that set the
        # flag between the holder's last check and its release
        # (verification/PanelThreads.tla, seed-norecheck).
        self._refresh_again = True
        while self._refresh_again and self._refresh_lock.acquire(blocking=False):
            try:
                while self._refresh_again:
                    self._refresh_again = False
                    try:
                        combined = self.collector.collect_combined()
                        if self.tray:
                            self.tray.update_combined(combined)
                    except Exception:
                        get_logger().exception("Refresh error")
            finally:
                self._refresh_lock.release()

    def _on_engine_change(self, engine: Engine) -> None:
        """Handle engine change from UI."""
        # Only the record. The panel holds every engine's snapshot and redraws from them, and
        # the tray follows no engine; a tab click used to start a whole refresh (the log
        # parse and every network call) for figures that were already on screen.
        self.collector.set_active_engine(engine)

    def _wait_seconds(self) -> int:
        """How long to wait for the next refresh: the interval as it is now. The config keeps it
        within limits where it is read and saved, but Event.wait raises OverflowError for what it
        cannot wait for (above threading.TIMEOUT_MAX), and that ended this thread for good: the
        loop does not rely on the value it is given."""
        try:
            return min(int(self.config.refresh_interval), MAX_REFRESH_INTERVAL)
        except (TypeError, ValueError, OverflowError):
            return Config().refresh_interval

    def _refresh_loop(self) -> None:
        """Background thread for periodic refresh."""
        while self._running:
            self._refresh()
            # The interval is read as each wait starts, so a saved one applies at once, and
            # the wait ends the moment Exit or a Settings save sets the event. The event is
            # cleared before _running is looked at: an Exit that lands between the two is seen
            # by the look, and one after them finds the event it sets still set.
            self._wake.clear()
            while self._running and self._wake.wait(self._wait_seconds()):
                self._wake.clear()      # a Settings save: the wait starts again, with the new interval

    def _on_refresh(self) -> None:
        """Handle manual refresh request."""
        threading.Thread(target=self._refresh, daemon=True).start()

    def _engine_switches(self) -> tuple:
        return (self.config.claude_enabled, self.config.codex_enabled, self.config.deepseek_enabled)

    def _on_settings_saved(self) -> None:
        """Handle a Settings save: the wait for the next refresh starts again with the
        interval as saved. A save refreshes nothing by itself, except one that switches an
        engine on: the panel would show nothing for it until the next scheduled refresh, up
        to a whole interval away, so that one refresh is started now (the single-flight
        `_refresh`, as the Refresh button does)."""
        switches = self._engine_switches()
        switched_on = any(now and not before for before, now in zip(self._engines_on, switches))
        self._engines_on = switches
        self._wake.set()
        if switched_on:
            self._on_refresh()

    def _on_exit(self) -> None:
        """Handle exit request."""
        self._running = False
        self._wake.set()

    def run(self) -> None:
        """Run the application."""
        self._running = True

        # Create tray manager
        self.tray = TrayManager(
            on_refresh=self._on_refresh,
            on_exit=self._on_exit,
            on_engine_change=self._on_engine_change,
            on_settings_saved=self._on_settings_saved,
            config=self.config,
        )

        # Wire up OAuth callbacks from collector to tray/window
        self.collector.set_oauth_callbacks(
            on_failure=self.tray.on_oauth_failure,
            on_success=self.tray.on_oauth_success,
        )

        # Load cached data immediately so UI shows something right away
        self._load_cached_initial()

        # Start background refresh thread (includes immediate first refresh)
        self._refresh_thread = threading.Thread(target=self._refresh_loop, daemon=True)
        self._refresh_thread.start()

        # Run tray in background thread (so tkinter can use main thread)
        self.tray.start_detached()

        # Run tkinter mainloop in main thread (required for Windows)
        try:
            import tkinter as tk
            # Create a hidden root window to run mainloop
            root = tk.Tk()
            root.withdraw()

            def check_running():
                # Tray menu and icon clicks arrive on pystray's thread and are
                # run here, because Tk belongs to this thread.
                self.tray.run_pending()
                if self._running:
                    root.after(100, check_running)
                else:
                    root.quit()

            # The first tick runs inside the loop, never before it: a tray that failed at once
            # has its exit queued for that tick, and Tk discards a quit() made before
            # mainloop() is entered, which left a process with no icon and no timer.
            root.after(0, check_running)
            root.mainloop()
        except KeyboardInterrupt:
            pass
        finally:
            self._running = False
            if self.tray:
                self.tray.stop()
            # A DeepSeek sign-in's worker is a daemon thread: it would end with the process
            # before its cleanup, leaving the browser open and its plaintext profile on disk.
            browser_signin.abandon_signin()
            get_logger().info("ClaudeBar exiting")


def main():
    """Entry point."""
    logger = setup_logging(debug=parse_args(sys.argv))
    install_crash_logging()
    if another_instance_running():
        logger.info("ClaudeBar is already running; this instance exits")
        return
    try:
        app = ClaudeBar()
        logger.info("ClaudeBar %s starting (refresh %ds, engines: %s)",
                    __version__, app.config.refresh_interval,
                    describe_engines(app.config))
        app.run()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        import traceback
        error_msg = f"ClaudeBar crashed: {e}\n{traceback.format_exc()}"
        # The log is the only place this survives: a windowed build has no
        # stderr, so the print below writes nowhere.
        logger.error(error_msg)
        print(error_msg, file=sys.stderr)
        if sys.platform == "win32":
            try:
                ctypes.windll.user32.MessageBoxW(0, str(e), "ClaudeBar Error", 0x10)
            except Exception:
                pass


if __name__ == "__main__":
    main()
