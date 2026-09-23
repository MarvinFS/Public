"""System tray icon and menu management."""

import logging
import os
import queue
import threading
from typing import Callable, Optional

from PIL import Image, ImageDraw
import pystray

from models import UsageSnapshot, CombinedSnapshot, Engine
from config import Config, get_resources_path
from icons import create_premium_icon, STATUS_COLORS
from logging_config import get_log_path
from pricing import format_tokens

logger = logging.getLogger("claudebar")

try:
    from ui_window import ClaudeBarWindow
    HAS_UI_WINDOW = True
except ImportError:
    HAS_UI_WINDOW = False


# Lock for thread-safe tray updates
_tray_lock = threading.Lock()


# Custom app icon path
_CUSTOM_ICON_PATH = get_resources_path() / "icons" / "app_icon.png"


def create_icon(status: str = "normal", percent: Optional[float] = None) -> Image.Image:
    """Create a tray icon - use custom icon if available, else generate.

    The custom icon gets a corner badge in the status colour once a limit
    crosses the warning threshold, so the thresholds stay visible with it.
    """
    # Use larger size for better quality (Windows scales down as needed)
    icon_size = 128
    if _CUSTOM_ICON_PATH.exists():
        try:
            with Image.open(_CUSTOM_ICON_PATH) as img:
                icon = img.convert("RGBA").resize((icon_size, icon_size), Image.Resampling.LANCZOS)
            if status in ("warning", "critical"):
                r = icon_size // 5
                ImageDraw.Draw(icon).ellipse((icon_size - 2 * r, icon_size - 2 * r, icon_size, icon_size),
                                             fill=STATUS_COLORS[status]["primary"], outline="#111111", width=4)
            return icon
        except Exception:
            pass
    return create_premium_icon(status, percent, icon_size)


class TrayManager:
    """Manages the system tray icon and menu."""

    def __init__(
        self,
        on_refresh: Callable[[], None],
        on_exit: Callable[[], None],
        on_settings: Optional[Callable[[], None]] = None,
        on_engine_change: Optional[Callable[[Engine], None]] = None,
        config: Optional[Config] = None,
        use_premium_ui: bool = True,
    ):
        self.on_refresh = on_refresh
        self.on_exit = on_exit
        self.on_settings = on_settings
        self.on_engine_change = on_engine_change
        self._config = config or Config()
        self._use_premium_ui = use_premium_ui and HAS_UI_WINDOW

        self._icon: Optional[pystray.Icon] = None
        self._snapshot: Optional[UsageSnapshot] = None
        self._combined: Optional[CombinedSnapshot] = None
        self._running = False
        self._ui_window: Optional[ClaudeBarWindow] = None
        # pystray runs its callbacks on its own thread. Tk belongs to the main
        # thread, and a Tk call made from here waits for it - while the main
        # thread may be waiting for a lock this thread holds. So the callbacks
        # that touch the panel are queued, and main.py's check_running tick
        # runs them (verification/PanelThreads.tla, P1).
        self._pending: queue.Queue = queue.Queue()

        # Initialize premium UI if enabled
        if self._use_premium_ui:
            self._ui_window = ClaudeBarWindow(
                on_refresh=self._on_ui_refresh,
                on_settings=self._on_ui_settings,
                on_exit=self._on_ui_exit,
                on_engine_change=self._on_ui_engine_change,
                config=self._config,
            )

    def _create_menu(self) -> pystray.Menu:
        """Create the context menu."""
        items = []

        # Show Details as first item (default action for double-click)
        if self._use_premium_ui and self._ui_window:
            items.append(pystray.MenuItem(
                "Show Details",
                self._on_show_details,
                default=True,  # Makes this the default action
            ))
            items.append(pystray.Menu.SEPARATOR)

        # Claude usage summary
        if self._snapshot and self._snapshot.logs_available:
            items.append(pystray.MenuItem("Claude", None, enabled=False))
            items.append(pystray.MenuItem(
                f"  Today: ${self._snapshot.today_cost_usd:.2f}",
                None,
                enabled=False,
            ))
            items.append(pystray.MenuItem(
                f"  Month: ${self._snapshot.month_cost_usd:.2f}",
                None,
                enabled=False,
            ))
            # Show today's tokens
            total_tokens = self._snapshot.today_tokens.total_tokens
            if total_tokens > 0:
                items.append(pystray.MenuItem(f"  {format_tokens(total_tokens)} tokens", None, enabled=False))

        # OpenAI usage summary
        if self._combined and self._combined.openai and self._combined.openai.available:
            items.append(pystray.Menu.SEPARATOR)
            items.append(pystray.MenuItem("OpenAI", None, enabled=False))
            openai = self._combined.openai
            if openai.session_available:
                items.append(pystray.MenuItem(
                    f"  Session: {openai.session_percent:.0f}% used",
                    None,
                    enabled=False,
                ))
            items.append(pystray.MenuItem(
                f"  Weekly: {openai.weekly_percent:.0f}% used",
                None,
                enabled=False,
            ))
            # Show today's tokens
            if openai.today_total_tokens > 0:
                items.append(pystray.MenuItem(f"  {format_tokens(openai.today_total_tokens)} tokens", None, enabled=False))

        # DeepSeek summary: balance always, spend when the usage fetch worked
        deepseek = self._combined.deepseek if self._combined else None
        if deepseek and (deepseek.balance_available or deepseek.usage_available):
            items.append(pystray.Menu.SEPARATOR)
            items.append(pystray.MenuItem("DeepSeek", None, enabled=False))
            if deepseek.balance_available:
                items.append(pystray.MenuItem(
                    f"  Balance: ${deepseek.balance_total:.2f}",
                    None, enabled=False))
            if deepseek.usage_available:
                items.append(pystray.MenuItem(f"  Today: ${deepseek.today_cost_usd:.2f}", None, enabled=False))
                items.append(pystray.MenuItem(f"  Month: ${deepseek.month_cost_usd:.2f}", None, enabled=False))
                if deepseek.week_tokens > 0:
                    items.append(pystray.MenuItem(
                        f"  {format_tokens(deepseek.week_tokens)} tokens this week", None, enabled=False))

        has_claude_stats = self._snapshot and self._snapshot.logs_available
        has_openai_stats = self._combined and self._combined.openai and self._combined.openai.available
        has_deepseek_stats = deepseek and (deepseek.balance_available or deepseek.usage_available)
        if has_claude_stats or has_openai_stats or has_deepseek_stats:
            items.append(pystray.Menu.SEPARATOR)

        # Action items
        items.append(pystray.MenuItem("Refresh", self._on_refresh_click))
        if self._ui_window:
            items.append(pystray.MenuItem("Reset window position", self._on_reset_position))

        if self.on_settings:
            items.append(pystray.MenuItem("Settings", self._on_settings_click))

        items.append(pystray.MenuItem("Open log file", self._on_open_log))

        items.append(pystray.Menu.SEPARATOR)
        items.append(pystray.MenuItem("Exit", self._on_exit_click))

        return pystray.Menu(*items)

    def _on_open_log(self, icon=None, item=None):
        """Open the log file in whatever handles .log files."""
        try:
            path = get_log_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch(exist_ok=True)
            os.startfile(str(path))
        except Exception as e:
            logger.warning("Could not open the log file: %s", e)

    def _on_main(self, fn, *args) -> None:
        """Run `fn` on the main thread, at its next check_running tick."""
        self._pending.put((fn, args))

    def run_pending(self) -> None:
        """Run the queued tray callbacks. Main thread only."""
        while True:
            try:
                fn, args = self._pending.get_nowait()
            except queue.Empty:
                return
            try:
                fn(*args)
            except Exception:
                logger.exception("Tray action failed")

    def _show(self):
        if self._ui_window:
            self._ui_window.show(self._snapshot)

    def _on_show_details(self, icon=None, item=None):
        """Show the premium details window."""
        self._on_main(self._show)

    def _on_refresh_click(self, icon, item):
        """Handle refresh menu click."""
        self.on_refresh()

    def _on_reset_position(self, icon, item):
        """Put the details window back at its default spot near the tray."""
        self._on_main(lambda: self._ui_window and self._ui_window.reset_position())

    def _on_settings_click(self, icon, item):
        """Handle settings menu click."""
        if self.on_settings:
            self._on_main(self.on_settings)

    def _on_exit_click(self, icon, item):
        """Handle exit menu click."""
        self._on_main(self._on_ui_exit)

    def _on_ui_refresh(self):
        """Handle refresh from UI window."""
        self.on_refresh()

    def _on_ui_settings(self):
        """Handle settings from UI window."""
        if self.on_settings:
            self.on_settings()

    def _on_ui_exit(self):
        """Handle exit from UI window."""
        self.stop()
        self.on_exit()

    def _on_ui_engine_change(self, engine: Engine):
        """Handle engine change from UI window."""
        if self.on_engine_change:
            self.on_engine_change(engine)

    def _get_icon_image(self) -> Image.Image:
        """Get the appropriate icon image for current state."""
        if self._snapshot is None:
            return create_icon("error")

        status = self._snapshot.get_status_level(
            self._config.warning_threshold,
            self._config.critical_threshold
        )
        percent = self._snapshot.max_percent

        return create_icon(status, percent if percent > 0 else None)

    def _get_tooltip(self) -> str:
        """Get tooltip text for current state."""
        if self._snapshot is None:
            return "ClaudeBar - Loading..."

        lines = ["ClaudeBar"]

        # Claude stats
        if self._snapshot.logs_available:
            lines.append(f"Today: ${self._snapshot.today_cost_usd:.2f}")
            lines.append(f"Month: ${self._snapshot.month_cost_usd:.2f}")

        # OpenAI stats
        if self._combined and self._combined.openai and self._combined.openai.available:
            openai = self._combined.openai
            if openai.session_available:
                lines.append(f"OpenAI: {openai.session_percent:.0f}%/{openai.weekly_percent:.0f}%")
            else:
                lines.append(f"OpenAI: {openai.weekly_percent:.0f}% weekly")

        # DeepSeek stats
        deepseek = self._combined.deepseek if self._combined else None
        if deepseek and deepseek.balance_available:
            lines.append(f"DeepSeek: ${deepseek.balance_total:.2f} balance")
        if deepseek and deepseek.usage_available:
            lines.append(f"DeepSeek today: ${deepseek.today_cost_usd:.2f}")

        lines.append(f"Updated: {self._snapshot.timestamp.strftime('%H:%M')}")

        return "\n".join(lines)

    def update(self, snapshot: UsageSnapshot) -> None:
        """Update the tray with new data (thread-safe)."""
        with _tray_lock:
            self._snapshot = snapshot

            try:
                if self._icon is not None:
                    self._icon.icon = self._get_icon_image()
                    self._icon.title = self._get_tooltip()
                    self._icon.menu = self._create_menu()
            except (AttributeError, RuntimeError):
                pass  # Icon was destroyed during update

            # Update premium UI window if available
            if self._ui_window:
                self._ui_window.update(snapshot)

    def update_combined(self, combined: CombinedSnapshot) -> None:
        """Update the tray with combined data (thread-safe).

        Accepted before start(), so the cached snapshot main.py loads first is
        on screen from the start; stop() clears the icon and the window.
        """
        with _tray_lock:
            self._combined = combined
            # Use Claude snapshot for tray icon (always available)
            self._snapshot = combined.claude

            try:
                if self._icon is not None:
                    self._icon.icon = self._get_icon_image()
                    self._icon.title = self._get_tooltip()
                    self._icon.menu = self._create_menu()
            except (AttributeError, RuntimeError):
                pass  # Icon was destroyed during update

            # Update premium UI window with both snapshots
            if self._ui_window:
                self._ui_window.update_combined(combined)

    def start(self) -> None:
        """Start the tray icon."""
        if self._running:
            return

        # Setup icon with click handler
        self._icon = pystray.Icon(
            name="claudebar",
            icon=self._get_icon_image(),
            title=self._get_tooltip(),
            menu=self._create_menu(),
        )

        # A left click runs the menu's default item, Show Details. pystray has
        # no separate activate hook; an on_activate attribute was never called.

        self._running = True
        self._icon.run()

    def start_detached(self) -> threading.Thread:
        """Start the tray icon in a background thread."""
        thread = threading.Thread(target=self.start, daemon=True)
        thread.start()
        return thread

    def stop(self) -> None:
        """Stop the tray icon."""
        self._running = False

        # Destroy UI window
        if self._ui_window:
            try:
                self._ui_window.destroy()
            except Exception:
                pass
            self._ui_window = None

        # Stop tray icon
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass
            self._icon = None

    @property
    def is_running(self) -> bool:
        """Check if the tray is running."""
        return self._running

    def on_oauth_failure(self, error_msg: str) -> None:
        """Forward OAuth failure to UI window (thread-safe)."""
        if self._ui_window:
            self._ui_window.on_oauth_failure(error_msg)

    def on_oauth_success(self) -> None:
        """Forward OAuth success to UI window (thread-safe)."""
        if self._ui_window:
            self._ui_window.on_oauth_success()
