"""Pixabay Music API client.

Activates when PIXABAY_API_KEY is set in the environment. Free signup at
https://pixabay.com/api/docs/#api_audio. Without the key, returns None and
the caller falls back to procedural drone BGM.

Tracks are filtered by minimum duration (must cover the video) and
preferred genre/mood tags. Cached on disk by query digest.
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

PIXABAY_BASE = "https://pixabay.com/api/"
_TIMEOUT = 30
_MIN_BYTES = 200_000


def _api_key() -> str | None:
    return os.environ.get("PIXABAY_API_KEY") or None


def is_available() -> bool:
    """True iff PIXABAY_API_KEY is set."""
    return _api_key() is not None


def fetch_music(
    query: str,
    *,
    cache_dir: Path,
    min_duration_sec: float = 60.0,
) -> Path | None:
    """Search Pixabay Music for a track matching query, download mp3, return path.

    The first track whose duration >= min_duration_sec is selected (results
    are ordered by relevance + popularity by default).
    """
    key = _api_key()
    if not key:
        return None

    digest = hashlib.sha256(
        f"music|{query}|{int(min_duration_sec)}".encode("utf-8")
    ).hexdigest()[:10]
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"pixabay_music_{digest}.mp3"
    if cache_path.exists() and cache_path.stat().st_size > _MIN_BYTES:
        return cache_path

    try:
        params = {
            "key": key,
            "q": query,
            "audio_type": "music",
            "per_page": 10,
        }
        resp = requests.get(PIXABAY_BASE, params=params, timeout=_TIMEOUT)
        resp.raise_for_status()
        hits = resp.json().get("hits", []) or []
    except requests.RequestException as e:
        logger.warning("Pixabay music search failed (%s): %s", query[:40], e)
        return None
    except ValueError as e:
        logger.warning("Pixabay music JSON decode failed: %s", e)
        return None

    chosen = None
    for h in hits:
        if (h.get("duration") or 0) < min_duration_sec:
            continue
        # Pixabay returns "audio" mp3 url
        url = h.get("audio") or h.get("audio_url")
        if url:
            chosen = url
            break

    if not chosen:
        logger.info("Pixabay music: no good match for %r", query[:40])
        return None

    try:
        with requests.get(chosen, stream=True, timeout=_TIMEOUT * 3) as r2:
            r2.raise_for_status()
            with open(cache_path, "wb") as f:
                for chunk in r2.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        f.write(chunk)
    except requests.RequestException as e:
        logger.warning("Pixabay music download failed: %s", e)
        cache_path.unlink(missing_ok=True)
        return None

    if cache_path.stat().st_size < _MIN_BYTES:
        cache_path.unlink(missing_ok=True)
        return None

    logger.info("Pixabay music cached: %s ← %r", cache_path.name, query[:40])
    return cache_path
