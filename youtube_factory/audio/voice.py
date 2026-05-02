"""Voice synthesis with WordCue timing extraction.

Two backends:
  1. VOICEVOX-compatible HTTP engine (VOICEVOX or AivisSpeech) — recommended,
     state-of-art free Japanese TTS. Mora-level timing reconstructed from
     accent_phrases for accurate subtitle alignment.
  2. Edge TTS fallback — Microsoft Edge's TTS (free, no key, WordBoundary cues).

Selection (in order):
  - YOUTUBE_FACTORY_TTS=edge → force Edge TTS
  - YOUTUBE_FACTORY_TTS=voicevox → force VOICEVOX (raises if unavailable)
  - default: probe VOICEVOX_URL (default http://localhost:50021); if reachable,
    use VOICEVOX; otherwise Edge TTS.

Both backends return SynthesisResult with the same shape.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import edge_tts
import requests

logger = logging.getLogger(__name__)

# Edge TTS defaults
DEFAULT_VOICE = "ja-JP-NanamiNeural"
DEFAULT_RATE = "+0%"

# VOICEVOX/AivisSpeech defaults
# Speaker 13 = 青山龍星 (calm male, news-anchor feel) on stock VOICEVOX
DEFAULT_VOICEVOX_URL = os.environ.get("VOICEVOX_URL", "http://localhost:50021")
DEFAULT_VOICEVOX_SPEAKER = int(os.environ.get("VOICEVOX_SPEAKER", "13"))
DEFAULT_VOICEVOX_SPEED = float(os.environ.get("VOICEVOX_SPEED", "1.05"))

_MAX_RETRIES = 3
_BACKOFF_BASE = 2

# Module-level cache for the autodetected backend
_BACKEND_CHOICE: str | None = None  # "voicevox_core" | "voicevox_http" | "edge" | None (re-probe)

# voicevox_core (Python lib, no GUI app needed) paths
VOICEVOX_CORE_DIR = Path(__file__).parent.parent / "assets" / "voicevox_core"
VOICEVOX_CORE_STYLE_ID = int(os.environ.get("VOICEVOX_STYLE_ID", "3"))  # 3 = ずんだもん ノーマル
VOICEVOX_CORE_SPEED = float(os.environ.get("VOICEVOX_SPEED", "1.05"))
_VOICEVOX_SYNTH = None  # cached Synthesizer
_VOICEVOX_LOADED_VVMS: set[str] = set()

# Whisper STT for forced alignment (replaces estimated mora timing with
# real audio-derived word timestamps). Activated when faster-whisper is
# importable and YOUTUBE_FACTORY_NO_WHISPER is not set. Model is loaded
# lazily on first call and cached.
WHISPER_MODEL_SIZE = os.environ.get("YOUTUBE_FACTORY_WHISPER_MODEL", "small")
_WHISPER_MODEL = None  # type: ignore[var-annotated]
_WHISPER_DISABLED = os.environ.get("YOUTUBE_FACTORY_NO_WHISPER", "0") in ("1", "true", "yes")


@dataclass
class WordCue:
    """One word/phrase/mora boundary event.

    vowel: ISO-style mora vowel code if known — 'a','i','u','e','o' for the
    five standard vowels, 'N' for the syllabic nasal /ん/, 'sil' for pauses
    and geminate stops, or None when the segmenter only knows the text.
    Used by avatar lip-sync to pick mouth shapes.
    """
    text: str
    start_sec: float
    end_sec: float
    vowel: str | None = None


@dataclass
class SynthesisResult:
    """Result of one TTS synthesis."""
    audio_path: Path
    duration_sec: float
    word_cues: list[WordCue] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Public entry point — backend router
# ---------------------------------------------------------------------------

def synthesize(
    text: str,
    audio_path: Path,
    voice: str = DEFAULT_VOICE,
    rate: str = DEFAULT_RATE,
) -> SynthesisResult:
    """Synthesize text → audio file, return path + duration + cues.

    Auto-routes to VOICEVOX-compatible engine if available, else Edge TTS.
    Result cues are then optionally replaced by Whisper-aligned cues for
    accurate audio-anchored timing (recommended; major quality win for
    subtitles + lip sync).
    """
    backend = _select_backend()
    global _BACKEND_CHOICE

    if backend == "voicevox_core":
        try:
            result = _synthesize_voicevox_core(text, audio_path)
            # voicevox_core gives accurate audio_query timing — skip Whisper
            return result
        except Exception as e:
            logger.warning("voicevox_core failed for %r: %s; falling back",
                           text[:30], e)
            _BACKEND_CHOICE = "edge"
            backend = "edge"

    if backend == "voicevox_http":
        try:
            result = _synthesize_voicevox(text, audio_path)
            return result
        except Exception as e:
            logger.warning("voicevox_http failed for %r: %s; falling back to Edge",
                           text[:30], e)
            _BACKEND_CHOICE = "edge"

    result = _synthesize_edge(text, audio_path, voice, rate)

    # Whisper realignment: replace estimated/edge cues with real audio-anchored timing
    if not _WHISPER_DISABLED:
        try:
            aligned = _whisper_align(result.audio_path)
            if aligned:
                result.word_cues = aligned
        except ImportError:
            pass  # faster-whisper not installed; keep estimated cues
        except Exception as e:
            logger.warning("Whisper align failed for %s (%s); keeping fallback cues",
                           audio_path.name, e)

    return result


def _select_backend() -> str:
    """Choose backend once; cache the choice.

    Priority:
      1. voicevox_core (Python lib, no app needed) — best Japanese voice for free
      2. voicevox_http (running engine on localhost:50021)
      3. edge — universal fallback
    """
    global _BACKEND_CHOICE
    if _BACKEND_CHOICE is not None:
        return _BACKEND_CHOICE

    forced = os.environ.get("YOUTUBE_FACTORY_TTS", "").lower().strip()
    if forced == "edge":
        _BACKEND_CHOICE = "edge"
        logger.info("TTS backend: edge (forced)")
        return _BACKEND_CHOICE
    if forced in ("voicevox_core", "vvc"):
        _BACKEND_CHOICE = "voicevox_core"
        logger.info("TTS backend: voicevox_core (forced)")
        return _BACKEND_CHOICE
    if forced in ("voicevox", "voicevox_http"):
        _BACKEND_CHOICE = "voicevox_http"
        logger.info("TTS backend: voicevox_http (forced) → %s", DEFAULT_VOICEVOX_URL)
        return _BACKEND_CHOICE

    # Autoprobe 1: voicevox_core lib + assets present
    if _voicevox_core_assets_ready():
        try:
            import voicevox_core  # noqa: F401
            _BACKEND_CHOICE = "voicevox_core"
            logger.info("TTS backend: voicevox_core (autodetected, no app required)")
            return _BACKEND_CHOICE
        except ImportError:
            pass

    # Autoprobe 2: voicevox_http engine
    try:
        r = requests.get(f"{DEFAULT_VOICEVOX_URL}/version", timeout=1.5)
        if r.status_code == 200:
            _BACKEND_CHOICE = "voicevox_http"
            logger.info("TTS backend: voicevox_http (autodetected) → %s", DEFAULT_VOICEVOX_URL)
            return _BACKEND_CHOICE
    except requests.RequestException:
        pass

    _BACKEND_CHOICE = "edge"
    logger.info("TTS backend: edge (voicevox_core/http unavailable)")
    return _BACKEND_CHOICE


def _voicevox_core_assets_ready() -> bool:
    """Check that all required VOICEVOX core asset files exist."""
    if not VOICEVOX_CORE_DIR.exists():
        return False
    has_dylib = any(VOICEVOX_CORE_DIR.glob("onnxruntime/lib/libvoicevox_onnxruntime*.dylib"))
    has_dict = (VOICEVOX_CORE_DIR / "dict" / "open_jtalk_dic_utf_8-1.11").exists()
    has_models = (VOICEVOX_CORE_DIR / "models" / "vvms").exists() and any(
        (VOICEVOX_CORE_DIR / "models" / "vvms").glob("*.vvm")
    )
    return has_dylib and has_dict and has_models


# ---------------------------------------------------------------------------
# Backend: VOICEVOX-compatible HTTP API
# ---------------------------------------------------------------------------

def _synthesize_voicevox(
    text: str,
    audio_path: Path,
    *,
    speaker: int = DEFAULT_VOICEVOX_SPEAKER,
    base_url: str = DEFAULT_VOICEVOX_URL,
    speed: float = DEFAULT_VOICEVOX_SPEED,
) -> SynthesisResult:
    """Call VOICEVOX/AivisSpeech HTTP API. Returns audio + mora-derived cues."""
    audio_path.parent.mkdir(parents=True, exist_ok=True)

    last_error: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            # Step 1: audio_query
            r1 = requests.post(
                f"{base_url}/audio_query",
                params={"text": text, "speaker": speaker},
                timeout=30,
            )
            r1.raise_for_status()
            query = r1.json()
            query["speedScale"] = speed
            query["outputSamplingRate"] = 44100
            query["outputStereo"] = False
            # Step 2: synthesis
            r2 = requests.post(
                f"{base_url}/synthesis",
                params={"speaker": speaker},
                data=json.dumps(query),
                headers={"Content-Type": "application/json"},
                timeout=180,
            )
            r2.raise_for_status()
            wav_bytes = r2.content
            break
        except Exception as e:
            last_error = e
            wait = _BACKOFF_BASE**attempt
            logger.warning(
                "VOICEVOX attempt %d/%d failed: %s. Retrying in %ds",
                attempt + 1, _MAX_RETRIES, e, wait,
            )
            time.sleep(wait)
    else:
        raise RuntimeError(f"VOICEVOX failed after {_MAX_RETRIES} attempts: {last_error}")

    # Write to caller's path; transcode if not .wav
    if audio_path.suffix.lower() == ".wav":
        audio_path.write_bytes(wav_bytes)
        out_path = audio_path
    else:
        tmp_wav = audio_path.with_suffix(".wav.tmp")
        tmp_wav.write_bytes(wav_bytes)
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y", "-loglevel", "error",
                    "-i", str(tmp_wav),
                    "-c:a", "libmp3lame", "-b:a", "192k",
                    str(audio_path),
                ],
                check=True, timeout=120,
            )
        finally:
            tmp_wav.unlink(missing_ok=True)
        out_path = audio_path

    # Reconstruct WordCues from accent_phrases (one cue per phrase)
    cues = _voicevox_cues_from_query(query)

    duration = measure_duration(out_path)
    logger.info(
        "VOICEVOX synth %d chars → %.2fs, %d cues (%s)",
        len(text), duration, len(cues), out_path.name,
    )
    return SynthesisResult(
        audio_path=out_path,
        duration_sec=duration,
        word_cues=cues,
    )


# ---------------------------------------------------------------------------
# Backend: voicevox_core Python lib (no app, library only)
# ---------------------------------------------------------------------------

def _get_voicevox_core_synth(style_id: int):
    """Lazy-load Synthesizer; load the .vvm containing the requested style."""
    global _VOICEVOX_SYNTH
    from voicevox_core.blocking import (  # type: ignore[import-not-found]
        Synthesizer, OpenJtalk, Onnxruntime, VoiceModelFile,
    )

    if _VOICEVOX_SYNTH is None:
        dylib_candidates = sorted(
            VOICEVOX_CORE_DIR.glob("onnxruntime/lib/libvoicevox_onnxruntime*.dylib")
        ) or sorted(
            VOICEVOX_CORE_DIR.glob("onnxruntime/lib/libvoicevox_onnxruntime*.so")
        )
        if not dylib_candidates:
            raise RuntimeError("voicevox onnxruntime not found in assets")
        dict_path = VOICEVOX_CORE_DIR / "dict" / "open_jtalk_dic_utf_8-1.11"
        logger.info("Loading voicevox_core (onnx + jtalk dict)...")
        ort = Onnxruntime.load_once(filename=str(dylib_candidates[0]))
        ojt = OpenJtalk(str(dict_path))
        _VOICEVOX_SYNTH = Synthesizer(ort, ojt)

    if style_id in [s.id for ch in _VOICEVOX_SYNTH.metas() for s in ch.styles]:
        return _VOICEVOX_SYNTH

    # Find which .vvm contains this style and load it
    for vvm in sorted((VOICEVOX_CORE_DIR / "models" / "vvms").glob("*.vvm")):
        if vvm.name in _VOICEVOX_LOADED_VVMS:
            continue
        with VoiceModelFile.open(str(vvm)) as m:
            for ch in m.metas:
                for st in ch.styles:
                    if st.id == style_id:
                        _VOICEVOX_SYNTH.load_voice_model(m)
                        _VOICEVOX_LOADED_VVMS.add(vvm.name)
                        logger.info(
                            "Loaded voicevox model %s (%s style %d %s)",
                            vvm.name, ch.name, st.id, st.name,
                        )
                        return _VOICEVOX_SYNTH
    raise ValueError(f"VOICEVOX style_id {style_id} not found in any .vvm")


def _synthesize_voicevox_core(
    text: str,
    audio_path: Path,
    *,
    style_id: int = VOICEVOX_CORE_STYLE_ID,
    speed: float = VOICEVOX_CORE_SPEED,
) -> SynthesisResult:
    """Synthesize via voicevox_core Python lib. Returns audio + accurate
    mora-level cues from the AudioQuery."""
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    synth = _get_voicevox_core_synth(style_id)

    query = synth.create_audio_query(text, style_id)
    query.speed_scale = speed
    # NOTE: do not change output_sampling_rate — voicevox_core rejects non-default values

    wav_bytes = synth.synthesis(query, style_id)

    if audio_path.suffix.lower() == ".wav":
        audio_path.write_bytes(wav_bytes)
        out_path = audio_path
    else:
        tmp = audio_path.with_suffix(".wav.tmp")
        tmp.write_bytes(wav_bytes)
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y", "-loglevel", "error",
                    "-i", str(tmp),
                    "-c:a", "libmp3lame", "-b:a", "192k",
                    str(audio_path),
                ],
                check=True, timeout=120,
            )
        finally:
            tmp.unlink(missing_ok=True)
        out_path = audio_path

    cues = _voicevox_cues_from_audio_query(query)
    duration = measure_duration(out_path)
    logger.info(
        "voicevox_core synth %d chars → %.2fs, %d cues (style %d, %s)",
        len(text), duration, len(cues), style_id, out_path.name,
    )
    return SynthesisResult(
        audio_path=out_path,
        duration_sec=duration,
        word_cues=cues,
    )


def _voicevox_cues_from_audio_query(query) -> list[WordCue]:
    """Convert voicevox_core AudioQuery (dataclass-like) to mora-level WordCues
    with vowel info — same shape as the HTTP path's _voicevox_cues_from_query."""
    speed = float(getattr(query, "speed_scale", 1.0)) or 1.0
    cursor = float(getattr(query, "pre_phoneme_length", 0.1)) / speed
    cues: list[WordCue] = []
    for ap in getattr(query, "accent_phrases", []) or []:
        for mora in getattr(ap, "moras", []) or []:
            cl = float(getattr(mora, "consonant_length", None) or 0.0)
            vl = float(getattr(mora, "vowel_length", None) or 0.0)
            start = cursor
            cursor += (cl + vl) / speed
            text = getattr(mora, "text", "") or ""
            vowel = (getattr(mora, "vowel", "") or "").lower() or None
            # Normalize: VOICEVOX uses 'A','I','U','E','O','N','cl','pau' etc.
            if vowel in {"a", "i", "u", "e", "o"}:
                norm_vowel = vowel
            elif vowel in {"n"}:
                norm_vowel = "N"
            else:
                norm_vowel = "sil"
            if text and cursor > start:
                cues.append(WordCue(
                    text=text, start_sec=start, end_sec=cursor, vowel=norm_vowel,
                ))
        pm = getattr(ap, "pause_mora", None)
        if pm is not None:
            pcl = float(getattr(pm, "consonant_length", None) or 0.0)
            pvl = float(getattr(pm, "vowel_length", None) or 0.0)
            cursor += (pcl + pvl) / speed
    return cues


def _voicevox_cues_from_query(query: dict) -> list[WordCue]:
    """Calculate per-accent-phrase cues from a VOICEVOX AudioQuery."""
    speed = float(query.get("speedScale") or 1.0) or 1.0
    cursor = float(query.get("prePhonemeLength") or 0.1) / speed
    cues: list[WordCue] = []
    for ap in query.get("accent_phrases", []) or []:
        ap_start = cursor
        chars: list[str] = []
        for mora in ap.get("moras", []) or []:
            cl = float(mora.get("consonant_length") or 0.0)
            vl = float(mora.get("vowel_length") or 0.0)
            cursor += (cl + vl) / speed
            t = mora.get("text") or ""
            if t:
                chars.append(t)
        ap_end = cursor
        pm = ap.get("pause_mora")
        if pm:
            pcl = float(pm.get("consonant_length") or 0.0)
            pvl = float(pm.get("vowel_length") or 0.0)
            cursor += (pcl + pvl) / speed
        if chars and ap_end > ap_start:
            cues.append(WordCue(
                text="".join(chars),
                start_sec=ap_start,
                end_sec=ap_end,
            ))
    return cues


# ---------------------------------------------------------------------------
# Backend: Edge TTS
# ---------------------------------------------------------------------------

async def _synthesize_edge_async(
    text: str,
    audio_path: Path,
    voice: str,
    rate: str,
) -> list[WordCue]:
    """Run Edge TTS and capture timing metadata.

    Edge TTS emits one of two boundary types depending on language:
      - WordBoundary  (English / European voices): per-word timing
      - SentenceBoundary (Japanese voices): per-sentence timing only

    For SentenceBoundary fallback we synthesize pseudo-word cues by splitting
    each sentence proportionally by character count — gives karaoke effect
    a natural-feeling cadence even though it isn't acoustically perfect.
    """
    communicate = edge_tts.Communicate(text, voice, rate=rate)
    word_cues: list[WordCue] = []
    sent_cues: list[WordCue] = []

    with open(audio_path, "wb") as audio_file:
        async for chunk in communicate.stream():
            chunk_type = chunk["type"]
            if chunk_type == "audio":
                audio_file.write(chunk["data"])
                continue
            if chunk_type not in ("WordBoundary", "SentenceBoundary"):
                continue
            # offset/duration are in 100-nanosecond units → seconds
            try:
                start_sec = chunk["offset"] / 10_000_000
                end_sec = (chunk["offset"] + chunk["duration"]) / 10_000_000
                text_chunk = chunk.get("text", "")
            except (KeyError, TypeError):
                continue
            cue = WordCue(text=text_chunk, start_sec=start_sec, end_sec=end_sec)
            if chunk_type == "WordBoundary":
                word_cues.append(cue)
            else:
                sent_cues.append(cue)

    if word_cues:
        return word_cues
    return _split_sentences_to_pseudo_words(sent_cues)


def _split_sentences_to_pseudo_words(
    sent_cues: list[WordCue], chunk_size: int = 4,
) -> list[WordCue]:
    """Split each SentenceBoundary cue into mora-level cues with vowel info
    via pyopenjtalk. Falls back to char-chunk distribution if pyopenjtalk
    is unavailable or fails on the input.

    Mora-level cues power both karaoke wipes (one wipe per mora) and avatar
    lip sync (vowel selects mouth shape).
    """
    try:
        import pyopenjtalk  # noqa: F401
        _PYOPENJTALK_OK = True
    except Exception as e:  # pragma: no cover
        logger.warning("pyopenjtalk unavailable (%s); using char-chunk fallback", e)
        _PYOPENJTALK_OK = False

    out: list[WordCue] = []
    for sc in sent_cues:
        if not sc.text or sc.end_sec <= sc.start_sec:
            continue

        if _PYOPENJTALK_OK:
            try:
                mora_pieces = _text_to_morae(sc.text)
                if mora_pieces:
                    out.extend(_distribute_mora_timing(mora_pieces, sc.start_sec, sc.end_sec))
                    continue
            except Exception as e:
                logger.debug("pyopenjtalk segment failed for %r: %s", sc.text[:30], e)

        # Fallback: char-chunk
        pieces = _split_by_punctuation(sc.text, chunk_size=chunk_size)
        if not pieces:
            continue
        total_chars = sum(max(1, len(p)) for p in pieces)
        sentence_dur = sc.end_sec - sc.start_sec
        cursor = sc.start_sec
        for piece in pieces:
            piece_dur = sentence_dur * (max(1, len(piece)) / total_chars)
            out.append(WordCue(
                text=piece,
                start_sec=cursor,
                end_sec=cursor + piece_dur,
            ))
            cursor += piece_dur
    return out


def _text_to_morae(text: str) -> list[dict]:
    """Convert Japanese text to a sequence of mora descriptors via pyopenjtalk.

    Each entry: {'text': '...', 'vowel': 'a'/'i'/'u'/'e'/'o'/'N'/'sil',
                 'weight': float duration weight}.
    The phoneme stream from pyopenjtalk's g2p is collapsed mora-by-mora:
      - leading consonant(s) are absorbed into the following vowel
      - 'N' (syllabic nasal /ん/) is its own mora
      - 'cl' (geminate /っ/) is a short silence-mora
      - 'pau' is a long pause-mora
      - uppercase vowels (devoiced) get a shorter weight
    """
    import pyopenjtalk

    phoneme_str = pyopenjtalk.g2p(text, kana=False) or ""
    tokens = phoneme_str.split()

    base_vowels = {"a", "i", "u", "e", "o"}
    morae: list[dict] = []
    consonant_buf = ""

    for tok in tokens:
        lower = tok.lower()
        is_devoiced = tok != lower and lower in base_vowels  # uppercase vowel
        if tok == "pau":
            consonant_buf = ""
            morae.append({"text": "、", "vowel": "sil", "weight": 2.5})
        elif tok == "sil":
            consonant_buf = ""
            # leading/trailing silences ignored — they're handled by Edge TTS timing
        elif lower in base_vowels:
            mora_text = consonant_buf + lower
            morae.append({
                "text": mora_text,
                "vowel": lower,
                "weight": 0.65 if is_devoiced else 1.0,
            })
            consonant_buf = ""
        elif tok == "N":
            consonant_buf = ""
            morae.append({"text": "ん", "vowel": "N", "weight": 0.8})
        elif tok == "cl":
            consonant_buf = ""
            morae.append({"text": "っ", "vowel": "sil", "weight": 0.5})
        else:
            # consonant — accumulate, will emit with next vowel
            consonant_buf = tok

    return morae


def _distribute_mora_timing(
    morae: list[dict], start_sec: float, end_sec: float,
) -> list[WordCue]:
    """Allocate per-mora WordCues across [start_sec, end_sec] by weight."""
    total_w = sum(m["weight"] for m in morae)
    if total_w <= 0:
        return []
    duration = end_sec - start_sec
    if duration <= 0:
        return []
    cursor = start_sec
    out: list[WordCue] = []
    for m in morae:
        slice_dur = duration * (m["weight"] / total_w)
        out.append(WordCue(
            text=m["text"],
            start_sec=cursor,
            end_sec=cursor + slice_dur,
            vowel=m["vowel"],
        ))
        cursor += slice_dur
    return out


def _split_by_punctuation(text: str, *, chunk_size: int) -> list[str]:
    """Split Japanese text at punctuation, then cap each segment by chunk_size."""
    pieces: list[str] = []
    current = ""
    breakers = "、。!?！？・"
    for ch in text:
        current += ch
        if ch in breakers and current.strip():
            pieces.append(current)
            current = ""
    if current.strip():
        pieces.append(current)

    # Further split long segments into chunk_size groups
    out: list[str] = []
    for seg in pieces:
        if len(seg) <= chunk_size + 1:
            out.append(seg)
            continue
        for i in range(0, len(seg), chunk_size):
            out.append(seg[i : i + chunk_size])
    return [p for p in out if p]


def _synthesize_edge(
    text: str,
    audio_path: Path,
    voice: str,
    rate: str,
) -> SynthesisResult:
    """Synthesize via Edge TTS with retries."""
    audio_path.parent.mkdir(parents=True, exist_ok=True)

    last_error: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            cues = asyncio.run(_synthesize_edge_async(text, audio_path, voice, rate))
            duration = measure_duration(audio_path)
            logger.info(
                "Edge TTS synth %d chars → %.2fs, %d cues (%s)",
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_whisper_model():
    """Lazy-load the Whisper model. Cached at module level."""
    global _WHISPER_MODEL
    if _WHISPER_MODEL is not None:
        return _WHISPER_MODEL
    from faster_whisper import WhisperModel  # raises ImportError if missing
    logger.info("Loading Whisper model (size=%s, this may take ~10s)...", WHISPER_MODEL_SIZE)
    _WHISPER_MODEL = WhisperModel(
        WHISPER_MODEL_SIZE, device="cpu", compute_type="int8",
    )
    return _WHISPER_MODEL


def _whisper_align(audio_path: Path) -> list[WordCue]:
    """Transcribe audio with Whisper, return mora-level WordCues.

    For each Whisper word:
      - Use its (start, end) timestamps as the truth (anchored to real audio)
      - Run pyopenjtalk on the recognized text to get vowel sequence
      - Distribute morae across the word's duration

    The combined output is mora-level cues with both accurate timing AND
    vowel info — best of both worlds for subtitles + avatar lip sync.
    """
    model = _get_whisper_model()
    segments, _info = model.transcribe(
        str(audio_path),
        language="ja",
        word_timestamps=True,
        beam_size=1,
        vad_filter=False,
    )

    cues: list[WordCue] = []
    for seg in segments:
        for w in (seg.words or []):
            text = (w.word or "").strip()
            if not text:
                continue
            try:
                morae = _text_to_morae(text)
            except Exception:
                morae = []
            if morae:
                cues.extend(_distribute_mora_timing(morae, float(w.start), float(w.end)))
            else:
                cues.append(WordCue(text=text, start_sec=float(w.start), end_sec=float(w.end)))
    return cues


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
