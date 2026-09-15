"""Premium popup window UI for ClaudeBar."""

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk
from typing import Optional, Callable
import ctypes
import threading
import io
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from PIL import Image, ImageTk

from models import (UsageSnapshot, OpenAISnapshot, DeepSeekSnapshot, CombinedSnapshot, Engine,
                    project_window, SESSION_WINDOW_HOURS, WEEKLY_WINDOW_HOURS, DAILY_HISTORY_DAYS)
from config import Config, save_config, get_resources_path
from currency import (
    format_currency, convert_usd, get_exchange_rates, get_supported_currencies,
    get_currency_symbol, ExchangeRates
)
from claude_check import check_claude_status, ClaudeStatus
from snapshot_cache import get_staleness_text
from pricing import format_tokens
import logging

import browser_signin
import deepseek_auth
import deepseek_usage

# GitHub repository URL
GITHUB_URL = "https://github.com/MarvinFS/Public/tree/main/claudebar"

# The sign-in path is otherwise silent, which makes a slow capture
# indistinguishable from a dead button.
logger = logging.getLogger("claudebar")


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
_DEEPSEEK_ICON_PATH = _ICONS_DIR / "deepseek_icon.png"

# Fallback mark sources. Raster favicons keep an SVG rasteriser out of the
# bundle, and a missing local asset still self-heals on the next run.
def _icon_url(domain: str) -> str:
    return f"https://www.google.com/s2/favicons?domain={domain}&sz=64"


# engine -> (display name, local icon, fallback domain, accent colour)
_ENGINE_META = {
    Engine.CLAUDE: ("Claude", _CLAUDE_ICON_PATH, "anthropic.com", "#F59E0B"),
    Engine.CODEX: ("Codex", _OPENAI_ICON_PATH, "openai.com", "#F59E0B"),
    Engine.DEEPSEEK: ("DeepSeek", _DEEPSEEK_ICON_PATH, "deepseek.com", "#4D6BFE"),
}

# engine -> Config attribute that enables it
_ENABLED_FLAG = {
    Engine.CLAUDE: "claude_enabled",
    Engine.CODEX: "codex_enabled",
    Engine.DEEPSEEK: "deepseek_enabled",
}

# Selector glyph geometry
_GLYPH_CANVAS = 64
_GLYPH_BOX = 52
_GLYPH_SIZE = 20


def _monochrome_glyph(img: Image.Image, canvas: int = _GLYPH_CANVAS,
                      box: int = _GLYPH_BOX) -> Image.Image:
    """Reduce a provider mark to a white-on-transparent mask of uniform size.

    Marks arrive in two shapes: an opaque card with a dark glyph, or a coloured
    glyph that already carries transparency. The opaque kind takes its mask from
    inverted luminance, the transparent kind keeps its own alpha. Either way the
    glyph is cropped and scaled to a common optical size so the selector row
    looks even.
    """
    rgba = img.convert("RGBA")
    alpha = rgba.getchannel("A")
    transparent = sum(alpha.histogram()[:16])
    if transparent > rgba.size[0] * rgba.size[1] * 0.05:
        mask = alpha
    else:
        flat = Image.new("RGB", rgba.size, (255, 255, 255))
        flat.paste(rgba, mask=alpha)
        mask = flat.convert("L").point(lambda v: 255 - v)

    mask = mask.point(lambda v: 0 if v < 14 else v)  # drop the halo
    bounds = mask.getbbox()
    if bounds:
        mask = mask.crop(bounds)
        w, h = mask.size
        scale = box / max(w, h)
        mask = mask.resize((max(1, round(w * scale)), max(1, round(h * scale))),
                           Image.Resampling.LANCZOS)

    out = Image.new("L", (canvas, canvas), 0)
    out.paste(mask, ((canvas - mask.size[0]) // 2, (canvas - mask.size[1]) // 2))
    return out


def _dim(hex_color: str, factor: float = 0.45) -> str:
    """A darker version of an accent, for the bars that are not today."""
    try:
        value = hex_color.lstrip("#")
        r, g, b = (int(value[i:i + 2], 16) for i in (0, 2, 4))
        return "#%02x%02x%02x" % (int(r * factor), int(g * factor), int(b * factor))
    except (ValueError, IndexError):
        return hex_color


def bar_index_at(x: int, n: int, width: int, gap: int = 4) -> Optional[int]:
    """Index of the bar under canvas x, or None when there are no bars.

    Bars sit on a fixed pitch of (width + gap) / n, so the hit test is a
    division rather than a scan. Pure maths, so it is unit-tested without Tk.
    """
    if n <= 0 or width <= 0 or x < 0 or x > width:
        return None
    return max(0, min(n - 1, int(x // ((width + gap) / n))))


class PaceBar(tk.Canvas):
    """Slim usage bar with a tick at the even-spend position."""

    def __init__(self, parent, width=348, height=6, accent="#F59E0B", **kwargs):
        super().__init__(parent, width=width, height=height + 2, highlightthickness=0, **kwargs)
        self.w, self.h, self.accent = width, height, accent
        self._value = (0.0, None)
        self._draw()
        # The panel is only as wide as its content needs, so take the width we
        # are actually given rather than the one guessed at construction.
        self.bind("<Configure>", self._on_resize)

    def _on_resize(self, event):
        if event.width != self.w:
            self.w = event.width
            self._draw()

    def _draw(self):
        self.delete("all")
        self.create_rectangle(0, 1, self.w, self.h + 1, fill="#262626", outline="")
        percent, expected = self._value
        pct = max(0.0, min(100.0, percent))
        if pct > 0:
            self.create_rectangle(0, 1, self.w * pct / 100, self.h + 1,
                                  fill=self.accent, outline="", tags="v")
        if expected is not None:
            x = self.w * max(0.0, min(100.0, expected)) / 100
            self.create_rectangle(x - 1, 0, x + 1, self.h + 2, fill="#f3f4f6", outline="", tags="v")

    def set_accent(self, accent: str):
        """Recolour without a repaint; the next set_value uses it."""
        self.accent = accent

    def set_value(self, percent, expected=None):
        self._value = (percent, expected)
        self._draw()


class BarChart(tk.Canvas):
    """Daily bars with a label under each, today highlighted, oldest on the left.

    Hovering a bar shows that day's figures in a flyout drawn on the canvas
    itself. Drawing in-canvas rather than in a Toplevel keeps the panel's
    topmost, borderless geometry out of the picture entirely.
    """

    def __init__(self, parent, width=348, height=44, accent="#F59E0B", font=None,
                 label_fg="#7f8899", **kwargs):
        # Label band from the font metrics, so a DPI-scaled font does not overlap the bars.
        self.label_h = tkfont.Font(root=parent, font=font).metrics("linespace") + 2 if font else 14
        super().__init__(parent, width=width, height=height + self.label_h, highlightthickness=0, **kwargs)
        self.w, self.h, self.accent, self.font = width, height, accent, font
        self.label_fg = label_fg
        # The flyout is read at a glance while the pointer moves, so it runs a
        # couple of points larger and bolder than the weekday labels under it.
        self.tip_font = (font[0], (font[1] if len(font) > 1 else 8) + 2, "bold") if font else None
        self._tooltips: list = []
        self._n = 0
        self._tip_index: Optional[int] = None
        self._values: list = []
        self._labels: tuple = ()
        self.bind("<Motion>", self._on_motion)
        self.bind("<Leave>", lambda e: self._show_tip(None))
        # Same reason as PaceBar: redraw at the width the panel ends up with.
        self.bind("<Configure>", self._on_resize)

    def _on_resize(self, event):
        if event.width != self.w:
            self.w = event.width
            self.set_values(self._values, self._labels)

    def set_accent(self, accent: str):
        self.accent = accent

    def set_values(self, values, labels=(), tooltips=()):
        self.delete("v")
        self._show_tip(None)
        self._tooltips = list(tooltips)
        self._values = list(values)
        self._labels = tuple(labels)
        self._n = len(values)
        if not values:
            return
        n = len(values)
        gap = 4
        bw = (self.w - gap * (n - 1)) / n
        peak = max(max(values), 0.01)
        for i, v in enumerate(values):
            x0 = i * (bw + gap)
            h = max(v / peak * (self.h - 2), 1)
            today = i == n - 1
            self.create_rectangle(x0, self.h - h, x0 + bw, self.h,
                                  fill=self.accent if today else _dim(self.accent), outline="", tags="v")
            if i < len(labels):
                self.create_text(x0 + bw / 2, self.h + self.label_h / 2, text=labels[i], font=self.font,
                                 fill=self.accent if today else self.label_fg, tags="v")

    def _on_motion(self, event):
        self._show_tip(bar_index_at(event.x, self._n, self.w))

    def _show_tip(self, index: Optional[int]):
        """Draw the flyout for one bar, or clear it. Cheap to call repeatedly."""
        if index == self._tip_index:
            return
        self._tip_index = index
        self.delete("tip")
        if index is None or not self._tooltips:
            return
        if index >= len(self._tooltips):
            return
        text = self._tooltips[index]
        if not text:
            return

        font = tkfont.Font(root=self, font=self.tip_font or self.font)
        pad = 7
        box_w = font.measure(text) + pad * 2
        box_h = font.metrics("linespace") + pad
        slot = (self.w + 4) / max(self._n, 1)
        centre = index * slot + slot / 2
        x0 = max(0, min(self.w - box_w, centre - box_w / 2))
        self.create_rectangle(x0, 0, x0 + box_w, box_h, fill="#000000", outline="#5a5a5a", tags="tip")
        self.create_text(x0 + box_w / 2, box_h / 2, text=text, font=self.tip_font or self.font,
                         fill="#ffffff", tags="tip")



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
    """Settings dialog for ClaudeBar.

    A Toplevel, not a ClaudeBarWindow, so it carries its own palette rather
    than reaching for the main window's colours.
    """

    # Kept identical to ClaudeBarWindow's palette so the two surfaces agree,
    # except for the hints: a settings panel is read, not skimmed, and
    # #6b7280 on #0f0f0f is barely legible.
    warn_color = "#ef4444"
    ok_color = "#22c55e"
    text_primary = "#ffffff"
    hint_color = "#d1d5db"      # a little dimmer than white
    label_color = "#e5e7eb"

    def __init__(self, parent, config: Config, on_save: Callable, on_close: Callable = None,
                 on_refresh: Callable = None):
        self.config = config
        self.on_save = on_save
        self.on_close = on_close
        # Fired after a credential change so the panel can refetch immediately
        # rather than waiting for the next scheduled refresh.
        self.on_refresh = on_refresh
        self._signin_thread = None
        self._signin_cancel = False
        self._signin_result = None
        self.parent = parent

        # Temporarily lower parent's topmost so dialog can appear on top
        try:
            parent.attributes('-topmost', False)
        except tk.TclError:
            pass

        self.dialog = tk.Toplevel(parent)
        self.dialog.title("Settings")
        self.dialog.configure(bg="#0f0f0f")
        self.dialog.resizable(False, False)
        self.dialog.transient(parent)
        self.dialog.grab_set()
        self.dialog.attributes('-topmost', True)

        # Build first, then size to fit: the credential section carries
        # explanatory text whose height depends on the font, so a fixed height
        # either clips it or leaves dead space.
        self._create_ui()
        self.dialog.update_idletasks()
        width = 360
        height = min(self._frame.winfo_reqheight() + 40,
                     self.dialog.winfo_screenheight() - 120)

        # Centre on the parent, then keep it on the monitor: the panel sits near
        # the tray, so a tall dialog would otherwise hang off the bottom edge
        # and hide its own Save button.
        x = parent.winfo_x() + (parent.winfo_width() - width) // 2
        y = parent.winfo_y() + (parent.winfo_height() - height) // 2
        x = max(0, min(x, self.dialog.winfo_screenwidth() - width))
        y = max(0, min(y, self.dialog.winfo_screenheight() - height - 60))
        self.dialog.geometry(f"{width}x{height}+{x}+{y}")

        # Ensure dialog is focused and on top
        self.dialog.lift()
        self.dialog.focus_force()
        self.dialog.focus_set()

        # Handle dialog close (restore parent topmost)
        self.dialog.protocol("WM_DELETE_WINDOW", self._on_dialog_close)

    def _create_ui(self):
        """Create the settings UI."""
        self._frame = tk.Frame(self.dialog, bg="#0f0f0f")
        frame = self._frame
        frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=20)

        # Title
        title = tk.Label(frame, text="Settings",
                        font=("Segoe UI", 14, "bold"),
                        fg="#ffffff", bg="#0f0f0f")
        title.pack(anchor=tk.W, pady=(0, 20))

        # Engines section
        engines_label = tk.Label(frame, text="Enabled Engines",
                                font=("Segoe UI", 10),
                                fg=self.label_color, bg="#0f0f0f")
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

        self.deepseek_var = tk.BooleanVar(value=self.config.deepseek_enabled)
        deepseek_cb = tk.Checkbutton(engines_frame, text="DeepSeek",
                                     variable=self.deepseek_var,
                                     font=("Segoe UI", 10),
                                     fg="#ffffff", bg="#0f0f0f",
                                     activebackground="#0f0f0f",
                                     activeforeground="#ffffff",
                                     selectcolor="#1a1a1a")
        deepseek_cb.pack(side=tk.LEFT, padx=(10, 0))

        self._create_deepseek_credentials(frame)

        # Currency selection
        currency_frame = tk.Frame(frame, bg="#0f0f0f")
        currency_frame.pack(fill=tk.X, pady=(0, 15))

        currency_label = tk.Label(currency_frame, text="Currency",
                                 font=("Segoe UI", 10),
                                 fg=self.label_color, bg="#0f0f0f")
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
                                font=("Segoe UI", 8),
                                fg=self.hint_color, bg="#0f0f0f")
        currency_info.pack(anchor=tk.W, pady=(5, 0))

        # Refresh interval
        refresh_frame = tk.Frame(frame, bg="#0f0f0f")
        refresh_frame.pack(fill=tk.X, pady=(0, 15))

        refresh_label = tk.Label(refresh_frame, text="Refresh Interval (seconds)",
                                font=("Segoe UI", 10),
                                fg=self.label_color, bg="#0f0f0f")
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

    def _hint(self, parent, text, colour=None):
        """A wrapped explanatory line under a field."""
        tk.Label(parent, text=text, font=("Segoe UI", 8), wraplength=320,
                 justify=tk.LEFT, fg=colour or self.hint_color,
                 bg="#0f0f0f").pack(anchor=tk.W)

    def _create_deepseek_credentials(self, frame):
        """DeepSeek needs two credentials, and they buy different things.

        Kept deliberately short. This is a settings panel, not a manual; a wall
        of small grey text is harder to read than saying less.
        """
        section = tk.Frame(frame, bg="#0f0f0f")
        section.pack(fill=tk.X, pady=(0, 15))

        self._hint(section, "Signing in gives the balance and the usage figures.")

        tk.Label(section, text="DeepSeek account", font=("Segoe UI", 9),
                 fg=self.label_color, bg="#0f0f0f").pack(anchor=tk.W, pady=(10, 0))
        signin_row = tk.Frame(section, bg="#0f0f0f")
        signin_row.pack(fill=tk.X, pady=(4, 0))
        self._signin_btn = tk.Button(
            signin_row, text="Sign in to DeepSeek…", command=self._start_deepseek_signin,
            font=("Segoe UI", 9), bg="#2a2a2a", fg="#ffffff",
            activebackground="#3a3a3a", activeforeground="#ffffff",
            relief=tk.FLAT, padx=12, pady=4, cursor="hand2")
        self._signin_btn.pack(side=tk.LEFT)
        self._hint(section, "Opens a browser you sign into. Your password stays with DeepSeek.")

        # Its own full-width line, wrapping. Sharing the row with the button
        # clipped anything longer than a short status, and an error rarely is.
        self._signin_status = tk.Label(section, text="", font=("Segoe UI", 8),
                                       fg=self.hint_color, bg="#0f0f0f",
                                       wraplength=320, justify=tk.LEFT, anchor=tk.W)
        self._signin_status.pack(fill=tk.X, pady=(4, 0))

        tk.Label(section, text="or paste a session token", font=("Segoe UI", 9),
                 fg=self.label_color, bg="#0f0f0f").pack(anchor=tk.W, pady=(8, 0))
        self.deepseek_token_var = tk.StringVar()
        tk.Entry(section, textvariable=self.deepseek_token_var, show="•", width=34,
                 bg="#1a1a1a", fg="#ffffff", insertbackground="#ffffff",
                 relief=tk.FLAT).pack(anchor=tk.W, pady=(2, 0))

        # One action, not two. It clears what ClaudeBar stores and the browser
        # profile ClaudeBar created; the user's own browser is never touched.
        actions = tk.Frame(section, bg="#0f0f0f")
        actions.pack(fill=tk.X, pady=(10, 0))
        tk.Button(actions, text="Forget sign-in", command=self._forget_signin,
                  font=("Segoe UI", 9), bg="#2a2a2a", fg="#ffffff",
                  activebackground="#3a3a3a", activeforeground="#ffffff",
                  relief=tk.FLAT, padx=12, pady=4, cursor="hand2").pack(side=tk.LEFT)
        self._credential_note = tk.Label(actions, text="", font=("Segoe UI", 8),
                                         fg=self.hint_color, bg="#0f0f0f")
        self._credential_note.pack(side=tk.LEFT, padx=(8, 0))
        self._hint(section, "Kept encrypted with Windows DPAPI. Only ClaudeBar's own "
                            "browser profile is cleared, never your browser.")

    # ---- DeepSeek sign-in -------------------------------------------------

    def _start_deepseek_signin(self):
        """Start sign-in, surfacing any failure in the status line.

        Tkinter reports a callback exception to stderr, which a windowed build
        has nowhere to show, so a dead button would be the only symptom.
        """
        try:
            self._begin_signin()
        except Exception as exc:                      # noqa: BLE001 - reported, not swallowed
            logger.exception("DeepSeek sign-in could not start")
            self._signin_failed(f"{type(exc).__name__}: {exc}")

    def _begin_signin(self):
        if self._signin_thread and self._signin_thread.is_alive():
            return
        if not browser_signin.find_browser():
            self._signin_status.config(
                text="No Chromium browser found · paste a token below",
                fg=self.warn_color)
            return

        process = browser_signin.launch()
        if process is None:
            self._signin_status.config(text="Could not open the browser", fg=self.warn_color)
            return

        logger.info("DeepSeek sign-in: opened %s against our own profile",
                    browser_signin.find_browser())
        self._signin_cancel = False
        self._signin_result = None
        self._signin_btn.config(state=tk.DISABLED)
        self._signin_status.config(text="Waiting for sign-in…", fg=self.hint_color)
        self._signin_thread = threading.Thread(target=self._signin_worker,
                                               args=(process,), daemon=True)
        self._signin_thread.start()
        # Tk is not thread-safe, so the main thread drives the UI and the
        # worker only leaves a result behind.
        self._poll_signin()

    def _poll_signin(self):
        """Main-thread poll for the worker's result."""
        result = self._signin_result
        if result is None:
            try:
                if self.dialog.winfo_exists():
                    self.dialog.after(250, self._poll_signin)
            except tk.TclError:
                pass
            return
        status, payload = result
        if status == "ok":
            self._signin_succeeded(payload)
        else:
            self._signin_failed(payload)

    def _signin_worker(self, process):
        """Background: wait for the browser to yield a session, then keep it.

        Touches no widgets; it only sets `_signin_result` for the main thread.
        """
        try:
            token = browser_signin.wait_for_token(
                timeout=600, interval=2.0, should_stop=lambda: self._signin_cancel)

            if not token:
                logger.info("DeepSeek sign-in: gave up waiting after %.0fs", 600)
                # Leave the window up: the user may still be typing, and
                # closing it under them would be worse than a stale window.
                self._signin_result = (
                    "fail", "Cancelled" if self._signin_cancel
                    else "Timed out; the browser is still open")
                return

            logger.info("DeepSeek sign-in: session token captured, validating")
            # Close every window on our profile, not just the one we spawned:
            # a repeat launch delegates to the running instance and exits, so
            # the process we hold is usually already gone.
            browser_signin.close()

            # Prove the token works before storing it, so a stale profile
            # cannot masquerade as a completed sign-in.
            summary = deepseek_usage.fetch_summary(token)
            if summary.error:
                logger.warning("DeepSeek sign-in: token rejected: %s", summary.error)
                self._signin_result = ("fail", summary.error)
                return

            if not deepseek_auth.save_user_token(token):
                self._signin_result = ("fail", "Could not store the token securely")
                return

            logger.info("DeepSeek sign-in: signed in, balance %.2f %s",
                        summary.total, summary.currency)
            self._signin_result = ("ok", summary)
        except Exception as exc:                      # noqa: BLE001 - reported, not swallowed
            logger.exception("DeepSeek sign-in failed")
            self._signin_result = ("fail", f"{type(exc).__name__}: {exc}")

    def _signin_failed(self, message: str):
        self._signin_btn.config(state=tk.NORMAL)
        self._signin_status.config(text=message, fg=self.warn_color)

    def _signin_succeeded(self, summary):
        self._signin_btn.config(state=tk.NORMAL)
        self._signin_status.config(
            text=f"Signed in · balance {summary.currency} {summary.total:.2f}",
            fg=self.ok_color)
        self._credential_note.config(text="Session stored")
        if self.on_refresh:
            self.on_refresh()

    def _forget_signin(self):
        """Drop every DeepSeek credential ClaudeBar holds, and its browser profile.

        One action on purpose: two buttons that differ only in how much they
        forget invite the wrong click. The profile is ClaudeBar's own, under
        its config directory; `forget` proves that before deleting anything, so
        the user's real browser is never a target.
        """
        deepseek_auth.clear_credentials()
        cleared = browser_signin.forget()
        self.deepseek_token_var.set("")
        self._credential_note.config(
            text="Signed out" if cleared else "Credentials cleared")
        if self.on_refresh:
            self.on_refresh()

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
        deepseek_enabled = self.deepseek_var.get()

        if not claude_enabled and not codex_enabled and not deepseek_enabled:
            self.error_label.config(text="At least one engine must be enabled")
            return

        self.config.claude_enabled = claude_enabled
        self.config.codex_enabled = codex_enabled
        self.config.deepseek_enabled = deepseek_enabled
        self.config.currency = self.currency_var.get()
        try:
            self.config.refresh_interval = int(self.refresh_var.get())
        except ValueError:
            pass

        save_config(self.config)

        # Credentials never touch config.json; a blank field keeps whatever is
        # already stored (or auto-detected) rather than clearing it.
        entered_token = self.deepseek_token_var.get().strip()
        if entered_token and not deepseek_auth.save_user_token(entered_token):
            self.error_label.config(text="Could not save the platform token securely")

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
        self._deepseek_snapshot: Optional[DeepSeekSnapshot] = None
        self._combined_snapshot: Optional[CombinedSnapshot] = None
        self._pending_snapshot: Optional[UsageSnapshot] = None  # Thread-safe pending update
        self._pending_openai_snapshot: Optional[OpenAISnapshot] = None
        self._pending_deepseek_snapshot: Optional[DeepSeekSnapshot] = None
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
        self._engine_btns: dict = {}       # Engine -> selector Label
        self._engine_icons: dict = {}      # Engine -> (active, inactive) PhotoImage
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
        # Lifted from #6b7280: the lines under each figure were a strain to read
        # on a 4K panel at 100% scaling.
        self.text_muted = "#7f8899"
        self.separator_color = "#2a2a2a"
        self.accent_color = "#F59E0B"  # Claude orange
        self.active_engine_bg = "#2a2a2a"  # Active engine button background
        self.inactive_engine_bg = "#0f0f0f"  # Inactive engine button background
        self.ok_color = "#22c55e"
        self.warn_color = "#ef4444"
        # Fonts: resolved in _create_window, font.families() needs a Tk root.
        self.sans = "Segoe UI"
        self.mono = "Consolas"

    def _load_engine_glyph(self, local_path: Path, url: str) -> Optional[Image.Image]:
        """Load a provider mark, downloading and caching it on a miss."""
        try:
            if not local_path.exists():
                try:
                    req = urllib.request.Request(url, headers={"User-Agent": "ClaudeBar/1.0"})
                    with urllib.request.urlopen(req, timeout=5) as response:
                        data = response.read()
                    img = Image.open(io.BytesIO(data))
                    local_path.parent.mkdir(parents=True, exist_ok=True)
                    img.save(local_path)
                except Exception:
                    return None
            with Image.open(local_path) as img:
                return _monochrome_glyph(img)
        except Exception:
            return None

    def _engine_icon_images(self, engine: Engine) -> Optional[tuple]:
        """(active, inactive) PhotoImages for one engine, or None.

        Both states come from one mask, so only the tint differs. The caller
        must keep the returned images alive or Tk will collect them.
        """
        _, local_path, domain, _ = _ENGINE_META[engine]
        mask = self._load_engine_glyph(local_path, _icon_url(domain))
        if mask is None:
            return None
        mask = mask.resize((_GLYPH_SIZE, _GLYPH_SIZE), Image.Resampling.LANCZOS)
        images = []
        for colour in (self.text_primary, self.text_muted):
            tinted = Image.new("RGBA", mask.size, colour)
            tinted.putalpha(mask)
            images.append(ImageTk.PhotoImage(tinted))
        return tuple(images)

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
        # Host for the region holding either the rate-limit rows or the DeepSeek
        # balance block. Children pack and unpack inside it, so re-showing one
        # never reorders the panel around it.
        self._limits_host = tk.Frame(content, bg=self.bg_color)
        self._limits_host.pack(fill=tk.X)
        self._create_limits(self._limits_host)
        self._create_balance(self._limits_host)
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

        # Width follows the content too. It only ever grows, so switching engine
        # tabs does not make the panel jump, and a larger system font widens the
        # panel instead of running the footer into the buttons.
        self._window_width = min(
            max(self._window_width, self._main_frame.winfo_reqwidth() + 4),
            self._window.winfo_screenwidth() - 40)

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
        """Get list of enabled engines from config, in selector order."""
        return [engine for engine in _ENGINE_META
                if getattr(self.config, _ENABLED_FLAG[engine], True)]

    # Every label in the panel goes through here, so the size bump that keeps
    # the panel legible on a 4K monitor lives in exactly one place.
    FONT_BUMP = 2

    def _font(self, size, weight="normal", mono=False):
        return (self.mono if mono else self.sans, size + self.FONT_BUMP, weight)

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
            for engine in enabled_engines:
                name = _ENGINE_META[engine][0]
                # The icon carries the button; the text is the fallback when a
                # mark is missing and could not be downloaded.
                icons = self._engine_icon_images(engine)
                if icons:
                    self._engine_icons[engine] = icons
                    btn = tk.Label(self._engine_frame, image=icons[0], bg=self.inactive_engine_bg,
                                   padx=8, pady=3, cursor="hand2")
                else:
                    btn = tk.Label(self._engine_frame, text=name, font=self._font(8),
                                   fg=self.text_primary, bg=self.inactive_engine_bg,
                                   padx=10, pady=3, cursor="hand2")
                btn.pack(side=tk.LEFT)
                btn.bind("<Button-1>", lambda e, en=engine: self._on_engine_select(en))
                self._engine_btns[engine] = btn
            self._paint_engine_buttons()

    def _paint_engine_buttons(self):
        for engine, btn in self._engine_btns.items():
            if not btn:
                continue
            on = self._active_engine == engine
            btn.config(bg=self.active_engine_bg if on else self.inactive_engine_bg)
            icons = self._engine_icons.get(engine)
            if icons:
                btn.config(image=icons[0] if on else icons[1])
            else:
                btn.config(fg=self.text_primary if on else self.text_muted,
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
        # The dot carries the whole connection state at a glance, so it runs
        # well ahead of the text beside it and sits on its own line box.
        self._status_indicator = tk.Label(status.master, text="\u25cf",
                                          font=self._font(13, "bold"),
                                          fg=self.text_muted, bg=self.bg_color)
        self._status_indicator.pack(side=tk.RIGHT, padx=(0, 6))

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

    def _create_balance(self, parent):
        """DeepSeek balance block. Stands in the row where the limits sit,
        because DeepSeek exposes no session or weekly quota."""
        self._balance_frame = tk.Frame(parent, bg=self.bg_color)
        _, _, self._balance_note = self._row(self._balance_frame, "Balance", "", self._font(9, "bold"),
                                             self._font(8), self.text_primary, self.text_muted)
        self._balance_value = tk.Label(self._balance_frame, text="", font=self._font(16, "bold", mono=True),
                                       fg=self.text_primary, bg=self.bg_color)
        self._balance_value.pack(anchor=tk.W, pady=(2, 0))
        # The state line used to carry a currency code on the right. The figure
        # above already shows its symbol, so that only repeated itself, and the
        # footer notes the case where the platform bills in something else.
        self._balance_state = tk.Label(self._balance_frame, text="", font=self._font(8),
                                       fg=self.ok_color, bg=self.bg_color)
        self._balance_state.pack(anchor=tk.W)

    def _create_stat(self, parent, key, title, row, column):
        f = tk.Frame(parent, bg=self.bg_color)
        # The gap between the two rows rides on the upper one.
        f.grid(row=row, column=column, sticky="ew", pady=(0, 8) if row == 0 else 0)
        heading = tk.Label(f, text=title, font=self._font(8), fg=self.text_muted, bg=self.bg_color)
        heading.pack(anchor=tk.W)
        value = tk.Label(f, text="", font=self._font(12, "bold", mono=True), fg=self.text_primary, bg=self.bg_color)
        value.pack(anchor=tk.W)
        sub = tk.Label(f, text="", font=self._font(8), fg=self.text_muted, bg=self.bg_color)
        sub.pack(anchor=tk.W)
        self._stats[key] = (heading, value, sub)

    def _create_stats(self, parent):
        """2x2 grid of stat cells. Titles and figures are set per engine; the
        slots only fix the positions."""
        grid = tk.Frame(parent, bg=self.bg_color)
        grid.pack(fill=tk.X)
        # Both columns share one uniform width. Laying each row out on its own
        # let the split fall at a different x per row, so the right-hand cells
        # did not line up whenever one row's text measured wider than the
        # other's ("377.9M tokens" against "2.73B tokens").
        grid.columnconfigure(0, weight=1, uniform="stat")
        grid.columnconfigure(1, weight=1, uniform="stat")
        cells = (("today", "Today"),
                 ("last31", f"Last {DAILY_HISTORY_DAYS} days"),
                 ("month", "This month"),
                 ("output", "Output today"))
        for index, (key, title) in enumerate(cells):
            self._create_stat(grid, key, title, row=index // 2, column=index % 2)

    def _create_chart(self, parent):
        _, self._chart_title, self._chart_note = self._row(
            parent, "Daily API-equivalent cost", "", self._font(8),
            self._font(8), self.text_muted, self.text_muted)
        tk.Frame(parent, bg=self.bg_color, height=4).pack()
        self._chart = BarChart(parent, width=348, bg=self.bg_color, font=self._font(7),
                               label_fg=self.text_muted)
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
            self._engine_name.config(text=_ENGINE_META[self._active_engine][0])

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
                self._status_indicator.config(fg=self.ok_color)
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
                self._status_indicator.config(fg=self.ok_color)
                text = "Connected"
                plan = self._claude_status.plan
                if plan:
                    text += f" · {plan}"
                self._status_text.config(text=text)
                return

            # Not authenticated via CLI - show specific error
            if oauth_error:
                self._status_indicator.config(fg=self.warn_color)
                if "refresh failed" in oauth_error.lower() or "token expired" in oauth_error.lower():
                    self._status_text.config(text="Session expired")
                elif "not found" in oauth_error.lower() or "no oauth" in oauth_error.lower():
                    self._status_text.config(text="Not logged in")
                elif "429" in oauth_error or "restricted" in oauth_error.lower() or "rate limit" in oauth_error.lower():
                    self._status_indicator.config(fg=self.accent_color)
                    self._status_text.config(text="API restricted")
                else:
                    self._status_text.config(text="Connection error")
                return

            # Fall back to claude_status check for non-authenticated states
            if self._claude_status:
                if self._claude_status.installed:
                    self._status_indicator.config(fg=self.accent_color)
                    text = "Not logged in"
                else:
                    self._status_indicator.config(fg=self.warn_color)
                    text = "Claude CLI not found"
                self._status_text.config(text=text)
            else:
                self._status_indicator.config(fg=self.text_muted)
                self._status_text.config(text="Checking...")
        elif self._active_engine == Engine.CODEX:
            # Show Codex connection status
            if self._openai_snapshot:
                if self._openai_snapshot.available:
                    self._status_indicator.config(fg=self.ok_color)
                    text = "Connected"
                    # Show plan type if available
                    if self._openai_snapshot.plan_type:
                        text += f" · {_plan_label(self._openai_snapshot.plan_type)}"
                    elif self._openai_snapshot.credits_remaining is not None:
                        text += f" · ${self._openai_snapshot.credits_remaining:.2f} credits"
                    self._status_text.config(text=text)
                elif self._openai_snapshot.error_message:
                    self._status_indicator.config(fg=self.warn_color)
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
                    self._status_indicator.config(fg=self.accent_color)
                    self._status_text.config(text="Not configured")
            else:
                self._status_indicator.config(fg=self.text_muted)
                self._status_text.config(text="Checking...")
        elif self._active_engine == Engine.DEEPSEEK:
            # The status row reports the connection only. The balance has its own
            # block below, with the top-up, grant and spend breakdown, so naming
            # it here just said the same figure twice on one screen.
            snap = self._deepseek_snapshot
            if snap is None:
                self._status_indicator.config(fg=self.text_muted)
                self._status_text.config(text="Checking...")
            elif snap.balance_available:
                if snap.usage_available:
                    self._status_indicator.config(fg=self.ok_color)
                    text = "Connected"
                else:
                    self._status_indicator.config(fg=self.accent_color)
                    text = "Connected \u00b7 balance only"
                self._status_text.config(text=text)
            elif snap.error_message == "Not signed in":
                self._status_indicator.config(fg=self.text_muted)
                self._status_text.config(text="Not signed in")
            elif snap.usage_error == "Session expired":
                self._status_indicator.config(fg=self.warn_color)
                self._status_text.config(text="Session expired")
            else:
                self._status_indicator.config(fg=self.warn_color)
                self._status_text.config(text=snap.balance_message)

    def _on_refresh_click(self):
        """Handle refresh button."""
        self.on_refresh()
        self._load_initial_data()

    def _on_settings_click(self):
        """Handle settings button."""
        if self._window:
            SettingsDialog(self._window, self.config, self._on_settings_saved,
                           on_refresh=self.on_refresh)

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
            self._engine_btns = {}
            self._engine_icons = {}
            self._engine_frame = None
            self._layout_key = None

        # Recreate and show
        self._create_window()
        if self._snapshot:
            self.update(self._snapshot)
        if self._openai_snapshot:
            self.update_openai(self._openai_snapshot)
        if self._deepseek_snapshot:
            self.update_deepseek(self._deepseek_snapshot)
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

    def update_deepseek(self, snapshot: DeepSeekSnapshot):
        """Queue DeepSeek snapshot update (thread-safe)."""
        with self._lock:
            self._pending_deepseek_snapshot = snapshot

    def update_combined(self, combined: CombinedSnapshot):
        """Queue combined snapshot update (thread-safe)."""
        with self._lock:
            self._pending_snapshot = combined.claude
            self._pending_openai_snapshot = combined.openai
            self._pending_deepseek_snapshot = combined.deepseek
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
            if self._pending_deepseek_snapshot:
                self._deepseek_snapshot = self._pending_deepseek_snapshot
                self._pending_deepseek_snapshot = None
                updated = True

        if not updated:
            return

        self._refresh_display_immediate()

    def _refresh_display_immediate(self):
        """Fill every widget from the active engine's snapshot."""
        if not self._window or not self._window.winfo_exists():
            return

        money = lambda usd: format_currency(usd, self.config.currency, self._exchange_rates)
        engine = self._active_engine
        self._apply_accent(engine)

        if engine == Engine.CLAUDE and self._snapshot:
            self._fill_claude_view(self._snapshot, money)
        elif engine == Engine.CODEX and self._openai_snapshot:
            self._fill_codex_view(self._openai_snapshot, money)
        elif engine == Engine.DEEPSEEK and self._deepseek_snapshot:
            self._fill_deepseek_view(self._deepseek_snapshot, money)

    def _apply_accent(self, engine: Engine):
        """Each engine tints the shared bars and chart with its own colour."""
        accent = _ENGINE_META[engine][3]
        for widget in (self._limits["session"]["bar"], self._limits["weekly"]["bar"],
                       self._extra_bar, self._chart):
            if widget is not None:
                widget.set_accent(accent)

    def _show_limits(self, session_visible: bool, extra_visible: bool):
        """Show the rate-limit rows and hide the DeepSeek balance block."""
        if self._balance_frame.winfo_manager():
            self._balance_frame.pack_forget()

        session_frame = self._limits["session"]["frame"]
        weekly_frame = self._limits["weekly"]["frame"]

        if not weekly_frame.winfo_manager():
            weekly_frame.pack(fill=tk.X)
        if session_visible and not session_frame.winfo_manager():
            session_frame.pack(fill=tk.X, pady=(0, 8), before=weekly_frame)
        elif not session_visible and session_frame.winfo_manager():
            session_frame.pack_forget()

        if extra_visible and not self._extra_frame.winfo_manager():
            self._extra_frame.pack(fill=tk.X, after=weekly_frame)
        elif not extra_visible and self._extra_frame.winfo_manager():
            self._extra_frame.pack_forget()

    def _show_balance(self) -> None:
        """Show the balance block, which stands where the limits would be."""
        for widget in (self._limits["session"]["frame"], self._limits["weekly"]["frame"],
                       self._extra_frame):
            if widget.winfo_manager():
                widget.pack_forget()
        if not self._balance_frame.winfo_manager():
            self._balance_frame.pack(fill=tk.X, pady=(0, 8))

    def _fill_claude_view(self, snap: UsageSnapshot, money):
        collecting = (self._is_collecting and snap.session_percent == 0 and snap.weekly_percent == 0
                      and not snap.is_stale)
        unavailable = not snap.cli_available and not snap.is_stale
        placeholder = "Collecting..." if collecting else ("Unavailable" if unavailable else None)

        self._fill_limit("session", snap.session_percent, snap.session_reset,
                         snap.session_resets_at, SESSION_WINDOW_HOURS, placeholder)
        self._fill_limit("weekly", snap.weekly_percent, snap.weekly_reset,
                         snap.weekly_resets_at, WEEKLY_WINDOW_HOURS, placeholder)
        self._show_limits(session_visible=True, extra_visible=snap.extra_enabled)

        if snap.extra_enabled:
            symbol = get_currency_symbol(snap.extra_currency.upper())
            self._extra_bar.set_value(snap.extra_percent)
            self._extra_amount.config(
                text=f"{snap.extra_percent:.0f}% used \u00b7 {symbol}{snap.extra_used:.2f} / {symbol}{snap.extra_limit:.2f}")

        self._set_stat("today", money(snap.today_cost_usd),
                       f"{format_tokens(snap.today_tokens.total_tokens)} tokens", "Today")
        self._set_stat("last31", money(snap.last31_cost_usd),
                       f"{format_tokens(snap.last31_tokens)} tokens", f"Last {DAILY_HISTORY_DAYS} days")
        self._set_stat("month", money(snap.month_cost_usd),
                       f"{format_tokens(snap.month_tokens.total_tokens)} tokens", "This month")
        self._set_stat("output", format_tokens(snap.today_tokens.output_tokens),
                       f"{format_tokens(snap.today_tokens.cache_read_input_tokens)} cache reads",
                       "Output today")

        week, labels = self._week_series(snap.daily_costs, snap.timestamp.date())
        self._fill_chart(week, labels, [f"{labels[i]} \u00b7 {money(v)}" for i, v in enumerate(week)],
                         "Daily API-equivalent cost", money)
        self._fill_top_model(snap.models_used, snap.month_cost_usd)
        self._fill_meta(snap, "Estimated from local Claude Code logs", f"{snap.pricing_source} rates")
        self._apply_layout(("claude", snap.extra_enabled))

    def _fill_codex_view(self, snap: OpenAISnapshot, money):
        collecting = (self._is_collecting and snap.session_percent == 0 and snap.weekly_percent == 0
                      and not snap.is_stale)
        placeholder = "Collecting..." if collecting else None

        self._fill_limit("session", snap.session_percent, snap.session_reset,
                         snap.session_resets_at, SESSION_WINDOW_HOURS, placeholder)
        self._fill_limit("weekly", snap.weekly_percent, snap.weekly_reset,
                         snap.weekly_resets_at, WEEKLY_WINDOW_HOURS, placeholder)
        self._show_limits(session_visible=snap.session_available, extra_visible=False)

        self._set_stat("today", money(snap.today_cost_usd),
                       f"{format_tokens(snap.today_total_tokens)} tokens", "Today")
        self._set_stat("last31", money(snap.last31_cost_usd),
                       f"{format_tokens(snap.last31_tokens)} tokens", f"Last {DAILY_HISTORY_DAYS} days")
        self._set_stat("month", money(snap.month_cost_usd),
                       f"{format_tokens(snap.month_total_tokens)} tokens", "This month")
        self._set_stat("output", format_tokens(snap.today_output_tokens),
                       f"{format_tokens(snap.today_cached_tokens)} cache reads", "Output today")

        week, labels = self._week_series(snap.daily_costs, snap.timestamp.date())
        self._fill_chart(week, labels, [f"{labels[i]} \u00b7 {money(v)}" for i, v in enumerate(week)],
                         "Daily API-equivalent cost", money)
        self._fill_top_model([], 0.0)
        self._fill_meta(snap, "Estimated from local Codex logs", f"{snap.pricing_source} rates")
        self._apply_layout(("codex", snap.session_available))

    def _money_fixed(self, usd: float) -> str:
        """Money with two decimals. format_currency drops to four below a cent,
        which is right for a day's spend but wrong for a balance."""
        symbol = get_currency_symbol(self.config.currency)
        return f"{symbol}{convert_usd(usd, self.config.currency, self._exchange_rates):,.2f}"

    def _fill_deepseek_view(self, snap: DeepSeekSnapshot, money):
        """DeepSeek has no quota, so its limits give way to balance and spend."""
        self._show_balance()

        self._balance_value.config(text=self._money_fixed(snap.balance_total) if snap.balance_available else "\u2014")
        if snap.balance_available:
            # Balance is what is left; the lifetime total is what was spent.
            # They come from different endpoints, so either can be missing.
            note = (f"Topped up {self._money_fixed(snap.balance_topped_up)} \u00b7 "
                    f"Granted {self._money_fixed(snap.balance_granted)}")
            if snap.total_cost_available:
                note += f" \u00b7 Spent {self._money_fixed(snap.total_cost_usd)}"
            self._balance_note.config(text=note)
            self._balance_state.config(text=snap.balance_message,
                                       fg=self.ok_color if snap.balance_usable else self.warn_color)
        else:
            self._balance_note.config(text="")
            self._balance_state.config(text=snap.balance_message, fg=self.warn_color)

        # Four windows, read left to right and top to bottom: each cell is a
        # cost with the token count for the same window underneath it. The slot
        # keys are positions in the shared grid, not the windows they show here.
        windows = (
            ("today", "Today", snap.today_cost_usd, snap.today_tokens),
            ("last31", "This month", snap.month_cost_usd, snap.month_tokens),
            ("month", "Last 7 days", snap.last7_cost_usd, snap.last7_tokens),
            ("output", "Prev month", snap.prev_month_cost_usd, snap.prev_month_tokens),
        )
        if snap.usage_available:
            for key, title, cost, tokens in windows:
                self._set_stat(key, money(cost), f"{format_tokens(tokens)} tokens", title)
        else:
            hint = "Sign in to see usage" if snap.usage_error else "No usage reported"
            for key, title, _, _ in windows:
                self._set_stat(key, "\u2014", hint, title)

        week, labels = self._week_series(snap.daily_costs, snap.timestamp.date(),
                                         dates=snap.daily_dates)
        tooltips = []
        for i, value in enumerate(week):
            parts = [f"{labels[i]} \u00b7 {money(value)}"]
            if i < len(snap.daily_tokens):
                parts.append(f"{format_tokens(snap.daily_tokens[i])} tokens")
            if i < len(snap.daily_requests):
                parts.append(f"{snap.daily_requests[i]:,} req")
            tooltips.append(" \u00b7 ".join(parts))
        self._fill_chart(week, labels, tooltips, "Daily spend, last 7 days", money)
        self._fill_top_model(snap.models_used, snap.month_cost_usd)

        # Both money sources normalise to USD upstream; say so when the account
        # is billed in something else, rather than showing a silent conversion.
        source_currencies = {snap.usage_currency if snap.usage_available else None,
                             snap.balance_currency if snap.balance_available else None}
        foreign = sorted(c for c in source_currencies if c and c != "USD")
        rates = f"converted from {', '.join(foreign)}" if foreign else ""
        if snap.usage_available:
            source = "Official DeepSeek platform usage"
        elif snap.balance_available:
            source = "Balance only · sign in for usage"
        else:
            source = "Sign in to DeepSeek in Settings"
        self._fill_meta(snap, source, rates)
        self._apply_layout(("deepseek", snap.usage_available))

    @staticmethod
    def _week_series(values, today, dates=()) -> tuple[list, list]:
        """Trailing 7 values plus weekday labels, oldest first.

        DeepSeek supplies its own dates (the platform buckets by its own day),
        so those win when present; otherwise the labels count back from today.
        """
        week = list(values)[-7:]
        labels = []
        for i in range(len(week)):
            index = len(week) - 1 - i
            iso = dates[index] if index < len(dates) else None
            stamp = None
            if iso:
                try:
                    stamp = datetime.strptime(iso, "%Y-%m-%d")
                except ValueError:
                    stamp = None
            labels.append((stamp or (today - timedelta(days=index))).strftime("%a"))
        return week, labels

    def _fill_chart(self, values, labels, tooltips, title, money):
        self._chart_title.config(text=title)
        self._chart.set_values(values, labels, tooltips)
        self._chart_note.config(text=f"7 days, peak {money(max(values))}" if values else "")

    def _fill_top_model(self, models, month_cost: float):
        top = max(models, key=lambda m: m.cost_usd, default=None)
        if top and month_cost > 0:
            self._top_model.config(text=f"Top model  {top.model}")
            self._top_share.config(text=f"{top.cost_usd / month_cost * 100:.0f}% of month")
        else:
            self._top_model.config(text="")
            self._top_share.config(text="")

    def _fill_meta(self, snap, source_text: str, rates_text: str):
        self._rates_label.config(text=rates_text)
        if self._is_collecting and snap.is_stale:
            self._updated_label.config(text="Collecting fresh data...")
        else:
            self._updated_label.config(text=f"Updated {snap.timestamp.strftime('%H:%M')}")
        # Clear once any snapshot has been applied (stale or fresh); the stale
        # label keeps communicating staleness.
        self._is_collecting = False
        self._stale_label.config(
            text=f"(cached from {get_staleness_text(snap.stale_since)})" if snap.is_stale and snap.stale_since else "")
        self._source_label.config(text=source_text)

    def _apply_layout(self, layout_key):
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

    def _set_stat(self, key, value, sub, title=None):
        heading, v, s = self._stats[key]
        if title is not None:
            heading.config(text=title)
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
