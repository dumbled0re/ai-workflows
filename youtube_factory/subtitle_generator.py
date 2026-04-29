"""Subtitle generation using Edge TTS WordBoundary metadata.

Edge TTS provides per-word timestamps as part of synthesis output. These are
significantly more accurate than character-count-based estimation, especially
for Japanese text with mixed English terms.

Output: ASS (Advanced SubStation Alpha) format for ffmpeg burn-in with rich styling.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from youtube_factory.audio_processor import SceneTiming
from youtube_factory.voice_synthesizer import WordCue

logger = logging.getLogger(__name__)

# Subtitle styling (ASS Style format)
ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
Timer: 100.0000

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Hiragino Kaku Gothic ProN W6,52,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,1,0,0,0,100,100,0,0,1,4,2,2,80,80,90,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


@dataclass
class SubtitleLine:
    start_sec: float
    end_sec: float
    text: str


def build_subtitles(
    scene_word_cues: list[list[WordCue]],
    scene_timings: list[SceneTiming],
    out_path: Path,
    *,
    max_chars_per_line: int = 22,
    max_line_duration: float = 4.0,
) -> Path:
    """Build ASS subtitle file from per-scene WordBoundary cues.

    Args:
        scene_word_cues: list of WordCue lists (one per scene)
        scene_timings: scene start/end on master timeline
        out_path: where to write the .ass file
        max_chars_per_line: target line length in Japanese characters
        max_line_duration: cap on line display time (seconds)
    """
    if len(scene_word_cues) != len(scene_timings):
        raise ValueError("scene_word_cues and scene_timings length mismatch")

    out_path.parent.mkdir(parents=True, exist_ok=True)

    all_lines: list[SubtitleLine] = []
    for cues, timing in zip(scene_word_cues, scene_timings, strict=True):
        scene_lines = _bucket_into_lines(
            cues, timing.start_sec, max_chars_per_line, max_line_duration,
        )
        all_lines.extend(scene_lines)

    # Write ASS file
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(ASS_HEADER)
        for line in all_lines:
            start = _ass_timestamp(line.start_sec)
            end = _ass_timestamp(line.end_sec)
            # Escape commas + braces in ASS text
            text = line.text.replace(",", "\\,").replace("{", "\\{").replace("}", "\\}")
            f.write(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}\n")

    logger.info("Generated %d subtitle lines → %s", len(all_lines), out_path)
    return out_path


def _bucket_into_lines(
    cues: list[WordCue],
    scene_offset: float,
    max_chars: int,
    max_dur: float,
) -> list[SubtitleLine]:
    """Group consecutive word cues into subtitle lines."""
    if not cues:
        return []

    lines: list[SubtitleLine] = []
    current_words: list[WordCue] = []
    current_chars = 0
    current_start: float | None = None

    for cue in cues:
        w_chars = len(cue.text)
        # Start new line if: char limit, duration limit, or starts a sentence
        if current_words:
            if (current_chars + w_chars > max_chars
                or (cue.end_sec - current_start) > max_dur):
                # Flush current line
                start = current_start
                end = current_words[-1].end_sec
                text = "".join(w.text for w in current_words).strip()
                if text:
                    lines.append(SubtitleLine(
                        start_sec=scene_offset + start,
                        end_sec=scene_offset + end,
                        text=text,
                    ))
                current_words = []
                current_chars = 0
                current_start = None

        if not current_words:
            current_start = cue.start_sec
        current_words.append(cue)
        current_chars += w_chars

    # Flush last line
    if current_words:
        start = current_start
        end = current_words[-1].end_sec
        text = "".join(w.text for w in current_words).strip()
        if text:
            lines.append(SubtitleLine(
                start_sec=scene_offset + start,
                end_sec=scene_offset + end,
                text=text,
            ))

    return lines


def _ass_timestamp(sec: float) -> str:
    """Format seconds as ASS timestamp H:MM:SS.cc"""
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h}:{m:02d}:{s:05.2f}"
