"""Audio post-processing: build master.wav from individual TTS outputs.

Strategy (per Codex review):
- Decode all individual TTS mp3s to PCM
- Concatenate with crossfade-free butt-joins (Edge TTS files end naturally)
- Apply EBU R128 loudnorm to normalize loudness
- Optionally mix BGM with sidechaincompress (ducking)
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Audio format
SAMPLE_RATE = 44100
CHANNELS = 2
BIT_DEPTH = "s16"

# Loudness targets (per Codex: -16 LUFS for YouTube voice content)
LUFS_TARGET = -16.0
TRUE_PEAK = -1.5
LRA = 11.0

# BGM ducking — tuned so BGM is clearly audible in voice gaps but
# politely steps down during narration. Base level 12dB below voice target
# (= "ambient bed" feel), gentle 3:1 ratio so duck is musical not
# pumping, threshold low enough that any spoken word triggers it.
BGM_VOLUME_DB = -18.0  # base BGM volume (audible bed during silences)
DUCK_RATIO = 3.0       # sidechain compression ratio (gentle)
DUCK_THRESHOLD = -32.0 # trigger duck on any voice activity


@dataclass
class SceneTiming:
    """One scene's start/end on the master audio timeline."""
    index: int
    start_sec: float
    end_sec: float

    @property
    def duration_sec(self) -> float:
        return self.end_sec - self.start_sec


def build_master_audio(
    audio_paths: list[Path],
    out_path: Path,
    *,
    gap_sec: float = 0.05,
) -> tuple[float, list[SceneTiming]]:
    """Concatenate audio files into a single normalized master.wav.

    Args:
        audio_paths: TTS mp3 files in order
        out_path: where to write master.wav
        gap_sec: tiny silence between scenes for breathing room

    Returns:
        (total_duration, scene_timings)
    """
    if not audio_paths:
        raise ValueError("No audio files to concat")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    work_dir = out_path.parent / "audio_work"
    work_dir.mkdir(exist_ok=True)

    # Step 1: convert each mp3 to wav with consistent format
    wav_paths = []
    for i, path in enumerate(audio_paths):
        wav_path = work_dir / f"scene_{i:03d}.wav"
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(path),
            "-ar", str(SAMPLE_RATE),
            "-ac", str(CHANNELS),
            "-c:a", "pcm_s16le",
            str(wav_path),
        ]
        subprocess.run(cmd, check=True, timeout=120)
        wav_paths.append(wav_path)

    # Step 2: probe each WAV for accurate duration & build scene timeline
    timings = []
    cumulative = 0.0
    for i, wav in enumerate(wav_paths):
        dur = _probe_duration(wav)
        timings.append(SceneTiming(index=i, start_sec=cumulative, end_sec=cumulative + dur))
        cumulative += dur
        if i < len(wav_paths) - 1:
            cumulative += gap_sec  # small gap before next scene

    # Step 3: concat with tiny silence between (concat demuxer + apad/atrim approach)
    # Easier: build a complex filter that concatenates with silence
    silence_wav = work_dir / "silence.wav"
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi",
        "-t", f"{gap_sec}",
        "-i", f"anullsrc=r={SAMPLE_RATE}:cl=stereo",
        "-c:a", "pcm_s16le",
        str(silence_wav),
    ]
    subprocess.run(cmd, check=True, timeout=30)

    # Build a list file for concat demuxer
    list_file = work_dir / "concat_list.txt"
    with open(list_file, "w") as f:
        for i, wav in enumerate(wav_paths):
            f.write(f"file '{wav.resolve()}'\n")
            if i < len(wav_paths) - 1:
                f.write(f"file '{silence_wav.resolve()}'\n")

    raw_master = work_dir / "master_raw.wav"
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "concat", "-safe", "0",
        "-i", str(list_file),
        "-c:a", "pcm_s16le",
        str(raw_master),
    ]
    subprocess.run(cmd, check=True, timeout=120)

    # Step 4: loudnorm 2-pass for accurate normalization
    _loudnorm_2pass(raw_master, out_path)

    total = _probe_duration(out_path)
    logger.info(
        "Master audio: %.2fs across %d scenes, target -%dLUFS",
        total, len(timings), abs(int(LUFS_TARGET)),
    )

    # Cleanup intermediate files
    for wav in wav_paths:
        wav.unlink(missing_ok=True)
    silence_wav.unlink(missing_ok=True)
    list_file.unlink(missing_ok=True)
    raw_master.unlink(missing_ok=True)

    return total, timings


def generate_procedural_bgm(out_path: Path, duration_sec: float) -> Path:
    """Generate a low-key ambient pad with ffmpeg lavfi only (no API/key).

    Texture (A minor with added 9th, breathy):
      - Sub bass A1 (55Hz) drone, very faint
      - Bass A2 (110Hz) main drone
      - Triad C3 / E3 with slight pitch modulation for chorus
      - 9th add (B3) with slow swell — gives "thoughtful news" mood
      - Slow LFO on top voices = breathing
      - Subtle aecho for space, lowpass for warmth
    Quiet enough to sit under narration without competing.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Note: chained sin() with subtle frequency modulation (LFO * t)
    # gives a "chorused" texture. Each voice has different LFO speed for
    # gradual phase wandering — natural, non-mechanical sound.
    expr = (
        "0.16*sin(2*PI*110*t)"                                  # A2
        "+0.13*sin(2*PI*(130.81 + 0.6*sin(2*PI*0.07*t))*t)"     # C3 chorused
        "+0.14*sin(2*PI*(164.81 + 0.7*sin(2*PI*0.09*t))*t)"     # E3 chorused
        "+0.10*sin(2*PI*220*t)"                                 # A3 (octave brightness)
        "+0.08*sin(2*PI*329.63*t)"                              # E4 air
        # 9th swells in slowly — emotional lift
        "+0.07*sin(2*PI*246.94*t)*max(0,sin(2*PI*0.05*t))"      # B3 swell
    )

    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-t", f"{duration_sec:.2f}",
        "-i", f"aevalsrc='{expr}:s={SAMPLE_RATE}'",
        "-af",
        # Keep enough midrange to be audible on laptop speakers,
        # warmth + space + breathing
        "highpass=f=80,"
        "lowpass=f=1800,"
        "aphaser=in_gain=0.5:out_gain=0.74:delay=3:decay=0.4:speed=0.22,"
        "aecho=0.5:0.7:60|110:0.22|0.16,"
        "tremolo=f=0.14:d=0.18,"
        f"afade=t=in:st=0:d=2.5,afade=t=out:st={max(0.0, duration_sec - 3.0):.2f}:d=3.0,"
        "aformat=channel_layouts=stereo",
        "-c:a", "pcm_s16le",
        "-ar", str(SAMPLE_RATE),
        str(out_path),
    ]
    subprocess.run(cmd, check=True, timeout=180)
    logger.info("Generated procedural BGM (%.1fs) → %s", duration_sec, out_path.name)
    return out_path


def mix_bgm(
    voice_path: Path,
    bgm_path: Path,
    out_path: Path,
    *,
    voice_lufs: float = LUFS_TARGET,
    bgm_db: float = BGM_VOLUME_DB,
) -> None:
    """Mix narration with BGM using sidechain compression for ducking.

    BGM is automatically attenuated when narration is present.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)

    voice_dur = _probe_duration(voice_path)

    # Filter graph:
    # 1. Loop BGM long enough, trim to voice duration
    # 2. Apply base BGM volume reduction
    # 3. Sidechain compress BGM with voice signal
    # 4. Mix voice + (compressed BGM)
    # 5. Final loudnorm

    # `normalize=0` keeps amix from auto-attenuating each input by 1/N
    # (the default normalize=1 was making BGM nearly inaudible after the
    # weight division). With normalize=0, the BGM's `volume=` setting is
    # the actual final level; voice stays at its loudnorm-target.
    filter_graph = (
        f"[1:a]aloop=loop=-1:size=2e+09,atrim=0:{voice_dur:.3f},asetpts=PTS-STARTPTS,"
        f"volume={bgm_db}dB,afade=t=in:st=0:d=1.5,afade=t=out:st={voice_dur - 2.5:.3f}:d=2.5[bgm];"
        f"[0:a]asplit=2[voice_in][voice_sidechain];"
        f"[bgm][voice_sidechain]sidechaincompress=threshold={_db_to_linear(DUCK_THRESHOLD):.4f}:"
        f"ratio={DUCK_RATIO}:attack=8:release=250:makeup=1[bgm_ducked];"
        f"[voice_in][bgm_ducked]amix=inputs=2:weights=1 1:normalize=0:duration=first:dropout_transition=0[mixed]"
    )

    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(voice_path),
        "-i", str(bgm_path),
        "-filter_complex", filter_graph,
        "-map", "[mixed]",
        "-c:a", "pcm_s16le",
        "-ar", str(SAMPLE_RATE),
        "-ac", str(CHANNELS),
        str(out_path),
    ]
    subprocess.run(cmd, check=True, timeout=300)
    logger.info("Mixed audio with BGM ducking → %s", out_path.name)


def _loudnorm_2pass(input_wav: Path, output_wav: Path) -> None:
    """2-pass EBU R128 loudness normalization."""
    # Pass 1: measure
    cmd = [
        "ffmpeg", "-y", "-i", str(input_wav),
        "-af", f"loudnorm=I={LUFS_TARGET}:TP={TRUE_PEAK}:LRA={LRA}:print_format=json",
        "-f", "null", "-",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    # Parse JSON from stderr (loudnorm outputs there)
    try:
        json_start = result.stderr.rindex("{")
        json_end = result.stderr.rindex("}") + 1
        data = json.loads(result.stderr[json_start:json_end])
    except (ValueError, json.JSONDecodeError):
        logger.warning("loudnorm pass 1 parse failed; falling back to single-pass")
        data = None

    # Pass 2: apply
    if data:
        af = (
            f"loudnorm=I={LUFS_TARGET}:TP={TRUE_PEAK}:LRA={LRA}:"
            f"measured_I={data['input_i']}:measured_TP={data['input_tp']}:"
            f"measured_LRA={data['input_lra']}:measured_thresh={data['input_thresh']}:"
            f"offset={data['target_offset']}:linear=true:print_format=summary"
        )
    else:
        af = f"loudnorm=I={LUFS_TARGET}:TP={TRUE_PEAK}:LRA={LRA}"

    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(input_wav),
        "-af", af,
        "-ar", str(SAMPLE_RATE),
        "-ac", str(CHANNELS),
        "-c:a", "pcm_s16le",
        str(output_wav),
    ]
    subprocess.run(cmd, check=True, timeout=180)


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


def _db_to_linear(db: float) -> float:
    """Convert dB to linear amplitude."""
    return 10 ** (db / 20.0)


def detect_silence_at_boundaries(
    audio_path: Path, timings: list[SceneTiming], tolerance: float = 0.5,
) -> list[tuple[int, float]]:
    """QA: detect prolonged silences at scene boundaries.

    Returns list of (scene_index, silence_duration) for problematic boundaries.
    Used for diagnostic output, not pipeline gating.
    """
    cmd = [
        "ffmpeg", "-i", str(audio_path),
        "-af", "silencedetect=noise=-50dB:d=0.4",
        "-f", "null", "-",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    issues: list[tuple[int, float]] = []
    # Parse silence_start / silence_end pairs from stderr
    silence_periods = []
    current_start = None
    for line in result.stderr.split("\n"):
        if "silence_start" in line:
            try:
                current_start = float(line.split("silence_start: ")[1].split()[0])
            except (IndexError, ValueError):
                pass
        elif "silence_end" in line and current_start is not None:
            try:
                end = float(line.split("silence_end: ")[1].split(" ")[0])
                silence_periods.append((current_start, end))
                current_start = None
            except (IndexError, ValueError):
                pass

    for sp_start, sp_end in silence_periods:
        sp_dur = sp_end - sp_start
        if sp_dur > tolerance:
            for t in timings:
                if abs(t.start_sec - sp_start) < 1.0 or abs(t.end_sec - sp_end) < 1.0:
                    issues.append((t.index, sp_dur))
                    break
    return issues
