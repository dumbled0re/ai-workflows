"""Edge TTS-based voice synthesis with WordBoundary timing extraction.

Uses Microsoft Edge's TTS service via the edge-tts library (no API key).
Captures WordBoundary metadata for accurate subtitle alignment.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import edge_tts

logger = logging.getLogger(__name__)

DEFAULT_VOICE = "ja-JP-NanamiNeural"
DEFAULT_RATE = "+0%"
_MAX_RETRIES = 3
_BACKOFF_BASE = 2


@dataclass
class WordCue:
    """One word boundary event from Edge TTS."""
    text: str
    start_sec: float
    end_sec: float


@dataclass
class SynthesisResult:
    """Result of one TTS synthesis."""
    audio_path: Path
    duration_sec: float
    word_cues: list[WordCue] = field(default_factory=list)


async def _synthesize_async(
    text: str,
    audio_path: Path,
    voice: str,
    rate: str,
) -> list[WordCue]:
    """Run TTS and capture WordBoundary metadata."""
    communicate = edge_tts.Communicate(text, voice, rate=rate)
    cues: list[WordCue] = []

    with open(audio_path, "wb") as audio_file:
        async for chunk in communicate.stream():
            chunk_type = chunk["type"]
            if chunk_type == "audio":
                audio_file.write(chunk["data"])
            elif chunk_type == "WordBoundary":
                # offset and duration are in 100-nanosecond units
                start_ns = chunk["offset"] / 10  # microseconds
                duration_ns = chunk["duration"] / 10
                cues.append(WordCue(
                    text=chunk["text"],
                    start_sec=start_ns / 1_000_000,
                    end_sec=(start_ns + duration_ns) / 1_000_000,
                ))
    return cues


def synthesize(
    text: str,
    audio_path: Path,
    voice: str = DEFAULT_VOICE,
    rate: str = DEFAULT_RATE,
) -> SynthesisResult:
    """Synthesize text → mp3, return audio path + duration + word cues.

    Retries up to 3 times with exponential backoff.
    """
    audio_path.parent.mkdir(parents=True, exist_ok=True)

    last_error: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            cues = asyncio.run(_synthesize_async(text, audio_path, voice, rate))
            duration = measure_duration(audio_path)
            logger.info(
                "Synthesized %d chars → %.2fs, %d word cues (%s)",
                len(text), duration, len(cues), audio_path.name,
            )
            return SynthesisResult(
                audio_path=audio_path,
                duration_sec=duration,
                word_cues=cues,
            )
        except Exception as e:
            last_error = e
            wait = _BACKOFF_BASE**attempt
            logger.warning(
                "Edge TTS attempt %d/%d failed: %s. Retrying in %ds",
                attempt + 1, _MAX_RETRIES, e, wait,
            )
            time.sleep(wait)

    raise RuntimeError(f"Edge TTS failed after {_MAX_RETRIES} attempts: {last_error}")


def measure_duration(audio_path: Path) -> float:
    """Use ffprobe to measure actual audio duration in seconds."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "json",
            str(audio_path),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    data = json.loads(result.stdout)
    return float(data["format"]["duration"])
