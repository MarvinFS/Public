"""Premium icon generation for ClaudeBar."""

from PIL import Image, ImageDraw, ImageFont
from typing import Optional
import math


# Claude brand colors
CLAUDE_ORANGE = "#D97706"  # Claude's orange/amber color
CLAUDE_CREAM = "#FDF4DC"

# Status colors with gradient stops
STATUS_COLORS = {
    "normal": {
        "primary": "#10B981",    # Emerald green
        "secondary": "#059669",  # Darker green
        "glow": "#34D399",       # Light green glow
    },
    "warning": {
        "primary": "#F59E0B",    # Amber
        "secondary": "#D97706",  # Darker amber
        "glow": "#FCD34D",       # Light amber glow
    },
    "critical": {
        "primary": "#EF4444",    # Red
        "secondary": "#DC2626",  # Darker red
        "glow": "#F87171",       # Light red glow
    },
    "error": {
        "primary": "#6B7280",    # Gray
        "secondary": "#4B5563",  # Darker gray
        "glow": "#9CA3AF",       # Light gray
    },
}


def hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    """Convert hex color to RGB tuple."""
    hex_color = hex_color.lstrip("#")
    return tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))


def create_gradient_circle(draw: ImageDraw, center: tuple, radius: int,
                          color1: str, color2: str, steps: int = 50):
    """Draw a circle with radial gradient effect."""
    cx, cy = center
    rgb1 = hex_to_rgb(color1)
    rgb2 = hex_to_rgb(color2)

    for i in range(steps, 0, -1):
        ratio = i / steps
        r = int(rgb1[0] * ratio + rgb2[0] * (1 - ratio))
        g = int(rgb1[1] * ratio + rgb2[1] * (1 - ratio))
        b = int(rgb1[2] * ratio + rgb2[2] * (1 - ratio))
        color = f"#{r:02x}{g:02x}{b:02x}"

        current_radius = int(radius * (i / steps))
        draw.ellipse(
            [cx - current_radius, cy - current_radius,
             cx + current_radius, cy + current_radius],
            fill=color
        )


def create_premium_icon(
    status: str = "normal",
    percent: Optional[float] = None,
    size: int = 64
) -> Image.Image:
    """
    Create a premium tray icon with modern design.

    Args:
        status: One of 'normal', 'warning', 'critical', 'error'
        percent: Optional percentage to display (0-100)
        size: Icon size in pixels
    """
    # Create high-res then downscale for antialiasing
    scale = 4
    hi_size = size * scale

    img = Image.new("RGBA", (hi_size, hi_size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    colors = STATUS_COLORS.get(status, STATUS_COLORS["normal"])
    center = (hi_size // 2, hi_size // 2)
    outer_radius = (hi_size - scale * 4) // 2

    # Draw subtle glow/shadow
    glow_rgb = hex_to_rgb(colors["glow"])
    for i in range(8, 0, -1):
        alpha = int(40 * (i / 8))
        glow_radius = outer_radius + i * scale
        draw.ellipse(
            [center[0] - glow_radius, center[1] - glow_radius,
             center[0] + glow_radius, center[1] + glow_radius],
            fill=(*glow_rgb, alpha)
        )

    # Draw main circle with gradient effect
    create_gradient_circle(
        draw, center, outer_radius,
        colors["glow"], colors["primary"], steps=30
    )

    # Draw inner highlight for 3D effect
    highlight_radius = int(outer_radius * 0.85)
    highlight_offset = -int(outer_radius * 0.15)
    highlight_center = (center[0] + highlight_offset, center[1] + highlight_offset)

    # Subtle inner glow
    inner_rgb = hex_to_rgb(colors["glow"])
    draw.ellipse(
        [highlight_center[0] - highlight_radius,
         highlight_center[1] - highlight_radius,
         highlight_center[0] + highlight_radius,
         highlight_center[1] + highlight_radius],
        fill=(*inner_rgb, 30)
    )

    # Draw "C" logo or percentage
    if percent is not None and percent > 0:
        # Draw percentage text
        text = f"{int(percent)}"
        try:
            font_size = int(hi_size * 0.4)
            font = ImageFont.truetype("segoeui.ttf", font_size)
        except (IOError, OSError):
            try:
                font = ImageFont.truetype("arial.ttf", font_size)
            except (IOError, OSError):
                font = ImageFont.load_default()

        bbox = draw.textbbox((0, 0), text, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]

        x = center[0] - text_width // 2
        y = center[1] - text_height // 2 - int(hi_size * 0.02)

        # Text shadow
        draw.text((x + scale, y + scale), text, fill=(0, 0, 0, 80), font=font)
        # Main text
        draw.text((x, y), text, fill="white", font=font)
    else:
        # Draw stylized "C" for Claude
        try:
            font_size = int(hi_size * 0.5)
            font = ImageFont.truetype("segoeuib.ttf", font_size)  # Bold
        except (IOError, OSError):
            try:
                font = ImageFont.truetype("arialbd.ttf", font_size)
            except (IOError, OSError):
                font = ImageFont.load_default()

        text = "C"
        bbox = draw.textbbox((0, 0), text, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]

        x = center[0] - text_width // 2
        y = center[1] - text_height // 2 - int(hi_size * 0.02)

        # Text shadow
        draw.text((x + scale, y + scale), text, fill=(0, 0, 0, 80), font=font)
        # Main text
        draw.text((x, y), text, fill="white", font=font)

    # Downscale with antialiasing
    img = img.resize((size, size), Image.Resampling.LANCZOS)

    return img


def create_progress_ring_icon(
    percent: float,
    size: int = 64,
    ring_width: float = 0.15
) -> Image.Image:
    """
    Create an icon with a progress ring around it.

    Args:
        percent: Progress percentage (0-100)
        size: Icon size in pixels
        ring_width: Ring width as fraction of radius
    """
    scale = 4
    hi_size = size * scale

    img = Image.new("RGBA", (hi_size, hi_size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    center = (hi_size // 2, hi_size // 2)
    outer_radius = (hi_size - scale * 8) // 2
    inner_radius = int(outer_radius * (1 - ring_width))

    # Background ring (dark)
    draw.ellipse(
        [center[0] - outer_radius, center[1] - outer_radius,
         center[0] + outer_radius, center[1] + outer_radius],
        fill="#1a1a1a"
    )

    # Progress arc
    if percent > 0:
        # Determine color based on percentage
        if percent < 50:
            color = "#10B981"  # Green
        elif percent < 80:
            color = "#F59E0B"  # Amber
        else:
            color = "#EF4444"  # Red

        # Draw arc using pieslice
        start_angle = -90
        end_angle = start_angle + (percent / 100) * 360

        draw.pieslice(
            [center[0] - outer_radius, center[1] - outer_radius,
             center[0] + outer_radius, center[1] + outer_radius],
            start=start_angle,
            end=end_angle,
            fill=color
        )

    # Inner circle (cutout effect)
    draw.ellipse(
        [center[0] - inner_radius, center[1] - inner_radius,
         center[0] + inner_radius, center[1] + inner_radius],
        fill="#0f0f0f"
    )

    # Draw percentage text in center
    text = f"{int(percent)}"
    try:
        font_size = int(inner_radius * 1.2)
        font = ImageFont.truetype("segoeui.ttf", font_size)
    except (IOError, OSError):
        font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), text, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]

    x = center[0] - text_width // 2
    y = center[1] - text_height // 2

    draw.text((x, y), text, fill="white", font=font)

    # Downscale
    img = img.resize((size, size), Image.Resampling.LANCZOS)

    return img


def create_app_logo(size: int = 256) -> Image.Image:
    """
    Create a high-quality app logo for ClaudeBar.

    Args:
        size: Logo size in pixels
    """
    scale = 2
    hi_size = size * scale

    img = Image.new("RGBA", (hi_size, hi_size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    center = (hi_size // 2, hi_size // 2)
    outer_radius = (hi_size - scale * 20) // 2

    # Draw outer gradient circle (Claude orange)
    create_gradient_circle(
        draw, center, outer_radius,
        "#FCD34D", "#D97706", steps=50
    )

    # Draw inner circle
    inner_radius = int(outer_radius * 0.7)
    draw.ellipse(
        [center[0] - inner_radius, center[1] - inner_radius,
         center[0] + inner_radius, center[1] + inner_radius],
        fill="#1a1a1a"
    )

    # Draw "C" letter
    try:
        font_size = int(inner_radius * 1.4)
        font = ImageFont.truetype("segoeuib.ttf", font_size)
    except (IOError, OSError):
        try:
            font = ImageFont.truetype("arialbd.ttf", font_size)
        except (IOError, OSError):
            font = ImageFont.load_default()

    text = "C"
    bbox = draw.textbbox((0, 0), text, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]

    x = center[0] - text_width // 2
    y = center[1] - text_height // 2 - int(hi_size * 0.01)

    # Gradient text effect (simulate with multiple draws)
    draw.text((x, y), text, fill="#F59E0B", font=font)

    # Downscale
    img = img.resize((size, size), Image.Resampling.LANCZOS)

    return img


def save_app_icons(output_dir: str = "resources/icons"):
    """Generate and save all app icons."""
    from pathlib import Path

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    # App logo
    logo = create_app_logo(256)
    logo.save(output_path / "logo.png")

    # Tray icons for different states
    for status in ["normal", "warning", "critical", "error"]:
        icon = create_premium_icon(status, None, 64)
        icon.save(output_path / f"tray_{status}.png")

    # Tray icons with percentages
    for pct in [25, 50, 75, 90, 100]:
        status = "normal" if pct < 80 else ("warning" if pct < 95 else "critical")
        icon = create_premium_icon(status, pct, 64)
        icon.save(output_path / f"tray_{pct}pct.png")

    print(f"Icons saved to {output_path}")
