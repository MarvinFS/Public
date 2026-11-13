"""The panel's drawn widgets: the usage bar and the daily bar chart.

Both take the panel's scale factor and multiply their own pixel constants by it, so at the
default 1.0 they draw exactly what they drew before the panel scaled.
"""

import tkinter as tk
import tkinter.font as tkfont
from typing import Optional

from PIL import Image, ImageDraw, ImageTk

from ui_scale import Scale


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


def _pill_image(width: int, height: int, percent: float, track: str, fill: str, bg: str,
                supersample: int = 4) -> Image.Image:
    """A track with a filled part, both with round ends, drawn at `supersample`x and
    scaled down so the ends are smooth.

    Tk draws canvas ovals without antialiasing, one pixel taller than a
    rectangle of the same box, so at 6 px round ends drawn on the canvas stuck
    out of the bar like bulbs.
    """
    w, h = max(width, height) * supersample, height * supersample
    big = Image.new("RGB", (w, h), bg)
    draw = ImageDraw.Draw(big)
    draw.rounded_rectangle((0, 0, w - 1, h - 1), radius=h // 2, fill=track)
    if percent > 0:
        filled = max(int(w * percent / 100), h)
        draw.rounded_rectangle((0, 0, filled - 1, h - 1), radius=h // 2, fill=fill)
    return big.resize((w // supersample, height), Image.Resampling.LANCZOS)


class PaceBar(tk.Canvas):
    """Slim usage bar with a tick at the even-spend position."""

    def __init__(self, parent, width=348, height=6, accent="#F59E0B", track="#3a3a3c", scale=1.0,
                 **kwargs):
        self._px = Scale(scale).px
        self._margin = self._px(1)       # room for the pace tick above and below the bar
        width, height = self._px(width), self._px(height)
        super().__init__(parent, width=width, height=height + 2 * self._margin, highlightthickness=0,
                         **kwargs)
        self.w, self.h, self.accent, self.track = width, height, accent, track
        self._value = (0.0, None)
        self._image = None           # Tk shows the bar only while this reference lives
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
        percent, expected = self._value
        pct = max(0.0, min(100.0, percent))
        self._image = ImageTk.PhotoImage(_pill_image(
            int(self.w), self.h, pct, self.track, self.accent, self.cget("bg")))
        self.create_image(0, self._margin, image=self._image, anchor=tk.NW)
        if expected is not None:
            x = self.w * max(0.0, min(100.0, expected)) / 100
            self.create_rectangle(x - self._margin, 0, x + self._margin, self.h + 2 * self._margin,
                                  fill="#f3f4f6", outline="", tags="v")

    def set_accent(self, accent: str):
        """Recolour without a repaint; the next set_value uses it."""
        self.accent = accent

    def set_value(self, percent, expected=None):
        self._value = (percent, expected)
        self._draw()


class BarChart(tk.Canvas):
    """Daily bars, today highlighted, oldest on the left.

    Hovering a bar shows that day's figures in a flyout drawn on the canvas
    itself, which keeps the panel's topmost, borderless geometry out of the
    picture entirely. The flyout is read at a glance while the pointer moves, so
    `tip_font` (the panel gives it a couple of points larger and bolder than the
    small print) is its own font; without one the flyout uses `font`.
    """

    def __init__(self, parent, width=348, height=44, accent="#F59E0B", font=None, tip_font=None,
                 scale=1.0, **kwargs):
        self._px = Scale(scale).px
        width, height = self._px(width), self._px(height)
        super().__init__(parent, width=width, height=height, highlightthickness=0, **kwargs)
        self.w, self.h, self.accent, self.font = width, height, accent, font
        self.tip_font = tip_font or font
        self._tooltips: list = []
        self._n = 0
        self._tip_index: Optional[int] = None
        self._values: list = []
        self.gap = self._px(4)
        self.bind("<Motion>", self._on_motion)
        self.bind("<Leave>", lambda e: self._show_tip(None))
        self.bind("<Configure>", self._on_resize)

    def _on_resize(self, event):
        if (event.width, event.height) != (self.w, self.h):
            self.w, self.h = event.width, event.height
            self.set_values(self._values, self._tooltips)

    def set_accent(self, accent: str):
        self.accent = accent

    def set_values(self, values, tooltips=()):
        self.delete("v")
        self._show_tip(None)
        self._tooltips = list(tooltips)
        self._values = list(values)
        self._n = len(values)
        if not values:
            return
        n = len(values)
        # A week gets room to breathe, a month gets thin bars.
        gap = self.gap = self._px(4 if n <= 7 else 2)
        bw = (self.w - gap * (n - 1)) / n
        peak = max(max(values), 0.01)
        for i, v in enumerate(values):
            x0 = i * (bw + gap)
            h = max(v / peak * (self.h - self._px(2)), 1)
            today = i == n - 1
            self.create_rectangle(x0, self.h - h, x0 + bw, self.h,
                                  fill=self.accent if today else _dim(self.accent), outline="", tags="v")

    def _on_motion(self, event):
        self._show_tip(bar_index_at(event.x, self._n, self.w, self.gap))

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

        font = tkfont.Font(root=self, font=self.tip_font)
        pad = self._px(7)
        box_w = font.measure(text) + pad * 2
        box_h = font.metrics("linespace") + pad
        slot = (self.w + self.gap) / max(self._n, 1)
        centre = index * slot + slot / 2
        x0 = max(0, min(self.w - box_w, centre - box_w / 2))
        self.create_rectangle(x0, 0, x0 + box_w, box_h, fill="#000000", outline="#5a5a5a", tags="tip")
        self.create_text(x0 + box_w / 2, box_h / 2, text=text, font=self.tip_font,
                         fill="#ffffff", tags="tip")
