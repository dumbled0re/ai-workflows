"""ASS subtitle generation with karaoke-style word highlighting.

Uses WordCue timing (from Edge TTS WordBoundary OR VOICEVOX accent_phrases)
to drive per-syllable karaoke wipe effects via ASS `\\kf` tags.

Visual:
  - Text bucketed into ~22-char lines, ~4s max each
  - Each line fades in/out (200ms)
  - Within each line, each word/phrase wipes from dim-white → amber in sync
    with narration (the "wipe front" is the currently-spoken word)
  - Strong outline + semi-transparent box for any-background readability

Karaoke can be disabled by setting YOUTUBE_FACTORY_KARAOKE=0 (plain subs).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from youtube_factory.audio.master import SceneTiming
from youtube_factory.audio.voice import WordCue

logger = logging.getLogger(__name__)

KARAOKE_ENABLED = os.environ.get("YOUTUBE_FACTORY_KARAOKE", "1") not in ("0", "false", "no")

# ASS colors are &HAABBGGRR& (alpha first, then BGR)
# Primary (initial/yet-to-speak):  dim white   #DDDDDD → AA00 BB DD GG DD RR DD
# Secondary (sung/active wipe):    amber       #FFC107 → BBGGRR = 07 C1 FF
# Outline:                         black       #000000
# Back (box):                      semi-trans black 70% → AA = A0
ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 0
ScaledBorderAndShadow: yes
Timer: 100.0000

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Hiragino Kaku Gothic ProN W6,58,&H00DDDDDD,&H0007C1FF,&H00000000,&HA0000000,1,0,0,0,100,100,0,0,3,5,2,2,90,90,110,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


@dataclass
class _LineBucket:
    """Group of WordCues that will become one Dialogue line."""
    words: list[WordCue] = field(default_factory=list)
    start_sec: float = 0.0


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

    line_count = 0
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(ASS_HEADER)
        for cues, timing in zip(scene_word_cues, scene_timings, strict=True):
            for bucket in _bucket_into_lines(cues, max_chars_per_line, max_line_duration):
                if not bucket.words:
                    continue
                start = timing.start_sec + bucket.words[0].start_sec
                end = timing.start_sec + bucket.words[-1].end_sec
                text = _render_bucket(bucket, line_start=bucket.words[0].start_sec)
                f.write(
                    f"Dialogue: 0,{_ass_ts(start)},{_ass_ts(end)},"
                    f"Default,,0,0,0,,{text}\n"
                )
                line_count += 1

    logger.info(
        "Generated %d subtitle lines → %s (karaoke=%s)",
        line_count, out_path, "on" if KARAOKE_ENABLED else "off",
    )
    return out_path


def _bucket_into_lines(
    cues: list[WordCue],
    max_chars: int,
    max_dur: float,
) -> list[_LineBucket]:
    """Group consecutive word cues into subtitle lines."""
    if not cues:
        return []

    buckets: list[_LineBucket] = []
    current = _LineBucket()
    current_chars = 0

    for cue in cues:
        w_chars = len(cue.text)
        if current.words:
            duration = cue.end_sec - current.words[0].start_sec
            if current_chars + w_chars > max_chars or duration > max_dur:
                buckets.append(current)
                current = _LineBucket()
                current_chars = 0

        if not current.words:
            current.start_sec = cue.start_sec
        current.words.append(cue)
        current_chars += w_chars

    if current.words:
        buckets.append(current)
    return buckets


def _render_bucket(bucket: _LineBucket, *, line_start: float) -> str:
    """Render one bucket as ASS Dialogue text. Karaoke or plain."""
    text_clean = _ass_escape("".join(w.text for w in bucket.words).strip())
    if not text_clean:
        return ""

    if not KARAOKE_ENABLED:
        return f"{{\\fad(150,150)}}{text_clean}"

    # Karaoke: group consecutive morae into ~3-mora chunks so each `\kf`
    # wipe lasts ~250-400ms (visually smooth) instead of per-mora flicker.
    chunks = _group_morae_for_karaoke(bucket.words, target_per_chunk=3)
    parts: list[str] = ["{\\fad(150,150)}"]
    n_chunks = len(chunks)
    for i, chunk in enumerate(chunks):
        first, last = chunk[0], chunk[-1]
        if i + 1 < n_chunks:
            seg_end = chunks[i + 1][0].start_sec
        else:
            seg_end = last.end_sec
        seg_dur = max(0.10, seg_end - first.start_sec)
        cs = max(1, int(round(seg_dur * 100)))
        chunk_text = _ass_escape("".join(c.text for c in chunk))
        parts.append(f"{{\\kf{cs}}}{chunk_text}")
    return "".join(parts)


def _group_morae_for_karaoke(
    cues: list[WordCue], *, target_per_chunk: int = 3,
) -> list[list[WordCue]]:
    """Cluster consecutive cues into groups of ~target_per_chunk, but break
    at punctuation cues (text 「、。」 or vowel == 'sil') so wipes align with
    natural prosodic boundaries."""
    chunks: list[list[WordCue]] = []
    current: list[WordCue] = []
    for cue in cues:
        is_break = cue.vowel == "sil" or cue.text in {"、", "。", "！", "？"}
        if is_break and current:
            chunks.append(current)
            chunks.append([cue])
            current = []
            continue
        current.append(cue)
        if len(current) >= target_per_chunk:
            chunks.append(current)
            current = []
    if current:
        chunks.append(current)
    return chunks


def _ass_escape(text: str) -> str:
    """Escape ASS-significant characters in text."""
    if not text:
        return ""
    # Backslash and braces are special; commas only in style fields, not text
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


def _ass_ts(sec: float) -> str:
    """Format seconds as ASS timestamp H:MM:SS.cc"""
    if sec < 0:
        sec = 0
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h}:{m:02d}:{s:05.2f}"
