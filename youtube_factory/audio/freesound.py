"""Freesound SFX API client.

Activates when FREESOUND_API_KEY is set. Free signup at
https://freesound.org/help/developers/ — token-based auth, no OAuth needed
for read-only sound search/download.

Usage pattern (called by audio.sfx layer at chapter boundaries):

    from youtube_factory.audio import freesound
    sfx = freesound.fetch_sfx("news intro stinger short", cache_dir=...)
    if sfx:
        # mix into master at desired offset
        ...

Files are cached on disk by query digest. Quality / license are
caller-checked against the response metadata.
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

FREESOUND_SEARCH = "https://freesound.org/apiv2/search/text/"
_TIMEOUT = 30
_MIN_BYTES = 5_000


def _api_key() -> str | None:
    return os.environ.get("FREESOUND_API_KEY") or None


def is_available() -> bool:
    """True iff FREESOUND_API_KEY is set."""
    return _api_key() is not None


def fetch_sfx(
    query: str,
    *,
    cache_dir: Path,
    max_duration_sec: float = 4.0,
    license_filter: str = "Creative Commons 0",
) -> Path | None:
    """Search Freesound and download the first matching short SFX.

    Filters:
      - duration <= max_duration_sec
      - license = CC0 by default (most permissive, no attribution required)
    """
    key = _api_key()
    if not key:
        return None

    digest = hashlib.sha256(
        f"sfx|{query}|{max_duration_sec:.1f}|{license_filter}".encode("utf-8")
    ).hexdigest()[:10]
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"freesound_{digest}.wav"
    if cache_path.exists() and cache_path.stat().st_size > _MIN_BYTES:
        return cache_path

    try:
        params = {
            "query": query,
            "filter": (
                f'duration:[0 TO {max_duration_sec}] '
                f'AND license:"{license_filter}"'
            ),
            "fields": "id,name,duration,license,previews",
            "page_size": 10,
            "token": key,
        }
        resp = requests.get(FREESOUND_SEARCH, params=params, timeout=_TIMEOUT)
        resp.raise_for_status()
        results = resp.json().get("results", []) or []
    except requests.RequestException as e:
        logger.warning("Freesound search failed (%s): %s", query[:40], e)
        return None
    except ValueError as e:
        logger.warning("Freesound JSON decode failed: %s", e)
        return None

    chosen = None
    for r in results:
        previews = r.get("previews") or {}
        # Prefer high-quality OGG; fallback to MP3
        url = previews.get("preview-hq-ogg") or previews.get("preview-hq-mp3")
        if url:
            chosen = (url, r)
            break

    if not chosen:
        logger.info("Freesound: no good match for %r", query[:40])
        return None

    url, meta = chosen
    try:
        with requests.get(url, stream=True, timeout=_TIMEOUT * 2) as r2:
            r2.raise_for_status()
            with open(cache_path, "wb") as f:
                for chunk in r2.iter_content(chunk_size=32 * 1024):
                    if chunk:
                        f.write(chunk)
    except requests.RequestException as e:
        logger.warning("Freesound download failed: %s", e)
        cache_path.unlink(missing_ok=True)
        return None

    if cache_path.stat().st_size < _MIN_BYTES:
        cache_path.unlink(missing_ok=True)
        return None

    logger.info(
        "Freesound SFX cached: %s ← %r (%.1fs %s)",
        cache_path.name, query[:40], meta.get("duration") or 0, meta.get("license") or "?",
    )
    return cache_path
