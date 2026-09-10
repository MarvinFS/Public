"""Premium popup window UI for ClaudeBar."""

import tkinter as tk
from tkinter import ttk
from typing import Optional, Callable
import threading
import io
import urllib.request
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageTk

from models import UsageSnapshot, OpenAISnapshot, CombinedSnapshot, Engine
from config import Config, save_config, get_resources_path
from currency import (
    format_currency, get_exchange_rates, get_supported_currencies,
    get_currency_symbol, ExchangeRates
)
from claude_check import check_claude_status, ClaudeStatus
from snapshot_cache import get_staleness_text
from pricing import format_tokens

# GitHub repository URL
GITHUB_URL = "https://github.com/MarvinFS/Public/tree/main/claudebar"


# OpenAI /usage plan_type enum -> friendly label. Without a map, a raw or new
# enum (e.g. "prolite", OpenAI's Pro Lite tier) leaks straight through .title().
_PLAN_LABELS = {
    "free": "Free",
    "go": "Go",
    "plus": "Plus",
    "pro": "Pro",
    "prolite": "Pro Lite",
    "team": "Team",
    "business": "Business",
    "enterprise": "Enterprise",
    "education": "Edu",
}


def _plan_label(plan_type: str) -> str:
    """Friendly display label for an OpenAI plan_type, with a graceful fallback
    for enums we don't know yet (underscores -> spaces, title-cased)."""
    return _PLAN_LABELS.get(plan_type.lower(), plan_type.replace("_", " ").title())


# Custom app icon path
_CUSTOM_ICON_PATH = get_resources_path() / "icons" / "app_icon.png"

# Engine icon paths (local cache)
_ICONS_DIR = get_resources_path() / "icons"
_CLAUDE_ICON_PATH = _ICONS_DIR / "claude_icon.png"
_OPENAI_ICON_PATH = _ICONS_DIR / "openai_icon.png"

# Fallback icon URLs (B&W/monochrome versions for dark UI)
_CLAUDE_ICON_URL = "https://cdn.iconscout.com/icon/free/png-256/free-anthropic-logo-icon-download-in-svg-png-gif-file-formats--technology-social-media-company-vol-2-pack-logos-icons-9294364.png"
_OPENAI_ICON_URL = "https://cdn.iconscout.com/icon/free/png-256/free-openai-logo-icon-download-in-svg-png-gif-file-formats--technology-social-media-company-brand-vol-4-pack-logos-icons-8800152.png"


class ModernProgressBar(tk.Canvas):
    """Custom progress bar with gradient and smooth animations."""

    def __init__(self, parent, width=300, height=20, **kwargs):
        super().__init__(parent, width=width, height=height,
                        highlightthickness=0, **kwargs)
        self.width = width
        self.height = height
        self._value = 0.0

        # Colors
        self.bg_color = "#252525"
        self.border_color = "#3a3a3a"

        # Draw background
        self._setup_background()

    def _setup_background(self):
        """Setup the background and border."""
        radius = self.height // 2
        self.create_rounded_rect(0, 0, self.width, self.height,
                                radius, fill=self.bg_color,
                                outline=self.border_color, width=1)

    def create_rounded_rect(self, x1, y1, x2, y2, radius, **kwargs):
        """Create a rounded rectangle."""
        points = [
            x1 + radius, y1, x1 + radius, y1, x2 - radius, y1, x2 - radius, y1,
            x2, y1, x2, y1 + radius, x2, y1 + radius, x2, y2 - radius,
            x2, y2 - radius, x2, y2, x2 - radius, y2, x2 - radius, y2,
            x1 + radius, y2, x1 + radius, y2, x1, y2, x1, y2 - radius,
            x1, y2 - radius, x1, y1 + radius, x1, y1 + radius, x1, y1,
        ]
        return self.create_polygon(points, smooth=True, **kwargs)

    def _get_gradient_color(self, percent):
        """Get color based on percentage (green -> yellow -> red)."""
        if percent < 50:
            r = int(16 + (245 - 16) * (percent / 50))
            g = int(185 + (158 - 185) * (percent / 50))
            b = int(129 + (11 - 129) * (percent / 50))
        elif percent < 80:
            r = int(245 + (251 - 245) * ((percent - 50) / 30))
            g = int(158 + (146 - 158) * ((percent - 50) / 30))
            b = int(11 + (60 - 11) * ((percent - 50) / 30))
        else:
            r = int(251 + (239 - 251) * ((percent - 80) / 20))
            g = int(146 + (68 - 146) * ((percent - 80) / 20))
            b = int(60 + (68 - 60) * ((percent - 80) / 20))
        return f"#{r:02x}{g:02x}{b:02x}"

    def _redraw(self):
        """Redraw the progress bar."""
        self.delete("progress")
        if self._value > 0:
            bar_width = (self.width - 4) * (self._value / 100)
            radius = (self.height - 4) // 2
            color = self._get_gradient_color(self._value)
            if bar_width > radius * 2:
                self.create_rounded_rect(2, 2, 2 + bar_width, self.height - 2,
                                       radius, fill=color, outline="",
                                       tags="progress")

    def set_value(self, percent):
        """Set the progress bar value (0-100)."""
        self._value = max(0, min(100, percent))
        self._redraw()


class SettingsDialog:
    """Settings dialog for ClaudeBar."""

    def __init__(self, parent, config: Config, on_save: Callable, on_close: Callable = None):
        self.config = config
        self.on_save = on_save
        self.on_close = on_close
        self.parent = parent

        # Temporarily lower parent's topmost so dialog can appear on top
        try:
            parent.attributes('-topmost', False)
        except tk.TclError:
            pass

        self.dialog = tk.Toplevel(parent)
        self.dialog.title("Settings")
        self.dialog.configure(bg="#0f0f0f")
        self.dialog.geometry("300x420")
        self.dialog.resizable(False, False)
        self.dialog.transient(parent)
        self.dialog.grab_set()
        self.dialog.attributes('-topmost', True)

        # Center on parent
        self.dialog.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width() - 300) // 2
        y = parent.winfo_y() + (parent.winfo_height() - 300) // 2
        self.dialog.geometry(f"+{x}+{y}")

        # Ensure dialog is focused and on top
        self.dialog.lift()
        self.dialog.focus_force()
        self.dialog.focus_set()

        # Handle dialog close (restore parent topmost)
        self.dialog.protocol("WM_DELETE_WINDOW", self._on_dialog_close)

        self._create_ui()

    def _create_ui(self):
        """Create the settings UI."""
        frame = tk.Frame(self.dialog, bg="#0f0f0f")
        frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=20)

        # Title
        title = tk.Label(frame, text="Settings",
                        font=("Segoe UI", 14, "bold"),
                        fg="#ffffff", bg="#0f0f0f")
        title.pack(anchor=tk.W, pady=(0, 20))

        # Engines section
        engines_label = tk.Label(frame, text="Enabled Engines",
                                font=("Segoe UI", 10),
                                fg="#9ca3af", bg="#0f0f0f")
        engines_label.pack(anchor=tk.W)

        engines_frame = tk.Frame(frame, bg="#0f0f0f")
        engines_frame.pack(fill=tk.X, pady=(5, 15))

        self.claude_var = tk.BooleanVar(value=self.config.claude_enabled)
        claude_cb = tk.Checkbutton(engines_frame, text="Claude",
                                   variable=self.claude_var,
                                   font=("Segoe UI", 10),
                                   fg="#ffffff", bg="#0f0f0f",
                                   activebackground="#0f0f0f",
                                   activeforeground="#ffffff",
                                   selectcolor="#1a1a1a")
        claude_cb.pack(side=tk.LEFT)

        self.codex_var = tk.BooleanVar(value=self.config.codex_enabled)
        codex_cb = tk.Checkbutton(engines_frame, text="Codex",
                                  variable=self.codex_var,
                                  font=("Segoe UI", 10),
                                  fg="#ffffff", bg="#0f0f0f",
                                  activebackground="#0f0f0f",
                                  activeforeground="#ffffff",
                                  selectcolor="#1a1a1a")
        codex_cb.pack(side=tk.LEFT, padx=(10, 0))

        # Currency selection
        currency_frame = tk.Frame(frame, bg="#0f0f0f")
        currency_frame.pack(fill=tk.X, pady=(0, 15))

        currency_label = tk.Label(currency_frame, text="Currency",
                                 font=("Segoe UI", 10),
                                 fg="#9ca3af", bg="#0f0f0f")
        currency_label.pack(anchor=tk.W)

        self.currency_var = tk.StringVar(value=self.config.currency)
        currency_menu = ttk.Combobox(currency_frame,
                                     textvariable=self.currency_var,
                                     values=get_supported_currencies(),
                                     state="readonly",
                                     width=15)
        currency_menu.pack(anchor=tk.W, pady=(5, 0))

        # Currency info
        currency_info = tk.Label(currency_frame,
                                text="Costs will be displayed in selected currency",
                                font=("Segoe UI", 9),
                                fg="#6b7280", bg="#0f0f0f")
        currency_info.pack(anchor=tk.W, pady=(5, 0))

        # Refresh interval
        refresh_frame = tk.Frame(frame, bg="#0f0f0f")
        refresh_frame.pack(fill=tk.X, pady=(0, 15))

        refresh_label = tk.Label(refresh_frame, text="Refresh Interval (seconds)",
                                font=("Segoe UI", 10),
                                fg="#9ca3af", bg="#0f0f0f")
        refresh_label.pack(anchor=tk.W)

        self.refresh_var = tk.StringVar(value=str(self.config.refresh_interval))
        refresh_entry = tk.Entry(refresh_frame, textvariable=self.refresh_var,
                                width=10, bg="#1a1a1a", fg="#ffffff",
                                insertbackground="#ffffff",
                                relief=tk.FLAT)
        refresh_entry.pack(anchor=tk.W, pady=(5, 0))

        # Validation error label (hidden by default)
        self.error_label = tk.Label(frame, text="",
                                    font=("Segoe UI", 9),
                                    fg="#EF4444", bg="#0f0f0f")
        self.error_label.pack(anchor=tk.W, pady=(5, 0))

        # Buttons
        button_frame = tk.Frame(frame, bg="#0f0f0f")
        button_frame.pack(fill=tk.X, pady=(20, 0))

        save_btn = tk.Button(button_frame, text="Save",
                            command=self._save,
                            font=("Segoe UI", 10),
                            bg="#10B981", fg="#ffffff",
                            activebackground="#059669",
                            relief=tk.FLAT, padx=20, pady=8)
        save_btn.pack(side=tk.RIGHT)

        cancel_btn = tk.Button(button_frame, text="Cancel",
                              command=self._on_dialog_close,
                              font=("Segoe UI", 10),
                              bg="#2a2a2a", fg="#ffffff",
                              activebackground="#3a3a3a",
                              relief=tk.FLAT, padx=20, pady=8)
        cancel_btn.pack(side=tk.RIGHT, padx=(0, 10))

    def _restore_parent(self):
        """Restore parent window's topmost state."""
        try:
            self.parent.attributes('-topmost', True)
        except tk.TclError:
            pass
        if self.on_close:
            self.on_close()

    def _on_dialog_close(self):
        """Handle dialog close."""
        self._restore_parent()
        self.dialog.destroy()

    def _save(self):
        """Save settings."""
        # Validate at least one engine is enabled
        claude_enabled = self.claude_var.get()
        codex_enabled = self.codex_var.get()

        if not claude_enabled and not codex_enabled:
            self.error_label.config(text="At least one engine must be enabled")
            return

        self.config.claude_enabled = claude_enabled
        self.config.codex_enabled = codex_enabled
        self.config.currency = self.currency_var.get()
        try:
            self.config.refresh_interval = int(self.refresh_var.get())
        except ValueError:
            pass

        save_config(self.config)
        self.on_save()
        self._restore_parent()
        self.dialog.destroy()


class ClaudeBarWindow:
    """Premium popup window for ClaudeBar."""

    def __init__(self, on_refresh: Callable, on_settings: Callable,
                 on_exit: Callable, config: Optional[Config] = None,
                 on_engine_change: Optional[Callable] = None):
        self.on_refresh = on_refresh
        self.on_settings = on_settings
        self.on_exit = on_exit
        self.on_engine_change = on_engine_change
        self.config = config or Config()

        self._window: Optional[tk.Tk] = None
        self._lock = threading.RLock()
        self._snapshot: Optional[UsageSnapshot] = None
        self._openai_snapshot: Optional[OpenAISnapshot] = None
        self._combined_snapshot: Optional[CombinedSnapshot] = None
        self._pending_snapshot: Optional[UsageSnapshot] = None  # Thread-safe pending update
        self._pending_openai_snapshot: Optional[OpenAISnapshot] = None
        self._exchange_rates: Optional[ExchangeRates] = None
        self._claude_status: Optional[ClaudeStatus] = None
        self._status_needs_update = False
        self._oauth_error: Optional[str] = None  # Track OAuth errors for status display
        self._is_collecting: bool = True  # Start as collecting until first data arrives

        # Engine state
        self._active_engine: Engine = Engine.CLAUDE
        self._openai_available: bool = False

        # UI elements
        self._status_indicator: Optional[tk.Label] = None
        self._status_text: Optional[tk.Label] = None
        self._session_frame: Optional[tk.Frame] = None
        self._weekly_frame: Optional[tk.Frame] = None
        self._session_bar: Optional[ModernProgressBar] = None
        self._weekly_bar: Optional[ModernProgressBar] = None
        self._session_label: Optional[tk.Label] = None
        self._weekly_label: Optional[tk.Label] = None
        self._session_reset: Optional[tk.Label] = None
        self._weekly_reset: Optional[tk.Label] = None
        self._extra_frame: Optional[tk.Frame] = None
        self._extra_bar: Optional[ModernProgressBar] = None
        self._extra_label: Optional[tk.Label] = None
        self._extra_amount: Optional[tk.Label] = None
        self._today_cost: Optional[tk.Label] = None
        self._today_tokens: Optional[tk.Label] = None
        self._month_cost: Optional[tk.Label] = None
        self._month_tokens: Optional[tk.Label] = None
        self._updated_label: Optional[tk.Label] = None
        self._stale_label: Optional[tk.Label] = None
        self._logo_image: Optional[ImageTk.PhotoImage] = None
        self._opening_link = False  # Flag to prevent hide during link click

        # Engine toggle UI elements
        self._claude_btn: Optional[tk.Label] = None
        self._openai_btn: Optional[tk.Label] = None
        self._claude_icon: Optional[ImageTk.PhotoImage] = None
        self._openai_icon: Optional[ImageTk.PhotoImage] = None
        self._engine_label: Optional[tk.Label] = None
        self._engine_frame: Optional[tk.Frame] = None

        self._last_extra_visible: Optional[bool] = None  # Track extra usage visibility
        self._last_session_visible: Optional[bool] = None  # Track 5-hour row visibility

        # Drag-to-move state (the window is borderless, so there is no title bar)
        self._drag_origin: Optional[tuple] = None
        self._user_position: Optional[tuple] = None  # Set once the user drags

        # Colors
        self.bg_color = "#0f0f0f"
        self.card_color = "#1a1a1a"
        self.text_primary = "#ffffff"
        self.text_secondary = "#d1d5db"
        self.text_tertiary = "#9ca3af"
        self.text_muted = "#6b7280"
        self.separator_color = "#2a2a2a"
        self.accent_color = "#F59E0B"  # Claude orange
        self.active_engine_bg = "#2a2a2a"  # Active engine button background
        self.inactive_engine_bg = "#0f0f0f"  # Inactive engine button background

    def _load_engine_icon(self, local_path: Path, url: str, size: int = 24) -> Optional[ImageTk.PhotoImage]:
        """Load engine icon from local cache or download from URL."""
        try:
            # Try local file first
            if local_path.exists():
                img = Image.open(local_path)
            else:
                # Download and cache
                try:
                    req = urllib.request.Request(url, headers={"User-Agent": "ClaudeBar/1.0"})
                    with urllib.request.urlopen(req, timeout=5) as response:
                        data = response.read()
                    img = Image.open(io.BytesIO(data))
                    # Save to cache
                    local_path.parent.mkdir(parents=True, exist_ok=True)
                    img.save(local_path)
                except Exception:
                    return None

            # Resize and convert to grayscale for B&W look
            img = img.resize((size, size), Image.Resampling.LANCZOS)
            # Convert to grayscale, then boost brightness for dark UI
            img = img.convert("LA")  # Luminance + Alpha
            return ImageTk.PhotoImage(img)
        except Exception:
            return None

    def _create_window(self):
        """Create the popup window."""
        # Use Toplevel since main.py creates the hidden root
        self._window = tk.Toplevel()
        self._window.title("ClaudeBar")
        self._window.configure(bg=self.bg_color)

        # Fixed width, dynamic height
        self._window_width = 380
        self._window.resizable(False, False)
        self._window.attributes('-topmost', True)
        self._window.overrideredirect(True)

        # Main container with border
        self._main_frame = tk.Frame(self._window, bg=self.bg_color,
                             highlightbackground="#3a3a3a",
                             highlightthickness=1)
        self._main_frame.pack(fill=tk.BOTH, expand=True)

        # Content
        content = tk.Frame(self._main_frame, bg=self.bg_color)
        content.pack(fill=tk.BOTH, expand=True, padx=16, pady=16)

        self._create_header(content)
        self._create_status_section(content)
        self._create_usage_section(content)
        self._create_cost_section(content)
        self._create_footer(content)

        self._window.bind('<Escape>', lambda e: self.hide())
        # Bound on the toplevel, which sits in every child widget's bindtags, so
        # the whole popup is a drag handle - it has no title bar to grab.
        self._window.bind('<Button-1>', self._on_drag_start, add='+')
        self._window.bind('<B1-Motion>', self._on_drag_move, add='+')
        self._window.withdraw()

        # Load initial data
        self._load_initial_data()

    def _on_drag_start(self, event):
        """Remember where the pointer grabbed the window."""
        self._drag_origin = None
        if self._window and self._window.winfo_exists():
            self._drag_origin = (event.x_root, event.y_root,
                                 self._window.winfo_x(), self._window.winfo_y())

    def _on_drag_move(self, event):
        """Move the window with the pointer and pin it there for later shows."""
        if not self._drag_origin or not self._window.winfo_exists():
            return
        grab_x, grab_y, win_x, win_y = self._drag_origin
        x = win_x + event.x_root - grab_x
        y = win_y + event.y_root - grab_y
        self._user_position = (x, y)
        self._window.geometry(f"+{x}+{y}")

    def _update_window_size(self):
        """Update window size based on content and position near system tray."""
        if not self._window or not self._window.winfo_exists():
            return

        # Let tkinter calculate required sizes
        self._window.update_idletasks()

        # Get required height from content
        required_height = self._main_frame.winfo_reqheight()

        # Add some padding and ensure minimum height
        window_height = max(500, required_height + 4)  # +4 for border

        # Keep wherever the user dragged it to; otherwise sit near the tray.
        if self._user_position:
            x, y = self._user_position
        else:
            screen_width = self._window.winfo_screenwidth()
            screen_height = self._window.winfo_screenheight()
            x = screen_width - self._window_width - 20
            y = screen_height - window_height - 60

        self._window.geometry(f"{self._window_width}x{window_height}+{x}+{y}")

    def _load_initial_data(self):
        """Load exchange rates and Claude status."""
        def load():
            self._exchange_rates = get_exchange_rates()
            self._claude_status = check_claude_status()
            # Set flag for UI thread to pick up
            with self._lock:
                self._status_needs_update = True

        threading.Thread(target=load, daemon=True).start()

    def on_oauth_failure(self, error_msg: str):
        """Handle OAuth failure by triggering status recheck (thread-safe)."""
        with self._lock:
            self._oauth_error = error_msg
            self._status_needs_update = True

    def on_oauth_success(self):
        """Handle OAuth success by clearing error state (thread-safe)."""
        with self._lock:
            self._oauth_error = None
            self._status_needs_update = True

    def _on_github_click(self, event):
        """Handle GitHub button click."""
        # Set flag to prevent FocusOut from hiding window
        self._opening_link = True
        # Use subprocess to call Windows start command - most reliable method
        import subprocess
        try:
            subprocess.Popen(['cmd', '/c', 'start', '', GITHUB_URL],
                           shell=False,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        except Exception:
            pass
        # Reset flag after a short delay
        if self._window:
            self._window.after(500, self._reset_link_flag)

    def _reset_link_flag(self):
        """Reset the link opening flag."""
        self._opening_link = False

    def _get_enabled_engines(self) -> list[Engine]:
        """Get list of enabled engines from config."""
        engines = []
        if self.config.claude_enabled:
            engines.append(Engine.CLAUDE)
        if self.config.codex_enabled:
            engines.append(Engine.CODEX)
        return engines

    def _create_header(self, parent):
        """Create the header with title, engine toggle, and GitHub link."""
        header = tk.Frame(parent, bg=self.bg_color)
        header.pack(fill=tk.X, pady=(0, 12))

        # Left side - Logo and title
        left_frame = tk.Frame(header, bg=self.bg_color)
        left_frame.pack(side=tk.LEFT)

        # Logo - use custom icon if available (72px for better visibility)
        logo_size = 72
        if _CUSTOM_ICON_PATH.exists():
            try:
                img = Image.open(_CUSTOM_ICON_PATH)
                img = img.resize((logo_size, logo_size), Image.Resampling.LANCZOS)
                self._logo_image = ImageTk.PhotoImage(img)
                logo = tk.Label(left_frame, image=self._logo_image,
                               bg=self.bg_color)
                logo.pack(side=tk.LEFT, padx=(0, 12))
            except Exception:
                # Fallback to drawn logo
                logo = tk.Canvas(left_frame, width=64, height=64,
                                bg=self.bg_color, highlightthickness=0)
                logo.pack(side=tk.LEFT, padx=(0, 12))
                logo.create_oval(2, 2, 62, 62, fill=self.accent_color, outline="")
                logo.create_text(32, 32, text="C", fill="#ffffff",
                                font=("Segoe UI", 24, "bold"))
        else:
            # Fallback to drawn logo
            logo = tk.Canvas(left_frame, width=64, height=64,
                            bg=self.bg_color, highlightthickness=0)
            logo.pack(side=tk.LEFT, padx=(0, 12))
            logo.create_oval(2, 2, 62, 62, fill=self.accent_color, outline="")
            logo.create_text(32, 32, text="C", fill="#ffffff",
                            font=("Segoe UI", 24, "bold"))

        title_frame = tk.Frame(left_frame, bg=self.bg_color)
        title_frame.pack(side=tk.LEFT)

        title = tk.Label(title_frame, text="ClaudeBar",
                        font=("Segoe UI", 16, "bold"),
                        fg=self.text_primary, bg=self.bg_color)
        title.pack(anchor=tk.W)

        subtitle = tk.Label(title_frame, text="AI Usage Tracker",
                           font=("Segoe UI", 9),
                           fg=self.text_muted, bg=self.bg_color)
        subtitle.pack(anchor=tk.W)

        # Right side - Close button, Engine toggle and GitHub link
        right_frame = tk.Frame(header, bg=self.bg_color)
        right_frame.pack(side=tk.RIGHT)

        # Close button (hides window, doesn't exit app)
        close_btn = tk.Label(right_frame, text="✕",
                            font=("Segoe UI", 12),
                            fg=self.text_muted, bg=self.bg_color,
                            cursor="hand2", padx=4)
        close_btn.pack(side=tk.TOP, anchor=tk.E)
        close_btn.bind("<Button-1>", lambda e: self.hide())
        close_btn.bind("<Enter>", lambda e: close_btn.config(fg="#EF4444"))
        close_btn.bind("<Leave>", lambda e: close_btn.config(fg=self.text_muted))

        # GitHub link
        github_btn = tk.Label(right_frame, text="GitHub ↗",
                             font=("Segoe UI", 9),
                             fg=self.accent_color, bg=self.bg_color,
                             cursor="hand2")
        github_btn.pack(side=tk.TOP, anchor=tk.E)
        github_btn.bind("<Button-1>", self._on_github_click)
        github_btn.bind("<Enter>", lambda e: github_btn.config(fg="#FCD34D"))
        github_btn.bind("<Leave>", lambda e: github_btn.config(fg=self.accent_color))

        # Engine toggle buttons (only show if more than one engine enabled)
        enabled_engines = self._get_enabled_engines()

        # Set active engine to first enabled if current is disabled
        if self._active_engine not in enabled_engines and enabled_engines:
            self._active_engine = enabled_engines[0]

        if len(enabled_engines) > 1:
            self._engine_frame = tk.Frame(right_frame, bg=self.bg_color)
            self._engine_frame.pack(side=tk.TOP, anchor=tk.E, pady=(8, 0))

            # Load engine icons
            self._claude_icon = self._load_engine_icon(_CLAUDE_ICON_PATH, _CLAUDE_ICON_URL, 20)
            self._openai_icon = self._load_engine_icon(_OPENAI_ICON_PATH, _OPENAI_ICON_URL, 20)

            btn_padx = 8
            btn_gap = 4

            # Claude button (if enabled)
            if self.config.claude_enabled:
                claude_bg = self.active_engine_bg if self._active_engine == Engine.CLAUDE else self.inactive_engine_bg
                self._claude_btn = tk.Label(self._engine_frame, text=" Claude" if not self._claude_icon else "",
                                            image=self._claude_icon if self._claude_icon else None,
                                            compound=tk.LEFT,
                                            font=("Segoe UI", 9),
                                            fg=self.text_primary, bg=claude_bg,
                                            padx=btn_padx, pady=4, cursor="hand2")
                if not self._claude_icon:
                    self._claude_btn.config(text="Claude")
                self._claude_btn.pack(side=tk.LEFT, padx=(0, btn_gap))
                self._claude_btn.bind("<Button-1>", lambda e: self._on_engine_select(Engine.CLAUDE))

            # OpenAI/Codex button (if enabled)
            if self.config.codex_enabled:
                openai_bg = self.active_engine_bg if self._active_engine == Engine.CODEX else self.inactive_engine_bg
                self._openai_btn = tk.Label(self._engine_frame, text=" Codex" if not self._openai_icon else "",
                                            image=self._openai_icon if self._openai_icon else None,
                                            compound=tk.LEFT,
                                            font=("Segoe UI", 9),
                                            fg=self.text_primary, bg=openai_bg,
                                            padx=btn_padx, pady=4, cursor="hand2")
                if not self._openai_icon:
                    self._openai_btn.config(text="Codex")
                self._openai_btn.pack(side=tk.LEFT)
                self._openai_btn.bind("<Button-1>", lambda e: self._on_engine_select(Engine.CODEX))

    def _on_engine_select(self, engine: Engine):
        """Handle engine selection with smooth fade transition."""
        if self._active_engine == engine:
            return

        # Update button backgrounds immediately (this is the "tab" change)
        if self._claude_btn:
            bg = self.active_engine_bg if engine == Engine.CLAUDE else self.inactive_engine_bg
            self._claude_btn.config(bg=bg)
        if self._openai_btn:
            bg = self.active_engine_bg if engine == Engine.CODEX else self.inactive_engine_bg
            self._openai_btn.config(bg=bg)

        self._active_engine = engine

        # Notify callback
        if self.on_engine_change:
            self.on_engine_change(engine)

        # Update status display for new engine
        self._update_status_display()

        # Update display with current data (this will also resize)
        self._refresh_display_immediate()

    def _create_status_section(self, parent):
        """Create Claude connection status section."""
        status_frame = tk.Frame(parent, bg=self.card_color)
        status_frame.pack(fill=tk.X, pady=(0, 16))

        inner = tk.Frame(status_frame, bg=self.card_color)
        inner.pack(fill=tk.X, padx=12, pady=10)

        # Status indicator
        self._status_indicator = tk.Label(inner, text="●",
                                         font=("Segoe UI", 12),
                                         fg="#6b7280", bg=self.card_color)
        self._status_indicator.pack(side=tk.LEFT)

        self._status_text = tk.Label(inner, text="Checking...",
                                    font=("Segoe UI", 10),
                                    fg=self.text_secondary, bg=self.card_color)
        self._status_text.pack(side=tk.LEFT, padx=(8, 0))

    def _update_status_display(self):
        """Update the status display based on active engine."""
        if not self._status_indicator or not self._status_text:
            return

        # Read shared state under a single lock acquisition
        with self._lock:
            oauth_error = self._oauth_error
            valid_snapshot = None
            if self._snapshot and self._snapshot.cli_available:
                valid_snapshot = self._snapshot
            elif self._pending_snapshot and self._pending_snapshot.cli_available:
                valid_snapshot = self._pending_snapshot

        if self._active_engine == Engine.CLAUDE:
            # Connection status is driven by claude auth status (CLI subprocess),
            # independent of whether the OAuth usage API returns data.

            # If we have valid usage data, we're definitely connected
            if valid_snapshot:
                with self._lock:
                    self._oauth_error = None
                self._status_indicator.config(fg="#10B981")
                text = "Connected"
                plan = self._claude_status.plan if self._claude_status else None
                if not plan and valid_snapshot.extra_enabled:
                    plan = "Max"
                if plan:
                    text += f" · {plan}"
                self._status_text.config(text=text)
                return

            # No usage data - check CLI auth status (primary status source)
            if self._claude_status and self._claude_status.authenticated:
                # CLI says we're logged in, even though usage API may be blocked
                self._status_indicator.config(fg="#10B981")
                text = "Connected"
                plan = self._claude_status.plan
                if plan:
                    text += f" · {plan}"
                self._status_text.config(text=text)
                return

            # Not authenticated via CLI - show specific error
            if oauth_error:
                self._status_indicator.config(fg="#EF4444")
                if "refresh failed" in oauth_error.lower() or "token expired" in oauth_error.lower():
                    self._status_text.config(text="Session expired")
                elif "not found" in oauth_error.lower() or "no oauth" in oauth_error.lower():
                    self._status_text.config(text="Not logged in")
                elif "429" in oauth_error or "restricted" in oauth_error.lower() or "rate limit" in oauth_error.lower():
                    self._status_indicator.config(fg="#F59E0B")
                    self._status_text.config(text="API restricted")
                else:
                    self._status_text.config(text="Connection error")
                return

            # Fall back to claude_status check for non-authenticated states
            if self._claude_status:
                if self._claude_status.installed:
                    self._status_indicator.config(fg="#F59E0B")
                    text = "Not logged in"
                else:
                    self._status_indicator.config(fg="#EF4444")
                    text = "Claude CLI not found"
                self._status_text.config(text=text)
            else:
                self._status_indicator.config(fg="#6b7280")
                self._status_text.config(text="Checking...")
        elif self._active_engine == Engine.CODEX:
            # Show Codex connection status
            if self._openai_snapshot:
                if self._openai_snapshot.available:
                    self._status_indicator.config(fg="#10B981")
                    text = "Connected"
                    # Show plan type if available
                    if self._openai_snapshot.plan_type:
                        text += f" · {_plan_label(self._openai_snapshot.plan_type)}"
                    elif self._openai_snapshot.credits_remaining is not None:
                        text += f" · ${self._openai_snapshot.credits_remaining:.2f} credits"
                    self._status_text.config(text=text)
                elif self._openai_snapshot.error_message:
                    self._status_indicator.config(fg="#EF4444")
                    # Show shorter error message
                    err = self._openai_snapshot.error_message
                    if "Run 'codex login'" in err:
                        text = "Not logged in"
                    elif "token expired" in err.lower():
                        text = "Token expired"
                    else:
                        text = "Connection error"
                    self._status_text.config(text=text)
                else:
                    self._status_indicator.config(fg="#F59E0B")
                    self._status_text.config(text="Not configured")
            else:
                self._status_indicator.config(fg="#6b7280")
                self._status_text.config(text="Checking...")

    def _create_section_title(self, parent, text):
        """Create a section title."""
        label = tk.Label(parent, text=text.upper(),
                        font=("Segoe UI", 9, "bold"),
                        fg=self.text_muted, bg=self.bg_color)
        label.pack(anchor=tk.W, pady=(0, 10))

    def _create_separator(self, parent):
        """Create a horizontal separator."""
        sep = tk.Frame(parent, bg=self.separator_color, height=1)
        sep.pack(fill=tk.X, pady=16)

    def _create_usage_section(self, parent):
        """Create the usage progress section."""
        self._create_section_title(parent, "Usage Limits")

        # Session usage
        session_frame = tk.Frame(parent, bg=self.bg_color)
        session_frame.pack(fill=tk.X, pady=(0, 14))
        self._session_frame = session_frame

        session_header = tk.Frame(session_frame, bg=self.bg_color)
        session_header.pack(fill=tk.X, pady=(0, 6))

        self._session_label = tk.Label(session_header, text="5-Hour · 0% used",
                                       font=("Segoe UI", 11),
                                       fg=self.text_secondary, bg=self.bg_color)
        self._session_label.pack(side=tk.LEFT)

        self._session_reset = tk.Label(session_header, text="",
                                       font=("Segoe UI", 9),
                                       fg=self.text_muted, bg=self.bg_color)
        self._session_reset.pack(side=tk.RIGHT)

        self._session_bar = ModernProgressBar(session_frame, width=348, height=20,
                                              bg=self.bg_color)
        self._session_bar.pack()

        # Weekly usage
        weekly_frame = tk.Frame(parent, bg=self.bg_color)
        weekly_frame.pack(fill=tk.X)
        self._weekly_frame = weekly_frame

        weekly_header = tk.Frame(weekly_frame, bg=self.bg_color)
        weekly_header.pack(fill=tk.X, pady=(0, 6))

        self._weekly_label = tk.Label(weekly_header, text="Weekly · 0% used",
                                      font=("Segoe UI", 11),
                                      fg=self.text_secondary, bg=self.bg_color)
        self._weekly_label.pack(side=tk.LEFT)

        self._weekly_reset = tk.Label(weekly_header, text="",
                                      font=("Segoe UI", 9),
                                      fg=self.text_muted, bg=self.bg_color)
        self._weekly_reset.pack(side=tk.RIGHT)

        self._weekly_bar = ModernProgressBar(weekly_frame, width=348, height=20,
                                            bg=self.bg_color)
        self._weekly_bar.pack()

        # Extra usage (initially hidden, shown only when enabled)
        self._extra_frame = tk.Frame(parent, bg=self.bg_color)
        # Don't pack yet - will be shown conditionally in _refresh_display

        extra_header = tk.Frame(self._extra_frame, bg=self.bg_color)
        extra_header.pack(fill=tk.X, pady=(14, 6))

        self._extra_label = tk.Label(extra_header, text="Extra · 0% used",
                                     font=("Segoe UI", 11),
                                     fg=self.text_secondary, bg=self.bg_color)
        self._extra_label.pack(side=tk.LEFT)

        self._extra_amount = tk.Label(extra_header, text="",
                                      font=("Segoe UI", 9),
                                      fg=self.text_muted, bg=self.bg_color)
        self._extra_amount.pack(side=tk.RIGHT)

        self._extra_bar = ModernProgressBar(self._extra_frame, width=348, height=20,
                                           bg=self.bg_color)
        self._extra_bar.pack()

        self._create_separator(parent)

    def _create_cost_section(self, parent):
        """Create the cost information section."""
        self._create_section_title(parent, "API-equivalent cost")

        cost_frame = tk.Frame(parent, bg=self.bg_color)
        cost_frame.pack(fill=tk.X)

        # Today
        today_row = tk.Frame(cost_frame, bg=self.bg_color)
        today_row.pack(fill=tk.X, pady=(0, 10))

        today_label = tk.Label(today_row, text="Today",
                              font=("Segoe UI", 10),
                              fg=self.text_muted, bg=self.bg_color)
        today_label.pack(side=tk.LEFT)

        today_value = tk.Frame(today_row, bg=self.bg_color)
        today_value.pack(side=tk.RIGHT)

        self._today_cost = tk.Label(today_value, text="$0.00",
                                   font=("Segoe UI", 12, "bold"),
                                   fg=self.text_primary, bg=self.bg_color)
        self._today_cost.pack(side=tk.LEFT)

        today_sep = tk.Label(today_value, text=" · ",
                           font=("Segoe UI", 10),
                           fg=self.text_muted, bg=self.bg_color)
        today_sep.pack(side=tk.LEFT)

        self._today_tokens = tk.Label(today_value, text="0 tokens",
                                     font=("Segoe UI", 10),
                                     fg=self.text_tertiary, bg=self.bg_color)
        self._today_tokens.pack(side=tk.LEFT)

        # Current calendar month
        month_row = tk.Frame(cost_frame, bg=self.bg_color)
        month_row.pack(fill=tk.X)

        month_label = tk.Label(month_row, text="This month",
                              font=("Segoe UI", 10),
                              fg=self.text_muted, bg=self.bg_color)
        month_label.pack(side=tk.LEFT)

        month_value = tk.Frame(month_row, bg=self.bg_color)
        month_value.pack(side=tk.RIGHT)

        self._month_cost = tk.Label(month_value, text="$0.00",
                                   font=("Segoe UI", 12, "bold"),
                                   fg=self.text_primary, bg=self.bg_color)
        self._month_cost.pack(side=tk.LEFT)

        month_sep = tk.Label(month_value, text=" · ",
                           font=("Segoe UI", 10),
                           fg=self.text_muted, bg=self.bg_color)
        month_sep.pack(side=tk.LEFT)

        self._month_tokens = tk.Label(month_value, text="0 tokens",
                                     font=("Segoe UI", 10),
                                     fg=self.text_tertiary, bg=self.bg_color)
        self._month_tokens.pack(side=tk.LEFT)

        self._create_separator(parent)

    def _create_footer(self, parent):
        """Create footer with action buttons and update time."""
        # Update time
        update_frame = tk.Frame(parent, bg=self.bg_color)
        update_frame.pack(fill=tk.X, pady=(0, 12))

        self._updated_label = tk.Label(update_frame, text="Updated just now",
                                       font=("Segoe UI", 9),
                                       fg=self.text_muted, bg=self.bg_color)
        self._updated_label.pack(side=tk.LEFT)

        # Stale data warning (hidden by default)
        self._stale_label = tk.Label(update_frame, text="",
                                     font=("Segoe UI", 9),
                                     fg="#F59E0B", bg=self.bg_color)
        self._stale_label.pack(side=tk.LEFT, padx=(8, 0))

        # Buttons
        footer = tk.Frame(parent, bg=self.bg_color)
        footer.pack(fill=tk.X)

        button_style = {
            "font": ("Segoe UI", 10),
            "bg": "#2a2a2a",
            "fg": self.text_primary,
            "activebackground": "#3a3a3a",
            "activeforeground": self.text_primary,
            "relief": tk.FLAT,
            "cursor": "hand2",
            "padx": 14,
            "pady": 8,
        }

        settings_btn = tk.Button(footer, text="⚙ Settings",
                                command=self._on_settings_click,
                                **button_style)
        settings_btn.pack(side=tk.LEFT)
        self._add_hover(settings_btn, "#2a2a2a", "#3a3a3a")

        right_buttons = tk.Frame(footer, bg=self.bg_color)
        right_buttons.pack(side=tk.RIGHT)

        refresh_btn = tk.Button(right_buttons, text="↻ Refresh",
                               command=self._on_refresh_click,
                               **button_style)
        refresh_btn.pack(side=tk.LEFT, padx=(0, 8))
        self._add_hover(refresh_btn, "#2a2a2a", "#3a3a3a")

        exit_btn = tk.Button(right_buttons, text="✕ Exit",
                            command=self._on_exit_click,
                            **button_style)
        exit_btn.pack(side=tk.LEFT)
        self._add_hover(exit_btn, "#2a2a2a", "#EF4444")

    def _add_hover(self, widget, normal, hover):
        """Add hover effect."""
        widget.bind("<Enter>", lambda e: widget.config(bg=hover))
        widget.bind("<Leave>", lambda e: widget.config(bg=normal))

    def _on_refresh_click(self):
        """Handle refresh button."""
        self.on_refresh()
        self._load_initial_data()

    def _on_settings_click(self):
        """Handle settings button."""
        if self._window:
            SettingsDialog(self._window, self.config, self._on_settings_saved)

    def _on_settings_saved(self):
        """Handle settings save with smooth transition."""
        self._exchange_rates = get_exchange_rates()

        # Hide window first for smoother transition
        if self._window:
            self._window.withdraw()
            # Small delay to ensure window is hidden before destruction
            self._window.after(50, self._rebuild_window)
        else:
            self._rebuild_window()

    def _rebuild_window(self):
        """Rebuild window after settings change."""
        # Destroy old window
        if self._window:
            self._window.destroy()
            self._window = None
            # Reset UI element references
            self._claude_btn = None
            self._openai_btn = None
            self._engine_frame = None
            self._last_extra_visible = None  # Reset layout tracking

        # Recreate and show
        self._create_window()
        if self._snapshot:
            self.update(self._snapshot)
        if self._openai_snapshot:
            self.update_openai(self._openai_snapshot)
        self.show()

    def _on_exit_click(self):
        """Handle exit button."""
        self.hide()
        self.on_exit()

    def update(self, snapshot: UsageSnapshot):
        """Queue snapshot update (thread-safe, can be called from any thread)."""
        with self._lock:
            self._pending_snapshot = snapshot

    def update_openai(self, snapshot: OpenAISnapshot):
        """Queue OpenAI snapshot update (thread-safe)."""
        with self._lock:
            self._pending_openai_snapshot = snapshot

    def update_combined(self, combined: CombinedSnapshot):
        """Queue combined snapshot update (thread-safe)."""
        with self._lock:
            self._pending_snapshot = combined.claude
            self._pending_openai_snapshot = combined.openai
            self._active_engine = combined.active_engine
            # Clear OAuth error if we received valid Claude data
            if combined.claude and combined.claude.cli_available:
                self._oauth_error = None
                self._status_needs_update = True

    def _apply_snapshot_update(self):
        """Apply pending snapshot update (must be called from main thread)."""
        updated = False
        with self._lock:
            if self._pending_snapshot:
                self._snapshot = self._pending_snapshot
                self._pending_snapshot = None
                updated = True
            if self._pending_openai_snapshot:
                self._openai_snapshot = self._pending_openai_snapshot
                self._pending_openai_snapshot = None
                updated = True

        if not updated:
            return

        self._refresh_display()

    def _refresh_display(self):
        """Refresh display (extra-usage visibility may change the layout/size)."""
        if not self._window or not self._window.winfo_exists():
            return
        self._refresh_display_immediate()

    def _refresh_display_immediate(self):
        """Refresh the display immediately (no fade)."""
        if not self._window or not self._window.winfo_exists():
            return

        # Get data for active engine
        snapshot = None  # Track for extra usage visibility check
        is_stale = False
        stale_since = None
        session_available = True  # Claude always reports a 5-hour window
        if self._active_engine == Engine.CLAUDE:
            snapshot = self._snapshot
            if not snapshot:
                return

            session_pct = snapshot.session_percent
            session_reset = snapshot.session_reset
            weekly_pct = snapshot.weekly_percent
            weekly_reset = snapshot.weekly_reset
            today_cost = snapshot.today_cost_usd
            today_tokens = snapshot.today_tokens.total_tokens
            month_cost = snapshot.month_cost_usd
            month_tokens = snapshot.month_tokens.total_tokens
            pricing_source = snapshot.pricing_source
            timestamp = snapshot.timestamp
            is_stale = snapshot.is_stale
            stale_since = snapshot.stale_since
        elif self._active_engine == Engine.CODEX:
            openai = self._openai_snapshot
            if not openai:
                # Show placeholder when no OpenAI data
                session_pct = 0.0
                session_reset = None
                weekly_pct = 0.0
                weekly_reset = None
                today_cost = 0.0
                today_tokens = 0
                month_cost = 0.0
                month_tokens = 0
                pricing_source = "bundled"
                timestamp = datetime.now()
            else:
                session_pct = openai.session_percent
                session_reset = openai.session_reset
                session_available = openai.session_available
                weekly_pct = openai.weekly_percent
                weekly_reset = openai.weekly_reset
                today_cost = openai.today_cost_usd  # From log parsing
                today_tokens = openai.today_total_tokens
                month_cost = openai.month_cost_usd
                month_tokens = openai.month_total_tokens
                pricing_source = openai.pricing_source
                timestamp = openai.timestamp
                is_stale = openai.is_stale
                stale_since = openai.stale_since

        currency = self.config.currency

        # Update progress bars (animate=False to ensure immediate rendering).
        # When there's no usable data, show a placeholder instead of a misleading
        # "0% used": "Collecting..." on the first cycle, "Unavailable" once OAuth
        # is confirmed down with no cache (Claude snapshot present but cli_available False).
        collecting_text = None
        if session_pct == 0 and weekly_pct == 0 and not is_stale:
            if self._is_collecting:
                collecting_text = "Collecting..."
            elif (self._active_engine == Engine.CLAUDE and snapshot
                    and not snapshot.cli_available):
                collecting_text = "Unavailable"

        if self._session_frame and self._weekly_frame:
            if session_available and not self._session_frame.winfo_manager():
                self._session_frame.pack(fill=tk.X, pady=(0, 14), before=self._weekly_frame)
            elif not session_available:
                self._session_frame.pack_forget()

        if self._session_bar and self._session_label:
            self._session_bar.set_value(session_pct)
            self._session_label.config(
                text=collecting_text or f"5-Hour · {session_pct:.0f}% used"
            )
        if self._session_reset:
            if session_reset:
                self._session_reset.config(text=f"Resets {session_reset}")
            elif not collecting_text:
                self._session_reset.config(text="")

        if self._weekly_bar and self._weekly_label:
            self._weekly_bar.set_value(weekly_pct)
            self._weekly_label.config(
                text=collecting_text or f"Weekly · {weekly_pct:.0f}% used"
            )
        if self._weekly_reset:
            if weekly_reset:
                self._weekly_reset.config(text=f"Resets {weekly_reset}")
            elif not collecting_text:
                self._weekly_reset.config(text="")

        # Update extra usage section (Claude only)
        # Track visibility changes for resize optimization
        extra_visible = False
        if self._extra_frame and self._extra_bar and self._extra_label and self._extra_amount:
            if self._active_engine == Engine.CLAUDE and snapshot and snapshot.extra_enabled:
                extra_visible = True
                # Show extra usage frame
                if not self._extra_frame.winfo_manager():
                    self._extra_frame.pack(fill=tk.X, after=self._weekly_bar.master)

                # Update extra usage values
                extra_pct = snapshot.extra_percent
                extra_used = snapshot.extra_used
                extra_limit = snapshot.extra_limit
                extra_currency = snapshot.extra_currency.upper()

                self._extra_bar.set_value(extra_pct)
                self._extra_label.config(text=f"Extra · {extra_pct:.0f}% used")

                # Format amount with proper currency symbol
                symbol = get_currency_symbol(extra_currency)
                self._extra_amount.config(text=f"{symbol}{extra_used:.2f} / {symbol}{extra_limit:.2f}")
            else:
                # Hide extra usage frame
                if self._extra_frame.winfo_manager():
                    self._extra_frame.pack_forget()

        # Track if layout changed
        layout_changed = (self._last_extra_visible != extra_visible
                          or self._last_session_visible != session_available)
        self._last_extra_visible = extra_visible
        self._last_session_visible = session_available

        # Update costs with currency conversion
        if self._today_cost:
            cost_str = format_currency(today_cost, currency, self._exchange_rates)
            self._today_cost.config(text=cost_str)
        if self._today_tokens:
            self._today_tokens.config(text=f"{format_tokens(today_tokens)} tokens")

        if self._month_cost:
            cost_str = format_currency(month_cost, currency, self._exchange_rates)
            self._month_cost.config(text=cost_str)
        if self._month_tokens:
            self._month_tokens.config(text=f"{format_tokens(month_tokens)} tokens")

        # Update timestamp and stale indicator
        if self._updated_label:
            time_str = timestamp.strftime("%H:%M")
            engine_names = {Engine.CLAUDE: "Claude", Engine.CODEX: "Codex"}
            engine_name = engine_names.get(self._active_engine, "Unknown")
            if self._is_collecting and is_stale:
                self._updated_label.config(text=f"{engine_name} · Collecting fresh data...")
            else:
                # Name the fallback so a models.dev outage is never silent.
                suffix = "" if pricing_source == "models.dev" else " · bundled rates"
                self._updated_label.config(text=f"{engine_name} · Updated at {time_str}{suffix}")
            # Clear once any snapshot has been applied (stale or fresh); token gaps
            # are now common, so don't pin "Collecting..." forever - the separate
            # stale label keeps communicating staleness.
            self._is_collecting = False

        # Update stale indicator
        if self._stale_label:
            if is_stale and stale_since:
                stale_text = get_staleness_text(stale_since)
                self._stale_label.config(text=f"(cached from {stale_text})")
            else:
                self._stale_label.config(text="")

        # Only resize window when layout structure changes (extra usage visibility)
        # This prevents flickering on regular data updates
        if layout_changed:
            self._update_window_size()

    def show(self, snapshot: Optional[UsageSnapshot] = None):
        """Show the window."""
        if snapshot:
            self.update(snapshot)

        if self._window is None:
            self._create_window()

        if self._window:
            # Apply any pending updates before showing
            self._apply_snapshot_update()
            # Update window size based on content before showing
            self._update_window_size()
            self._window.deiconify()
            self._window.lift()
            self._window.focus_force()
            # Start update loop for data updates
            self._start_update_loop()

    def _start_update_loop(self):
        """Process tkinter events and check for pending updates."""
        if not self._window:
            return

        try:
            if not self._window.winfo_exists():
                return

            # Apply any pending data updates
            self._apply_snapshot_update()

            # Check if status needs update
            with self._lock:
                if self._status_needs_update:
                    self._status_needs_update = False
                    self._update_status_display()

            # Continue loop if window exists
            self._window.after(50, self._start_update_loop)
        except tk.TclError:
            pass  # Window was destroyed

    def hide(self):
        """Hide the window."""
        if self._window:
            self._window.withdraw()

    def destroy(self):
        """Destroy the window."""
        if self._window:
            self._window.destroy()
            self._window = None

    def toggle(self, snapshot: Optional[UsageSnapshot] = None):
        """Toggle window visibility."""
        if self._window and self._window.winfo_viewable():
            self.hide()
        else:
            self.show(snapshot)
