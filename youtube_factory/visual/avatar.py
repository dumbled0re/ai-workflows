"""Avatar lip-flap renderer with phoneme-aware mouth selection.

Two modes, picked automatically by `render_avatar_pngs`:
  1. Phoneme mode (preferred): caller supplies a WordCue list with `.vowel`
     (a/i/u/e/o/N/sil from pyopenjtalk). Each frame's mouth state is the
     vowel of the cue active at that timestamp.
  2. Amplitude mode (fallback): per-frame RMS of master_voice.wav
     thresholded into 3 mouth states. Used when vowel info is missing.

Both modes interleave a random blink schedule.

We use 3 mouth-shape buckets (wide / half / closed) sourced from the same 4
Pollinations face PNGs — keeps the character visually consistent rather
than hopping between 5 different vowel-specific renders per word.
   wide   — open vowels (a, o)
   half   — close+spread vowels (i, e, u)
   closed — N, sil, geminate stops, pauses

Caller (main.py) does the final ffmpeg `overlay` to place the PNG sequence
as a corner badge on the main video.
"""

from __future__ import annotations

import logging
import math
import random
import struct
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw

from youtube_factory.audio.voice import WordCue

logger = logging.getLogger(__name__)

DEFAULT_AVATAR_SIZE = 320
DEFAULT_FPS = 30

# Amplitude thresholds (normalized [0, 1]). Tuned for Edge TTS dynamics.
THRESHOLD_HALF = 0.12
THRESHOLD_WIDE = 0.40

# Blink behavior
BLINK_DUR_FRAMES = 3
BLINK_GAP_RANGE_SEC = (2.5, 5.5)

# Vowel → mouth shape mapping (3 shape buckets, character-consistent).
VOWEL_TO_SHAPE: dict[str, str] = {
    "a":   "wide",
    "o":   "wide",
    "i":   "half",
    "e":   "half",
    "u":   "half",
    "N":   "closed",
    "sil": "closed",
}


def render_avatar_pngs(
    voice_path: Path,
    faces_dir: Path,
    out_dir: Path,
    *,
    duration_sec: float,
    fps: int = DEFAULT_FPS,
    avatar_size: int = DEFAULT_AVATAR_SIZE,
    seed: int = 42,
    word_cues: list[WordCue] | None = None,
) -> int:
    """Render avatar PNG sequence to out_dir.

    word_cues: when provided AND any cue has `.vowel` set, the per-frame
    mouth state is selected from the active cue's vowel (phoneme mode).
    Otherwise falls back to amplitude-thresholded RMS.

    Returns the number of frames written.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    # Wipe any existing frames to avoid stale composition
    for old in out_dir.glob("frame_*.png"):
        old.unlink()

    faces = {
        "closed": _prepare_face(faces_dir / "face_closed.png", avatar_size),
        "half":   _prepare_face(faces_dir / "face_half_open.png", avatar_size),
        "wide":   _prepare_face(faces_dir / "face_wide_open.png", avatar_size),
        "blink":  _prepare_face(faces_dir / "face_blink.png", avatar_size),
    }

    n_frames = max(1, int(round(duration_sec * fps)))
    rng = random.Random(seed)
    blink_set = _plan_blinks(n_frames, fps, rng)

    use_phoneme = bool(word_cues) and any(c.vowel for c in (word_cues or []))

    if use_phoneme:
        plan = _plan_from_cues(word_cues, n_frames=n_frames, fps=fps)
        # Smooth out single-frame state changes — eliminates jittery micro-flicker
        # between consecutive vowels at mora boundaries while keeping the actual
        # speaking rhythm visible.
        plan = _smooth_plan(plan, min_run=4)
        logger.info("Avatar: phoneme-driven (%d cues with vowel)", sum(1 for c in word_cues if c.vowel))
    else:
        # Amplitude fallback
        amps = _per_frame_rms(voice_path, fps=fps)
        if not amps:
            logger.warning("No audio amplitudes parsed; using static closed face")
            amps_norm = [0.0] * n_frames
        else:
            sorted_amps = sorted(amps)
            p90 = sorted_amps[int(0.9 * (len(sorted_amps) - 1))] or max(sorted_amps) or 1.0
            amps_norm = [min(1.0, a / p90) for a in amps]
            if len(amps_norm) < n_frames:
                amps_norm.extend([0.0] * (n_frames - len(amps_norm)))
            else:
                amps_norm = amps_norm[:n_frames]
        smoothed = _smooth(amps_norm, window=3)
        plan = []
        for i in range(n_frames):
            a = smoothed[i] if i < len(smoothed) else 0.0
            if a < THRESHOLD_HALF:
                plan.append("closed")
            elif a < THRESHOLD_WIDE:
                plan.append("half")
            else:
                plan.append("wide")
        logger.info("Avatar: amplitude-driven (%d frames)", n_frames)

    for i in range(n_frames):
        state = "blink" if i in blink_set else plan[i]
        faces[state].save(out_dir / f"frame_{i:05d}.png", "PNG")

    logger.info(
        "Avatar PNGs: %d frames (%.1fs @ %dfps) → %s",
        n_frames, duration_sec, fps, out_dir,
    )
    return n_frames


def _plan_from_cues(
    cues: list[WordCue], *, n_frames: int, fps: int,
) -> list[str]:
    """Per-frame mouth state plan from WordCue list with vowel info."""
    plan = ["closed"] * n_frames
    for cue in cues:
        if cue.vowel is None:
            continue
        shape = VOWEL_TO_SHAPE.get(cue.vowel, "closed")
        start_f = max(0, int(cue.start_sec * fps))
        end_f = min(n_frames, int(round(cue.end_sec * fps)))
        for f in range(start_f, end_f):
            plan[f] = shape
    return plan


def _smooth_plan(plan: list[str], *, min_run: int = 4) -> list[str]:
    """Collapse runs shorter than min_run into the surrounding state to
    suppress micro-flicker. Preserves overall speaking rhythm because longer
    runs (>= min_run frames ~133ms) are kept as-is."""
    if len(plan) < min_run * 2:
        return plan
    out = list(plan)
    i = 0
    n = len(out)
    while i < n:
        j = i + 1
        while j < n and out[j] == out[i]:
            j += 1
        run_len = j - i
        if run_len < min_run and i > 0:
            # Extend the previous state over this short run
            prev_state = out[i - 1]
            for k in range(i, j):
                out[k] = prev_state
        i = j
    return out


def _prepare_face(src: Path, size: int) -> Image.Image:
    """Load face PNG, center-square crop, resize, apply circular mask + ring."""
    img = Image.open(src).convert("RGBA")
    w, h = img.size
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    img = img.crop((left, top, left + side, top + side))
    img = img.resize((size, size), Image.Resampling.LANCZOS)

    # Circular alpha mask
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, size, size], fill=255)
    masked = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    masked.paste(img, (0, 0), mask)

    # Thin white ring border for definition against any background
    draw = ImageDraw.Draw(masked)
    border = max(3, size // 80)
    draw.ellipse(
        [border // 2, border // 2, size - border // 2 - 1, size - border // 2 - 1],
        outline=(255, 255, 255, 235),
        width=border,
    )
    return masked


def _per_frame_rms(
    wav_path: Path, *, fps: int, sample_rate: int = 22050,
) -> list[float]:
    """Decode mono float32 PCM via ffmpeg, compute per-frame RMS."""
    proc = subprocess.run(
        [
            "ffmpeg", "-loglevel", "error", "-i", str(wav_path),
            "-f", "f32le", "-ac", "1", "-ar", str(sample_rate),
            "-",
        ],
        capture_output=True, check=True, timeout=120,
    )
    raw = proc.stdout
    n_samples = len(raw) // 4
    if n_samples == 0:
        return []

    samples_per_frame = max(1, sample_rate // fps)
    rms: list[float] = []
    for i in range(0, n_samples, samples_per_frame):
        chunk_bytes = raw[i * 4 : (i + samples_per_frame) * 4]
        if not chunk_bytes:
            break
        chunk = struct.unpack(f"{len(chunk_bytes) // 4}f", chunk_bytes)
        if not chunk:
            continue
        ssum = sum(s * s for s in chunk)
        rms.append(math.sqrt(ssum / len(chunk)))
    return rms


def _smooth(values: list[float], *, window: int = 3) -> list[float]:
    """Simple moving average. Window must be odd; rounded down if even."""
    if window <= 1 or len(values) < window:
        return list(values)
    half = window // 2
    out: list[float] = []
    for i in range(len(values)):
        lo = max(0, i - half)
        hi = min(len(values), i + half + 1)
        out.append(sum(values[lo:hi]) / (hi - lo))
    return out


def _plan_blinks(n_frames: int, fps: int, rng: random.Random) -> set[int]:
    """Schedule blinks: 3-frame closed eyes, every 2.5-5.5 sec."""
    blinks: set[int] = set()
    cursor = int(fps * rng.uniform(*BLINK_GAP_RANGE_SEC))
    while cursor + BLINK_DUR_FRAMES < n_frames:
        for k in range(BLINK_DUR_FRAMES):
            blinks.add(cursor + k)
        cursor += int(fps * rng.uniform(*BLINK_GAP_RANGE_SEC))
    return blinks
