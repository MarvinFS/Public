"""Premium popup window UI for ClaudeBar."""

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk
from typing import Optional, Callable
import contextlib
import ctypes
import math
import threading
import time
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageTk

from models import (UsageSnapshot, OpenAISnapshot, DeepSeekSnapshot, CombinedSnapshot, Engine,
                    project_window, reset_clock, SESSION_WINDOW_HOURS, WEEKLY_WINDOW_HOURS, DAILY_HISTORY_DAYS)
from config import (Config, save_config, get_resources_path, clamp_refresh_interval, MIN_REFRESH_INTERVAL,
                    MAX_REFRESH_INTERVAL, PANEL_VIEWS)
from currency import (
    format_currency, convert_usd, get_exchange_rates, get_supported_currencies,
    get_currency_symbol, ExchangeRates
)
from claude_check import check_claude_status, ClaudeStatus
from pricing import format_tokens
import banked_resets
import reset_redeem
import forecast
import forecast_text
import quota_history
import log_parser
from forecast_chart import ForecastChart, ChartData, nice_ticks
from monitor import Monitor, make_layered, monitor_at, window_picture
from ui_scale import (DROP_ORDER, PANEL_MAX_SCALE, PANEL_MIN_SCALE, Scale, auto_scale, blocks_to_hide, clamp,
                      scale_for_width, width_for_scale, zone_at)
from ui_widgets import PaceBar, BarChart
import logging

import browser_signin
import deepseek_auth
import deepseek_usage

# GitHub repository URL
GITHUB_URL = "https://github.com/MarvinFS/Public/tree/main/claudebar"

# The sign-in path is otherwise silent, which makes a slow capture
# indistinguishable from a dead button.
logger = logging.getLogger("claudebar")


# Plan enum -> friendly label (OpenAI's /usage plan_type, Claude's auth status). Without a map, a raw or new
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


# The status dot's colour by tone, as the name of the window attribute that holds it.
_STATUS_TONES = {"ok": "ok_color", "warn": "warn_color", "attention": "accent_color",
                 "muted": "text_muted"}


def _claude_status_for(snapshot, claude_status, oauth_error: Optional[str]) -> tuple:
    # Connection status is driven by claude auth status (CLI subprocess),
    # independent of whether the OAuth usage API returns data.

    # If we have valid usage data, we're definitely connected
    if snapshot:
        plan = claude_status.plan if claude_status else None
        if not plan and snapshot.extra_enabled:
            plan = "Max"
        return "ok", _plan_label(plan) if plan else ""

    # No usage data - check CLI auth status (primary status source)
    if claude_status and claude_status.authenticated:
        # CLI says we're logged in, even though usage API may be blocked
        return "ok", _plan_label(claude_status.plan) if claude_status.plan else ""

    # Not authenticated via CLI - show specific error
    if oauth_error:
        error = oauth_error.lower()
        if "refresh failed" in error or "token expired" in error:
            return "warn", "Session expired"
        if "not found" in error or "no oauth" in error:
            return "warn", "Not logged in"
        if "429" in oauth_error or "restricted" in error or "rate limit" in error:
            return "attention", "API restricted"
        return "warn", "Connection error"

    # Fall back to claude_status check for non-authenticated states
    if claude_status:
        if claude_status.installed:
            return "attention", "Not logged in"
        return "warn", "Claude CLI not found"
    return "muted", ""


def _codex_status_for(snapshot) -> tuple:
    if snapshot is None:
        return "muted", ""
    if snapshot.available:
        if snapshot.plan_type:
            return "ok", _plan_label(snapshot.plan_type)
        if snapshot.credits_remaining is not None:
            return "ok", f"${snapshot.credits_remaining:.2f} credits"
        return "ok", ""
    if snapshot.error_message:
        # Show shorter error message
        err = snapshot.error_message
        if "Run 'codex login'" in err:
            return "warn", "Not logged in"
        if "token expired" in err.lower():
            return "warn", "Token expired"
        return "warn", "Connection error"
    return "attention", "Not configured"


def _deepseek_status_for(snapshot) -> tuple:
    # The status row reports the connection only. The balance has its own
    # block below, so naming it here said the same figure twice.
    if snapshot is None:
        return "muted", ""
    if snapshot.balance_available:
        if snapshot.usage_available:
            return "ok", ""
        return "attention", "Balance only"
    if snapshot.error_message == "Not signed in":
        return "muted", "Not signed in"
    if snapshot.usage_error == "Session expired":
        return "warn", "Session expired"
    return "warn", snapshot.balance_message


def status_for(engine: Engine, snapshot, claude_status=None, oauth_error: Optional[str] = None) -> tuple:
    """The status row for an engine as (tone, text); the panel colours the dot by tone.

    `snapshot` is the engine's own latest one (for Claude, only one whose usage
    call worked). Tones: "ok" connected, "warn" something is wrong, "attention"
    connected but needs a look, "muted" nothing to report. The text is the plan
    when one is known, and otherwise stays empty until something is wrong.
    `claude_status` and `oauth_error` are read for Claude only.
    """
    if engine == Engine.CLAUDE:
        return _claude_status_for(snapshot, claude_status, oauth_error)
    if engine == Engine.CODEX:
        return _codex_status_for(snapshot)
    return _deepseek_status_for(snapshot)


# Engine icon paths. The three marks ship in resources/icons; a missing one falls back to
# the text button, and nothing is downloaded: that would be network I/O on the Tk thread.
_ICONS_DIR = get_resources_path() / "icons"
_CUSTOM_ICON_PATH = _ICONS_DIR / "app_icon.png"
_CLAUDE_ICON_PATH = _ICONS_DIR / "claude_icon.png"
_OPENAI_ICON_PATH = _ICONS_DIR / "openai_icon.png"
_DEEPSEEK_ICON_PATH = _ICONS_DIR / "deepseek_icon.png"

# engine -> (display name, icon, accent colour)
_ENGINE_META = {
    Engine.CLAUDE: ("Claude", _CLAUDE_ICON_PATH, "#F59E0B"),
    Engine.CODEX: ("Codex", _OPENAI_ICON_PATH, "#F59E0B"),
    Engine.DEEPSEEK: ("DeepSeek", _DEEPSEEK_ICON_PATH, "#4D6BFE"),
}

# engine -> Config attribute that enables it
_ENABLED_FLAG = {
    Engine.CLAUDE: "claude_enabled",
    Engine.CODEX: "codex_enabled",
    Engine.DEEPSEEK: "deepseek_enabled",
}

# The labels of the two panel views, per engine: DeepSeek has spend and a
# balance rather than costs and a quota.
_TAB_TITLES = {
    Engine.CLAUDE: ("Cost", "Quota"),
    Engine.CODEX: ("Cost", "Quota"),
    Engine.DEEPSEEK: ("Spend", "Balance"),
}


def tab_titles(engine: Engine) -> tuple:
    """(cost view, forecast view) labels for an engine's subtabs."""
    return _TAB_TITLES[engine]


# How long the result of spending a banked reset stays on the panel.
_NOTICE_SECONDS = 90

# The panel shows times that move without any new data (a countdown, whether a window
# has ended), so while it is open it is redrawn this often.
_REDRAW_SECONDS = 60

# How often the panel looks for what arrived (a snapshot, a banked-reset result, the status
# flag): quickly while it is on screen, and slowly while it is hidden, where the same work
# only has to be done by the time it is shown again.
_TICK_VISIBLE_MS = 50
_TICK_HIDDEN_MS = 500

# `claude auth status` starts the Claude CLI: how long its answer is reused.
_CLAUDE_STATUS_SECONDS = 60

# What the session says while a cached reading shows none running, yet the logs show requests
# since: a session is in use and its usage is unknown until the next reading.
_SESSION_RUNNING = "Session running"
_SESSION_RUNNING_PACE = "Usage shows with the next reading"
_SESSION_RUNNING_CHART = "Session running - the chart starts with the next reading"
# A log written this recently means someone is at work, even before the first request of the
# new window: a tool call or a long reply can sit between the reset and that request.
_STILL_WORKING = timedelta(minutes=3)

# Quota view: chart height, and how many detail rows the frame can hold.
_FORECAST_HEIGHT = 110
_FORECAST_ROWS = 6

# A point in the panel's header: the panel is on the monitor that holds it.
_HEADER_POINT = (40, 20)

# The outer strip of the panel, in design pixels, where a press resizes it and does not move it,
# and the cursor each of its zones shows.
_RESIZE_MARGIN = 8
_RESIZE_CURSORS = {"n": "size_ns", "s": "size_ns", "e": "size_we", "w": "size_we",
                   "nw": "size_nw_se", "se": "size_nw_se", "ne": "size_ne_sw", "sw": "size_ne_sw"}

# Ctrl+plus, Ctrl+minus and a wheel notch (120 units) change the panel scale by this much.
_SCALE_STEP = 0.1
_WHEEL_NOTCH = 120

# The width check of a resize (_set_scale) aims this far under its estimate at the first miss, and twice as far at
# each miss after. A width is a step of the factor: text goes by whole pixels, so below an estimate it falls by far
# less than in proportion until the next font size, a plateau up to 0.075 of the factor wide. Starting at 0.001
# the five misses reached 0.031 of it, and a drag into the work area's edge then fell back to the panel's own size
# (6 of 129 placements, up to 232 pixels of room left empty); from 0.004 they reach 0.124 and none did.
_FIT_MARGIN = 0.004

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


@lru_cache(maxsize=32)
def _engine_glyph(path: Path, size: int) -> Optional[Image.Image]:
    """A provider mark as a white-on-transparent mask `size` pixels square, or None when the file
    is missing or unreadable. Kept for the life of the process, so a rebuilt panel reuses it."""
    try:
        with Image.open(path) as img:
            mask = _monochrome_glyph(img)
    except Exception:
        return None
    return mask.resize((size, size), Image.Resampling.LANCZOS)


@lru_cache(maxsize=32)
def _logo(size: int) -> Optional[Image.Image]:
    """The app mark `size` pixels square, or None when it is missing or unreadable."""
    try:
        with Image.open(_CUSTOM_ICON_PATH) as img:
            return img.convert("RGBA").resize((size, size), Image.Resampling.LANCZOS)
    except Exception:
        return None


def _monitor_near(widget, x: int, y: int) -> Monitor:
    """The monitor under the screen point (x, y), or the nearest one. When Windows will not say, the
    primary screen as Tk sees it, at 96 dpi."""
    found = monitor_at(x, y)
    if found:
        return found
    width, height = widget.winfo_screenwidth(), widget.winfo_screenheight()
    return Monitor((0, 0, width, height), height, 96)


def _open_url(url: str) -> None:
    """Open a link in the default browser via `start`, the route that has
    proved most reliable from a windowed build."""
    import subprocess
    try:
        subprocess.Popen(['cmd', '/c', 'start', '', url], shell=False,
                         creationflags=subprocess.CREATE_NO_WINDOW)
    except Exception:
        pass


def _round_corners(window) -> None:
    """Ask DWM for Windows 11's rounded corners on a borderless window.

    The attribute belongs on the frame window, the parent of Tk's client hwnd,
    and only once the window has been mapped. Older Windows rejects it and the
    corners stay square.
    """
    try:
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        value = ctypes.c_int(2)  # DWMWCP_ROUND
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            ctypes.c_void_p(hwnd), 33,  # DWMWA_WINDOW_CORNER_PREFERENCE
            ctypes.byref(value), ctypes.sizeof(value))
    except (AttributeError, OSError):
        pass


class _Scaled:
    """The Linux port's helper names: a surface with a `scale` gives Tk every length and padding
    through `_px` and `_spacing`."""

    scale = Scale()

    def _px(self, pixels):
        return self.scale.px(pixels)

    def _spacing(self, value):
        return self.scale.spacing(value)


def _place_over(dialog, parent, width: int, height: int, margin: int) -> None:
    """Size a dialog and centre it on its parent, inside the work area of the monitor it lands on
    (`margin` clear of its edge): the panel sits near the tray, so a tall dialog would otherwise
    hang off the bottom edge and hide its own buttons. `height` is the client area's."""
    x = parent.winfo_x() + (parent.winfo_width() - width) // 2
    y = parent.winfo_y() + (parent.winfo_height() - height) // 2
    left, top, right, bottom = _monitor_near(parent, x + width // 2, y + height // 2).work
    dialog.update_idletasks()
    bar = dialog.winfo_rooty() - dialog.winfo_y()          # the title bar above the client area
    height = min(height, bottom - top - bar - margin)
    x = max(left, min(x, right - width))
    y = max(top, min(y, bottom - height - bar - margin))
    dialog.geometry(f"{width}x{height}+{x}+{y}")


class SettingsDialog(_Scaled):
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

    def _font(self, size, weight="normal"):
        return ("Segoe UI", self.scale.font_px(size), weight)

    def __init__(self, parent, config: Config, on_save: Callable, on_close: Callable = None,
                 on_refresh: Callable = None, session_state: Callable = None,
                 scale: Optional[Scale] = None):
        self.config = config
        self.scale = scale or Scale()
        self.on_save = on_save
        self.on_close = on_close
        # Returns the panel's current DeepSeek snapshot, or None. Asked rather
        # than passed so the answer is current when the dialog reads it, and so
        # there is one judgement of whether the session works - the panel's -
        # instead of a second one here that could disagree with it.
        self._session_state = session_state
        # Fired after a credential change so the panel can refetch immediately
        # rather than waiting for the next scheduled refresh.
        self.on_refresh = on_refresh
        self._signin_thread = None
        self._signin_cancel = False
        self._signin_result = None
        self._forget_thread = None
        self._forget_result = None
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
        _place_over(self.dialog, parent, self._px(360),
                    self._frame.winfo_reqheight() + self._px(40), self._px(12))

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
        frame.pack(fill=tk.BOTH, expand=True, padx=self._px(20), pady=self._px(20))

        # Title
        title = tk.Label(frame, text="Settings",
                        font=self._font(14, "bold"),
                        fg="#ffffff", bg="#0f0f0f")
        title.pack(anchor=tk.W, pady=self._spacing((0, 20)))

        # Engines section
        engines_label = tk.Label(frame, text="Enabled Engines",
                                font=self._font(10),
                                fg=self.label_color, bg="#0f0f0f")
        engines_label.pack(anchor=tk.W)

        engines_frame = tk.Frame(frame, bg="#0f0f0f")
        engines_frame.pack(fill=tk.X, pady=self._spacing((5, 15)))

        self.claude_var = tk.BooleanVar(value=self.config.claude_enabled)
        claude_cb = tk.Checkbutton(engines_frame, text="Claude",
                                   variable=self.claude_var,
                                   font=self._font(10),
                                   fg="#ffffff", bg="#0f0f0f",
                                   activebackground="#0f0f0f",
                                   activeforeground="#ffffff",
                                   selectcolor="#1a1a1a")
        claude_cb.pack(side=tk.LEFT)

        self.codex_var = tk.BooleanVar(value=self.config.codex_enabled)
        codex_cb = tk.Checkbutton(engines_frame, text="Codex",
                                  variable=self.codex_var,
                                  font=self._font(10),
                                  fg="#ffffff", bg="#0f0f0f",
                                  activebackground="#0f0f0f",
                                  activeforeground="#ffffff",
                                  selectcolor="#1a1a1a")
        codex_cb.pack(side=tk.LEFT, padx=self._spacing((10, 0)))

        self.deepseek_var = tk.BooleanVar(value=self.config.deepseek_enabled)
        deepseek_cb = tk.Checkbutton(engines_frame, text="DeepSeek",
                                     variable=self.deepseek_var,
                                     font=self._font(10),
                                     fg="#ffffff", bg="#0f0f0f",
                                     activebackground="#0f0f0f",
                                     activeforeground="#ffffff",
                                     selectcolor="#1a1a1a")
        deepseek_cb.pack(side=tk.LEFT, padx=self._spacing((10, 0)))

        self._create_deepseek_credentials(frame)

        # Currency selection
        currency_frame = tk.Frame(frame, bg="#0f0f0f")
        currency_frame.pack(fill=tk.X, pady=self._spacing((0, 15)))

        currency_label = tk.Label(currency_frame, text="Currency",
                                 font=self._font(10),
                                 fg=self.label_color, bg="#0f0f0f")
        currency_label.pack(anchor=tk.W)

        self.currency_var = tk.StringVar(value=self.config.currency)
        currency_menu = ttk.Combobox(currency_frame,
                                     textvariable=self.currency_var,
                                     values=get_supported_currencies(),
                                     state="readonly", font=self._font(9),
                                     width=15)
        # The drop-down list is a plain Tk listbox with its own font.
        self.dialog.option_add("*TCombobox*Listbox.font", self._font(9))
        currency_menu.pack(anchor=tk.W, pady=self._spacing((5, 0)))

        # Currency info
        currency_info = tk.Label(currency_frame,
                                text="Costs will be displayed in selected currency",
                                font=self._font(8),
                                fg=self.hint_color, bg="#0f0f0f")
        currency_info.pack(anchor=tk.W, pady=self._spacing((5, 0)))

        # Refresh interval
        refresh_frame = tk.Frame(frame, bg="#0f0f0f")
        refresh_frame.pack(fill=tk.X, pady=self._spacing((0, 15)))

        refresh_label = tk.Label(refresh_frame,
                                 text=f"Refresh Interval (seconds, {MIN_REFRESH_INTERVAL} - {MAX_REFRESH_INTERVAL})",
                                font=self._font(10),
                                fg=self.label_color, bg="#0f0f0f")
        refresh_label.pack(anchor=tk.W)

        self.refresh_var = tk.StringVar(value=str(self.config.refresh_interval))
        refresh_entry = tk.Entry(refresh_frame, textvariable=self.refresh_var,
                                width=10, font=self._font(9), bg="#1a1a1a", fg="#ffffff",
                                insertbackground="#ffffff",
                                relief=tk.FLAT)
        refresh_entry.pack(anchor=tk.W, pady=self._spacing((5, 0)))

        # Validation error label (hidden by default)
        self.error_label = tk.Label(frame, text="",
                                    font=self._font(9),
                                    fg="#EF4444", bg="#0f0f0f")
        self.error_label.pack(anchor=tk.W, pady=self._spacing((5, 0)))

        # Buttons
        button_frame = tk.Frame(frame, bg="#0f0f0f")
        button_frame.pack(fill=tk.X, pady=self._spacing((20, 0)))

        # The project link lives here; the panel keeps to the figures.
        github = tk.Label(button_frame, text="GitHub", font=self._font(9, "underline"),
                          fg=self.hint_color, bg="#0f0f0f", cursor="hand2")
        github.pack(side=tk.LEFT)
        github.bind("<Button-1>", lambda e: _open_url(GITHUB_URL))

        save_btn = tk.Button(button_frame, text="Save",
                            command=self._save,
                            font=self._font(10),
                            bg="#10B981", fg="#ffffff",
                            activebackground="#059669",
                            relief=tk.FLAT, padx=self._px(20), pady=self._px(8))
        save_btn.pack(side=tk.RIGHT)

        cancel_btn = tk.Button(button_frame, text="Cancel",
                              command=self._on_dialog_close,
                              font=self._font(10),
                              bg="#2a2a2a", fg="#ffffff",
                              activebackground="#3a3a3a",
                              relief=tk.FLAT, padx=self._px(20), pady=self._px(8))
        cancel_btn.pack(side=tk.RIGHT, padx=self._spacing((0, 10)))

    def _hint(self, parent, text, colour=None):
        """A wrapped explanatory line under a field."""
        tk.Label(parent, text=text, font=self._font(8), wraplength=self._px(320),
                 justify=tk.LEFT, fg=colour or self.hint_color,
                 bg="#0f0f0f").pack(anchor=tk.W)

    def _create_deepseek_credentials(self, frame):
        """DeepSeek needs two credentials, and they buy different things.

        Kept deliberately short. This is a settings panel, not a manual; a wall
        of small grey text is harder to read than saying less.
        """
        section = tk.Frame(frame, bg="#0f0f0f")
        section.pack(fill=tk.X, pady=self._spacing((0, 15)))

        self._hint(section, "Signing in gives the balance and the usage figures.")

        tk.Label(section, text="DeepSeek account", font=self._font(9),
                 fg=self.label_color, bg="#0f0f0f").pack(anchor=tk.W, pady=self._spacing((10, 0)))
        signin_row = tk.Frame(section, bg="#0f0f0f")
        signin_row.pack(fill=tk.X, pady=self._spacing((4, 0)))
        self._signin_btn = tk.Button(
            signin_row, text="Sign in to DeepSeek…", command=self._start_deepseek_signin,
            font=self._font(9), bg="#2a2a2a", fg="#ffffff",
            activebackground="#3a3a3a", activeforeground="#ffffff",
            relief=tk.FLAT, padx=self._px(12), pady=self._px(4), cursor="hand2")
        self._signin_btn.pack(side=tk.LEFT)
        self._hint(section, "Opens a browser you sign into. Your password stays with DeepSeek.")

        # Its own full-width line, wrapping. Sharing the row with the button
        # clipped anything longer than a short status, and an error rarely is.
        self._signin_status = tk.Label(section, text="", font=self._font(8),
                                       fg=self.hint_color, bg="#0f0f0f",
                                       wraplength=self._px(320), justify=tk.LEFT, anchor=tk.W)
        self._signin_status.pack(fill=tk.X, pady=self._spacing((4, 0)))

        tk.Label(section, text="or paste a session token", font=self._font(9),
                 fg=self.label_color, bg="#0f0f0f").pack(anchor=tk.W, pady=self._spacing((8, 0)))
        self.deepseek_token_var = tk.StringVar()
        tk.Entry(section, textvariable=self.deepseek_token_var, show="•", width=34,
                 font=self._font(9), bg="#1a1a1a", fg="#ffffff", insertbackground="#ffffff",
                 relief=tk.FLAT).pack(anchor=tk.W, pady=self._spacing((2, 0)))

        # One action, not two. It clears what ClaudeBar stores and the browser
        # profile ClaudeBar created; the user's own browser is never touched.
        actions = tk.Frame(section, bg="#0f0f0f")
        actions.pack(fill=tk.X, pady=self._spacing((10, 0)))
        tk.Button(actions, text="Forget sign-in", command=self._forget_signin,
                  font=self._font(9), bg="#2a2a2a", fg="#ffffff",
                  activebackground="#3a3a3a", activeforeground="#ffffff",
                  relief=tk.FLAT, padx=self._px(12), pady=self._px(4), cursor="hand2").pack(side=tk.LEFT)
        self._credential_note = tk.Label(actions, text="", font=self._font(8),
                                         fg=self.hint_color, bg="#0f0f0f")
        self._credential_note.pack(side=tk.LEFT, padx=self._spacing((8, 0)))
        self._hint(section, "Kept encrypted with Windows DPAPI. Only ClaudeBar's own "
                            "browser profile is cleared, never your browser.")

        # Last in the section, deliberately: it reads and writes both widgets
        # above, and calling it before the status label existed raised an
        # AttributeError partway through building the dialog - which Tk reports
        # to stderr, where the application log never sees it, and whose only
        # symptom is a dialog that stops drawing at that point.
        self._sync_signin_button()

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
        if self._forget_thread is not None and self._forget_thread.is_alive():
            return
        # Either engine will do: Firefox is the fallback when no Chromium browser
        # is installed, and the token is read the same way once it is. Asking for
        # a Chromium browser here refused a machine that had a sign-in path.
        if not (browser_signin.find_browser() or browser_signin.find_firefox()):
            self._signin_status.config(
                text="No Chromium or Firefox browser found · paste a token below",
                fg=self.warn_color)
            return

        self._signin_cancel = False
        self._signin_result = None
        self._signin_btn.config(state=tk.DISABLED)
        self._signin_status.config(text="Waiting for sign-in…", fg=self.hint_color)
        # The worker opens the browser (see _run_signin): that may have to wait for an earlier
        # sign-in to finish closing its own, and the Tk thread must not.
        self._signin_thread = threading.Thread(target=self._run_signin, daemon=True)
        self._signin_thread.start()
        # Tk is not thread-safe, so the main thread drives the UI and the
        # worker only leaves a result behind.
        self._poll_signin()

    def _poll_signin(self):
        """Main-thread poll for the worker's result."""
        # Asked before the result is read: a worker that ends after this look has set its result
        # by then, and the next look finds it. A result that Forget took away ends the polling.
        running = self._signin_thread is not None and self._signin_thread.is_alive()
        result = self._signin_result
        if result is None:
            try:
                if running and self.dialog.winfo_exists():
                    self.dialog.after(250, self._poll_signin)
            except tk.TclError:
                pass
            return
        status, payload = result
        if status == "ok":
            self._signin_succeeded(payload)
        else:
            self._signin_failed(payload)

    def _run_signin(self):
        """Background: take the process's one sign-in, open the browser, and see it through.

        Settings closed during a sign-in only cancels it: its worker is inside a poll and
        cleans up when it wakes, which closes every browser on the profile and deletes the
        profile. Opening the next browser only after that (`browser_signin.session`) keeps the
        late cleanup of the old sign-in from taking down the new one."""
        try:
            with browser_signin.session():
                if self._signin_cancel:
                    self._signin_result = ("fail", "Cancelled")
                    return
                process = browser_signin.launch()
                if process is None:
                    self._signin_result = ("fail", "Could not open the browser")
                    return
                logger.info("DeepSeek sign-in: opened %s against our own profile",
                            browser_signin.find_browser() or browser_signin.find_firefox())
                # From here the worker owns the browser and closes it however the sign-in ends.
                self._signin_worker(process)
        except Exception as exc:                      # noqa: BLE001 - reported, not swallowed
            logger.exception("DeepSeek sign-in could not start")
            self._signin_result = ("fail", f"{type(exc).__name__}: {exc}")

    def _signin_worker(self, process):
        """Background: wait for the browser to yield a session, then keep it.

        Touches no widgets; it only sets `_signin_result` for the main thread. However it
        ends (token kept, cancelled, timed out, rejected, an error), the browser it was
        started for is closed and its profile, which holds the token in plaintext, deleted.
        """
        browser_ended = False
        try:
            token = browser_signin.wait_for_token(
                timeout=600, interval=2.0, should_stop=lambda: self._signin_cancel)

            if not token:
                if self._signin_cancel:
                    logger.info("DeepSeek sign-in: cancelled, worker stopped")
                else:
                    logger.info("DeepSeek sign-in: gave up waiting after %.0fs", 600)
                self._signin_result = (
                    "fail", "Cancelled" if self._signin_cancel else "Timed out")
                return

            logger.info("DeepSeek sign-in: session token captured, validating")
            # Close every window on our profile, not just the one we spawned: a repeat
            # launch delegates to the running instance and exits, so the process we hold
            # is usually already gone. The token is in memory now, so the profile goes too.
            browser_signin.end_signin(process)
            browser_ended = True

            # Prove the token works before storing it, so a stale profile
            # cannot masquerade as a completed sign-in.
            summary = deepseek_usage.fetch_summary(token)
            if summary.error:
                logger.warning("DeepSeek sign-in: token rejected: %s", summary.error)
                self._signin_result = ("fail", summary.error)
                return

            # Validation takes a network round trip, long enough for the dialog
            # to be closed or Forget to be pressed meanwhile. Either one means
            # this token must not be stored.
            if self._signin_cancel:
                logger.info("DeepSeek sign-in: cancelled before the token was stored")
                self._signin_result = ("fail", "Cancelled")
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
        finally:
            if not browser_ended:
                browser_signin.end_signin(process)

    def _sync_signin_button(self):
        """Enable the button only when there is no working session.

        A button that stays live after signing in invites a second sign-in that
        cannot help, and it hides the fact that the first one worked. A stored
        token is not the same as a working one either: it expires, and the panel
        already knows when it has, so the button comes back when it does.
        """
        if not hasattr(self, "_signin_btn"):
            return
        snapshot = self._session_state() if self._session_state else None
        expired = snapshot is not None and (
            snapshot.error_message == "Not signed in"
            or snapshot.usage_error == "Session expired")

        if snapshot is not None and snapshot.balance_available:
            self._signin_btn.config(state=tk.DISABLED)
            self._signin_status.config(
                text=f"Signed in · balance {snapshot.balance_currency} "
                     f"{snapshot.balance_total:,.2f}",
                fg=self.ok_color)
            return

        if expired:
            self._signin_btn.config(state=tk.NORMAL)
            self._signin_status.config(text="Session expired · sign in again",
                                       fg=self.warn_color)
            return

        stored = deepseek_auth.discover_user_token() is not None
        self._signin_btn.config(state=tk.NORMAL if not stored else tk.DISABLED)
        if stored:
            self._signin_status.config(text="Signed in", fg=self.ok_color)

    def _signin_failed(self, message: str):
        self._signin_btn.config(state=tk.NORMAL)
        self._signin_status.config(text=message, fg=self.warn_color)

    def _signin_succeeded(self, summary):
        # Stays disabled: the session works now, and the button is for when it
        # does not. _sync_signin_button decides that, so there is one rule.
        self._signin_btn.config(state=tk.DISABLED)
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

        A sign-in still running has to stop first, and that takes up to a poll interval and the
        browser's closing, so the work is on a thread of its own and the labels follow through
        `_poll_forget`: the Tk thread never waits for it.
        """
        if self._forget_thread is not None and self._forget_thread.is_alive():
            return
        self._signin_cancel = True
        # Off until the forget is done, so no sign-in starts in the middle of it (and the click
        # is seen to have done something while the sign-in it stops winds down).
        self._signin_btn.config(state=tk.DISABLED)
        self._forget_result = None
        self._forget_thread = threading.Thread(target=self._run_forget, daemon=True)
        self._forget_thread.start()
        self._poll_forget()

    def _run_forget(self):
        """Background: forget everything once no sign-in is running.

        Taking the one-sign-in-at-a-time session is what waits for a worker that was told to stop
        to be done (browser closed, its profile deleted): a worker still running would store its
        token after this had cleared it. Nothing else can start a browser until this is done."""
        try:
            with browser_signin.session():
                deepseek_auth.clear_credentials()
                cleared = browser_signin.forget()
            self._forget_result = ("done", cleared)
        except Exception:                             # noqa: BLE001 - reported, not swallowed
            logger.exception("DeepSeek forget sign-in failed")
            self._forget_result = ("failed", False)

    def _poll_forget(self):
        """Main-thread poll for the end of the forget, then the dialog follows it."""
        result = self._forget_result
        if result is None:
            try:
                if self.dialog.winfo_exists():
                    self.dialog.after(100, self._poll_forget)
            except tk.TclError:
                pass
            return
        status, cleared = result
        # The sign-in this stopped ends with "Cancelled": that is the user's own doing, not news,
        # and it must not land after the status line has been cleared.
        self._signin_result = None
        # Only the user knows they have signed out of the platform, so this is
        # what brings the button back by hand.
        self._signin_btn.config(state=tk.NORMAL)
        self._signin_status.config(text="")
        if status != "done":
            self._credential_note.config(text="Could not forget the sign-in")
            return
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

    def _stop_signin(self, wait: float = 0.0):
        """Tell the sign-in worker to stop, optionally waiting for it.

        The worker notices within one poll interval (2 s) while it waits for the
        browser, and checks again just before it stores a token.
        """
        self._signin_cancel = True
        thread = self._signin_thread
        if wait and thread and thread.is_alive():
            thread.join(wait)

    def _on_dialog_close(self):
        """Handle dialog close."""
        self._stop_signin()
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

        # Credentials never touch config.json; a blank field keeps whatever is
        # already stored (or auto-detected) rather than clearing it. Stored
        # first: when it fails, the dialog stays open with the pasted token
        # still in the field, and nothing else has been saved yet.
        entered_token = self.deepseek_token_var.get().strip()
        if entered_token and not deepseek_auth.save_user_token(entered_token):
            self.error_label.config(text="Could not save the platform token securely")
            return

        self.config.claude_enabled = claude_enabled
        self.config.codex_enabled = codex_enabled
        self.config.deepseek_enabled = deepseek_enabled
        self.config.currency = self.currency_var.get()
        try:
            self.config.refresh_interval = clamp_refresh_interval(int(self.refresh_var.get()))
        except ValueError:
            pass

        save_config(self.config)

        self._stop_signin()
        self.on_save()
        self._restore_parent()
        self.dialog.destroy()


class ResetConfirmDialog(_Scaled):
    """Confirmation before spending a banked reset, which cannot be undone.

    Modal, like the settings dialog. Cancel is the default: it takes the focus,
    and Escape, the window close button and a stray Return all cancel. Only a
    deliberate click on (or Tab to and press of) "Use reset" confirms; one that
    lands in the first ARM_SECONDS, such as the second click of a double-click
    on the panel's Use button, is ignored. The callbacks run once, after the
    dialog is gone.
    """

    ARM_SECONDS = 0.6

    def __init__(self, parent, lines, on_confirm, on_cancel, style):
        self._parent = parent
        self.scale = getattr(style, "scale", None) or Scale()
        self._on_confirm, self._on_cancel = on_confirm, on_cancel
        self._finished = False
        self._armed_at = time.monotonic() + self.ARM_SECONDS
        try:
            parent.attributes('-topmost', False)
        except tk.TclError:
            pass

        self.dialog = tk.Toplevel(parent)
        self.dialog.title("Use banked reset")
        self.dialog.configure(bg=style.bg)
        self.dialog.resizable(False, False)
        self.dialog.transient(parent)
        self.dialog.grab_set()
        self.dialog.attributes('-topmost', True)

        frame = tk.Frame(self.dialog, bg=style.bg)
        frame.pack(fill=tk.BOTH, expand=True, padx=self._px(20), pady=self._px(18))
        tk.Label(frame, text="Use a banked reset?", font=style.font(11, "bold"),
                 fg=style.primary, bg=style.bg).pack(anchor=tk.W)
        *details, warning = lines
        for i, line in enumerate(details):
            tk.Label(frame, text=line, font=style.font(10 if i == 0 else 9, "bold" if i == 0 else "normal"),
                     fg=style.primary if i == 0 else style.secondary, bg=style.bg,
                     wraplength=self._px(320), justify=tk.LEFT, anchor=tk.W).pack(
                         anchor=tk.W, pady=self._spacing((10 if i == 0 else 3, 0)))
        tk.Label(frame, text=warning, font=style.font(9, "bold"), fg=style.accent, bg=style.bg,
                 wraplength=self._px(320), justify=tk.LEFT, anchor=tk.W).pack(anchor=tk.W, pady=self._spacing((14, 0)))

        buttons = tk.Frame(frame, bg=style.bg)
        buttons.pack(fill=tk.X, pady=self._spacing((18, 0)))
        self.use_button = self._button(buttons, "Use reset", self._confirm, style, style.warn, style.bg)
        self.cancel_button = self._button(buttons, "Cancel", self._cancel, style, style.primary,
                                          style.button_bg)
        self.use_button.pack(side=tk.RIGHT)
        self.cancel_button.pack(side=tk.RIGHT, padx=self._spacing((0, 8)))

        self.dialog.bind("<Escape>", lambda e: self._cancel())
        self.dialog.bind("<Return>", lambda e: self._cancel())
        self.dialog.protocol("WM_DELETE_WINDOW", self._cancel)

        self.dialog.update_idletasks()
        _place_over(self.dialog, parent, max(self._px(360), self.dialog.winfo_reqwidth()),
                    self.dialog.winfo_reqheight(), self._px(12))
        self.dialog.lift()
        self.dialog.focus_force()
        self.cancel_button.focus_set()

    def _button(self, parent, text, command, style, fg, bg):
        button = tk.Button(parent, text=text, command=command, font=style.font(9, "bold"),
                           fg=fg, bg=bg, activeforeground=style.primary,
                           activebackground=style.hover_bg, relief=tk.FLAT, bd=0, cursor="hand2",
                           padx=self._px(14), pady=self._px(5), takefocus=1, highlightthickness=self._px(2),
                           highlightbackground=style.bg, highlightcolor=style.accent)
        # Return presses the focused button, so Tab then Return is a deliberate confirm.
        button.bind("<Return>", lambda e: (command(), "break")[1])
        button.bind("<Enter>", lambda e: button.config(bg=style.hover_bg))
        button.bind("<Leave>", lambda e: button.config(bg=bg))
        return button

    def _close(self):
        try:
            self.dialog.grab_release()
            self.dialog.destroy()
        except tk.TclError:
            pass
        try:
            self._parent.attributes('-topmost', True)
        except tk.TclError:
            pass

    def _confirm(self):
        if self._finished or time.monotonic() < self._armed_at:
            return
        self._finished = True
        self._close()
        self._on_confirm()

    def _cancel(self):
        if self._finished:
            return
        self._finished = True
        self._close()
        self._on_cancel()


class ClaudeBarWindow(_Scaled):
    """Premium popup window for ClaudeBar."""

    def __init__(self, on_refresh: Callable, on_settings: Callable,
                 on_exit: Callable, config: Optional[Config] = None,
                 on_engine_change: Optional[Callable] = None,
                 on_settings_saved: Optional[Callable] = None):
        self.on_refresh = on_refresh
        self.on_settings = on_settings
        self.on_exit = on_exit
        self.on_engine_change = on_engine_change
        self.on_settings_saved = on_settings_saved
        self.config = config or Config()

        self._window: Optional[tk.Tk] = None
        self._main_frame: Optional[tk.Frame] = None
        # The factor every length and font goes through (see _choose_scale), and the monitor's
        # DPI part of it, which the dialogs keep whole.
        self.scale = Scale()
        self._dpi_scale = 1.0
        self._lock = threading.RLock()
        self._snapshot: Optional[UsageSnapshot] = None
        self._openai_snapshot: Optional[OpenAISnapshot] = None
        self._deepseek_snapshot: Optional[DeepSeekSnapshot] = None
        self._pending_snapshot: Optional[UsageSnapshot] = None  # Thread-safe pending update
        self._pending_openai_snapshot: Optional[OpenAISnapshot] = None
        self._pending_deepseek_snapshot: Optional[DeepSeekSnapshot] = None
        self._exchange_rates: Optional[ExchangeRates] = None
        self._claude_status: Optional[ClaudeStatus] = None
        self._claude_status_at = 0.0     # time.monotonic() of that answer
        self._loading = False            # the loader thread below is running
        # When a Claude Code log was last written, as the last look saw it (see _logs_written_since).
        self._log_write: Optional[datetime] = None
        self._looking = False            # a thread is looking at the logs
        self._log_asked: Optional[tuple] = None    # (since, answer) of the last redraw that asked
        self._status_needs_update = False
        self._oauth_error: Optional[str] = None  # Track OAuth errors for status display
        self._is_collecting: bool = True  # Start as collecting until first data arrives

        # Engine state
        self._active_engine: Engine = Engine.CLAUDE
        self._openai_available: bool = False

        # UI elements (built in _build_content)
        self._status_indicator: Optional[tk.Canvas] = None
        self._status_dot: Optional[int] = None
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
        self._tabs: dict = {}            # view -> (label, underline)
        self._cost_frame: Optional[tk.Frame] = None
        self._forecast_frame: Optional[tk.Frame] = None
        self._fc_value: Optional[tk.Label] = None      # the headline figure: the burn rate
        self._fc_caption: Optional[tk.Label] = None    # beside it: what the rate was measured on
        self._fc_chart: Optional[ForecastChart] = None
        self._fc_rows: list = []         # (row, label, value) widgets of the detail rows
        self._fc_row_count = 0           # how many of them are filled
        self._banked_frame: Optional[tk.Frame] = None
        self._banked_visible = False
        # Spending a banked reset: the confirm/idempotency/in-flight rules, the runner
        # (replaceable, so tests never reach a real endpoint), and the worker's result.
        self._redeem_flow = reset_redeem.RedeemFlow()
        self._run_attempt = reset_redeem.run_attempt
        self._redeem_result: Optional[tuple] = None    # (provider, outcome), set by the worker
        self._redeem_thread: Optional[threading.Thread] = None
        self._banked_notice: Optional[tuple] = None    # (text, colour, until, provider)
        self._notice_job = None
        # Until a snapshot collected after the last result arrives, the old one's
        # claim on the reset is out of date: nothing is offered from a snapshot
        # collected before this.
        self._redeem_floor = datetime.min
        self._logo_image: Optional[ImageTk.PhotoImage] = None
        self._layout_key = None      # which optional rows are visible
        self._loop_running = False   # the update loop is scheduled
        self._tick_job = None        # its pending `after` job
        self._redraw_at = 0.0        # time.monotonic() after which the panel is redrawn

        # Engine toggle UI elements
        self._engine_btns: dict = {}       # Engine -> selector Label
        self._engine_icons: dict = {}      # Engine -> (active, inactive) PhotoImage
        self._engine_frame: Optional[tk.Frame] = None

        # Drag-to-move state (the window is borderless, so there is no title bar)
        self._drag_origin: Optional[tuple] = None
        self._user_position: Optional[tuple] = self._saved_position()
        # Drag-to-resize: the press in the outer margin, the idle job that applies the pointer, the
        # corner that stays put (x, y, from the right, from the bottom), and the wheel's unused units.
        self._resize: Optional[SimpleNamespace] = None
        self._resize_job = None
        self._pin: Optional[tuple] = None
        # The Label that shows a picture of the panel over its content while that changes (see _veiled).
        self._curtain: Optional[tk.Label] = None
        self._wheel = 0

        # Colors. A soft charcoal card rather than near-black; the separator,
        # bar track and selected tab are steps up from it.
        self.bg_color = "#1c1c1e"
        self.text_primary = "#ffffff"
        self.text_secondary = "#d1d5db"
        # Lifted from #6b7280: the lines under each figure were a strain to read
        # on a 4K panel at 100% scaling.
        self.text_muted = "#7f8899"
        self.separator_color = "#2c2c2e"
        self.track_color = "#3a3a3c"
        self.border_color = "#333333"
        self.accent_color = "#F59E0B"  # Claude orange
        self.active_engine_bg = "#3a3a3c"  # Active engine button background
        self.inactive_engine_bg = self.bg_color  # Inactive engine button background
        self.ok_color = "#22c55e"
        self.warn_color = "#ef4444"
        # Fonts: resolved in _create_toplevel, font.families() needs a Tk root.
        self.sans = "Segoe UI"
        self.mono = "Consolas"

    def _engine_icon_images(self, engine: Engine) -> Optional[tuple]:
        """(active, inactive) PhotoImages for one engine, or None.

        Both states come from one mask, so only the tint differs. The caller
        must keep the returned images alive or Tk will collect them.
        """
        mask = _engine_glyph(_ENGINE_META[engine][1], self._px(_GLYPH_SIZE))
        if mask is None:
            return None
        images = []
        for colour in (self.text_primary, self.text_muted):
            tinted = Image.new("RGBA", mask.size, colour)
            tinted.putalpha(mask)
            images.append(ImageTk.PhotoImage(tinted))
        return tuple(images)

    def _create_window(self):
        """Create the popup: the borderless window once, then its content."""
        self._create_toplevel()
        self._build_content(self._choose_scale())
        # Load initial data
        self._load_initial_data()

    def _create_toplevel(self):
        """The borderless popup window and its bindings. It lives as long as the panel; what it
        shows is built, and built again, by _build_content."""
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

        self._window.resizable(False, False)
        self._window.attributes('-topmost', True)
        self._window.overrideredirect(True)

        self._window.bind('<Escape>', lambda e: self.hide())
        # Bound on the toplevel, which sits in every child widget's bindtags, so
        # the whole popup is a drag handle - it has no title bar to grab. Its outer
        # margin resizes instead, and shows the cursor that says so.
        self._window.bind('<Button-1>', self._on_drag_start, add='+')
        self._window.bind('<B1-Motion>', self._on_drag_move, add='+')
        self._window.bind('<ButtonRelease-1>', self._on_drag_end, add='+')
        self._window.bind('<Motion>', self._on_pointer, add='+')
        self._window.bind('<Leave>', self._on_pointer, add='+')
        for sequence, step in (("<Control-plus>", 1), ("<Control-equal>", 1), ("<Control-KP_Add>", 1),
                               ("<Control-minus>", -1), ("<Control-KP_Subtract>", -1)):
            self._window.bind(sequence, lambda e, step=step: self._step_scale(step))
        self._window.bind('<Control-0>', lambda e: self.reset_size())
        self._window.bind('<Control-MouseWheel>', self._on_wheel)
        self._window.withdraw()

    def _build_content(self, k: float):
        """Build the panel's widgets at scale factor `k` inside the window, in place of any it
        holds. A rebuild (after Settings, or on a monitor of another DPI) leaves the window
        itself alone, so nothing is destroyed and mapped again."""
        if self._main_frame is not None:
            self._main_frame.destroy()
        self.scale = Scale(k)
        # What these named is gone with the old content.
        self._engine_btns, self._engine_icons, self._engine_frame = {}, {}, None
        self._limits, self._stats, self._tabs, self._fc_rows = {}, {}, {}, []
        self._fc_row_count = 0
        self._banked_visible = self._banked_pinned = False
        self._layout_key = None
        self._pads, self._rows = {}, {}
        self._fit_hidden: set = set()
        self._fit_range = (0, 0)

        # Fixed width floor, dynamic height; the natural width is what all the blocks ask for
        self._natural_width = self._window_width = self._px(380)

        # Main container with border
        self._main_frame = tk.Frame(self._window, bg=self.bg_color,
                             highlightbackground=self.border_color,
                             highlightthickness=self._px(1))
        self._pack_main()

        # Every stack of blocks is one grid column, a fixed row per block (_put): grid_remove and
        # grid hide and show a block in its own row, so nothing is ever reordered.
        self._content = tk.Frame(self._main_frame, bg=self.bg_color)
        self._content.pack(fill=tk.BOTH, expand=True, padx=self._px(16), pady=self._px(16))
        content = self._content

        self._create_header(content)
        self._create_status_row(content)
        self._hairline(content)
        # Host for the region holding either the rate-limit rows or the DeepSeek balance block.
        self._limits_host = self._put(tk.Frame(content, bg=self.bg_color))
        self._create_limits(self._limits_host)
        self._create_balance(self._limits_host)
        self._create_banked(self._limits_host)
        # The Cost/Quota half with its tab strip: dropped as one block.
        self._lower = self._put(tk.Frame(content, bg=self.bg_color))
        self._hairline(self._lower, pady=(10, 6))
        self._create_tabs(self._lower)
        # Host for the two views of the lower half, one cell that holds either.
        self._view_host = self._put(tk.Frame(self._lower, bg=self.bg_color))
        self._cost_frame = self._put(tk.Frame(self._view_host, bg=self.bg_color), row=0)
        self._create_stats(self._cost_frame)
        self._create_chart(self._cost_frame)
        self._forecast_frame = self._put(tk.Frame(self._view_host, bg=self.bg_color), row=0, shown=False)
        self._create_forecast(self._forecast_frame)
        self._create_footer(content)
        # What the fit hides, by block (the names of DROP_ORDER).
        self._fit_widgets = {
            "source": [self._source_label], "top_model": [self._top_group],
            "pace": [w["pace"] for w in self._limits.values()],
            "chart": [self._chart_group, self._fc_rows_host],
            "stats": [self._stats[key][0].master for key in ("month", "last31")],
            "half": [self._lower], "status": [self._status_row]}

    def _pack_main(self):
        """The content frame fills the window, beneath the curtain: a frame made during a rebuild is created above it."""
        self._main_frame.pack(fill=tk.BOTH, expand=True)
        self._main_frame.lower()

    def _place(self, widget, row, column=0, pady=0, sticky="ew", shown=True):
        """Grid `widget`, noting its padding (the fit needs it for a hidden block); grid remembers the place."""
        pad = self._spacing(pady)
        self._pads[str(widget)] = sum(pad) if isinstance(pad, tuple) else 2 * pad
        widget.grid(row=row, column=column, sticky=sticky, pady=pad)
        if not shown:
            widget.grid_remove()
        return widget

    def _put(self, widget, pady=0, sticky="ew", row=None, shown=True):
        """The next block of a one-column stack (a frame holds grid children or pack ones, never both)."""
        master = widget.master
        master.columnconfigure(0, weight=1)
        if row is None:
            row = self._rows[str(master)] = self._rows.get(str(master), -1) + 1
        return self._place(widget, row, 0, pady, sticky, shown)

    @staticmethod
    def _show(widget, shown: bool):
        """Put a block back in its row, or take it out of the layout."""
        widget.grid() if shown else widget.grid_remove()

    def _target_monitor(self) -> Monitor:
        """The monitor the panel is, or will be, shown on: the one under where the user dragged
        it, else the primary one, where the tray is."""
        if self._user_position:
            x, y = (self._user_position[0] + _HEADER_POINT[0], self._user_position[1] + _HEADER_POINT[1])
        else:
            x, y = 0, 0
        return _monitor_near(self._window, x, y)

    def _choose_scale(self) -> float:
        """The factor for the monitor the panel is on: its DPI scale, times the panel scale from
        config, or the one its height asks for when config has none. Also remembers the DPI part,
        which the dialogs keep whole."""
        monitor = self._target_monitor()
        self._dpi_scale = monitor.dpi / 96
        return self._dpi_scale * (self.config.panel_scale or auto_scale(monitor.height, self._dpi_scale))

    def _dialog_scale(self) -> Scale:
        """The dialogs grow with the panel but never shrink below 1.0: they are read, not glanced
        at. The monitor's DPI part of the factor stays whole."""
        return Scale(max(self._dpi_scale, self.scale.k))

    def _zone(self, event) -> Optional[str]:
        """The resize zone under the pointer of `event`."""
        window = self._window
        return zone_at(event.x_root - window.winfo_rootx(), event.y_root - window.winfo_rooty(),
                       window.winfo_width(), window.winfo_height(), self._px(_RESIZE_MARGIN))

    def _on_pointer(self, event):
        """Show the resize cursor in the margin and the normal one anywhere else."""
        if self._resize is None and self._window and self._window.winfo_exists():
            cursor = _RESIZE_CURSORS.get(self._zone(event), "")
            if self._window.cget("cursor") != cursor:
                self._window.config(cursor=cursor)

    def _on_drag_start(self, event):
        """A press in the outer margin starts a resize; anywhere else it grabs the window to move it."""
        self._drag_origin = self._resize = self._pin = None
        if not (self._window and self._window.winfo_exists()):
            return
        window = self._window
        x, y, width, height = window.winfo_x(), window.winfo_y(), window.winfo_width(), window.winfo_height()
        zone = self._zone(event)
        if zone is None:
            self._drag_origin = (event.x_root, event.y_root, x, y)
            return
        try:
            window.grab_set()       # the first rebuild destroys the press widget, and Tk drops the drag
        except tk.TclError:
            pass
        # The opposite edge stays put; `base` is the width at panel scale 1.
        self._pin = (x + width if "w" in zone else x, y + height if "n" in zone else y, "w" in zone, "n" in zone)
        self._resize = SimpleNamespace(
            zone=zone, press=(event.x_root, event.y_root), pointer=(event.x_root, event.y_root),
            box=(x, y, width, height), work=self._target_monitor().work, moved=False,
            base=width / (self.scale.k / self._dpi_scale),
            target=None, picture=None, live=False)

    def _on_drag_move(self, event):
        """Move the window with the pointer, or in a resize note where it is and leave the work to the idle."""
        if self._resize is not None:
            self._resize.pointer = (event.x_root, event.y_root)
            if self._resize_job is None:
                self._resize_job = self._window.after_idle(self._apply_resize)
            return
        if not self._drag_origin or not self._window.winfo_exists():
            return
        grab_x, grab_y, win_x, win_y = self._drag_origin
        x = win_x + event.x_root - grab_x
        y = win_y + event.y_root - grab_y
        self._user_position = (x, y)
        self._window.geometry(f"+{x}+{y}")

    def _apply_resize(self, final: bool = False):
        """Follow the pointer, once per idle: the width sets the scale, the height the cap. While the
        pointer moves the content is not built: the window takes the size the panel will have and shows a
        picture of it (_preview). `final` is the release, which builds it once."""
        self._resize_job = None
        drag = self._resize
        if drag is None or not self._window.winfo_exists():
            return
        drag.moved = True
        x, y, width, height = drag.box
        left, right = drag.work[0], drag.work[2]
        dx, dy = drag.pointer[0] - drag.press[0], drag.pointer[1] - drag.press[1]
        sideways = "e" in drag.zone or "w" in drag.zone
        scale = self.scale.k / self._dpi_scale
        tall = None
        if sideways:
            wide = width + dx if "e" in drag.zone else width - dx
            room = min(right - x if "e" in drag.zone else x + width - left, self._max_width(drag.work))
            scale = round(scale_for_width(min(wide, room), drag.base), 3)
            drag.target = (scale, room)
        if "n" in drag.zone or "s" in drag.zone:
            tall = height + dy if "s" in drag.zone else height - dy
            self.config.panel_max_height = max(1, round(tall / (self._dpi_scale * scale)))
        if not sideways:
            self._update_window_size()
        elif final:
            drag.live = False                   # the window is the content's again
            self._set_scale(*drag.target)
        else:
            self._preview(drag, scale, tall)

    def _preview(self, drag, scale: float, tall: Optional[int]):
        """The panel at panel scale `scale`, not built: the window takes the size it will have there
        (`tall` caps the height in a corner drag) and a picture of the panel, stretched to that, stands in
        for the content. Without a picture the window alone follows."""
        if not drag.live:
            drag.picture, drag.live = self._capture(), True
        window = self._window
        shot_width, shot_height = drag.picture.size if drag.picture else (window.winfo_width(), window.winfo_height())
        width = width_for_scale(scale, drag.base)
        full = round(shot_height * width / shot_width)
        height = full if tall is None else max(min(full, tall), round(self._fit_range[0] * width / shot_width))
        room = drag.work[3] - drag.work[1] - 2 * self._px(12)
        if self._pin:
            # The edge that stays put stays: a picture taller than the room beyond it is cut there, and the window
            # does not slide to make room (it went back to its place when the build, which drops blocks, fitted).
            room = min(room, self._pin[1] - drag.work[1] if self._pin[3] else drag.work[3] - self._pin[1])
        self._place_window(drag.work, width, min(height, room))
        if drag.picture:
            self._hold(drag.picture, (width, full))
            if self._main_frame.winfo_manager():
                self._main_frame.pack_forget()      # no relayout of what nobody sees, at every motion

    def _on_drag_end(self, event):
        """The one save of a drag: the position, and after a resize the scale and the cap."""
        drag = self._resize
        if drag is not None:
            if self._resize_job is not None:
                self._window.after_cancel(self._resize_job)
                self._resize_job = None
            if event is not None:               # Windows merges motions: the release is the last word
                drag.pointer = (event.x_root, event.y_root)
            try:
                if drag.pointer != drag.press or drag.moved:
                    self._apply_resize(final=True)
            finally:
                self._resize = self._pin = None
                self._window.grab_release()
                self._drop_curtain()
            if drag.moved:
                self._clear_cap_if_full()
                self.config.window_x, self.config.window_y = self._user_position
                save_config(self.config)
            self._sync_scale()
            if event is not None:
                self._on_pointer(event)
        elif self._drag_origin and self._user_position:
            self._keep_inside()
            self.config.window_x, self.config.window_y = self._user_position
            save_config(self.config)
            if self._window is not None:
                self._sync_scale()
        self._drag_origin = None

    def _clear_cap_if_full(self):
        """A cap that lets everything show is no cap; one below the smallest panel is that panel's height."""
        cap = self.config.panel_max_height
        if cap is not None:
            smallest, full = self._fit_range
            self.config.panel_max_height = (None if self._px(cap) >= full
                                            else max(cap, math.ceil(smallest / self.scale.k)))

    def _max_width(self, work: tuple) -> int:
        """The widest the panel may be on a monitor with this work area."""
        return work[2] - work[0] - self._px(40)

    def _set_scale(self, scale: float, room: int):
        """Build again at panel scale `scale`, or the largest below it whose width fits `room`, and never one
        smaller than the panel has (it fits: it is there; a width is a step of the factor, so the estimates could
        otherwise jump over it and a drag outward at the edge of the work area left the panel smaller). The width is
        checked because text does not scale exactly: up to six builds, each aiming a little further under the room
        than the one before (_FIT_MARGIN, doubled at each miss), since the plain estimate could stay a pixel or two
        over for three builds, which pulled the pinned edge of a drag that ended at the work area's edge as far in;
        the sixth is that known fit. A width that fits at once, the usual case, is one build. Saves nothing."""
        floor = round(min(scale, self.scale.k / self._dpi_scale), 3)
        with self._veiled():
            for step in range(6):
                if step == 5:
                    scale = floor               # the last build is the one that is known to fit
                self.config.panel_scale = round(clamp(scale, PANEL_MIN_SCALE, PANEL_MAX_SCALE), 3)
                self._sync_scale()
                need = self._natural_width
                if need <= room or self.config.panel_scale <= max(PANEL_MIN_SCALE, floor):
                    break
                scale = max(self.config.panel_scale * room / need - _FIT_MARGIN * 2 ** step, floor)
            self._update_window_size()

    def _step_scale(self, steps: int):
        """Ctrl+plus, Ctrl+minus, Ctrl+wheel: `steps` of 0.1 on the scale, inside the work area."""
        if self._window and self._window.winfo_exists():
            self._set_scale(round(self.scale.k / self._dpi_scale + steps * _SCALE_STEP, 2),
                            self._max_width(self._target_monitor().work))
            save_config(self.config)

    def _on_wheel(self, event):
        """A notch is a step; a finer wheel's deltas add up until they make one."""
        self._wheel += event.delta
        steps = int(self._wheel / _WHEEL_NOTCH)
        self._wheel -= steps * _WHEEL_NOTCH
        if steps:
            self._step_scale(steps)

    def reset_size(self):
        """Automatic scale and height again (Ctrl+0, the tray's "Reset panel size")."""
        self.config.panel_scale = self.config.panel_max_height = None
        save_config(self.config)
        if self._window and self._window.winfo_exists():
            with self._veiled():
                self._sync_scale()
                self._update_window_size()

    def _saved_position(self) -> Optional[tuple]:
        """The persisted position, unless it would land off every monitor
        (a screen was unplugged or rearranged) - then the default spot."""
        x, y = self.config.window_x, self.config.window_y
        if (isinstance(x, int) and isinstance(y, int)
                and monitor_at(x + _HEADER_POINT[0], y + _HEADER_POINT[1], nearest=False)):
            return (x, y)
        return None

    def reset_position(self):
        """Forget the dragged position and return to the spot near the tray."""
        self._user_position = None
        self.config.window_x = self.config.window_y = None
        save_config(self.config)
        if self._window and self._window.winfo_exists():
            self._update_window_size()

    def _sync_scale(self) -> bool:
        """Build the content again when the factor it was built at is not the one now asked for (another
        monitor, or a panel scale that changed). True when it did."""
        k = self._choose_scale()
        if k == self.scale.k:
            return False
        with self._veiled():
            self._build_content(k)
            self._refill()
            self._update_window_size()
        return True

    def _capture(self) -> Optional[Image.Image]:
        """A picture of the panel as it is on screen, or None when it is hidden or Windows will not give one."""
        if not (self._window and self._window.winfo_exists() and self._window.winfo_viewable()):
            return None
        return window_picture(self._window.winfo_id())

    def _anchor(self) -> str:
        """The corner of the window that stays put when its size changes, where a picture of it is pinned."""
        right, bottom = (self._pin[2], self._pin[3]) if self._pin else (not self._user_position,) * 2
        return ("s" if bottom else "n") + ("e" if right else "w")

    def _hold(self, picture: Image.Image, size: Optional[tuple] = None):
        """Show `picture`, stretched to `size`, over the whole window, above the content, in one widget, pinned
        to the corner of the window that stays put."""
        shown = picture if size in (None, picture.size) else picture.resize(size, Image.Resampling.BILINEAR)
        photo = ImageTk.PhotoImage(shown)
        if self._curtain is None:
            self._curtain = tk.Label(self._window, bd=0, highlightthickness=0, bg=self.bg_color)
            # Layered, it is composed over its siblings and not cut out of them: the content under it
            # keeps painting, and is whole the moment the curtain goes.
            make_layered(self._curtain.winfo_id())
            self._curtain.place(x=0, y=0, relwidth=1, relheight=1)
        self._curtain.config(image=photo, anchor=self._anchor())
        self._curtain.photo = photo                 # Tk does not keep the image alive
        self._curtain.lift()

    def _drop_curtain(self):
        """Take the picture away. What is under it is laid out and drawn to the end first (a pass of the
        event loop: a resize leaves layout and paints queued), so the panel appears whole."""
        curtain = self._curtain
        if curtain is not None and curtain.winfo_exists():
            try:
                if self._main_frame.winfo_exists() and not self._main_frame.winfo_manager():
                    self._pack_main()           # a live resize took it out of the window
                self._window.update()
            finally:
                curtain.destroy()
        self._curtain = None

    @contextlib.contextmanager
    def _veiled(self):
        """While the block changes the panel (a rebuild, a fit) a picture of it lies over the content, so
        no destroyed or half-built panel is ever on screen. A hidden panel needs none, and inside a
        resize, which holds its own picture, the block adds none."""
        picture = None if self._curtain is not None else self._capture()
        if picture is None:
            yield
            return
        self._hold(picture)
        self._window.update_idletasks()
        try:
            yield
        finally:
            self._drop_curtain()

    def _update_window_size(self):
        """Fit the content to the height it may have, then size and place the window."""
        if not self._window or not self._window.winfo_exists() or (self._resize and self._resize.live):
            return

        # Let tkinter calculate required sizes
        self._window.update_idletasks()
        work = self._target_monitor().work

        # Width follows the content and only ever grows, so switching tabs does not make the panel
        # jump. It is taken with every block showing and again after the fit, so a dropped block
        # (the footer's note) keeps its width: the width is a matter of the scale alone.
        self._grow_width(work)
        hide = self._fit(work[3] - work[1])
        changed = hide != self._fit_hidden
        # Blocks coming and going change the panel: a picture of it lies over it until the window has its new height.
        with self._veiled() if changed else contextlib.nullcontext():
            if changed:
                self._show_blocks(hide)
            self._grow_width(work)

            # Required height from content, plus the border
            self._place_window(work, self._window_width, self._main_frame.winfo_reqheight() + self._px(4))

    def _place_window(self, work: tuple, width: int, height: int):
        """Size the window and put it inside the work area: a resize keeps one corner; else it is where the
        user dragged it to, or near the tray."""
        left, top, right, bottom = work
        if self._pin:
            x = self._pin[0] - (width if self._pin[2] else 0)
            y = self._pin[1] - (height if self._pin[3] else 0)
        elif self._user_position:
            x, y = self._user_position
        else:
            x = right - width - self._px(20)
            y = bottom - height - self._px(12)
        # Either way inside the work area, so a taller panel never hangs off the monitor.
        x, y = self._inside(work, x, y, width, height)
        if self._pin:
            self._user_position = (x, y)

        self._window.geometry(f"{width}x{height}+{x}+{y}")

    @staticmethod
    def _inside(work: tuple, x: int, y: int, width: int, height: int) -> tuple:
        """The place nearest (x, y) where a panel of this size is wholly inside the work area."""
        left, top, right, bottom = work
        return max(left, min(x, right - width)), max(top, min(y, bottom - height))

    def _keep_inside(self):
        """A move drag lets go of the panel wherever the pointer was, the corner of the screen included. The
        panel is pulled back inside the work area of the monitor it is on, and that is the place it keeps."""
        window = self._window
        if not (window and window.winfo_exists() and self._user_position):
            return
        x, y = self._inside(self._target_monitor().work, *self._user_position,
                            window.winfo_width(), window.winfo_height())
        if (x, y) != self._user_position:
            self._user_position = (x, y)
            window.geometry(f"+{x}+{y}")

    def _grow_width(self, work: tuple):
        self._natural_width = max(self._natural_width, self._main_frame.winfo_reqwidth() + self._px(4))
        self._window_width = min(self._natural_width, self._max_width(work))

    def _fit(self, work_height: int) -> set:
        """The blocks that do not fit the height the panel may have (the cap or the work area), least
        important first (DROP_ORDER). Heights are asked of the widgets, which know them while hidden:
        hiding a block to look would paint every state on the way."""
        cost = self.config.panel_view != "forecast"
        height = lambda widget: widget.winfo_reqheight() + self._pads[str(widget)]
        heights = {
            "source": 0,                    # shares the buttons' line: frees width only
            "top_model": height(self._top_group) if cost else 0,
            "pace": sum(height(w["pace"]) for w in self._limits.values() if w["frame"].winfo_manager()),
            "chart": height(self._chart_group) if cost else (
                height(self._fc_rows_host) if self._fc_row_count else 0),
            "stats": max(map(height, self._fit_widgets["stats"])) if cost else 0,
            "status": height(self._status_row)}
        shown = lambda name: name not in self._fit_hidden
        # The half is its block less the inner ones that are out; a banked block that only informs goes with it.
        heights["half"] = (height(self._lower) + (height(self._banked_frame)
                                                   if self._banked_visible and not self._banked_pinned else 0)
                           - sum(heights[name] for name in ("top_model", "chart", "stats") if shown(name)))
        content = self._content.winfo_reqheight()
        base = content - sum(heights[name] for name in DROP_ORDER if shown(name))
        overhead = self._main_frame.winfo_reqheight() + self._px(4) - content
        cap = self.config.panel_max_height
        limit = min(self._px(cap) if cap else work_height, work_height - 2 * self._px(12))
        self._fit_range = (overhead + base, overhead + base + sum(heights.values()))

        return blocks_to_hide({"base": base, **heights}, limit - overhead)

    def _show_blocks(self, hide: set):
        """Hide the blocks of `hide` and show the rest."""
        self._fit_hidden = hide
        for name, widgets in self._fit_widgets.items():
            for widget in widgets:
                self._show(widget, name not in hide)
        self._place_banked()
        self._place_rows()
        self._window.update_idletasks()

    def _place_banked(self):
        """Show the banked block when it has something to say, unless its half is dropped and it only
        informs: a block with the Use button or a result stays."""
        dropped = "half" in self._fit_hidden and not self._banked_pinned
        self._show(self._banked_frame, self._banked_visible and not dropped)

    def _load_initial_data(self):
        """Load exchange rates and Claude status, one load at a time."""
        with self._lock:
            if self._loading:
                return      # one is running, and sets the flag below when it is done
            self._loading = True

        def load():
            try:
                self._exchange_rates = get_exchange_rates()
                self._refresh_claude_status()
            except Exception:
                # The status row waits for the flag below; a raise here left it as it was for good.
                logger.exception("Could not load the exchange rates or the Claude status")
            # Set flag for UI thread to pick up
            with self._lock:
                self._status_needs_update = True
                self._loading = False

        threading.Thread(target=load, daemon=True).start()

    def _refresh_claude_status(self):
        """`claude auth status` starts the Claude CLI, a process of its own, and every window
        build and every click on Refresh asked for it. It runs at most once a minute and the
        answer is reused in between. Not at all while Claude is switched off: nothing in the
        panel reads it."""
        if not self.config.claude_enabled:
            return
        if self._claude_status is not None and \
                time.monotonic() - self._claude_status_at < _CLAUDE_STATUS_SECONDS:
            return
        self._claude_status = check_claude_status()
        self._claude_status_at = time.monotonic()

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

    def _get_enabled_engines(self) -> list[Engine]:
        """Get list of enabled engines from config, in selector order."""
        return [engine for engine in _ENGINE_META
                if getattr(self.config, _ENABLED_FLAG[engine], True)]

    # Every label in the panel goes through here, so the size bump that keeps
    # the panel legible on a 4K monitor lives in exactly one place.
    FONT_BUMP = 2

    def _font(self, size, weight="normal", mono=False, scale=None):
        """A font from design points, sized in pixels so it follows the panel's factor and not the
        DPI Tk sampled at start. `scale` is for a dialog, which has a factor of its own."""
        return (self.mono if mono else self.sans,
                (scale or self.scale).font_px(size + self.FONT_BUMP), weight)

    def _label(self, parent, **options):
        """A tk.Label. Tk's own border and padding (2, 1 and 1 pixels) are pixels too, so they
        follow the scale unless the call says otherwise. A label that keeps the default padding gets
        border and padding as one length, rounded once: the border of a flat label is only space,
        and rounded apart from the padding, the 1.5 pixels of it at 0.75 (the factor of a 1080p
        screen) became 2 on every label at once, a panel about 50 pixels taller than at 0.74.
        (Tk adds no padding to an image, so a label with one keeps its border as it was.)"""
        if not {"padx", "pady", "bd", "borderwidth", "image"} & options.keys():
            return tk.Label(parent, **{"bd": 0, "padx": self._px(3), "pady": self._px(3), **options})
        return tk.Label(parent, **{"bd": self._px(2), "padx": self._px(1), "pady": self._px(1), **options})

    def _button(self, parent, **options):
        """A tk.Button, with the same scaled defaults and its focus ring (1 pixel)."""
        return tk.Button(parent, **{"bd": self._px(2), "padx": self._px(1), "pady": self._px(1),
                                    "highlightthickness": self._px(1), **options})

    def _hairline(self, parent, pady=(10, 10)):
        return self._put(tk.Frame(parent, bg=self.separator_color, height=self._px(1)), pady)

    def _row(self, parent, left, right, lf, rf, lc, rc, pady=0):
        """Two labels on one line, left- and right-aligned. Returns (row, left, right)."""
        r = self._put(tk.Frame(parent, bg=self.bg_color), pady)
        l = self._label(r, text=left, font=lf, fg=lc, bg=self.bg_color)
        l.pack(side=tk.LEFT)
        rr = self._label(r, text=right, font=rf, fg=rc, bg=self.bg_color)
        rr.pack(side=tk.RIGHT)
        return r, l, rr

    def _create_header(self, parent):
        """Logo and provider name, engine tabs, close."""
        header = self._put(tk.Frame(parent, bg=self.bg_color))

        logo = _logo(self._px(28))
        if logo is not None:
            self._logo_image = ImageTk.PhotoImage(logo)
            self._label(header, image=self._logo_image,
                        bg=self.bg_color).pack(side=tk.LEFT, padx=self._spacing((0, 8)))

        self._engine_name = self._label(header, text="", font=self._font(11, "bold"),
                                        fg=self.text_primary, bg=self.bg_color)
        self._engine_name.pack(side=tk.LEFT)

        close_btn = self._label(header, text="\u2715", font=self._font(10),
                                fg=self.text_muted, bg=self.bg_color, cursor="hand2", padx=self._px(4))
        close_btn.pack(side=tk.RIGHT)
        close_btn.bind("<Button-1>", lambda e: self.hide())
        close_btn.bind("<Enter>", lambda e: close_btn.config(fg=self.warn_color))
        close_btn.bind("<Leave>", lambda e: close_btn.config(fg=self.text_muted))

        enabled_engines = self._get_enabled_engines()
        if self._active_engine not in enabled_engines and enabled_engines:
            self._active_engine = enabled_engines[0]
        if len(enabled_engines) > 1:
            self._engine_frame = tk.Frame(header, bg=self.bg_color)
            self._engine_frame.pack(side=tk.RIGHT, padx=self._spacing((0, 8)))
            for engine in enabled_engines:
                name = _ENGINE_META[engine][0]
                # The icon carries the button; the text is the fallback when a
                # mark is missing and could not be downloaded.
                icons = self._engine_icon_images(engine)
                if icons:
                    self._engine_icons[engine] = icons
                    btn = self._label(self._engine_frame, image=icons[0], bg=self.inactive_engine_bg,
                                      padx=self._px(8), pady=self._px(3), cursor="hand2")
                else:
                    btn = self._label(self._engine_frame, text=name, font=self._font(8),
                                      fg=self.text_primary, bg=self.inactive_engine_bg,
                                      padx=self._px(10), pady=self._px(3), cursor="hand2")
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

    def _create_status_row(self, parent):
        """Updated time (and staleness) on the left, connection dot on the right.

        The dot says connected; words beside it are the plan, or the problem.
        """
        r, self._updated_label, status = self._row(
            parent, "Updated just now", "", self._font(8), self._font(8),
            self.text_muted, self.text_muted, pady=(6, 0))
        self._status_row, self._status_text = r, status
        # The dot carries the whole connection state at a glance, so it runs
        # well ahead of the text beside it and sits on its own line box.
        # Drawn, not typed. As a bullet glyph the dot's box is the font's line
        # box, so its ink sat low against the word beside it and the space
        # between them was the glyph's advance width rather than a chosen
        # number - both visible the moment the row is looked at closely. A
        # circle in a canvas of the dot's own size is centred on the row like
        # any other widget, and the gap is the padding and nothing else.
        dot = self._px(9)
        self._status_indicator = tk.Canvas(status.master, width=dot, height=dot,
                                           bg=self.bg_color, highlightthickness=0,
                                           bd=0)
        self._status_dot = self._status_indicator.create_oval(
            0, 0, dot - 1, dot - 1, fill=self.text_muted, outline="")
        self._status_indicator.pack(side=tk.RIGHT, padx=self._spacing((0, 6)))

        self._stale_label = self._label(r, text="", font=self._font(8), fg=self.accent_color, bg=self.bg_color)
        self._stale_label.pack(side=tk.LEFT, padx=self._spacing((6, 0)))
        # Only DeepSeek fills this: a note that its figures were converted.
        self._rates_label = self._label(r, text="", font=self._font(8), fg=self.text_muted, bg=self.bg_color)
        self._rates_label.pack(side=tk.LEFT, padx=self._spacing((6, 0)))

    def _create_limit(self, parent, key, title):
        """One rate-limit block: title/reset, bar, left/run-out, pace."""
        frame = self._put(tk.Frame(parent, bg=self.bg_color), (0, 8))
        _, heading, reset = self._row(frame, title, "", self._font(9, "bold"), self._font(8),
                                      self.text_primary, self.text_muted)
        bar = self._put(PaceBar(frame, width=348, bg=self.bg_color, track=self.track_color, scale=self.scale.k))
        _, left, note = self._row(frame, "", "", self._font(8, mono=True), self._font(8),
                                  self.text_secondary, self.text_muted)
        pace = self._put(self._label(frame, text="", font=self._font(8), fg=self.text_muted, bg=self.bg_color),
                         sticky="w")
        self._limits[key] = {"frame": frame, "title": heading, "reset": reset, "bar": bar,
                             "left": left, "note": note, "pace": pace}

    def _create_limits(self, parent):
        self._create_limit(parent, "session", "Session (5-hour)")
        self._create_limit(parent, "weekly", "Weekly")

        # Extra usage (paid overage), shown only when enabled.
        self._extra_frame = self._put(tk.Frame(parent, bg=self.bg_color), shown=False)
        _, _, self._extra_amount = self._row(self._extra_frame, "Extra usage", "", self._font(9, "bold"),
                                             self._font(8), self.text_primary, self.text_muted)
        self._extra_bar = self._put(PaceBar(self._extra_frame, width=348, bg=self.bg_color,
                                            track=self.track_color, scale=self.scale.k))

    def _create_balance(self, parent):
        """DeepSeek balance block. Stands in the row where the limits sit,
        because DeepSeek exposes no session or weekly quota."""
        self._balance_frame = self._put(tk.Frame(parent, bg=self.bg_color), (0, 8), shown=False)
        self._row(self._balance_frame, "Balance", "", self._font(9, "bold"),
                  self._font(8), self.text_primary, self.text_muted)
        self._balance_value = self._put(
            self._label(self._balance_frame, text="", font=self._font(16, "bold", mono=True),
                        fg=self.text_primary, bg=self.bg_color), (2, 0), "w")
        # Shown only when the balance cannot pay for calls; a usable balance
        # needs no caption. Shown by _fill_deepseek_view.
        self._balance_state = self._put(
            self._label(self._balance_frame, text="", font=self._font(8), fg=self.warn_color, bg=self.bg_color),
            sticky="w", shown=False)

    def _create_banked(self, parent):
        """Saved one-time limit resets (Claude and Codex), under the limit bars.
        Shown only when there is something to say."""
        self._banked_frame = self._put(tk.Frame(parent, bg=self.bg_color), (8, 0), shown=False)
        row, _, self._banked_hint = self._row(self._banked_frame, "Banked resets", "",
                                              self._font(9, "bold"), self._font(8),
                                              self.text_primary, self.text_muted)
        # The action. Packed only while a live reset can be spent (or one is in
        # flight, when it shows disabled); otherwise a short reason stands here.
        self._banked_use = self._button(
            row, text="Use", command=self._on_use_reset, font=self._font(8, "bold"),
            fg=self.accent_color, bg=self.bg_color, activeforeground=self.text_primary,
            activebackground=self.active_engine_bg, disabledforeground=self.text_muted,
            relief=tk.FLAT, bd=0, cursor="hand2", padx=self._px(8), pady=0, takefocus=1,
            highlightthickness=self._px(1), highlightbackground=self.bg_color,
            highlightcolor=self.accent_color)
        self._banked_use.bind("<Enter>", lambda e: self._banked_use.cget("state") == tk.NORMAL
                              and self._banked_use.config(fg=self.text_primary))
        self._banked_use.bind("<Leave>", lambda e: self._banked_use.config(fg=self.accent_color))
        self._banked_line = self._put(self._label(
            self._banked_frame, text="", font=self._font(8), fg=self.text_secondary, bg=self.bg_color),
            sticky="w", shown=False)
        # What happened to the last attempt to spend one (until it times out).
        self._banked_notice_label = self._put(self._label(
            self._banked_frame, text="", font=self._font(8), fg=self.text_secondary,
            bg=self.bg_color, justify=tk.LEFT, anchor=tk.W, wraplength=self._px(348)),
            (2, 0), "w", shown=False)

    def _create_tabs(self, parent):
        """Two labels that switch the lower half between today's costs and the
        quota forecast. The active one carries an underline in the provider's
        accent, resting on a hairline that runs the width of the strip."""
        self._tabs = {}
        strip = self._put(tk.Frame(parent, bg=self.bg_color))
        row = tk.Frame(strip, bg=self.bg_color)
        row.pack(fill=tk.X)
        for view in PANEL_VIEWS:
            tab = tk.Frame(row, bg=self.bg_color)
            tab.pack(side=tk.LEFT, padx=self._spacing((0, 18)))
            # A label rather than a button so it matches the rest of the panel's
            # type; focusable, with Return and Space, so the keyboard reaches it.
            label = self._label(tab, text="", font=self._font(9, "bold"), bg=self.bg_color,
                                cursor="hand2", takefocus=1, highlightthickness=self._px(1),
                                highlightbackground=self.bg_color, highlightcolor=self.accent_color,
                                padx=0, pady=self._px(2))
            label.pack()
            rule = tk.Frame(tab, bg=self.bg_color, height=self._px(2))
            rule.pack(fill=tk.X)
            for event in ("<Button-1>", "<Return>", "<space>"):
                label.bind(event, lambda e, v=view: self._on_view_select(v))
            label.bind("<Enter>", lambda e, v=view: self._tab_hover(v, True))
            label.bind("<Leave>", lambda e, v=view: self._tab_hover(v, False))
            self._tabs[view] = (label, rule)
        tk.Frame(strip, bg=self.separator_color, height=self._px(1)).pack(fill=tk.X)
        self._put(tk.Frame(parent, bg=self.bg_color, height=self._px(10)))

    def _tab_hover(self, view, inside):
        label, _ = self._tabs[view]
        if view != self.config.panel_view:
            label.config(fg=self.text_secondary if inside else self.text_muted)

    def _paint_tabs(self, engine: Engine):
        accent = _ENGINE_META[engine][2]
        for view, title in zip(PANEL_VIEWS, tab_titles(engine)):
            label, rule = self._tabs[view]
            on = view == self.config.panel_view
            label.config(text=title, fg=self.text_primary if on else self.text_muted)
            rule.config(bg=accent if on else self.bg_color)

    def _sync_view(self, engine: Engine):
        """Label the tabs for this engine and show the chosen half of the panel."""
        self._paint_tabs(engine)
        forecast_view = self.config.panel_view == "forecast"
        self._show(self._cost_frame, not forecast_view)
        self._show(self._forecast_frame, forecast_view)

    def _on_view_select(self, view: str):
        if view == self.config.panel_view:
            return
        self.config.panel_view = view
        save_config(self.config)
        self._refresh_display_immediate()

    def _create_forecast(self, parent):
        """The quota (or balance) view: the rate as its headline, the chart, detail rows. It says
        only what the blocks above the tabs do not: no percent left, balance, banked count or
        "cached" (forecast_text words it)."""
        header = self._put(tk.Frame(parent, bg=self.bg_color), (0, 6))
        self._fc_value = self._label(header, text="", font=self._font(16, "bold", mono=True),
                                     fg=self.accent_color, bg=self.bg_color)
        self._fc_value.pack(side=tk.LEFT)
        self._fc_caption = self._label(header, text="", font=self._font(8), fg=self.text_muted,
                                       bg=self.bg_color)
        self._fc_caption.pack(side=tk.LEFT, padx=self._spacing((8, 0)), pady=self._spacing((0, 2)), anchor=tk.S)
        self._fc_chart = self._put(ForecastChart(
            parent, width=348, height=_FORECAST_HEIGHT, bg=self.bg_color, font=self._font(7),
            tip_font=self._font(9, "bold"), scale=self.scale.k, grid=self.separator_color,
            muted=self.text_muted))
        self._fc_rows_host = self._put(tk.Frame(parent, bg=self.bg_color), (8, 0), shown=False)
        # The detail rows are a pool: each fill shows the first few, in order (_set_forecast_rows).
        self._fc_rows = []
        for _ in range(_FORECAST_ROWS):
            row, left, right = self._row(self._fc_rows_host, "", "", self._font(8), self._font(8),
                                         self.text_muted, self.text_secondary, pady=1)
            row.grid_remove()
            self._fc_rows.append((row, left, right))

    def _set_forecast_rows(self, rows) -> int:
        """Show the given (label, value, tone) rows in the pool, in order."""
        colours = {"normal": self.text_secondary, "ok": self.ok_color,
                   "warn": self.warn_color, "muted": self.text_muted}
        shown = rows[:len(self._fc_rows)]
        for index, (frame, left, right) in enumerate(self._fc_rows):
            self._show(frame, index < len(shown))
            if index < len(shown):
                label, value, tone = shown[index]
                left.config(text=label)
                right.config(text=value, fg=colours[tone])
        self._fc_row_count = len(shown)
        self._place_rows()
        return len(shown)

    def _place_rows(self):
        """Show the detail rows' host when it has a row and the fit has not dropped it. An empty host
        would keep the height of the rows it last held, a gap under the chart."""
        self._show(self._fc_rows_host, self._fc_row_count > 0 and "chart" not in self._fit_hidden)

    def _forecast_empty(self, message: str) -> int:
        """Nothing to forecast yet: the plot stays, with the reason on it."""
        self._fc_value.config(text="—")
        self._fc_caption.config(text="")
        self._fc_chart.set_data(None, message)
        return self._set_forecast_rows([])

    def _create_stat(self, parent, key, title, row, column):
        # The gap between the two rows rides on the lower one, which the fit drops.
        f = self._place(tk.Frame(parent, bg=self.bg_color), row, column, (8, 0) if row else 0)
        heading = self._label(f, text=title, font=self._font(8), fg=self.text_muted, bg=self.bg_color)
        heading.pack(anchor=tk.W)
        value = self._label(f, text="", font=self._font(12, "bold", mono=True), fg=self.text_primary, bg=self.bg_color)
        value.pack(anchor=tk.W)
        sub = self._label(f, text="", font=self._font(8), fg=self.text_muted, bg=self.bg_color)
        sub.pack(anchor=tk.W)
        self._stats[key] = (heading, value, sub)

    def _create_stats(self, parent):
        """2x2 grid of stat cells. Titles and figures are set per engine; the
        slots only fix the positions."""
        grid = self._put(tk.Frame(parent, bg=self.bg_color))
        # Both columns share one uniform width. Laying each row out on its own
        # let the split fall at a different x per row, so the right-hand cells
        # did not line up whenever one row's text measured wider than the
        # other's ("377.9M tokens" against "2.73B tokens").
        grid.columnconfigure(0, weight=1, uniform="stat")
        grid.columnconfigure(1, weight=1, uniform="stat")
        cells = (("today", "Today"),
                 ("last7", "Last 7 days"),
                 ("month", "This month"),
                 ("last31", f"Last {DAILY_HISTORY_DAYS} days"))
        for index, (key, title) in enumerate(cells):
            self._create_stat(grid, key, title, row=index // 2, column=index % 2)

    def _create_chart(self, parent):
        """The daily chart under its hairline, and the top-model row under that: two blocks the fit
        drops one after the other."""
        self._chart_group = self._put(tk.Frame(parent, bg=self.bg_color))
        self._hairline(self._chart_group)
        _, self._chart_title, self._chart_note = self._row(
            self._chart_group, "Daily API-equivalent cost", "", self._font(8),
            self._font(8), self.text_muted, self.text_muted)
        self._put(tk.Frame(self._chart_group, bg=self.bg_color, height=self._px(4)))
        # 78 px: tall enough for 31 bars to read as a shape, the same on every
        # engine so the chart does not change as the tabs do.
        self._chart = self._put(BarChart(
            self._chart_group, width=348, height=78, bg=self.bg_color, font=self._font(7),
            tip_font=self._font(9, "bold"), scale=self.scale.k))
        self._top_group = self._put(tk.Frame(parent, bg=self.bg_color))
        self._put(tk.Frame(self._top_group, bg=self.bg_color, height=self._px(6)))
        _, self._top_model, self._top_share = self._row(
            self._top_group, "", "", self._font(8, mono=True), self._font(8), self.text_secondary, self.text_muted)

    def _create_footer(self, parent):
        """Source note on the left, actions on the right."""
        self._hairline(parent)
        footer = self._put(tk.Frame(parent, bg=self.bg_color))
        footer.columnconfigure(0, weight=1)
        self._source_label = self._label(footer, text="", font=self._font(8), fg=self.text_muted, bg=self.bg_color)
        self._source_label.grid(row=0, column=0, sticky="w")

        button_style = {
            "font": self._font(8), "bg": self.bg_color, "fg": self.text_secondary,
            "activebackground": self.active_engine_bg, "activeforeground": self.text_primary,
            "relief": tk.FLAT, "cursor": "hand2", "padx": self._px(6), "pady": self._px(2), "bd": 0,
        }
        for column, (text, command, hover) in enumerate((("Settings", self._on_settings_click, self.text_primary),
                                                         ("Refresh", self._on_refresh_click, self.text_primary),
                                                         ("Exit", self._on_exit_click, self.warn_color)), 1):
            btn = self._button(footer, text=text, command=command, **button_style)
            btn.grid(row=0, column=column)
            btn.bind("<Enter>", lambda e, b=btn, c=hover: b.config(fg=c))
            btn.bind("<Leave>", lambda e, b=btn: b.config(fg=self.text_secondary))

    def _update_status_display(self):
        """Update the status display based on active engine.

        A green dot is the whole "connected" message; the text beside it is the
        plan when one is known, and otherwise stays empty until something is
        wrong.
        """
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

        engine = self._active_engine
        if engine == Engine.CLAUDE:
            snapshot = valid_snapshot
            if snapshot:
                with self._lock:
                    self._oauth_error = None
        elif engine == Engine.CODEX:
            snapshot = self._openai_snapshot
        else:
            snapshot = self._deepseek_snapshot

        tone, text = status_for(engine, snapshot, self._claude_status, oauth_error)
        self._status_colour(getattr(self, _STATUS_TONES[tone]))
        self._status_text.config(text=text)

    def _status_colour(self, colour):
        """Colour the status dot. It is a canvas item, not a label's foreground."""
        if self._status_indicator and self._status_dot is not None:
            self._status_indicator.itemconfig(self._status_dot, fill=colour)

    def _on_refresh_click(self):
        """Handle refresh button."""
        self.on_refresh()
        self._load_initial_data()

    def _on_settings_click(self):
        """Handle settings button."""
        if self._window:
            SettingsDialog(self._window, self.config, self._on_settings_saved,
                           on_refresh=self.on_refresh,
                           session_state=lambda: self._deepseek_snapshot,
                           scale=self._dialog_scale())

    def _on_settings_saved(self):
        """Handle settings save: the content is built again, in the window that is already up."""
        if self.on_settings_saved:
            self.on_settings_saved()
        # No rates fetch here: this is the Tk thread. The rebuild below starts the loader.
        self._rebuild_window()

    def _rebuild_window(self):
        """Build the content again after a Settings change, inside the window that is up."""
        with self._veiled():
            if self._window is None:
                self._create_window()
            else:
                self._build_content(self._choose_scale())
                self._load_initial_data()
            self._refill()
            self.show()

    def _refill(self):
        """Fill widgets that were just built from the snapshots the panel already holds."""
        if self._snapshot or self._openai_snapshot or self._deepseek_snapshot:
            self._refresh_display_immediate()
            self._update_status_display()

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
            # combined.active_engine is not read back: the panel owns the choice
            # of engine, and a snapshot collected before a tab click carries
            # the engine from before it.
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
        # The status row is part of this display, not a separate one. Without
        # this a new snapshot filled the balance block while the row above it
        # still described the previous one - a panel reading "Not signed in"
        # directly above a live balance, which is what a sign-in looked like.
        self._update_status_display()

    def _refresh_display_immediate(self):
        """Fill every widget from the active engine's snapshot."""
        if not self._window or not self._window.winfo_exists():
            return

        self._redraw_at = time.monotonic() + _REDRAW_SECONDS
        money = lambda usd: format_currency(usd, self.config.currency, self._exchange_rates)
        engine = self._active_engine
        self._apply_accent(engine)
        self._sync_view(engine)

        if engine == Engine.CLAUDE and self._snapshot:
            self._fill_claude_view(self._snapshot, money)
        elif engine == Engine.CODEX and self._openai_snapshot:
            self._fill_codex_view(self._openai_snapshot, money)
        elif engine == Engine.DEEPSEEK and self._deepseek_snapshot:
            self._fill_deepseek_view(self._deepseek_snapshot, money)

    def _redraw_if_due(self):
        """Redraw an open panel once a minute: a countdown moves, and a window can end,
        without any new reading arriving."""
        if time.monotonic() >= self._redraw_at and self._window.winfo_viewable():
            self._refresh_display_immediate()

    def _apply_accent(self, engine: Engine):
        """Each engine tints the shared bars and chart with its own colour."""
        accent = _ENGINE_META[engine][2]
        for widget in (self._limits["session"]["bar"], self._limits["weekly"]["bar"],
                       self._extra_bar, self._chart, self._fc_chart):
            if widget is not None:
                widget.set_accent(accent)

    def _show_limits(self, session_visible: bool, extra_visible: bool):
        """Show the rate-limit rows and hide the DeepSeek balance block."""
        self._show(self._balance_frame, False)
        self._show(self._limits["session"]["frame"], session_visible)
        self._show(self._limits["weekly"]["frame"], True)
        self._show(self._extra_frame, extra_visible)

    def _show_balance(self) -> None:
        """Show the balance block, which stands where the limits would be."""
        for widget in (self._limits["session"]["frame"], self._limits["weekly"]["frame"],
                       self._extra_frame):
            self._show(widget, False)
        self._show(self._balance_frame, True)
        self._banked_visible = False
        self._place_banked()

    def _collecting(self, snap) -> bool:
        """Nothing has been read yet: no figure, and not a cached reading either."""
        return (self._is_collecting and snap.session_percent == 0 and snap.weekly_percent == 0
                and not snap.is_stale)

    @staticmethod
    def _at_work(wrote: Optional[datetime], since: datetime) -> bool:
        """Whether the newest log write says a session is at work: after `since`, or within the
        last _STILL_WORKING."""
        return wrote is not None and wrote > min(since, datetime.now().astimezone() - _STILL_WORKING)

    def _logs_written_since(self, since: datetime) -> bool:
        """Whether a session is at work, by the logs: one written after `since`, or within the last
        _STILL_WORKING, as the last look at the logs saw it.

        A look stats every transcript (90 ms for 1,500 of them), which the Tk thread must not
        spend, so it is made on a thread of its own: each redraw that asks starts one (one at a
        time) and is answered from the one before. The panel is redrawn as soon as a look turns
        up a different answer than the redraw drew, so a session that begins is on screen about
        a redraw (a minute) after it began, as it was when the look ran inline. It could not come
        from the refresh instead: that is as old as the refresh interval (five minutes by
        default, whatever the user sets), and a cached reading is just what a refresh that was
        refused leaves."""
        with self._lock:
            wrote = self._log_write
            answer = self._at_work(wrote, since)
            self._log_asked = (since, answer)
            start = not self._looking
            self._looking = True
        if start:
            threading.Thread(target=self._look_at_logs, name="claudebar-log-look", daemon=True).start()
        return answer

    def _look_at_logs(self):
        try:
            wrote = log_parser.last_log_write()
        except Exception:
            logger.exception("Could not look at the Claude Code logs")
            wrote = self._log_write
        with self._lock:
            self._log_write = wrote
            self._looking = False
            asked = self._log_asked
            if asked is not None and self._at_work(wrote, asked[0]) != asked[1]:
                self._redraw_at = 0.0       # what is on screen is out of date: the next tick redraws

    def _fill_claude_view(self, snap: UsageSnapshot, money):
        unavailable = not snap.cli_available and not snap.is_stale
        # Unavailable wins over Collecting. _is_collecting is cleared at the end of the render rather than the
        # start, because _fill_meta needs it, so the first render after startup has it still set - and with both
        # percentages at zero that made a definitive failure read as "Collecting..." for one frame. A failed
        # fetch is not a pending one.
        placeholder = "Unavailable" if unavailable else ("Collecting..." if self._collecting(snap) else None)
        running = False
        since = None if placeholder else snap.session_unseen_since()
        if since is not None:       # the log scan is only worth its time for a reading like this
            running = self._logs_written_since(since)

        self._limits["session"]["title"].config(text="Session (5-hour)")
        self._limits["weekly"]["title"].config(text="Weekly")
        self._fill_limit("session", snap.session_percent, snap.session_reset,
                         snap.session_resets_at, SESSION_WINDOW_HOURS, placeholder,
                         quota_history.window_key(Engine.CLAUDE.value, True), running)
        self._fill_limit("weekly", snap.weekly_percent, snap.weekly_reset,
                         snap.weekly_resets_at, WEEKLY_WINDOW_HOURS, placeholder)
        self._show_limits(session_visible=True, extra_visible=snap.extra_enabled)
        banked_shown = self._fill_banked(snap)

        if snap.extra_enabled:
            symbol = get_currency_symbol(snap.extra_currency.upper())
            self._extra_bar.set_value(snap.extra_percent)
            self._extra_amount.config(
                text=f"{snap.extra_percent:.0f}% used \u00b7 {symbol}{snap.extra_used:.2f} / {symbol}{snap.extra_limit:.2f}")

        self._fill_log_view(Engine.CLAUDE, snap, money, snap.models_used,
                            placeholder or (_SESSION_RUNNING_CHART if running else None),
                            banked_shown, snap.extra_enabled,
                            "Estimated from local Claude Code logs", "Usage limits from the Claude API")

    def _fill_codex_view(self, snap: OpenAISnapshot, money):
        placeholder = "Collecting..." if self._collecting(snap) else None

        # The window lengths come from the API: the "weekly" slot holds any
        # window over six hours, and a free plan's is 30 days.
        self._limits["session"]["title"].config(text=f"Session ({snap.session_window_hours:g}-hour)")
        self._limits["weekly"]["title"].config(text=forecast_text.window_title(snap.weekly_window_hours))
        # Only the charted window is recorded, so only its bar has a recent rate to use.
        session_charted = snap.charted_window()[0]
        key = quota_history.window_key(Engine.CODEX.value, session_charted)
        self._fill_limit("session", snap.session_percent, snap.session_reset,
                         snap.session_resets_at, snap.session_window_hours, placeholder,
                         key if session_charted else None)
        self._fill_limit("weekly", snap.weekly_percent, snap.weekly_reset,
                         snap.weekly_resets_at, snap.weekly_window_hours, placeholder,
                         None if session_charted else key)
        self._show_limits(session_visible=snap.session_available, extra_visible=False)
        banked_shown = self._fill_banked(snap)

        self._fill_log_view(Engine.CODEX, snap, money, [], placeholder, banked_shown,
                            snap.session_available, "Estimated from local Codex logs",
                            "Usage limits from the Codex API")

    def _fill_log_view(self, engine: Engine, snap, money, models, placeholder, banked_shown: bool,
                       layout_flag: bool, cost_source: str, forecast_source: str):
        """What the Claude and Codex views share once their own limits are filled: the
        figures costed from local logs, the quota view, the footer and the layout.
        `layout_flag` is the one optional row that engine adds (extra usage, or the
        session limit)."""
        self._set_stat("today", money(snap.today_cost_usd),
                       f"{format_tokens(snap.today_total_tokens)} tokens", "Today")
        self._set_stat("month", money(snap.month_cost_usd),
                       f"{format_tokens(snap.month_total_tokens)} tokens", "This month")
        self._set_stat("last31", money(snap.last31_cost_usd),
                       f"{format_tokens(snap.last31_tokens)} tokens", f"Last {DAILY_HISTORY_DAYS} days")
        self._fill_daily_costs(snap.daily_costs, snap.timestamp.date(), money)
        self._fill_top_model(models, snap.month_cost_usd)
        quota_rows = self._fill_quota_view(engine, snap, snap.banked, placeholder)
        self._fill_meta(snap, self._source_text(cost_source, forecast_source), "")
        self._apply_layout((engine.value, layout_flag, banked_shown, self.config.panel_view, quota_rows))

    def _fill_daily_costs(self, daily_costs, today, money):
        """The week cell and the month chart, for the engines costed from local
        logs. Their snapshots keep a cost per day but no tokens per day, so the
        week cell carries a daily average rather than a token count."""
        days = list(daily_costs)
        week = days[-7:]
        total = sum(week)
        self._set_stat("last7", money(total), f"avg {money(total / max(len(week), 1))}/day",
                       "Last 7 days")
        self._fill_chart(days, today, "Daily API-equivalent cost", money)

    def _money_fixed(self, usd: float) -> str:
        """Money with two decimals. format_currency drops to four below a cent,
        which is right for a day's spend but wrong for a balance."""
        symbol = get_currency_symbol(self.config.currency)
        return f"{symbol}{convert_usd(usd, self.config.currency, self._exchange_rates):,.2f}"

    def _fill_deepseek_view(self, snap: DeepSeekSnapshot, money):
        """DeepSeek has no quota, so its limits give way to balance and spend."""
        self._show_balance()

        self._balance_value.config(text=self._money_fixed(snap.balance_total) if snap.balance_available else "\u2014")
        # The figure stands alone. The state line is for when the balance cannot
        # pay for calls; "Available for API calls" under every usable balance,
        # and the top-up total beside it, were read past rather than read.
        usable = snap.balance_available and snap.balance_usable
        self._balance_state.config(text="" if usable else snap.balance_message)
        self._show(self._balance_state, not usable)

        # Four windows, read left to right and top to bottom: each cell is a
        # cost with the token count for the same window underneath it. The slot
        # keys are positions in the shared grid, not the windows they show here.
        windows = (
            ("today", "Today", snap.today_cost_usd, snap.today_tokens),
            ("last7", "Last 7 days", snap.last7_cost_usd, snap.last7_tokens),
            ("month", "This month", snap.month_cost_usd, snap.month_tokens),
            ("last31", "Prev month", snap.prev_month_cost_usd, snap.prev_month_tokens),
        )
        if snap.usage_available:
            for key, title, cost, tokens in windows:
                self._set_stat(key, money(cost), f"{format_tokens(tokens)} tokens", title)
        else:
            hint = "Sign in to see usage" if snap.usage_error else "No usage reported"
            for key, title, _, _ in windows:
                self._set_stat(key, "\u2014", hint, title)

        extras = []
        for i in range(len(snap.daily_costs)):
            parts = []
            if i < len(snap.daily_tokens):
                parts.append(f"{format_tokens(snap.daily_tokens[i])} tokens")
            if i < len(snap.daily_requests):
                parts.append(f"{snap.daily_requests[i]:,} req")
            extras.append(parts)
        self._fill_chart(snap.daily_costs, snap.timestamp.date(), "Daily spend", money,
                         dates=snap.daily_dates, extras=extras)
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
        balance_rows = self._fill_balance_view(snap, money)
        self._fill_meta(snap, self._source_text(
            source, "Balance from the DeepSeek platform" if snap.balance_available else source), rates)
        self._apply_layout(("deepseek", snap.usage_available, usable, self.config.panel_view,
                            balance_rows))

    @staticmethod
    def _chart_days(count, today, dates=()) -> list:
        """The date of each of `count` bars, oldest first.

        DeepSeek supplies its own dates (the platform buckets by its own day),
        so those win when present; otherwise the days count back from today.
        Both are oldest first, like the chart, so the dates are read forwards:
        indexing them by days-ago paired every bar with the wrong day and read
        the whole row back to front.
        """
        # Trimmed like the values, so a longer parallel series still lines up.
        window = list(dates)[-count:] if count and dates else []
        days = []
        for i in range(count):
            stamp = None
            if i < len(window) and window[i]:
                try:
                    stamp = datetime.strptime(window[i], "%Y-%m-%d").date()
                except ValueError:
                    stamp = None
            days.append(stamp or today - timedelta(days=count - 1 - i))
        return days

    def _fill_chart(self, values, today, title, money, dates=(), extras=()):
        """The bars, and a flyout per bar: date and cost, then any extras."""
        values = list(values)
        tooltips = []
        for i, (day, value) in enumerate(zip(self._chart_days(len(values), today, dates), values)):
            parts = [f"{day:%b} {day.day}", money(value)]
            parts += extras[i] if i < len(extras) else []
            tooltips.append(" · ".join(parts))
        self._chart_title.config(text=title)
        self._chart.set_values(values, tooltips)
        self._chart_note.config(text=f"{len(values)} days, peak {money(max(values))}" if values else "")

    def _fill_top_model(self, models, month_cost: float):
        top = max(models, key=lambda m: m.cost_usd, default=None)
        if top and month_cost > 0:
            self._top_model.config(text=f"Top model  {top.model}")
            self._top_share.config(text=f"{top.cost_usd / month_cost * 100:.0f}% of month")
        else:
            self._top_model.config(text="")
            self._top_share.config(text="")

    def _fill_meta(self, snap, source_text: str, rates_text: str):
        self._rates_label.config(text=f"· {rates_text}" if rates_text else "")
        # One time, when the figures were last updated. For old ones that is the
        # last good reading, not the attempt that failed: two times side by side
        # ("Updated 16:28 (from 16:23)") read as a contradiction.
        old = snap.is_stale and snap.stale_since is not None
        when = snap.stale_since if old else snap.timestamp
        stamp = f"{when:%H:%M}" if when.date() == datetime.now().date() else f"{when:%b %d %H:%M}"
        collecting = self._is_collecting and snap.is_stale
        self._updated_label.config(text="Collecting fresh data..." if collecting else f"Updated {stamp}")
        # Clear once any snapshot has been applied (stale or fresh); the stale
        # label keeps communicating staleness.
        self._is_collecting = False
        self._stale_label.config(text="(cached)" if old else "")
        self._source_label.config(text=source_text)

    def _apply_layout(self, layout_key):
        if layout_key != self._layout_key:
            self._layout_key = layout_key
            self._update_window_size()

    def _source_text(self, cost_text: str, forecast_text_: str) -> str:
        """The footer's source note follows the half of the panel showing."""
        return forecast_text_ if self.config.panel_view == "forecast" else cost_text

    def _fill_banked(self, snap) -> tuple:
        """Show the banked-resets block under the limit bars, or hide it.

        Drawn when there is something to say: at least one reset available, or
        a failure to load them, or the result of spending one. An account that is
        not eligible, or has none, gets no row. Returns what decides the block's
        height (whether it shows, its line, the Use button, the notice's text),
        which is part of the layout key.
        """
        banked, stale = snap.banked, snap.is_stale
        now = datetime.now(timezone.utc)
        notice = self._active_notice()
        has_data = banked is not None and (
            banked.state == "failed" or (banked.state == "ok" and banked.available > 0))
        # A result stays on the panel even when the reset it spent was the last one.
        self._banked_visible = shown = has_data or notice is not None
        if not shown:
            self._banked_pinned = False
            self._place_banked()
            return (False,)

        # Spendable only from live data; cached data is shown, never offered.
        claim = (banked_resets.claim_for(banked, True, now)
                 if banked is not None and self._spendable(snap) else None)
        in_flight = self._redeem_flow.in_flight
        use_shown = claim is not None or in_flight
        # A block that offers the reset or carries a result is not dropped with the Cost/Quota half.
        self._banked_pinned = use_shown or notice is not None
        if use_shown:
            self._banked_hint.config(text="")
            self._banked_use.config(text="Using…" if in_flight else "Use",
                                    state=tk.DISABLED if in_flight else tk.NORMAL)
            if not self._banked_use.winfo_manager():
                self._banked_use.pack(side=tk.RIGHT)
        else:
            if self._banked_use.winfo_manager():
                self._banked_use.pack_forget()
            # Why a live reset cannot be used right now; cached data names no reason.
            self._banked_hint.config(
                text="" if stale or banked is None else banked_resets.claim_hint(banked, now))

        if has_data:
            if banked.state == "failed":
                text, colour = "Couldn't load banked resets", self.text_muted
            else:
                text, urgent = banked_resets.summary_text(banked, now)
                colour = self.accent_color if urgent else self.text_secondary
            if stale:
                text += " · cached"
            self._banked_line.config(text=text, fg=colour)
        self._show(self._banked_line, has_data)
        if notice is not None:
            self._banked_notice_label.config(text=notice[0], fg=notice[1])
        self._show(self._banked_notice_label, notice is not None)
        self._place_banked()
        return True, has_data, use_shown, notice[0] if notice is not None else ""

    # --- spending a banked reset ---------------------------------------------------------

    def _active_snapshot(self):
        return {Engine.CLAUDE: self._snapshot,
                Engine.CODEX: self._openai_snapshot}.get(self._active_engine)

    def _spendable(self, snap) -> bool:
        """Live data, collected after the last attempt finished: never the cached
        fallback, and never a snapshot that predates a result."""
        return snap is not None and not snap.is_stale and snap.timestamp > self._redeem_floor

    def _banked_claim(self):
        """What may be spent right now from the active engine, or None."""
        snap = self._active_snapshot()
        if not self._spendable(snap):
            return None
        return banked_resets.claim_for(snap.banked, True, datetime.now(timezone.utc))

    def _usage_text(self) -> str:
        """The current usage the confirmation quotes, in the panel's own words."""
        snap = self._active_snapshot()
        if self._active_engine == Engine.CLAUDE:
            return f"session {snap.session_percent:.0f}%, weekly {snap.weekly_percent:.0f}%"
        weekly = f"{forecast_text.window_title(snap.weekly_window_hours).lower()} {snap.weekly_percent:.0f}%"
        return f"session {snap.session_percent:.0f}%, {weekly}" if snap.session_available else weekly

    def _on_use_reset(self):
        """The Use button: ask first. Nothing is sent until the dialog confirms."""
        claim = self._banked_claim()
        if not self._window or not self._redeem_flow.request(claim):
            return
        lines = reset_redeem.confirm_lines(claim, self._usage_text(), datetime.now(timezone.utc))
        scale = self._dialog_scale()
        style = SimpleNamespace(
            bg=self.bg_color, primary=self.text_primary, secondary=self.text_secondary,
            muted=self.text_muted, accent=self.accent_color, warn=self.warn_color,
            button_bg=self.active_engine_bg, hover_bg=self.track_color, scale=scale,
            font=lambda size, weight="normal": self._font(size, weight, scale=scale))
        ResetConfirmDialog(self._window, lines, self._confirm_redeem, self._cancel_redeem, style)

    def _cancel_redeem(self):
        self._redeem_flow.cancel()

    def _confirm_redeem(self):
        """The user confirmed: send exactly one request, off the UI thread."""
        if self._redeem_flow.in_flight:
            return                                   # a repeated confirmation sends nothing
        attempt = self._redeem_flow.confirm(self._banked_claim())
        if attempt is None:
            self._set_notice("The reset changed · check the panel and try again", "warn",
                             self._active_engine.value)
            return
        # The flow is "sending" from here, so the worker starts before anything
        # else that could fail: a repaint error must not strand it.
        try:
            self._redeem_thread = threading.Thread(target=self._redeem_worker, args=(attempt,),
                                                   daemon=True)
            self._redeem_thread.start()
        except RuntimeError:                         # no thread: nothing was sent
            self._redeem_flow.finish(reset_redeem.NOT_SENT)
            self._set_notice(reset_redeem.NOT_SENT.message, "warn", self._active_engine.value)
            return
        self._refresh_display_immediate()            # Use shows disabled while it is out

    def _redeem_worker(self, attempt):
        try:
            outcome = self._run_attempt(attempt)
        except Exception as e:                       # never let a message carry a secret
            logger.warning("Banked reset request failed: %s", type(e).__name__)
            outcome = reset_redeem.UNKNOWN
        with self._lock:
            self._redeem_result = (attempt.provider, outcome)

    def _apply_redeem_result(self):
        """On the UI thread: show what happened, then re-fetch usage rather than
        working out the new state."""
        with self._lock:
            result, self._redeem_result = self._redeem_result, None
        if result is None:
            return
        provider, outcome = result
        self._redeem_flow.finish(outcome)
        self._redeem_floor = datetime.now()
        self._set_notice(outcome.message, "ok" if outcome.kind == "success"
                         else "info" if outcome.kind == "nothing" else "warn", provider)
        self.on_refresh()

    def _set_notice(self, text: str, tone: str, provider: str):
        colour = {"ok": self.ok_color, "info": self.text_secondary, "warn": self.warn_color}[tone]
        self._banked_notice = (text, colour, time.monotonic() + _NOTICE_SECONDS, provider)
        if self._window and self._window.winfo_exists():
            if self._notice_job is not None:
                self._window.after_cancel(self._notice_job)
            self._notice_job = self._window.after(_NOTICE_SECONDS * 1000, self._expire_notice)
        self._refresh_display_immediate()

    def _expire_notice(self):
        self._notice_job = None
        self._banked_notice = None
        self._refresh_display_immediate()

    def _active_notice(self):
        notice = self._banked_notice
        if notice is None or time.monotonic() >= notice[2] or notice[3] != self._active_engine.value:
            return None
        return notice

    def _banked_notice_text(self) -> str:
        notice = self._active_notice()
        return notice[0] if notice else ""

    def _fill_quota_view(self, engine: Engine, snap, banked, placeholder) -> int:
        """The quota view of the snapshot's charted window: the 5-hour session,
        or a Codex plan's one long window. Returns the detail rows shown (0
        while the other view is up, so the history is not read for nothing)."""
        if self.config.panel_view != "forecast":
            return 0
        accent = _ENGINE_META[engine][2]
        self._fc_value.config(fg=accent)
        self._fc_chart.set_accent(accent)
        session, used, resets_at, window_hours = snap.charted_window()
        if placeholder or (resets_at is None and used == 0):
            return self._forecast_empty(placeholder or ("No session running" if session
                                                        else "No quota reported"))

        now = datetime.now(timezone.utc)
        reset = forecast.as_utc(resets_at)
        if reset is not None and reset <= now:
            # The reading is the last one of a session that is over, so its figures
            # say nothing about the one that began at the reset. Drawing them
            # would put the old quota on the new window.
            return self._forecast_empty(f"{'Session' if session else 'Window'} reset - "
                                        "the chart starts with the next reading")
        records = quota_history.load(quota_history.window_key(engine.value, session),
                                     now - quota_history.RETENTION)
        history = forecast.with_resets(records, now)
        f = forecast.forecast_quota(history, used, forecast.as_utc(resets_at), window_hours, now)
        value, caption = forecast_text.quota_header(f, window_hours, session)
        self._fc_value.config(text=value)
        self._fc_caption.config(text=caption)
        self._fc_chart.set_data(ChartData(
            recorded=f.recorded, projected=f.projected, now=now, start=f.start, end=f.end,
            y_max=100.0, y_ticks=[(0, "0%"), (50, "50%"), (100, "100%")],
            day_ticks=forecast.time_ticks(f.start, f.end),
            estimated=f.estimated, reset=f.next_reset, reset_label=forecast_text.gain_text(f),
            tip=lambda t, v, projected: forecast_text.tip_text(
                t, v, projected, now, None, forecast_text.percent_left)))
        return self._set_forecast_rows(forecast_text.quota_rows(f, banked, now))

    def _axis_money(self, usd: float) -> str:
        """A gridline label: whole units when it is one, cents otherwise."""
        amount = convert_usd(usd, self.config.currency, self._exchange_rates)
        symbol = get_currency_symbol(self.config.currency)
        return f"{symbol}{amount:,.0f}" if abs(amount - round(amount)) < 1e-6 else f"{symbol}{amount:,.2f}"

    def _fill_balance_view(self, snap: DeepSeekSnapshot, money) -> int:
        """The balance view: recorded balance and a projection at the recent spend."""
        if self.config.panel_view != "forecast":
            return 0
        accent = _ENGINE_META[Engine.DEEPSEEK][2]
        self._fc_value.config(fg=accent)
        self._fc_chart.set_accent(accent)
        if not snap.balance_available:
            return self._forecast_empty(snap.balance_message)

        now = datetime.now(timezone.utc)
        history = [(r.t, r.value) for r in quota_history.load("deepseek", now - quota_history.RETENTION)]
        f = forecast.forecast_balance(history, snap.balance_total, snap.daily_costs, now)
        value, caption = forecast_text.balance_header(f, money)
        self._fc_value.config(text=value)
        self._fc_caption.config(text=caption)
        # Gridlines sit at round values of the currency shown, whatever the rate.
        factor = convert_usd(1.0, self.config.currency, self._exchange_rates) or 1.0
        ticks = [(v / factor, self._axis_money(v / factor)) for v in nice_ticks(f.y_max * factor)]
        self._fc_chart.set_data(ChartData(
            recorded=f.recorded, projected=f.projected, now=now, start=f.start, end=f.end,
            y_max=f.y_max, y_ticks=ticks,
            day_ticks=forecast.day_ticks(f.start, f.end),
            tip=lambda t, v, projected: forecast_text.tip_text(
                t, v, projected, now, None, lambda x: f"{self._money_fixed(x)} left")))
        return self._set_forecast_rows(forecast_text.balance_rows(f, now, None))

    @staticmethod
    def _recent_runout(history_key, percent, resets_at, window_hours) -> float:
        """Hours until a window runs dry at the quota view's rate (how fast it drained
        lately), so the bar and the chart give one answer; inf when it lasts to the reset."""
        now = datetime.now(timezone.utc)
        reset = forecast.as_utc(resets_at)
        history = forecast.with_resets(quota_history.load(history_key, now - quota_history.RETENTION), now)
        rate = forecast.recent_rate(history, percent, reset, window_hours, now)
        dry = forecast.runout_time(now, 100 - percent, rate, reset, window_hours)
        return float("inf") if dry is None else (dry - now).total_seconds() / 3600

    def _fill_limit(self, key, percent, reset_str, resets_at, window_hours, placeholder,
                    history_key=None, running=False):
        """`history_key` names the recorded window behind this bar (the one the quota view
        charts); a bar with no recording predicts from its window average. `running`: the
        reading is of no session, but the logs show one in use, so no figure is claimed."""
        w = self._limits[key]
        clock = reset_clock(resets_at)
        if running:
            w["bar"].set_value(0.0, None)
            for cell in ("reset", "note"):
                w[cell].config(text="")
            w["left"].config(text=_SESSION_RUNNING)
            w["pace"].config(text=_SESSION_RUNNING_PACE)
            return
        if clock and clock[0] and not placeholder:
            # The reading is the last of a window that is over. A reset always starts the
            # next one at nothing used, and a refused request can hold the next reading back
            # for twenty minutes, so show the new window as full until then.
            w["bar"].set_value(0.0, None)
            w["reset"].config(text=f"Reset {clock[1]}")
            w["left"].config(text="100% left")
            w["note"].config(text="")
            w["pace"].config(text="New session" if key == "session" else "New window")
            return
        proj = project_window(percent, resets_at, window_hours)
        w["bar"].set_value(percent, proj[0] if proj else None)
        # The words from the reading itself only when it carries no reset time to count down.
        reset_text = clock[1] if clock else reset_str
        w["reset"].config(text=f"Resets {reset_text}" if reset_text else "")
        w["left"].config(text=placeholder or f"{100 - percent:.0f}% left")
        if placeholder or proj is None:
            for cell in ("note", "pace"):
                w[cell].config(text="")
            return
        expected, remaining, runout = proj
        if history_key:
            runout = self._recent_runout(history_key, percent, resets_at, window_hours)
        lasts = runout >= remaining
        w["note"].config(text="Lasts until reset" if lasts else forecast_text.runout_text(runout),
                         fg=self.ok_color if lasts else self.warn_color)
        ahead = percent - expected
        w["pace"].config(text="On pace" if ahead <= 5 else f"{ahead:.0f}% ahead of pace")

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
        else:
            self._sync_scale()      # the monitor under the panel may have another DPI or height

        if self._window:
            # Apply any pending updates before showing
            self._apply_snapshot_update()
            # Update window size based on content before showing
            self._update_window_size()
            self._window.deiconify()
            # Needs the frame window, which exists only once the panel is mapped.
            self._window.update_idletasks()
            _round_corners(self._window)
            self._window.lift()
            self._window.focus_force()
            # Start update loop for data updates
            self._start_update_loop()

    def _start_update_loop(self):
        """Start the update loop, unless it already runs.

        show() calls this on every open, and each call used to start one more
        loop. The loop is scheduled on the hidden root rather than the panel:
        rebuilding the panel after Settings destroys it, and a callback
        scheduled through a destroyed widget never fires.
        """
        if self._loop_running:
            # It may be on the slow tick of a hidden panel: bring the next one forward.
            if self._tick_job is not None and self._window:
                self._window.master.after_cancel(self._tick_job)
                self._tick_job = self._window.master.after(_TICK_VISIBLE_MS, self._update_tick)
            return
        self._loop_running = True
        logger.debug("Panel update loop started")
        self._update_tick()

    def _apply_status_update(self):
        """Redraw the status row if something asked for it."""
        with self._lock:
            if self._status_needs_update:
                self._status_needs_update = False
                self._update_status_display()

    def _update_tick(self):
        """Apply pending snapshots and status, then reschedule."""
        self._tick_job = None
        try:
            if not self._window or not self._window.winfo_exists():
                self._loop_running = False
                return

            # Apply any pending data updates, then the status. A step that fails is logged and
            # must not stop the loop, nor keep a finished redeem result from landing: an
            # exception out of this method is the last tick the panel ever gets.
            for step in (self._apply_snapshot_update, self._apply_redeem_result, self._redraw_if_due,
                         self._apply_status_update):
                try:
                    step()
                except tk.TclError:
                    raise
                except Exception:
                    logger.exception("Panel update failed")

            # 50 ms is for a panel someone is looking at; a hidden one does the same work,
            # only later, which is why the loop never stops.
            delay = _TICK_VISIBLE_MS if self._window.winfo_viewable() else _TICK_HIDDEN_MS
            self._tick_job = self._window.master.after(delay, self._update_tick)
        except tk.TclError:
            self._loop_running = False  # Window was destroyed

    def hide(self):
        """Hide the window. A resize in progress ends with it: no release comes to a window that is gone."""
        if self._window:
            if self._resize:
                self._window.grab_release()
            self._resize = self._pin = None
            self._drop_curtain()
            self._window.withdraw()

    def destroy(self):
        """Destroy the window."""
        if self._window:
            self._window.destroy()
            self._window = self._curtain = None

    def toggle(self, snapshot: Optional[UsageSnapshot] = None):
        """Toggle window visibility."""
        if self._window and self._window.winfo_viewable():
            self.hide()
        else:
            self.show(snapshot)
