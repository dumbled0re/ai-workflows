"""Pexels Photo + Video API client.

Activates when PEXELS_API_KEY is set in the environment. Without the key,
all functions return None and the calling cascade continues to its next
fallback. Free Pexels signup at https://www.pexels.com/api/.

Photos are fetched as 1920x1080 stills; videos as 1080p horizontal MP4
clips. Both are cached on disk by query+source hash so re-runs of the same
script don't re-download.

The cinematic editorial overlay treatment (dark veil, lower-third banner,
accent bars, brand label, bottom text) mirrors what AI images receive in
visual.images, so Pexels output blends seamlessly with Pollinations output.
"""

from __future__ import annotations

import hashlib
import logging
import os
import urllib.parse
from io import BytesIO
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFilter

logger = logging.getLogger(__name__)

PEXELS_PHOTO_BASE = "https://api.pexels.com/v1/search"
PEXELS_VIDEO_BASE = "https://api.pexels.com/videos/search"
_TIMEOUT = 30
_HEADERS_JSON = {
    "Accept": "application/json",
    "User-Agent": "youtube_factory/1.0",
}
_MIN_PHOTO_BYTES = 80_000
_MIN_VIDEO_BYTES = 200_000


def _api_key() -> str | None:
    return os.environ.get("PEXELS_API_KEY") or None


def is_available() -> bool:
    """True iff PEXELS_API_KEY is set."""
    return _api_key() is not None


def fetch_photo(
    query: str,
    *,
    cache_dir: Path | None = None,
    orientation: str = "landscape",
    min_width: int = 1280,
) -> Image.Image | None:
    """Search Pexels for a photo matching query, return PIL.Image or None.

    The first photo whose width >= min_width is selected (Pexels orders by
    relevance + popularity, so #1 is usually fine).
    """
    key = _api_key()
    if not key:
        return None

    digest = hashlib.sha256(f"photo|{query}|{orientation}".encode("utf-8")).hexdigest()[:10]

    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path = cache_dir / f"pexels_photo_{digest}.jpg"
        if cache_path.exists() and cache_path.stat().st_size > _MIN_PHOTO_BYTES:
            try:
                return Image.open(cache_path).convert("RGB")
            except Exception:
                cache_path.unlink(missing_ok=True)
    else:
        cache_path = None

    try:
        params = {"query": query, "orientation": orientation, "per_page": 5}
        resp = requests.get(
            PEXELS_PHOTO_BASE,
            headers={**_HEADERS_JSON, "Authorization": key},
            params=params,
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        photos = resp.json().get("photos", []) or []
    except requests.RequestException as e:
        logger.warning("Pexels photo search failed (%s): %s", query[:40], e)
        return None

    chosen_url: str | None = None
    for p in photos:
        if (p.get("width") or 0) < min_width:
            continue
        srcs = p.get("src") or {}
        chosen_url = srcs.get("large2x") or srcs.get("large") or srcs.get("original")
        if chosen_url:
            break

    if not chosen_url:
        logger.info("Pexels photo: no good match for %r", query[:40])
        return None

    try:
        r2 = requests.get(chosen_url, timeout=_TIMEOUT)
        r2.raise_for_status()
        if len(r2.content) < _MIN_PHOTO_BYTES:
            return None
        img = Image.open(BytesIO(r2.content)).convert("RGB")
        if cache_path is not None:
            try:
                img.save(cache_path, "JPEG", quality=92)
            except Exception as e:
                logger.debug("Pexels photo cache save failed: %s", e)
        return img
    except (requests.RequestException, OSError) as e:
        logger.warning("Pexels photo download failed: %s", e)
        return None


def fetch_video_clip(
    query: str,
    *,
    cache_dir: Path,
    min_duration_sec: float = 4.0,
    min_width: int = 1280,
) -> Path | None:
    """Search Pexels Videos for a clip, download the smallest 1080p MP4 file
    that fits criteria. Returns the local path or None.

    Caching is by `query+min_duration` digest. The clip can be looped on the
    main video timeline by callers (ffmpeg `-stream_loop -1`).
    """
    key = _api_key()
    if not key:
        return None

    digest = hashlib.sha256(
        f"video|{query}|{min_duration_sec:.1f}".encode("utf-8")
    ).hexdigest()[:10]

    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"pexels_video_{digest}.mp4"
    if cache_path.exists() and cache_path.stat().st_size > _MIN_VIDEO_BYTES:
        return cache_path

    try:
        params = {
            "query": query,
            "orientation": "landscape",
            "size": "medium",
            "per_page": 8,
        }
        resp = requests.get(
            PEXELS_VIDEO_BASE,
            headers={**_HEADERS_JSON, "Authorization": key},
            params=params,
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        videos = resp.json().get("videos", []) or []
    except requests.RequestException as e:
        logger.warning("Pexels video search failed (%s): %s", query[:40], e)
        return None

    chosen_url: str | None = None
    for v in videos:
        if (v.get("duration") or 0) < min_duration_sec:
            continue
        files = v.get("video_files") or []
        # Prefer 1080p HD MP4
        candidates = [
            f for f in files
            if (f.get("width") or 0) >= min_width
            and (f.get("file_type") or "").endswith("mp4")
        ]
        candidates.sort(key=lambda f: f.get("width") or 0)  # smallest qualifying
        if candidates:
            chosen_url = candidates[0].get("link")
            break

    if not chosen_url:
        logger.info("Pexels video: no good match for %r", query[:40])
        return None

    try:
        with requests.get(chosen_url, stream=True, timeout=_TIMEOUT * 3) as r2:
            r2.raise_for_status()
            with open(cache_path, "wb") as f:
                for chunk in r2.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        f.write(chunk)
    except requests.RequestException as e:
        logger.warning("Pexels video download failed: %s", e)
        cache_path.unlink(missing_ok=True)
        return None

    if cache_path.stat().st_size < _MIN_VIDEO_BYTES:
        cache_path.unlink(missing_ok=True)
        return None

    logger.info("Pexels video cached: %s ← %r", cache_path.name, query[:40])
    return cache_path


# ---------------------------------------------------------------------------
# Image rendering with overlay treatment (matches AI/OG path style)
# ---------------------------------------------------------------------------

def render_pexels_photo(
    out_path: Path,
    *,
    query: str,
    text_overlay: str,
    accent_color: tuple,
    source_url: str = "",
    cache_dir: Path | None = None,
) -> bool:
    """Fetch Pexels photo and apply the standard overlay treatment.

    Returns True iff successful.
    """
    img = fetch_photo(query, cache_dir=cache_dir)
    if img is None:
        return False

    # Local imports to avoid import cycles + keep this module loadable when
    # PEXELS_API_KEY is unset.
    from youtube_factory.visual.images import (
        VIDEO_W, VIDEO_H,
        _fit_cover, _draw_bottom_text, _draw_brand_label, _draw_lower_third,
    )

    img = _fit_cover(img, VIDEO_W, VIDEO_H)
    img = img.filter(ImageFilter.GaussianBlur(radius=1.0))

    veil = Image.new("RGBA", (VIDEO_W, VIDEO_H), (0, 0, 0, 55))
    img = Image.alpha_composite(img.convert("RGBA"), veil).convert("RGB")

    bottom_overlay = Image.new("RGBA", (VIDEO_W, VIDEO_H), (0, 0, 0, 0))
    bdraw = ImageDraw.Draw(bottom_overlay)
    grad_top = int(VIDEO_H * 0.45)
    for y in range(grad_top, VIDEO_H):
        ratio = (y - grad_top) / (VIDEO_H - grad_top)
        alpha = int(210 * (ratio ** 1.4))
        bdraw.line([(0, y), (VIDEO_W, y)], fill=(0, 0, 0, alpha))
    img = Image.alpha_composite(img.convert("RGBA"), bottom_overlay).convert("RGB")

    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, VIDEO_W, 8], fill=accent_color)
    draw.rectangle([0, VIDEO_H - 8, VIDEO_W, VIDEO_H], fill=accent_color)

    if text_overlay:
        _draw_bottom_text(img, text_overlay, max_chars=18, font_size=124)
    if source_url:
        _draw_lower_third(img, source_url=source_url, accent_color=accent_color)
    _draw_brand_label(img, "AI NEWS DAILY", color=accent_color)

    img.save(out_path, "JPEG", quality=92)
    logger.info("Pexels photo: %s ← %r", out_path.name, query[:60])
    return True
