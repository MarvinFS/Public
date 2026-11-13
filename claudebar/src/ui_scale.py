"""The panel's one scale factor, and the helpers that turn design pixels into screen pixels.

Pure arithmetic: no Tk and no Win32, so the Linux port can take this file as it is. Every
size in the panel is a design pixel, drawn for a 1440-px-tall screen at 100% desktop scaling
(factor 1.0), and reaches the screen through a Scale.
"""

from typing import Optional, Union

# The screen height the layout was drawn for.
REFERENCE_HEIGHT = 1440

# What the automatic rule may ask for: below the floor the text stops being readable, above
# the ceiling the panel is absurd.
MIN_SCALE = 0.7
MAX_SCALE = 2.0

# What the user may ask for (config.panel_scale, and the panel's width when it is resized).
PANEL_MIN_SCALE = 0.7
PANEL_MAX_SCALE = 2.5

# Pixels in a point at 100% desktop scaling: a design font size is in points at 96 dpi.
POINT_PX = 96 / 72


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def auto_scale(screen_height: int, dpi_scale: float = 1.0) -> float:
    """The factor for a screen of `screen_height` pixels whose desktop is scaled by `dpi_scale`.

    Worked from the logical screen (physical pixels over the desktop's own scale), so a
    display the desktop already scales is not scaled twice: 4K at 200% is 1080 logical, 0.75
    here, and the desktop's 2x on top makes the panel 1.5 overall, as 4K at 100% would give.
    """
    if screen_height <= 0:
        return 1.0
    return clamp(screen_height / max(0.1, dpi_scale) / REFERENCE_HEIGHT, MIN_SCALE, MAX_SCALE)


class Scale:
    """Design pixels at one factor `k`: lengths, paddings and font sizes."""

    def __init__(self, k: float = 1.0):
        self.k = float(k)

    def px(self, pixels: float) -> int:
        """A length in screen pixels. Zero stays zero, anything else is at least one pixel
        (a hairline never vanishes), and the sign is kept."""
        if not pixels:
            return 0
        size = max(1, int(abs(pixels) * self.k + 0.5))
        return size if pixels > 0 else -size

    def spacing(self, value: Union[float, tuple, list]) -> Union[int, tuple]:
        """A padding as Tk takes it: one number, or a (before, after) pair."""
        if isinstance(value, (tuple, list)):
            return tuple(self.px(part) for part in value)
        return self.px(value)

    def font_px(self, points: float) -> int:
        """A font size, as the negative number of pixels Tk reads as a pixel size. Fonts follow
        this factor and not the DPI Tk sampled at start: at 1.0, 9 points is 12 pixels, the
        size Tk itself makes of 9 points at 96 dpi."""
        return -max(1, int(points * POINT_PX * self.k + 0.5))


def scale_for_width(width: float, base_width: float) -> float:
    """The factor of a panel `width` pixels wide whose factor-1.0 width is `base_width`."""
    return clamp(width / base_width, PANEL_MIN_SCALE, PANEL_MAX_SCALE)


def width_for_scale(k: float, base_width: float) -> int:
    """The inverse of scale_for_width: the width of the panel at factor `k`."""
    return round(clamp(k, PANEL_MIN_SCALE, PANEL_MAX_SCALE) * base_width)


# What the fit hides as the height shrinks, first dropped first. "source" is the footer's note,
# "top_model" the row under the daily chart, "pace" the line under each limit bar, "chart" the daily
# chart (Cost) or the detail rows (Quota), "stats" the second row of figures, "half" the whole
# Cost/Quota half with its tabs, "status" the "Updated" row.
DROP_ORDER = ("source", "top_model", "pace", "chart", "stats", "half", "status")


def zone_at(x: float, y: float, width: float, height: float, margin: float) -> Optional[str]:
    """The resize zone of the point (x, y) in a window `width` x `height`: the edge or corner whose
    outer `margin` pixels hold it ("n", "s", "e", "w", "ne", "nw", "se", "sw"), None inside them
    and outside the window."""
    if not (0 <= x < width and 0 <= y < height):
        return None
    vertical = "n" if y < margin else "s" if y >= height - margin else ""
    horizontal = "w" if x < margin else "e" if x >= width - margin else ""
    return vertical + horizontal or None


def blocks_to_hide(heights: dict, available: float, order: tuple = DROP_ORDER) -> set:
    """The blocks of `order` to hide so that `heights` (name -> pixels, every block in it, the ones
    that are always kept as well) fit in `available`. They go in `order`, so what is hidden is always
    a prefix of it, even a block that frees no height (the footer's note shares a line with the
    buttons) goes before the next one. Everything is hidden when even that does not fit."""
    total, hidden = sum(heights.values()), set()
    for name in order:
        if total <= available:
            break
        hidden.add(name)
        total -= heights.get(name, 0)
    return hidden
