"""Image generation for video shots.

Strategy:
- Headline cards (Pillow-rendered) with bold typography for narrative segments
- Chapter cards with large numerals for story dividers
- OG images only when they look good (size + format checks)
- Fallback to text card always works

No external API keys required.
"""

from __future__ import annotations

import logging
import re
from io import BytesIO
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont

logger = logging.getLogger(__name__)

VIDEO_W = 1920
VIDEO_H = 1080

# Color palette (modern news show feel)
COLOR_BG_DARK = (12, 18, 32)        # near-black blue
COLOR_BG_MID = (28, 38, 64)         # mid blue
COLOR_BG_ACCENT = (220, 38, 38)     # red accent
COLOR_TEXT_PRIMARY = (250, 250, 250) # near-white
COLOR_TEXT_SECONDARY = (180, 200, 230)
COLOR_TEXT_NUMBER = (245, 200, 60)  # gold for numbers
COLOR_BAR = (220, 38, 38)           # red bar

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36"
    ),
}
_TIMEOUT = 10
_MIN_OG_WIDTH = 600
_MIN_OG_HEIGHT = 300


STORY_COLOR_PALETTE = [
    (220, 38, 38),    # red
    (37, 99, 235),    # blue
    (16, 185, 129),   # green
    (147, 51, 234),   # purple
    (245, 158, 11),   # amber
    (236, 72, 153),   # pink
]


def generate_image(
    out_path: Path,
    *,
    text_overlay: str = "",
    image_query: str = "",
    image_source: str = "auto",
    source_url: str = "",
    assets_dir: Path | None = None,
    chapter_number: int | None = None,
    is_chapter_card: bool = False,
    story_color_index: int = 0,
) -> None:
    """Generate one image for a shot.

    chapter_number: If provided AND is_chapter_card=True, render a chapter card
                    with the number (01, 02, ...) and the text as story title.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)

    accent = STORY_COLOR_PALETTE[story_color_index % len(STORY_COLOR_PALETTE)]

    if is_chapter_card and chapter_number is not None:
        _render_chapter_card(out_path, chapter_number, text_overlay, accent_color=accent)
        return

    # Try OG image only if explicitly requested
    if image_source == "og" and source_url:
        if _try_og_image(source_url, out_path, text_overlay, accent_color=accent):
            return

    # Default: bold text card with story-color accent
    _render_headline_card(out_path, text_overlay or image_query or "AI News", accent_color=accent)


# ---------------------------------------------------------------------------
# Card renderers
# ---------------------------------------------------------------------------

def _render_headline_card(out_path: Path, text: str, accent_color: tuple = COLOR_BG_ACCENT) -> None:
    """Render a polished headline card with diagonal accent and large text."""
    img = _make_gradient_bg(VIDEO_W, VIDEO_H, COLOR_BG_DARK, COLOR_BG_MID)
    draw = ImageDraw.Draw(img)

    # Subtle grid pattern (newspaper feel)
    _draw_subtle_grid(img)

    # Diagonal accent
    overlay = Image.new("RGBA", (VIDEO_W, VIDEO_H), (0, 0, 0, 0))
    odraw = ImageDraw.Draw(overlay)
    odraw.polygon(
        [(0, 0), (VIDEO_W * 0.4, 0), (0, VIDEO_H * 0.5)],
        fill=(*accent_color, 38),
    )
    odraw.polygon(
        [(VIDEO_W, VIDEO_H), (VIDEO_W * 0.6, VIDEO_H), (VIDEO_W, VIDEO_H * 0.5)],
        fill=(*accent_color, 22),
    )
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(img)

    # Top + bottom accent bars
    draw.rectangle([0, 0, VIDEO_W, 8], fill=accent_color)
    draw.rectangle([0, VIDEO_H - 8, VIDEO_W, VIDEO_H], fill=accent_color)

    # Main text - large and centered
    _draw_large_text(img, text, max_chars=14, font_size=140)

    # Brand label bottom-right
    _draw_brand_label(img, "AI NEWS DAILY", color=accent_color)

    img.save(out_path, "JPEG", quality=92)
    logger.info("Headline card rendered: %s", out_path.name)


def _render_chapter_card(out_path: Path, number: int, title: str, accent_color: tuple = COLOR_BG_ACCENT) -> None:
    """Render a chapter card with large number and story title."""
    # Background tinted toward the accent color
    bg_top = (10, 12, 24)
    bg_bot = tuple(int(c * 0.15) for c in accent_color)
    img = _make_gradient_bg(VIDEO_W, VIDEO_H, bg_top, bg_bot)
    draw = ImageDraw.Draw(img)

    _draw_subtle_grid(img)

    # Vertical accent bar on left
    draw.rectangle([0, 0, 16, VIDEO_H], fill=accent_color)

    # Huge number
    font_num = _find_japanese_font(440, bold=True)
    num_text = f"{number:02d}"
    bbox = draw.textbbox((0, 0), num_text, font=font_num)
    num_w = bbox[2] - bbox[0]
    num_h = bbox[3] - bbox[1]
    num_x = 140
    num_y = (VIDEO_H - num_h) // 2 - 30
    # Stroke for emphasis
    for dx in range(-3, 4):
        for dy in range(-3, 4):
            draw.text((num_x + dx, num_y + dy), num_text, font=font_num, fill=(0, 0, 0))
    draw.text((num_x, num_y), num_text, font=font_num, fill=COLOR_TEXT_NUMBER)

    # "STORY" small label
    font_label = _find_japanese_font(56, bold=True)
    draw.text(
        (num_x + 12, num_y - 80),
        "STORY",
        font=font_label,
        fill=accent_color,
    )

    # Story title on the right
    title_x = num_x + num_w + 100
    title_w = VIDEO_W - title_x - 100
    _draw_wrapped_text(
        img,
        title,
        x=title_x,
        y_center=VIDEO_H // 2,
        max_width_px=title_w,
        font_size=88,
        color=COLOR_TEXT_PRIMARY,
        line_spacing=20,
    )

    # Bottom accent bar
    draw.rectangle([0, VIDEO_H - 8, VIDEO_W, VIDEO_H], fill=accent_color)

    _draw_brand_label(img, "AI NEWS DAILY", color=accent_color)
    img.save(out_path, "JPEG", quality=92)
    logger.info("Chapter card %02d rendered: %s", number, out_path.name)


def _try_og_image(source_url: str, out_path: Path, text_overlay: str, accent_color: tuple = COLOR_BG_ACCENT) -> bool:
    """Try to use OG image. Returns False if image quality is poor."""
    og_url = _fetch_og_image(source_url)
    if not og_url:
        return False
    try:
        resp = requests.get(og_url, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        img = Image.open(BytesIO(resp.content)).convert("RGB")
        if img.width < _MIN_OG_WIDTH or img.height < _MIN_OG_HEIGHT:
            logger.debug("OG too small (%dx%d), using card", img.width, img.height)
            return False

        # Scale to fill, then add dark overlay + text
        img = _fit_cover(img, VIDEO_W, VIDEO_H)
        img = img.filter(ImageFilter.GaussianBlur(radius=2.0))

        # Strong dark gradient (image is just background)
        overlay = Image.new("RGBA", (VIDEO_W, VIDEO_H), (0, 0, 0, 90))
        img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")

        # Bottom heavy darken for text area
        bottom_overlay = Image.new("RGBA", (VIDEO_W, VIDEO_H), (0, 0, 0, 0))
        bdraw = ImageDraw.Draw(bottom_overlay)
        for y in range(VIDEO_H // 3, VIDEO_H):
            alpha = int(220 * (y - VIDEO_H // 3) / (VIDEO_H * 2 / 3))
            bdraw.line([(0, y), (VIDEO_W, y)], fill=(0, 0, 0, alpha))
        img = Image.alpha_composite(img.convert("RGBA"), bottom_overlay).convert("RGB")

        # Accent bars
        draw = ImageDraw.Draw(img)
        draw.rectangle([0, 0, VIDEO_W, 6], fill=accent_color)
        draw.rectangle([0, VIDEO_H - 6, VIDEO_W, VIDEO_H], fill=accent_color)

        if text_overlay:
            _draw_bottom_text(img, text_overlay, max_chars=20, font_size=120)
        _draw_brand_label(img, "AI NEWS DAILY", color=accent_color)

        img.save(out_path, "JPEG", quality=92)
        logger.info("OG image used: %s", out_path.name)
        return True
    except Exception as e:
        logger.debug("OG image processing failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_gradient_bg(w: int, h: int, top: tuple, bottom: tuple) -> Image.Image:
    """Vertical gradient background."""
    img = Image.new("RGB", (w, h), top)
    draw = ImageDraw.Draw(img)
    for y in range(h):
        ratio = y / h
        r = int(top[0] + (bottom[0] - top[0]) * ratio)
        g = int(top[1] + (bottom[1] - top[1]) * ratio)
        b = int(top[2] + (bottom[2] - top[2]) * ratio)
        draw.line([(0, y), (w, y)], fill=(r, g, b))
    return img


def _fit_cover(img: Image.Image, w: int, h: int) -> Image.Image:
    src_ratio = img.width / img.height
    dst_ratio = w / h
    if src_ratio > dst_ratio:
        new_w = int(img.height * dst_ratio)
        offset = (img.width - new_w) // 2
        img = img.crop((offset, 0, offset + new_w, img.height))
    else:
        new_h = int(img.width / dst_ratio)
        offset = (img.height - new_h) // 2
        img = img.crop((0, offset, img.width, offset + new_h))
    return img.resize((w, h), Image.Resampling.LANCZOS)


def _fetch_og_image(url: str) -> str | None:
    if not url:
        return None
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        if resp.status_code != 200:
            return None
        match = re.search(
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
            resp.text,
            re.IGNORECASE,
        ) or re.search(
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
            resp.text,
            re.IGNORECASE,
        )
        return match.group(1) if match else None
    except Exception:
        return None


def _find_japanese_font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    candidates_bold = [
        "/System/Library/Fonts/ヒラギノ角ゴシック W8.ttc",
        "/System/Library/Fonts/ヒラギノ角ゴシック W7.ttc",
        "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Black.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    ]
    candidates_regular = [
        "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc",
        "/System/Library/Fonts/ヒラギノ角ゴシック W4.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    ]
    for path in candidates_bold if bold else candidates_regular:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    # Last resort
    for path in candidates_regular if bold else candidates_bold:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def _draw_large_text(img: Image.Image, text: str, max_chars: int, font_size: int) -> None:
    """Centered large text with strong shadow."""
    if not text:
        return
    draw = ImageDraw.Draw(img)
    font = _find_japanese_font(font_size, bold=True)
    lines = _wrap_text(text, max_chars=max_chars)
    line_h = font.size + 28
    total_h = line_h * len(lines)
    y_start = (VIDEO_H - total_h) // 2

    for i, line in enumerate(lines):
        bbox = draw.textbbox((0, 0), line, font=font)
        text_w = bbox[2] - bbox[0]
        x = (VIDEO_W - text_w) // 2
        y = y_start + i * line_h
        # Heavy shadow for impact
        for dx in range(-4, 5, 2):
            for dy in range(-4, 5, 2):
                draw.text((x + dx, y + dy), line, font=font, fill=(0, 0, 0))
        draw.text((x, y), line, font=font, fill=COLOR_TEXT_PRIMARY)


def _draw_bottom_text(img: Image.Image, text: str, max_chars: int, font_size: int) -> None:
    """Large text anchored at bottom-left."""
    if not text:
        return
    draw = ImageDraw.Draw(img)
    font = _find_japanese_font(font_size, bold=True)
    lines = _wrap_text(text, max_chars=max_chars)
    line_h = font.size + 24
    total_h = line_h * len(lines)
    y_start = VIDEO_H - total_h - 120

    for i, line in enumerate(lines):
        x = 100
        y = y_start + i * line_h
        for dx in range(-4, 5, 2):
            for dy in range(-4, 5, 2):
                draw.text((x + dx, y + dy), line, font=font, fill=(0, 0, 0))
        draw.text((x, y), line, font=font, fill=COLOR_TEXT_PRIMARY)


def _draw_wrapped_text(
    img: Image.Image,
    text: str,
    x: int,
    y_center: int,
    max_width_px: int,
    font_size: int,
    color: tuple,
    line_spacing: int = 16,
) -> None:
    """Draw text wrapped to fit max_width_px, vertically centered around y_center."""
    if not text:
        return
    draw = ImageDraw.Draw(img)
    font = _find_japanese_font(font_size, bold=True)

    lines = _wrap_by_width(text, font, draw, max_width_px)
    line_h = font.size + line_spacing
    total_h = line_h * len(lines)
    y_start = y_center - total_h // 2

    for i, line in enumerate(lines):
        y = y_start + i * line_h
        for dx in [-3, -2, 2, 3]:
            for dy in [-3, -2, 2, 3]:
                draw.text((x + dx, y + dy), line, font=font, fill=(0, 0, 0))
        draw.text((x, y), line, font=font, fill=color)


def _wrap_by_width(text: str, font, draw, max_w: int) -> list[str]:
    lines = []
    current = ""
    for ch in text:
        test = current + ch
        bbox = draw.textbbox((0, 0), test, font=font)
        if bbox[2] - bbox[0] > max_w and current:
            lines.append(current)
            current = ch
        else:
            current = test
    if current:
        lines.append(current)
    return lines


def _draw_brand_label(img: Image.Image, label: str, color: tuple = COLOR_BG_ACCENT) -> None:
    """Small brand label bottom-right."""
    draw = ImageDraw.Draw(img)
    font = _find_japanese_font(36, bold=True)
    bbox = draw.textbbox((0, 0), label, font=font)
    w = bbox[2] - bbox[0]
    x = VIDEO_W - w - 60
    y = VIDEO_H - 90
    draw.rectangle([x - 16, y - 8, x + w + 16, y + 50], fill=color)
    draw.text((x, y), label, font=font, fill=COLOR_TEXT_PRIMARY)


def _draw_subtle_grid(img: Image.Image) -> None:
    """Subtle dot-grid pattern overlay (low opacity)."""
    overlay = Image.new("RGBA", (VIDEO_W, VIDEO_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    spacing = 60
    for x in range(spacing, VIDEO_W, spacing):
        for y in range(spacing, VIDEO_H, spacing):
            draw.ellipse([x - 1, y - 1, x + 1, y + 1], fill=(255, 255, 255, 22))
    img_rgba = img.convert("RGBA")
    composed = Image.alpha_composite(img_rgba, overlay).convert("RGB")
    # Mutate img
    img.paste(composed)


def _wrap_text(text: str, max_chars: int) -> list[str]:
    lines = []
    current = ""
    for ch in text:
        current += ch
        if len(current) >= max_chars:
            lines.append(current)
            current = ""
    if current:
        lines.append(current)
    return lines


# ---------------------------------------------------------------------------
# Thumbnail
# ---------------------------------------------------------------------------

def render_thumbnail(out_path: Path, headline: str, subtitle: str = "") -> None:
    """Render a 1280x720 YouTube thumbnail with high contrast."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    w, h = 1280, 720
    img = _make_gradient_bg(w, h, (10, 12, 24), (60, 14, 14))
    draw = ImageDraw.Draw(img)

    # Big red blocks corners
    draw.rectangle([0, 0, w, 14], fill=COLOR_BG_ACCENT)
    draw.rectangle([0, h - 14, w, h], fill=COLOR_BG_ACCENT)
    draw.rectangle([0, 0, 14, h], fill=COLOR_BG_ACCENT)

    # Main headline - very large
    font_main = _find_japanese_font(160, bold=True)
    lines = _wrap_text(headline, max_chars=8)
    line_h = font_main.size + 24
    total_h = line_h * len(lines)
    y_start = (h - total_h) // 2 - 30

    for i, line in enumerate(lines):
        bbox = draw.textbbox((0, 0), line, font=font_main)
        text_w = bbox[2] - bbox[0]
        x = (w - text_w) // 2
        y = y_start + i * line_h
        for dx in range(-6, 7, 2):
            for dy in range(-6, 7, 2):
                draw.text((x + dx, y + dy), line, font=font_main, fill=(0, 0, 0))
        draw.text((x, y), line, font=font_main, fill=COLOR_TEXT_NUMBER)

    if subtitle:
        font_sub = _find_japanese_font(60, bold=True)
        bbox = draw.textbbox((0, 0), subtitle, font=font_sub)
        text_w = bbox[2] - bbox[0]
        x = (w - text_w) // 2
        y = y_start + total_h + 30
        for dx in [-3, -2, 2, 3]:
            for dy in [-3, -2, 2, 3]:
                draw.text((x + dx, y + dy), subtitle, font=font_sub, fill=(0, 0, 0))
        draw.text((x, y), subtitle, font=font_sub, fill=COLOR_TEXT_PRIMARY)

    img.save(out_path, "JPEG", quality=95)
    logger.info("Thumbnail saved: %s", out_path)
