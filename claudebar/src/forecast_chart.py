"""The forecast chart: the recorded quota as a glowing line over a fill that
fades downward, the estimated start of the window softer than the recording,
the projection as a dashed line that fades as it gets less certain, a "Now"
marker, gridlines, and the reset labelled with what it gives back.

The coordinate maths, the hover lookup and the graphics are pure functions
(tested without a window): render_plot draws every line and fill with
antialiasing through Pillow and hands back an image. ForecastChart is the Canvas
that lays that image down and puts its own text over it, in the panel's fonts,
on the BarChart pattern: it redraws when resized and draws its flyout on the
canvas itself, which keeps the panel's topmost, borderless geometry out of the
picture.
"""

import math
import tkinter as tk
import tkinter.font as tkfont
from bisect import bisect_left
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, NamedTuple, Optional

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps, ImageTk

from forecast import value_at

# Room above the plot for the 100% label and "Now", below it for day labels.
PAD_TOP = 14
PAD_BOTTOM = 16

# One hover sample every this many pixels.
HOVER_STEP = 2

# Shapes are drawn this many times larger and scaled down, which smooths them.
SUPERSAMPLE = 3
DEFAULT_TICKS = [(0, "0%"), (50, "50%"), (100, "100%")]
LINE_WIDTH = 2.0
ESTIMATE_WIDTH = 1.5
GLOW = 0.45                      # opacity of the halo around the recorded line
FILL_ALPHA = 120                 # the fill under the recording, at the top of the plot
ESTIMATE_FILL_ALPHA = 70
ESTIMATE_LINE_ALPHA = 150
PROJECTION_ALPHA = (235, 95)     # opacity at the start and at the far end of the projection
DASH, GAP = 7.0, 4.5
ESTIMATE_LABEL_MIN_WIDTH = 90    # no "estimated" caption on a stretch shorter than this


class HoverPoint(NamedTuple):
    x: float
    y: float
    t: datetime
    value: float
    projected: bool


def x_of(when: datetime, start: datetime, end: datetime, width: float) -> float:
    return (when - start) / (end - start) * width


def y_of(value: float, y_max: float, height: float) -> float:
    """Pixel row of a value; 0 sits on the plot bottom, y_max on the top."""
    fraction = min(max(value / y_max, 0.0), 1.0) if y_max > 0 else 0.0
    return PAD_TOP + (height - PAD_TOP - PAD_BOTTOM) * (1 - fraction)


def to_pixels(points: list, start: datetime, end: datetime, y_max: float,
              width: float, height: float) -> list:
    return [(x_of(t, start, end, width), y_of(v, y_max, height)) for t, v in points]


def hover_samples(recorded: list, projected: list, now: datetime, start: datetime,
                  end: datetime, y_max: float, width: float, height: float,
                  step: int = HOVER_STEP) -> list:
    """The line sampled every `step` pixels, each sample carrying its own time,
    value and whether it lies on the projection. Nothing before the recording
    began: hovering where there is no line shows no flyout."""
    samples = []
    for x in range(0, int(width) + 1, step):
        when = start + (end - start) * (x / width)
        is_projected = when > now
        value = value_at(projected if is_projected else recorded, when)
        if value is not None:
            samples.append(HoverPoint(x, y_of(value, y_max, height), when, value, is_projected))
    return samples


def nearest_point(points: list, x: float, max_dist: Optional[float] = None):
    """The point (anything with an .x, sorted by x) nearest to x, a tie going
    left; None when there are none, or the nearest is farther than max_dist."""
    if not points:
        return None
    i = bisect_left([p.x for p in points], x)
    best = min(points[max(i - 1, 0):i + 1], key=lambda p: (abs(p.x - x), p.x))
    return best if max_dist is None or abs(best.x - x) <= max_dist else None


def nice_ticks(y_max: float, lines: int = 3) -> list:
    """Round gridline values from zero up to y_max, at most `lines` above zero
    (a step of 1, 2, 5 or 10 times a power of ten)."""
    if y_max <= 0:
        return [0.0]
    raw = y_max / (lines + 1)
    magnitude = 10 ** math.floor(math.log10(raw))
    step = next(m * magnitude for m in (1, 2, 5, 10) if m * magnitude >= raw)
    count = min(int(y_max / step + 1e-9), lines)
    return [round(step * k, 10) for k in range(count + 1)]


@dataclass
class ChartData:
    recorded: list
    projected: list
    now: datetime
    start: datetime
    end: datetime
    y_max: float
    y_ticks: list = field(default_factory=list)      # [(value, label)]
    day_ticks: list = field(default_factory=list)    # [(time, label)]
    tip: Callable = None                             # (time, value, projected) -> text
    estimated: list = field(default_factory=list)    # the window before the first reading
    reset: Optional[datetime] = None                 # the next reset, when it is in view
    reset_label: str = ""                            # what it gives back, e.g. "+80%"


def _rgb(colour: str) -> tuple:
    value = colour.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


class _Painter:
    """Antialiased drawing: everything is drawn SUPERSAMPLE times larger on one
    surface and scaled down at the end. Points are in output pixels."""

    def __init__(self, width: int, height: int, background: tuple):
        self.width, self.height = width, height
        self.image = Image.new("RGBA", (width * SUPERSAMPLE, height * SUPERSAMPLE), background + (255,))

    def _scaled(self, points):
        return [(x * SUPERSAMPLE, y * SUPERSAMPLE) for x, y in points]

    def _blank(self):
        return Image.new("RGBA", self.image.size, (0, 0, 0, 0))

    def line(self, points, width, colour, alpha=255, glow=0.0, caps=True):
        if len(points) < 2:
            return
        pts = self._scaled(points)
        if glow:
            halo = self._blank()
            ImageDraw.Draw(halo).line(pts, fill=colour + (int(255 * glow),),
                                      width=int(width * 3 * SUPERSAMPLE), joint="curve")
            self.image.alpha_composite(halo.filter(ImageFilter.GaussianBlur(2.6 * SUPERSAMPLE)))
        layer = self._blank()
        draw = ImageDraw.Draw(layer)
        draw.line(pts, fill=colour + (alpha,), width=max(int(width * SUPERSAMPLE), 1), joint="curve")
        if caps:
            r = width * SUPERSAMPLE / 2
            for x, y in (pts[0], pts[-1]):
                draw.ellipse([x - r, y - r, x + r, y + r], fill=colour + (alpha,))
        self.image.alpha_composite(layer)

    def dashed(self, points, width, colour, dash, gap, alpha=(255, 255)):
        """A dashed polyline whose opacity runs from alpha[0] at its start to
        alpha[1] at its end (measured along the path)."""
        total = sum(math.dist(a, b) for a, b in zip(points, points[1:]))
        layer = self._blank()
        draw = ImageDraw.Draw(layer)
        w, r = max(int(width * SUPERSAMPLE), 1), width * SUPERSAMPLE / 2
        along, drawing, left = 0.0, True, dash
        for a, b in zip(points, points[1:]):
            length = math.dist(a, b)
            pos = 0.0
            while pos < length - 1e-9:
                step = min(left, length - pos)
                if drawing:
                    p0 = (a[0] + (b[0] - a[0]) * pos / length, a[1] + (b[1] - a[1]) * pos / length)
                    p1 = (a[0] + (b[0] - a[0]) * (pos + step) / length,
                          a[1] + (b[1] - a[1]) * (pos + step) / length)
                    mid = (along + pos + step / 2) / total if total else 0.0
                    fill = colour + (int(alpha[0] + (alpha[1] - alpha[0]) * mid),)
                    q0, q1 = self._scaled([p0, p1])
                    draw.line([q0, q1], fill=fill, width=w)
                    for x, y in (q0, q1):
                        draw.ellipse([x - r, y - r, x + r, y + r], fill=fill)
                pos += step
                left -= step
                if left <= 1e-9:
                    drawing = not drawing
                    left = dash if drawing else gap
            along += length
        self.image.alpha_composite(layer)

    def fill(self, points, base, top, colour, alpha):
        """The area under a polyline, `alpha` strong at `top` and fading to
        nothing at `base`."""
        shape = Image.new("L", self.image.size, 0)
        ImageDraw.Draw(shape).polygon(
            self._scaled(points + [(points[-1][0], base), (points[0][0], base)]), fill=255)
        rows = max(int((base - top) * SUPERSAMPLE), 1)
        ramp = ImageOps.invert(Image.linear_gradient("L")).resize((self.image.size[0], rows))
        fade = Image.new("L", self.image.size, 0)
        fade.paste(ramp, (0, int(top * SUPERSAMPLE)))
        mask = ImageChops.multiply(ImageChops.multiply(shape, fade),
                                   Image.new("L", self.image.size, alpha))
        layer = Image.new("RGBA", self.image.size, colour + (0,))
        layer.putalpha(mask)
        self.image.alpha_composite(layer)

    def dot(self, x, y, radius, colour, ring, ring_width):
        layer = self._blank()
        draw = ImageDraw.Draw(layer)
        for r, c in ((radius + ring_width, ring), (radius, colour)):
            draw.ellipse([(x - r) * SUPERSAMPLE, (y - r) * SUPERSAMPLE,
                          (x + r) * SUPERSAMPLE, (y + r) * SUPERSAMPLE], fill=c + (255,))
        self.image.alpha_composite(layer)

    def pill(self, rect, colour):
        x0, y0, x1, y1 = rect
        layer = self._blank()
        ImageDraw.Draw(layer).rounded_rectangle(
            [x0 * SUPERSAMPLE, y0 * SUPERSAMPLE, x1 * SUPERSAMPLE, y1 * SUPERSAMPLE],
            radius=(y1 - y0) / 2 * SUPERSAMPLE, fill=colour + (255,))
        self.image.alpha_composite(layer)

    def result(self):
        return self.image.convert("RGB").resize((self.width, self.height), Image.LANCZOS)


def reset_pill_rect(d: Optional[ChartData], width: float, height: float, text_width: float,
                    text_height: float) -> Optional[tuple]:
    """Where the reset's gain label sits, as (x0, y0, x1, y1): just left of the
    reset line, where the plot is empty (the projection restarts on its right),
    under the top gridline, or under the projection where it would cover it;
    on the right when the left has no room. None without a reset in view or a
    label."""
    if d is None or d.reset is None or not d.reset_label:
        return None
    rx = x_of(d.reset, d.start, d.end, width)
    if not 0 < rx < width:
        return None
    w, h = text_width + 8, text_height + 4
    x0 = rx - 3 - w
    if x0 < 2:
        x0 = rx + 3
    y0 = PAD_TOP + 3
    # The projection falls steadily beside the reset, so its ends give its rows under the pill.
    rows = [y_of(v, d.y_max, height) for v in
            (value_at(d.projected, d.start + (d.end - d.start) * (x / width)) for x in (x0, x0 + w))
            if v is not None]
    if rows and min(rows) - 2 < y0 + h and max(rows) + 2 > y0:
        y0 = max(rows) + 4
    return (x0, y0, x0 + w, y0 + h)


def render_plot(d: Optional[ChartData], width: int, height: int, accent: str, background: str,
                grid: str, muted: str, pill: Optional[tuple] = None):
    """The chart's graphics as an RGB image: fills, gridlines, the recorded and
    estimated lines, the projection, the reset's hairline, the Now marker and
    the reset label's pill. The canvas puts its text over it."""
    accent, bg, grid, muted = _rgb(accent), _rgb(background), _rgb(grid), _rgb(muted)
    paint = _Painter(width, height, bg)
    ticks = d.y_ticks if d else DEFAULT_TICKS
    y_max = d.y_max if d else 100.0
    base = y_of(0, y_max, height)
    px = (lambda pts: to_pixels(pts, d.start, d.end, y_max, width, height)) if d else None

    estimated = px(d.estimated) if d else []
    recorded = px(d.recorded) if d else []
    if len(estimated) >= 2:
        paint.fill(estimated, base, PAD_TOP, accent, ESTIMATE_FILL_ALPHA)
    if len(recorded) >= 2:
        paint.fill(recorded, base, PAD_TOP, accent, FILL_ALPHA)
    for value, _ in ticks:
        y = round(y_of(value, y_max, height)) + 0.5        # a 1 px line fills one pixel row
        paint.line([(0, y), (width, y)], 1, grid, caps=False)
    if d is None:
        return paint.result()

    for when, _ in d.day_ticks:
        x = round(x_of(when, d.start, d.end, width)) + 0.5
        paint.line([(x, base), (x, base + 3)], 1, muted, 160, caps=False)
    if d.reset is not None:
        rx = x_of(d.reset, d.start, d.end, width)
        if 0 <= rx <= width:
            paint.line([(rx, PAD_TOP), (rx, base)], 1, muted, 110, caps=False)
    projected = px(d.projected)
    if len(projected) >= 2:
        paint.dashed(projected, 1.8, accent, DASH, GAP, PROJECTION_ALPHA)
    paint.line(estimated, ESTIMATE_WIDTH, accent, ESTIMATE_LINE_ALPHA)
    paint.line(recorded, LINE_WIDTH, accent, glow=GLOW)

    x_now = x_of(d.now, d.start, d.end, width)
    paint.dashed([(x_now, PAD_TOP - 2), (x_now, base)], 1, muted, 2.5, 3, (140, 140))
    if recorded:
        paint.dot(recorded[-1][0], recorded[-1][1], 3.2, accent, bg, 1.8)
    if pill is not None:
        paint.pill(pill, accent)
    return paint.result()


class ForecastChart(tk.Canvas):
    """The rendered plot with the panel's text over it; flyout on hover."""

    def __init__(self, parent, width=348, height=110, accent="#F59E0B", font=None,
                 grid="#2c2c2e", muted="#7f8899", **kwargs):
        super().__init__(parent, width=width, height=height, highlightthickness=0, **kwargs)
        self.w, self.h, self.accent, self.font = width, height, accent, font
        self.grid_colour, self.muted = grid, muted
        self._photo = None      # the plot image, kept alive for as long as it is shown
        # The flyout is read at a glance while the pointer moves, so it runs a
        # couple of points larger and bolder than the panel's small print.
        self.tip_font = (font[0], (font[1] if len(font) > 1 else 8) + 2, "bold") if font else None
        self._data: Optional[ChartData] = None
        self._message = ""
        self._hover: list = []
        self._tip_at: Optional[tuple] = None
        self.bind("<Motion>", self._on_motion)
        self.bind("<Leave>", lambda e: self._show_tip(None))
        self.bind("<Configure>", self._on_resize)

    def _on_resize(self, event):
        if (event.width, event.height) != (self.w, self.h):
            self.w, self.h = event.width, event.height
            self._redraw()

    def set_accent(self, accent: str):
        self.accent = accent

    def set_data(self, data: Optional[ChartData], message: str = ""):
        """Draw a forecast, or (with None) just the empty plot and a message."""
        self._data, self._message = data, message
        self._redraw()

    def _redraw(self):
        self.delete("all")
        self._tip_at = None
        self._hover = []
        if self.w < 2 or self.h < 2:
            return
        d = self._data
        pill = reset_pill_rect(d, self.w, self.h, *self._label_size(d.reset_label)) if d else None
        background = "#%02x%02x%02x" % tuple(v >> 8 for v in self.winfo_rgb(self.cget("bg")))
        self._photo = ImageTk.PhotoImage(render_plot(d, self.w, self.h, self.accent, background,
                                                     self.grid_colour, self.muted, pill),
                                         master=self)
        self.create_image(0, 0, image=self._photo, anchor=tk.NW)
        self._labels(d)
        if d is None:
            if self._message:
                # Midway between the 100% and 50% gridlines, not across the 50% one.
                self.create_text(self.w / 2, (y_of(100, 100, self.h) + y_of(50, 100, self.h)) / 2,
                                 text=self._message, fill=self.muted, font=self.font)
            return

        self._now_label(d)
        if pill is not None:
            self.create_text((pill[0] + pill[2]) / 2, (pill[1] + pill[3]) / 2, text=d.reset_label,
                             fill=background, font=self._bold_font())
        self._estimate_caption(d)
        self._hover = hover_samples(d.recorded, d.projected, d.now, d.start, d.end, d.y_max,
                                    self.w, self.h)

    def _now_label(self, d: ChartData):
        """"Now" beside its rule, on the right: after the gridline labels when the
        rule is close enough to the left edge to run into them, and on the left of
        the rule when the right side has no room."""
        font = tkfont.Font(root=self, font=self.font)
        x_now = x_of(d.now, d.start, d.end, self.w)
        width, line = font.measure("Now"), font.metrics("linespace")
        x = x_now + 4
        for value, label in d.y_ticks:
            top = y_of(value, d.y_max, self.h) - 1 - line       # the label's rows: top to y - 1
            if top < 1 + line and y_of(value, d.y_max, self.h) - 1 > 1:
                x = max(x, 3 + font.measure(label) + 4)
        if x + width <= self.w:
            self.create_text(x, 1, text="Now", anchor=tk.NW, fill=self.muted, font=self.font)
        else:                                          # at the right edge: left of the rule
            self.create_text(x_now - 4, 1, text="Now", anchor=tk.NE, fill=self.muted, font=self.font)

    def _bold_font(self):
        return (self.font[0], self.font[1], "bold") if self.font and len(self.font) > 1 else self.font

    def _label_size(self, text: str) -> tuple:
        font = tkfont.Font(root=self, font=self._bold_font() or self.font)
        return font.measure(text), font.metrics("linespace")

    def _estimate_caption(self, d: ChartData):
        """"estimated" over the stretch the recording has not reached yet, when
        enough of it shows to carry a caption."""
        if len(d.estimated) < 2:
            return
        (x0, y0), (x1, y1) = to_pixels(d.estimated[:1] + d.estimated[-1:], d.start, d.end, d.y_max,
                                       self.w, self.h)
        left = max(x0, 0.0)
        if x1 - left < ESTIMATE_LABEL_MIN_WIDTH:
            return
        x = left + 36                                  # clear of the gridline labels
        y = y0 + (y1 - y0) * (x - x0) / (x1 - x0)
        if y - 5 >= tkfont.Font(root=self, font=self.font).metrics("linespace"):
            self.create_text(x, y - 5, text="estimated", anchor=tk.SW, fill=self.muted, font=self.font)
        else:                                          # no room above a line near the top
            self.create_text(x, y + 5, text="estimated", anchor=tk.NW, fill=self.muted, font=self.font)

    def _labels(self, d: Optional[ChartData]):
        """The gridline value labels and a label per axis tick."""
        ticks = d.y_ticks if d else DEFAULT_TICKS
        y_max = d.y_max if d else 100
        for value, label in ticks:
            y = y_of(value, y_max, self.h)
            self.create_text(3, y - 1, text=label, anchor=tk.SW, fill=self.muted, font=self.font)
        if d is None:
            return
        axis_font = tkfont.Font(root=self, font=self.font)
        for when, label in d.day_ticks:
            x = x_of(when, d.start, d.end, self.w)
            if x + 3 + axis_font.measure(label) > self.w:      # cut by the edge: leave it out
                continue
            self.create_text(x + 3, self.h - 1, text=label, anchor=tk.SW, fill=self.muted,
                             font=self.font)

    def _on_motion(self, event):
        hit = nearest_point(self._hover, event.x, max_dist=HOVER_STEP * 3)
        self._show_tip(hit)

    def _show_tip(self, point: Optional[HoverPoint]):
        """Mark the point and draw its flyout, or clear both. Cheap to repeat."""
        key = None if point is None else (point.x, point.t)
        if key == self._tip_at:
            return
        self._tip_at = key
        self.delete("tip")
        if point is None or self._data is None or self._data.tip is None:
            return

        text = self._data.tip(point.t, point.value, point.projected)
        font = tkfont.Font(root=self, font=self.tip_font or self.font)
        pad = 7
        box_w = font.measure(text) + pad * 2
        box_h = font.metrics("linespace") + pad
        x0 = max(0, min(self.w - box_w, point.x - box_w / 2))
        # Above the point when there is room, else below, so it never covers the line it describes.
        y0 = point.y - box_h - 8
        if y0 < 0:
            y0 = point.y + 8
        self.create_oval(point.x - 3, point.y - 3, point.x + 3, point.y + 3,
                         fill="#ffffff", outline=self.accent, width=2, tags="tip")
        self.create_rectangle(x0, y0, x0 + box_w, y0 + box_h, fill="#000000",
                              outline="#5a5a5a", tags="tip")
        self.create_text(x0 + box_w / 2, y0 + box_h / 2, text=text, font=self.tip_font or self.font,
                         fill="#ffffff", tags="tip")

