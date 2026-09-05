"""System tray icon and menu management."""

import threading
from typing import Callable, Optional

from PIL import Image
import pystray

from models import UsageSnapshot, CombinedSnapshot, Engine
from config import Config, get_resources_path
from icons import create_premium_icon
from pricing import format_tokens

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
    """Create a tray icon - use custom icon if available, else generate."""
    # Use larger size for better quality (Windows scales down as needed)
    icon_size = 128
    if _CUSTOM_ICON_PATH.exists():
        try:
            with Image.open(_CUSTOM_ICON_PATH) as img:
                return img.resize((icon_size, icon_size), Image.Resampling.LANCZOS)
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

        has_claude_stats = self._snapshot and self._snapshot.logs_available
        has_openai_stats = self._combined and self._combined.openai and self._combined.openai.available
        if has_claude_stats or has_openai_stats:
            items.append(pystray.Menu.SEPARATOR)

        # Action items
        items.append(pystray.MenuItem("Refresh", self._on_refresh_click))

        if self.on_settings:
            items.append(pystray.MenuItem("Settings", self._on_settings_click))

        items.append(pystray.Menu.SEPARATOR)
        items.append(pystray.MenuItem("Exit", self._on_exit_click))

        return pystray.Menu(*items)

    def _on_show_details(self, icon=None, item=None):
        """Show the premium details window."""
        if self._ui_window:
            self._ui_window.show(self._snapshot)

    def _on_tray_click(self, icon, item=None):
        """Handle tray icon click."""
        if self._use_premium_ui and self._ui_window:
            # Show premium popup window
            self._ui_window.toggle(self._snapshot)
        # If no premium UI, default right-click menu will show

    def _on_refresh_click(self, icon, item):
        """Handle refresh menu click."""
        self.on_refresh()

    def _on_settings_click(self, icon, item):
        """Handle settings menu click."""
        if self.on_settings:
            self.on_settings()

    def _on_exit_click(self, icon, item):
        """Handle exit menu click."""
        self.stop()
        self.on_exit()

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

        lines.append(f"Updated: {self._snapshot.timestamp.strftime('%H:%M')}")

        return "\n".join(lines)

    def update(self, snapshot: UsageSnapshot) -> None:
        """Update the tray with new data (thread-safe)."""
        if not self._running:
            return

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
        """Update the tray with combined data (thread-safe)."""
        if not self._running:
            return

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

        # Add left-click handler for premium UI
        if self._use_premium_ui:
            self._icon.on_activate = self._on_tray_click

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
