"""Premium popup window UI for ClaudeBar."""

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk
from typing import Optional, Callable
import ctypes
import threading
import io
import urllib.request
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageTk

from models import (UsageSnapshot, OpenAISnapshot, CombinedSnapshot, Engine, project_window,
                    SESSION_WINDOW_HOURS, WEEKLY_WINDOW_HOURS, DAILY_HISTORY_DAYS)
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


class PaceBar(tk.Canvas):
    """Slim usage bar with a tick at the even-spend position."""

    def __init__(self, parent, width=348, height=6, accent="#F59E0B", **kwargs):
        super().__init__(parent, width=width, height=height + 2, highlightthickness=0, **kwargs)
        self.w, self.h, self.accent = width, height, accent
        self.create_rectangle(0, 1, width, height + 1, fill="#262626", outline="")

    def set_value(self, percent, expected=None):
        self.delete("v")
        pct = max(0.0, min(100.0, percent))
        if pct > 0:
            self.create_rectangle(0, 1, self.w * pct / 100, self.h + 1,
                                  fill=self.accent, outline="", tags="v")
        if expected is not None:
            x = self.w * max(0.0, min(100.0, expected)) / 100
            self.create_rectangle(x - 1, 0, x + 1, self.h + 2, fill="#f3f4f6", outline="", tags="v")


class BarChart(tk.Canvas):
    """Daily bars, today highlighted, oldest on the left."""

    def __init__(self, parent, width=348, height=44, accent="#F59E0B", **kwargs):
        super().__init__(parent, width=width, height=height, highlightthickness=0, **kwargs)
        self.w, self.h, self.accent = width, height, accent

    def set_values(self, values):
        self.delete("v")
        if not values:
            return
        n = len(values)
        gap = 2
        bw = (self.w - gap * (n - 1)) / n
        peak = max(max(values), 0.01)
        for i, v in enumerate(values):
            x0 = i * (bw + gap)
            h = max(v / peak * (self.h - 2), 1)
            self.create_rectangle(x0, self.h - h, x0 + bw, self.h,
                                  fill=self.accent if i == n - 1 else "#7a5210", outline="", tags="v")


def _point_on_screen(x: int, y: int) -> bool:
    """Whether a screen point lies on any monitor (Win32; assume yes elsewhere)."""
    try:
        class _Point(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
        # MONITOR_DEFAULTTONULL: a point in no monitor's rect returns NULL.
        return bool(ctypes.windll.user32.MonitorFromPoint(_Point(x, y), 0))
    except Exception:
        return True


def _fmt_hours(h: float) -> str:
    if h == float("inf"):
        return "never"
    d, r = divmod(h, 24)
    if d >= 1:
        return f"{int(d)}d {int(r)}h"
    return f"{int(r)}h {int((r % 1) * 60)}m"


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

        # UI elements (built in _create_window)
        self._status_indicator: Optional[tk.Label] = None
        self._status_text: Optional[tk.Label] = None
        self._engine_name: Optional[tk.Label] = None
        self._updated_label: Optional[tk.Label] = None
        self._stale_label: Optional[tk.Label] = None
        self._rates_label: Optional[tk.Label] = None
        self._limits: dict = {}          # "session" / "weekly" -> widget dict
        self._extra_frame: Optional[tk.Frame] = None
        self._extra_bar: Optional[PaceBar] = None
        self._extra_amount: Optional[tk.Label] = None
        self._stats: dict = {}           # key -> (title, value, sub) labels
        self._chart: Optional[BarChart] = None
        self._chart_note: Optional[tk.Label] = None
        self._top_model: Optional[tk.Label] = None
        self._top_share: Optional[tk.Label] = None
        self._source_label: Optional[tk.Label] = None
        self._logo_image: Optional[ImageTk.PhotoImage] = None
        self._opening_link = False  # Flag to prevent hide during link click
        self._layout_key = None      # which optional rows are visible

        # Engine toggle UI elements
        self._claude_btn: Optional[tk.Label] = None
        self._openai_btn: Optional[tk.Label] = None
        self._engine_frame: Optional[tk.Frame] = None

        # Drag-to-move state (the window is borderless, so there is no title bar)
        self._drag_origin: Optional[tuple] = None
        self._user_position: Optional[tuple] = self._saved_position()

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
        self.ok_color = "#22c55e"
        self.warn_color = "#ef4444"
        # Fonts: resolved in _create_window, font.families() needs a Tk root.
        self.sans = "Segoe UI"
        self.mono = "Consolas"

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
        # Windows 11 ships both; older installs keep the classic defaults above.
        families = set(tkfont.families(self._window))
        if "Segoe UI Variable Text" in families:
            self.sans = "Segoe UI Variable Text"
        if "Cascadia Mono" in families:
            self.mono = "Cascadia Mono"
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
        self._create_engine_rows(content)
        self._hairline(content)
        self._create_limits(content)
        self._hairline(content)
        self._create_stats(content)
        self._hairline(content)
        self._create_chart(content)
        self._create_footer(content)

        self._window.bind('<Escape>', lambda e: self.hide())
        # Bound on the toplevel, which sits in every child widget's bindtags, so
        # the whole popup is a drag handle - it has no title bar to grab.
        self._window.bind('<Button-1>', self._on_drag_start, add='+')
        self._window.bind('<B1-Motion>', self._on_drag_move, add='+')
        self._window.bind('<ButtonRelease-1>', self._on_drag_end, add='+')
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

    def _on_drag_end(self, event):
        """Persist the dragged position so it survives a restart."""
        if self._drag_origin and self._user_position:
            self.config.window_x, self.config.window_y = self._user_position
            save_config(self.config)
        self._drag_origin = None

    def _saved_position(self) -> Optional[tuple]:
        """The persisted position, unless it would land off every monitor
        (a screen was unplugged or rearranged) - then the default spot."""
        x, y = self.config.window_x, self.config.window_y
        if isinstance(x, int) and isinstance(y, int) and _point_on_screen(x + 40, y + 20):
            return (x, y)
        return None

    def reset_position(self):
        """Forget the dragged position and return to the spot near the tray."""
        self._user_position = None
        self.config.window_x = self.config.window_y = None
        save_config(self.config)
        if self._window and self._window.winfo_exists():
            self._update_window_size()

    def _update_window_size(self):
        """Update window size based on content and position near system tray."""
        if not self._window or not self._window.winfo_exists():
            return

        # Let tkinter calculate required sizes
        self._window.update_idletasks()

        # Get required height from content
        required_height = self._main_frame.winfo_reqheight()

        window_height = required_height + 4  # +4 for border

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

    def _font(self, size, weight="normal", mono=False):
        return (self.mono if mono else self.sans, size, weight)

    def _hairline(self, parent, pady=(10, 10)):
        tk.Frame(parent, bg=self.separator_color, height=1).pack(fill=tk.X, pady=pady)

    def _row(self, parent, left, right, lf, rf, lc, rc, pady=0):
        """Two labels on one line, left- and right-aligned. Returns (row, left, right)."""
        r = tk.Frame(parent, bg=self.bg_color)
        r.pack(fill=tk.X, pady=pady)
        l = tk.Label(r, text=left, font=lf, fg=lc, bg=self.bg_color)
        l.pack(side=tk.LEFT)
        rr = tk.Label(r, text=right, font=rf, fg=rc, bg=self.bg_color)
        rr.pack(side=tk.RIGHT)
        return r, l, rr

    def _create_header(self, parent):
        """Logo, title (links to GitHub), engine toggle, close."""
        header = tk.Frame(parent, bg=self.bg_color)
        header.pack(fill=tk.X)

        if _CUSTOM_ICON_PATH.exists():
            try:
                img = Image.open(_CUSTOM_ICON_PATH).resize((28, 28), Image.Resampling.LANCZOS)
                self._logo_image = ImageTk.PhotoImage(img)
                tk.Label(header, image=self._logo_image, bg=self.bg_color).pack(side=tk.LEFT, padx=(0, 8))
            except Exception:
                self._logo_image = None

        title = tk.Label(header, text="ClaudeBar", font=self._font(11, "bold"),
                         fg=self.text_primary, bg=self.bg_color, cursor="hand2")
        title.pack(side=tk.LEFT)
        title.bind("<Button-1>", self._on_github_click)
        title.bind("<Enter>", lambda e: title.config(fg=self.accent_color))
        title.bind("<Leave>", lambda e: title.config(fg=self.text_primary))

        close_btn = tk.Label(header, text="\u2715", font=self._font(10),
                             fg=self.text_muted, bg=self.bg_color, cursor="hand2", padx=4)
        close_btn.pack(side=tk.RIGHT)
        close_btn.bind("<Button-1>", lambda e: self.hide())
        close_btn.bind("<Enter>", lambda e: close_btn.config(fg=self.warn_color))
        close_btn.bind("<Leave>", lambda e: close_btn.config(fg=self.text_muted))

        enabled_engines = self._get_enabled_engines()
        if self._active_engine not in enabled_engines and enabled_engines:
            self._active_engine = enabled_engines[0]
        if len(enabled_engines) > 1:
            self._engine_frame = tk.Frame(header, bg=self.bg_color)
            self._engine_frame.pack(side=tk.RIGHT, padx=(0, 8))
            for engine, name in ((Engine.CLAUDE, "Claude"), (Engine.CODEX, "Codex")):
                btn = tk.Label(self._engine_frame, text=name, font=self._font(8),
                               fg=self.text_primary, bg=self.inactive_engine_bg,
                               padx=10, pady=3, cursor="hand2")
                btn.pack(side=tk.LEFT)
                btn.bind("<Button-1>", lambda e, en=engine: self._on_engine_select(en))
                if engine == Engine.CLAUDE:
                    self._claude_btn = btn
                else:
                    self._openai_btn = btn
            self._paint_engine_buttons()

    def _paint_engine_buttons(self):
        for btn, engine in ((self._claude_btn, Engine.CLAUDE), (self._openai_btn, Engine.CODEX)):
            if btn:
                on = self._active_engine == engine
                btn.config(bg=self.active_engine_bg if on else self.inactive_engine_bg,
                           fg=self.text_primary if on else self.text_muted,
                           font=self._font(8, "bold" if on else "normal"))

    def _on_engine_select(self, engine: Engine):
        """Switch the displayed engine."""
        if self._active_engine == engine:
            return
        self._active_engine = engine
        self._paint_engine_buttons()
        if self.on_engine_change:
            self.on_engine_change(engine)
        self._update_status_display()
        self._refresh_display_immediate()

    def _create_engine_rows(self, parent):
        """Engine name + connection status, then updated time + rate source."""
        _, self._engine_name, status = self._row(parent, "Claude", "", self._font(9, "bold"),
                                                  self._font(8), self.text_primary, self.text_muted,
                                                  pady=(10, 0))
        # The status label is the right-hand cell; the dot sits just before it.
        status.pack_forget()
        self._status_text = status
        self._status_text.pack(side=tk.RIGHT)
        self._status_indicator = tk.Label(status.master, text="\u25cf", font=self._font(8),
                                          fg=self.text_muted, bg=self.bg_color)
        self._status_indicator.pack(side=tk.RIGHT, padx=(0, 4))

        r, self._updated_label, self._rates_label = self._row(
            parent, "Updated just now", "", self._font(8), self._font(8), self.text_muted, self.text_muted)
        self._stale_label = tk.Label(r, text="", font=self._font(8), fg=self.accent_color, bg=self.bg_color)
        self._stale_label.pack(side=tk.LEFT, padx=(6, 0))

    def _create_limit(self, parent, key, title):
        """One rate-limit block: title/reset, bar, left/run-out, pace/elapsed."""
        frame = tk.Frame(parent, bg=self.bg_color)
        frame.pack(fill=tk.X, pady=(0, 8))
        _, _, reset = self._row(frame, title, "", self._font(9, "bold"), self._font(8),
                                self.text_primary, self.text_muted)
        bar = PaceBar(frame, width=348, bg=self.bg_color)
        bar.pack(fill=tk.X)
        _, left, note = self._row(frame, "", "", self._font(8, mono=True), self._font(8),
                                  self.text_secondary, self.text_muted)
        _, pace, elapsed = self._row(frame, "", "", self._font(8), self._font(8),
                                     self.text_muted, self.text_muted)
        self._limits[key] = {"frame": frame, "reset": reset, "bar": bar, "left": left,
                             "note": note, "pace": pace, "elapsed": elapsed}

    def _create_limits(self, parent):
        self._create_limit(parent, "session", "Session (5-hour)")
        self._create_limit(parent, "weekly", "Weekly")

        # Extra usage (paid overage), packed only when enabled.
        self._extra_frame = tk.Frame(parent, bg=self.bg_color)
        _, _, self._extra_amount = self._row(self._extra_frame, "Extra usage", "", self._font(9, "bold"),
                                             self._font(8), self.text_primary, self.text_muted)
        self._extra_bar = PaceBar(self._extra_frame, width=348, bg=self.bg_color)
        self._extra_bar.pack(fill=tk.X)

    def _create_stat(self, parent, key, title):
        f = tk.Frame(parent, bg=self.bg_color)
        f.pack(side=tk.LEFT, fill=tk.X, expand=True)
        tk.Label(f, text=title, font=self._font(8), fg=self.text_muted, bg=self.bg_color).pack(anchor=tk.W)
        value = tk.Label(f, text="", font=self._font(12, "bold", mono=True), fg=self.text_primary, bg=self.bg_color)
        value.pack(anchor=tk.W)
        sub = tk.Label(f, text="", font=self._font(8), fg=self.text_muted, bg=self.bg_color)
        sub.pack(anchor=tk.W)
        self._stats[key] = (value, sub)

    def _create_stats(self, parent):
        """2x2 grid: today / last 31 days, this month / output today."""
        top = tk.Frame(parent, bg=self.bg_color)
        top.pack(fill=tk.X)
        self._create_stat(top, "today", "Today")
        self._create_stat(top, "last31", f"Last {DAILY_HISTORY_DAYS} days")
        tk.Frame(parent, bg=self.bg_color, height=8).pack()
        bottom = tk.Frame(parent, bg=self.bg_color)
        bottom.pack(fill=tk.X)
        self._create_stat(bottom, "month", "This month")
        self._create_stat(bottom, "output", "Output today")

    def _create_chart(self, parent):
        _, _, self._chart_note = self._row(parent, "Daily API-equivalent cost", "", self._font(8),
                                           self._font(8), self.text_muted, self.text_muted)
        tk.Frame(parent, bg=self.bg_color, height=4).pack()
        self._chart = BarChart(parent, width=348, bg=self.bg_color)
        self._chart.pack(fill=tk.X)
        tk.Frame(parent, bg=self.bg_color, height=6).pack()
        _, self._top_model, self._top_share = self._row(
            parent, "", "", self._font(8, mono=True), self._font(8), self.text_secondary, self.text_muted)

    def _create_footer(self, parent):
        """Source note on the left, actions on the right."""
        self._hairline(parent, (12, 6))
        footer = tk.Frame(parent, bg=self.bg_color)
        footer.pack(fill=tk.X)
        self._source_label = tk.Label(footer, text="", font=self._font(8), fg=self.text_muted, bg=self.bg_color)
        self._source_label.pack(side=tk.LEFT)

        button_style = {
            "font": self._font(8), "bg": self.bg_color, "fg": self.text_secondary,
            "activebackground": self.active_engine_bg, "activeforeground": self.text_primary,
            "relief": tk.FLAT, "cursor": "hand2", "padx": 6, "pady": 2, "bd": 0,
        }
        for text, command, hover in (("Exit", self._on_exit_click, self.warn_color),
                                     ("Refresh", self._on_refresh_click, self.text_primary),
                                     ("Settings", self._on_settings_click, self.text_primary)):
            btn = tk.Button(footer, text=text, command=command, **button_style)
            btn.pack(side=tk.RIGHT)
            btn.bind("<Enter>", lambda e, b=btn, c=hover: b.config(fg=c))
            btn.bind("<Leave>", lambda e, b=btn: b.config(fg=self.text_secondary))

    def _update_status_display(self):
        """Update the status display based on active engine."""
        if not self._status_indicator or not self._status_text:
            return
        if self._engine_name:
            self._engine_name.config(text="Claude" if self._active_engine == Engine.CLAUDE else "Codex")

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
            self._layout_key = None

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

        self._refresh_display_immediate()

    def _refresh_display_immediate(self):
        """Fill every widget from the active engine's snapshot."""
        if not self._window or not self._window.winfo_exists():
            return

        currency = self.config.currency
        money = lambda usd: format_currency(usd, currency, self._exchange_rates)
        claude = self._active_engine == Engine.CLAUDE
        snap = self._snapshot if claude else self._openai_snapshot
        if snap is None:
            return

        # Which optional rows show; a change re-measures the window.
        session_visible = claude or snap.session_available
        extra_visible = claude and snap.extra_enabled
        layout_key = (claude, session_visible, extra_visible)

        collecting = (self._is_collecting and snap.session_percent == 0 and snap.weekly_percent == 0
                      and not snap.is_stale)
        unavailable = claude and not snap.cli_available and not snap.is_stale
        placeholder = "Collecting..." if collecting else ("Unavailable" if unavailable else None)

        self._fill_limit("session", snap.session_percent, snap.session_reset,
                         snap.session_resets_at, SESSION_WINDOW_HOURS, placeholder)
        self._fill_limit("weekly", snap.weekly_percent, snap.weekly_reset,
                         snap.weekly_resets_at, WEEKLY_WINDOW_HOURS, placeholder)
        frame = self._limits["session"]["frame"]
        if session_visible and not frame.winfo_manager():
            frame.pack(fill=tk.X, pady=(0, 8), before=self._limits["weekly"]["frame"])
        elif not session_visible and frame.winfo_manager():
            frame.pack_forget()

        if extra_visible:
            if not self._extra_frame.winfo_manager():
                self._extra_frame.pack(fill=tk.X, after=self._limits["weekly"]["frame"])
            symbol = get_currency_symbol(snap.extra_currency.upper())
            self._extra_bar.set_value(snap.extra_percent)
            self._extra_amount.config(
                text=f"{snap.extra_percent:.0f}% used \u00b7 {symbol}{snap.extra_used:.2f} / {symbol}{snap.extra_limit:.2f}")
        elif self._extra_frame.winfo_manager():
            self._extra_frame.pack_forget()

        # Cost grid
        if claude:
            today_tokens = snap.today_tokens.total_tokens
            month_tokens = snap.month_tokens.total_tokens
            output_today = snap.today_tokens.output_tokens
            cache_reads = snap.today_tokens.cache_read_input_tokens
        else:
            today_tokens = snap.today_total_tokens
            month_tokens = snap.month_total_tokens
            output_today = snap.today_output_tokens
            cache_reads = snap.today_cached_tokens
        self._set_stat("today", money(snap.today_cost_usd), f"{format_tokens(today_tokens)} tokens")
        self._set_stat("last31", money(snap.last31_cost_usd), f"{format_tokens(snap.last31_tokens)} tokens")
        self._set_stat("month", money(snap.month_cost_usd), f"{format_tokens(month_tokens)} tokens")
        self._set_stat("output", format_tokens(output_today), f"{format_tokens(cache_reads)} cache reads")

        # Chart + top model (per-model split exists for Claude only)
        self._chart.set_values(snap.daily_costs)
        peak = max(snap.daily_costs) if snap.daily_costs else 0.0
        self._chart_note.config(text=f"{len(snap.daily_costs)} days, peak {money(peak)}")
        top = max(snap.models_used, key=lambda m: m.cost_usd, default=None) if claude else None
        if top and snap.month_cost_usd > 0:
            self._top_model.config(text=f"Top model  {top.model}")
            self._top_share.config(text=f"{top.cost_usd / snap.month_cost_usd * 100:.0f}% of month")
        else:
            self._top_model.config(text="")
            self._top_share.config(text="")

        # Meta rows and footer
        self._rates_label.config(text=f"{snap.pricing_source} rates")
        if self._is_collecting and snap.is_stale:
            self._updated_label.config(text="Collecting fresh data...")
        else:
            self._updated_label.config(text=f"Updated {snap.timestamp.strftime('%H:%M')}")
        # Clear once any snapshot has been applied (stale or fresh); the stale
        # label keeps communicating staleness.
        self._is_collecting = False
        self._stale_label.config(
            text=f"(cached from {get_staleness_text(snap.stale_since)})" if snap.is_stale and snap.stale_since else "")
        self._source_label.config(
            text=f"Estimated from local {'Claude Code' if claude else 'Codex'} logs")

        if layout_key != self._layout_key:
            self._layout_key = layout_key
            self._update_window_size()

    def _fill_limit(self, key, percent, reset_str, resets_at, window_hours, placeholder):
        w = self._limits[key]
        proj = project_window(percent, resets_at, window_hours)
        w["bar"].set_value(percent, proj[0] if proj else None)
        w["reset"].config(text=f"Resets {reset_str}" if reset_str else "")
        w["left"].config(text=placeholder or f"{100 - percent:.0f}% left")
        if placeholder or proj is None:
            for cell in ("note", "pace", "elapsed"):
                w[cell].config(text="")
            return
        expected, remaining, runout = proj
        lasts = runout >= remaining
        w["note"].config(text="Lasts until reset" if lasts else f"Runs out in {_fmt_hours(runout)}",
                         fg=self.ok_color if lasts else self.warn_color)
        ahead = percent - expected
        w["pace"].config(text="On pace" if ahead <= 5 else f"{ahead:.0f}% ahead of pace")
        w["elapsed"].config(text=f"{_fmt_hours(window_hours - remaining)} elapsed of {_fmt_hours(window_hours)}")

    def _set_stat(self, key, value, sub):
        v, s = self._stats[key]
        v.config(text=value)
        s.config(text=sub)

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
