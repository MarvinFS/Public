"""ClaudeBar - Windows System Tray App for Claude Usage Tracking."""

import sys
import threading
import time
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

from config import ensure_config_exists, Config
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


def parse_args(argv: list) -> bool:
    """Read the command line. Returns whether debug logging was requested.

    A windowed build has nowhere to print, so --debug is about the log file:
    it keeps the routine per-refresh lines that INFO drops.
    """
    return "--debug" in argv[1:]


class ClaudeBar:
    """Main application class."""

    def __init__(self):
        self.config: Config = ensure_config_exists()
        self.collector = DataCollector()
        self.tray: Optional[TrayManager] = None
        self._running = False
        self._refresh_thread: Optional[threading.Thread] = None
        self._refresh_lock = threading.Lock()

    def _load_cached_initial(self) -> None:
        """Load cached data for immediate display on startup."""
        try:
            from snapshot_cache import load_cache, load_deepseek_cache
            from models import CombinedSnapshot, DeepSeekSnapshot, OpenAISnapshot, UsageSnapshot
            cached_claude, cached_openai, cached_at = load_cache()
            cached_deepseek, _ = load_deepseek_cache()
            if cached_claude or cached_openai or cached_deepseek:
                combined = CombinedSnapshot(
                    claude=cached_claude or UsageSnapshot(timestamp=datetime.now()),
                    openai=cached_openai or OpenAISnapshot(timestamp=datetime.now()),
                    deepseek=cached_deepseek or DeepSeekSnapshot(timestamp=datetime.now()),
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
        # Single-flight: refresh loop, manual button and engine-change all call this
        # on separate threads; skip overlapping calls instead of stacking collects.
        if not self._refresh_lock.acquire(blocking=False):
            return
        try:
            combined = self.collector.collect_combined()
            if self.tray:
                self.tray.update_combined(combined)
        except Exception as e:
            get_logger().error(f"Refresh error: {e}")
        finally:
            self._refresh_lock.release()

    def _on_engine_change(self, engine: Engine) -> None:
        """Handle engine change from UI."""
        self.collector.set_active_engine(engine)
        # Trigger refresh to update display
        threading.Thread(target=self._refresh, daemon=True).start()

    def _refresh_loop(self) -> None:
        """Background thread for periodic refresh."""
        while self._running:
            self._refresh()
            # Sleep in small increments to allow quick shutdown
            for _ in range(self.config.refresh_interval):
                if not self._running:
                    break
                time.sleep(1)

    def _on_refresh(self) -> None:
        """Handle manual refresh request."""
        threading.Thread(target=self._refresh, daemon=True).start()

    def _on_exit(self) -> None:
        """Handle exit request."""
        self._running = False

    def run(self) -> None:
        """Run the application."""
        self._running = True

        # Create tray manager
        self.tray = TrayManager(
            on_refresh=self._on_refresh,
            on_exit=self._on_exit,
            on_engine_change=self._on_engine_change,
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
                if self._running:
                    root.after(100, check_running)
                else:
                    root.quit()

            check_running()
            root.mainloop()
        except KeyboardInterrupt:
            pass
        finally:
            self._running = False
            if self.tray:
                self.tray.stop()
            get_logger().info("ClaudeBar exiting")


def main():
    """Entry point."""
    logger = setup_logging(debug=parse_args(sys.argv))
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
