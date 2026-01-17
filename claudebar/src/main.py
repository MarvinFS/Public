"""ClaudeBar - Windows System Tray App for Claude Usage Tracking."""

import sys
import threading
import time
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

# Add src directory to path for imports
src_dir = Path(__file__).parent
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))

from config import ensure_config_exists, Config
from data_collector import DataCollector
from tray import TrayManager
from models import Engine


class ClaudeBar:
    """Main application class."""

    def __init__(self):
        self.config: Config = ensure_config_exists()
        self.collector = DataCollector()
        self.tray: Optional[TrayManager] = None
        self._running = False
        self._refresh_thread: Optional[threading.Thread] = None

    def _refresh(self) -> None:
        """Refresh usage data and update tray."""
        try:
            combined = self.collector.collect_combined()
            if self.tray:
                self.tray.update_combined(combined)
        except Exception as e:
            print(f"Refresh error: {e}", file=sys.stderr)

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

        # Initial data collection
        self._refresh()

        # Start background refresh thread
        self._refresh_thread = threading.Thread(target=self._refresh_loop, daemon=True)
        self._refresh_thread.start()

        # Run tray in background thread (so tkinter can use main thread)
        tray_thread = self.tray.start_detached()

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


def main():
    """Entry point."""
    app = ClaudeBar()
    app.run()


if __name__ == "__main__":
    main()
