"""Video assembly pipeline: silent visual master + audio overlay.

Pipeline (per Codex review):
1. Render each scene as silent video (image + ken-burns, NO audio)
2. Cross-fade scenes with xfade (with proper offset accounting)
3. Burn in ASS subtitles
4. Mux with master audio (already mixed with BGM if applicable)
5. Validate output

Each step is a separate ffmpeg call for debuggability.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

VIDEO_W, VIDEO_H = 1920, 1080
FPS = 30
CROSSFADE_SEC = 0.4


@dataclass
class SilentScene:
    """One silent visual scene."""
    image_path: Path
    duration_sec: float
    ken_burns: bool = True


def render_silent_scene(
    scene: SilentScene, out_path: Path, *, scene_index: int = 0
) -> None:
    """Render one silent scene with optional ken-burns zoom."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    total_frames = int(scene.duration_sec * FPS)

    if scene.ken_burns:
        # Slow zoom in (1.0 → 1.10) over scene duration
        # Pre-scale to large size to avoid jitter (Codex recommendation)
        zoom_step = 0.10 / total_frames
        # Alternate zoom direction per scene for variety
        if scene_index % 2 == 0:
            # Zoom in
            zoom_expr = f"min(zoom+{zoom_step:.6f}\\,1.10)"
            xy = "x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
        else:
            # Zoom in from slight pan
            zoom_expr = f"min(zoom+{zoom_step:.6f}\\,1.10)"
            xy = "x='iw/2-(iw/zoom/2)+sin(on/30)*20':y='ih/2-(ih/zoom/2)'"

        vf = (
            f"scale=4000:-1,"
            f"zoompan=z='{zoom_expr}':d={total_frames}:{xy}:s={VIDEO_W}x{VIDEO_H}:fps={FPS}"
        )
    else:
        vf = f"scale={VIDEO_W}:{VIDEO_H}:flags=lanczos,fps={FPS}"

    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-loop", "1", "-i", str(scene.image_path),
        "-filter_complex", f"[0:v]{vf}[v]",
        "-map", "[v]",
        "-c:v", "libx264",
        "-preset", "fast",
        "-pix_fmt", "yuv420p",
        "-t", f"{scene.duration_sec:.3f}",
        "-an",  # no audio
        str(out_path),
    ]
    _run_ffmpeg(cmd, f"render_silent_scene {out_path.name}")


def crossfade_scenes(
    scene_paths: list[Path], scene_durations: list[float], out_path: Path,
) -> float:
    """Concatenate silent scenes with xfade transitions.

    Returns total visual duration (which is shorter than sum of scene durations
    by (n-1) * CROSSFADE_SEC).
    """
    if len(scene_paths) != len(scene_durations):
        raise ValueError("scene_paths and scene_durations length mismatch")

    if len(scene_paths) == 1:
        # Single scene: just copy
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(scene_paths[0]),
            "-c", "copy",
            str(out_path),
        ]
        _run_ffmpeg(cmd, "single_scene_copy")
        return scene_durations[0]

    # Build complex filter for chained xfade
    # Output duration = sum(durations) - (n-1) * fade
    fade = CROSSFADE_SEC

    # Inputs
    inputs: list[str] = []
    for path in scene_paths:
        inputs.extend(["-i", str(path)])

    # Filter graph: chain xfades with cumulative offset
    # offset_i = sum(durations[0..i]) - (i+1) * fade
    filter_parts = []
    cumulative = 0.0
    last_label = "0:v"

    for i in range(1, len(scene_paths)):
        cumulative += scene_durations[i - 1]
        offset = cumulative - i * fade
        next_label = f"x{i}"
        filter_parts.append(
            f"[{last_label}][{i}:v]xfade=transition=fade:duration={fade}:offset={offset:.3f}[{next_label}]"
        )
        last_label = next_label

    filter_graph = ";".join(filter_parts)

    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        *inputs,
        "-filter_complex", filter_graph,
        "-map", f"[{last_label}]",
        "-c:v", "libx264",
        "-preset", "fast",
        "-pix_fmt", "yuv420p",
        "-r", str(FPS),
        "-an",
        str(out_path),
    ]
    _run_ffmpeg(cmd, "crossfade_scenes")

    total = sum(scene_durations) - (len(scene_paths) - 1) * fade
    return total


def burn_subtitles(video_path: Path, ass_path: Path, out_path: Path) -> None:
    """Burn ASS subtitles into video using libass."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # ASS path needs special escaping for ffmpeg subtitles filter
    ass_str = str(ass_path).replace(":", "\\:").replace("'", "'\\\\\\''")
    vf = f"subtitles=filename='{ass_str}'"

    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(video_path),
        "-vf", vf,
        "-c:v", "libx264",
        "-preset", "fast",
        "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        str(out_path),
    ]
    _run_ffmpeg(cmd, "burn_subtitles")


def mux_audio(
    video_path: Path, audio_path: Path, out_path: Path,
    *, fade_out_video_at: float | None = None,
) -> None:
    """Mux video with audio. Optionally fade video to black at the end."""
    out_path.parent.mkdir(parents=True, exist_ok=True)

    audio_dur = _probe_duration(audio_path)
    video_dur = _probe_duration(video_path)
    final_dur = min(audio_dur, video_dur)

    # If video is shorter than audio (audio extends past visuals), pad with last frame
    # Or if video is longer, trim to audio length
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(video_path),
        "-i", str(audio_path),
        "-c:v", "libx264",
        "-preset", "fast",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        "-ar", "44100",
        "-shortest",
        "-t", f"{final_dur:.3f}",
        str(out_path),
    ]
    _run_ffmpeg(cmd, "mux_audio")


def validate_output(
    path: Path, *, min_duration: float = 30.0, max_duration: float = 1200.0,
) -> dict:
    """Validate the final output file. Returns metadata dict."""
    if not path.exists():
        raise RuntimeError(f"Output does not exist: {path}")

    size = path.stat().st_size
    if size < 1_000_000:
        raise RuntimeError(f"Output too small: {size} bytes")

    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration:stream=codec_type,codec_name,width,height",
            "-of", "json",
            str(path),
        ],
        capture_output=True, text=True, check=True, timeout=30,
    )
    data = json.loads(result.stdout)
    duration = float(data["format"]["duration"])

    if duration < min_duration:
        raise RuntimeError(f"Output too short: {duration:.1f}s (min {min_duration}s)")
    if duration > max_duration:
        raise RuntimeError(f"Output too long: {duration:.1f}s (max {max_duration}s)")

    streams = data.get("streams", [])
    has_video = any(s.get("codec_type") == "video" for s in streams)
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    if not has_video or not has_audio:
        raise RuntimeError(f"Missing streams: video={has_video}, audio={has_audio}")

    metadata = {
        "duration_sec": round(duration, 2),
        "size_bytes": size,
        "size_mb": round(size / 1_000_000, 2),
        "streams": streams,
    }
    logger.info(
        "Output validated: %.1fs, %.1fMB",
        metadata["duration_sec"], metadata["size_mb"],
    )
    return metadata


def _probe_duration(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "json",
            str(path),
        ],
        capture_output=True, text=True, check=True, timeout=30,
    )
    return float(json.loads(result.stdout)["format"]["duration"])


def _run_ffmpeg(cmd: list[str], label: str) -> None:
    logger.info("ffmpeg: %s", label)
    try:
        subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            timeout=600,
        )
    except subprocess.CalledProcessError as e:
        logger.error("ffmpeg failed: %s\nstderr (last 2000):\n%s",
                     label, e.stderr[-2000:] if e.stderr else "")
        raise RuntimeError(f"ffmpeg {label} failed (exit {e.returncode})")
