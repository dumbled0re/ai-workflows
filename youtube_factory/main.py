"""youtube_factory CLI: video generation pipeline (audio-first).

Pipeline (per Codex review):
  1. Synthesize all narration with WordBoundary metadata
  2. Build master.wav (concat → loudnorm)
  3. Generate ASS subtitles from WordBoundary cues
  4. Generate per-scene images
  5. Render silent video (ken-burns + xfade)
  6. Burn in subtitles
  7. Mux master audio
  8. Validate

Phases:
  render   end-to-end from script.json
  demo     local end-to-end with hand-written sample script
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _data_dir() -> Path:
    return Path(__file__).parent / "data"


def _assets_dir() -> Path:
    return Path(__file__).parent / "assets"


def phase_render(script_path: Path | None = None) -> None:
    """Audio-first end-to-end render."""
    from youtube_factory.audio.master import build_master_audio, mix_bgm
    from youtube_factory.audio.voice import synthesize
    from youtube_factory.script import VideoScript
    from youtube_factory.video.compose import (
        SilentScene, burn_subtitles, crossfade_scenes,
        mux_audio, render_silent_scene, validate_output,
    )
    from youtube_factory.visual.images import generate_image, render_thumbnail
    from youtube_factory.visual.subtitles import build_subtitles

    data = _data_dir()
    work = data / "work"
    work.mkdir(parents=True, exist_ok=True)

    script_path = script_path or (data / "script.json")
    if not script_path.exists():
        raise SystemExit(f"Script not found: {script_path}")

    with open(script_path, encoding="utf-8") as f:
        raw = json.load(f)
    script = VideoScript.model_validate(raw)
    logger.info(
        "Script: %d stories, ~%.0fs estimated",
        len(script.stories), script.estimated_duration_sec(),
    )

    # ============ Phase 1: Synthesize all narration ============
    logger.info("=" * 60)
    logger.info("Phase 1: Synthesize narration")
    logger.info("=" * 60)

    scenes_meta: list[dict] = []  # for image generation
    audio_paths: list[Path] = []
    word_cues_per_scene = []

    # Intro — cinematic news-show opening
    intro_result = synthesize(script.intro_narration, work / "audio_intro.mp3")
    audio_paths.append(intro_result.audio_path)
    word_cues_per_scene.append(intro_result.word_cues)
    scenes_meta.append({
        "type": "intro",
        "duration_sec": intro_result.duration_sec,
        "text_overlay": script.thumbnail_text,
        "image_query": (
            "futuristic AI news broadcast studio, holographic displays, "
            "neon blue glow, anchor desk silhouette, dramatic cinematic lighting"
        ),
        "image_source": "ai",
        "is_chapter_card": False,
        "story_color_index": 0,
    })

    # Stories with chapter cards
    for s_idx, story in enumerate(script.stories):
        # Chapter card
        chapter_text = f"続いて{s_idx + 1}つ目のニュースです。"
        chapter_result = synthesize(chapter_text, work / f"audio_chapter_{s_idx:02d}.mp3")
        audio_paths.append(chapter_result.audio_path)
        word_cues_per_scene.append(chapter_result.word_cues)
        scenes_meta.append({
            "type": "chapter",
            "duration_sec": chapter_result.duration_sec,
            "text_overlay": story.title,
            "image_query": "chapter",
            "image_source": "chapter",
            "chapter_number": s_idx + 1,
            "is_chapter_card": True,
            "story_color_index": s_idx + 1,
        })

        # Shots
        for sh_idx, shot in enumerate(story.shots):
            r = synthesize(
                shot.narration,
                work / f"audio_s{s_idx:02d}_{sh_idx:02d}.mp3",
            )
            audio_paths.append(r.audio_path)
            word_cues_per_scene.append(r.word_cues)
            scenes_meta.append({
                "type": "shot",
                "duration_sec": r.duration_sec,
                "text_overlay": shot.text_overlay,
                "image_query": shot.image_query,
                "image_source": shot.image_source,
                "source_url": story.source_url,
                "is_chapter_card": False,
                "story_color_index": s_idx + 1,
            })

    # Outro — cinematic closing
    outro_result = synthesize(script.outro_narration, work / "audio_outro.mp3")
    audio_paths.append(outro_result.audio_path)
    word_cues_per_scene.append(outro_result.word_cues)
    scenes_meta.append({
        "type": "outro",
        "duration_sec": outro_result.duration_sec,
        "text_overlay": "ご視聴\nありがとうございました",
        "image_query": (
            "cinematic closing scene, golden particles drifting, "
            "abstract glowing AI energy field, warm dusk lighting, peaceful atmosphere"
        ),
        "image_source": "ai",
        "is_chapter_card": False,
        "story_color_index": 0,
    })

    logger.info("Synthesized %d audio segments", len(audio_paths))

    # ============ Phase 2: Build master.wav ============
    logger.info("=" * 60)
    logger.info("Phase 2: Build master audio")
    logger.info("=" * 60)

    master_voice = work / "master_voice.wav"
    total_audio_dur, scene_timings = build_master_audio(
        audio_paths, master_voice, gap_sec=0.05,
    )
    logger.info("Master audio: %.2fs", total_audio_dur)

    # ============ Phase 3: Mix BGM ============
    # Priority: asset file > Pixabay (key) > procedural drone > skip
    bgm_dir = _assets_dir() / "bgm"
    bgm_files = (
        list(bgm_dir.glob("*.mp3")) + list(bgm_dir.glob("*.wav"))
        if bgm_dir.exists() else []
    )

    bgm_disabled = os.environ.get("YOUTUBE_FACTORY_NO_BGM", "0") in ("1", "true", "yes")
    bgm_query = os.environ.get(
        "YOUTUBE_FACTORY_BGM_QUERY", "ambient cinematic news background instrumental",
    )

    if bgm_disabled:
        logger.info("BGM disabled via YOUTUBE_FACTORY_NO_BGM")
        master_audio = master_voice
    elif bgm_files:
        logger.info("=" * 60)
        logger.info("Phase 3: Mix BGM from asset (%s)", bgm_files[0].name)
        logger.info("=" * 60)
        master_audio = work / "master_with_bgm.wav"
        mix_bgm(master_voice, bgm_files[0], master_audio)
    else:
        from youtube_factory.audio import pixabay as pixabay_music
        bgm_track: Path | None = None

        if pixabay_music.is_available():
            logger.info("=" * 60)
            logger.info("Phase 3: Fetch BGM from Pixabay (%r)", bgm_query)
            logger.info("=" * 60)
            bgm_track = pixabay_music.fetch_music(
                bgm_query,
                cache_dir=data / "cache" / "pixabay_music",
                min_duration_sec=max(60.0, total_audio_dur),
            )

        if bgm_track is None:
            logger.info("=" * 60)
            logger.info("Phase 3: Mix procedural BGM (drone fallback)")
            logger.info("=" * 60)
            from youtube_factory.audio.master import generate_procedural_bgm
            bgm_track = work / "bgm_drone.wav"
            try:
                generate_procedural_bgm(bgm_track, total_audio_dur + 1.0)
            except Exception as e:
                logger.warning("Procedural BGM failed (%s); skipping BGM", e)
                bgm_track = None

        if bgm_track is not None:
            try:
                master_audio = work / "master_with_bgm.wav"
                mix_bgm(master_voice, bgm_track, master_audio)
            except Exception as e:
                logger.warning("BGM mix failed (%s); using voice-only", e)
                master_audio = master_voice
        else:
            master_audio = master_voice
            master_audio = master_voice

    # ============ Phase 4: Generate subtitles ============
    logger.info("=" * 60)
    logger.info("Phase 4: Generate subtitles (ASS)")
    logger.info("=" * 60)
    subtitle_path = work / "subtitles.ass"
    build_subtitles(word_cues_per_scene, scene_timings, subtitle_path)

    # ============ Phase 5: Generate images (parallel) ============
    logger.info("=" * 60)
    logger.info("Phase 5: Generate images (parallel, AI-first)")
    logger.info("=" * 60)
    from concurrent.futures import ThreadPoolExecutor, as_completed

    cache_dir = data / "cache" / "ai_images"
    image_paths: list[Path] = [work / f"image_{i:03d}.jpg" for i in range(len(scenes_meta))]

    def _gen_one(i: int, meta: dict) -> tuple[int, str]:
        generate_image(
            image_paths[i],
            text_overlay=meta.get("text_overlay", ""),
            image_query=meta.get("image_query", ""),
            image_source=meta.get("image_source", "ai"),
            source_url=meta.get("source_url", ""),
            assets_dir=_assets_dir(),
            chapter_number=meta.get("chapter_number"),
            is_chapter_card=meta.get("is_chapter_card", False),
            story_color_index=meta.get("story_color_index", 0),
            cache_dir=cache_dir,
        )
        return i, image_paths[i].name

    # Pollinations.ai rate-limits aggressive parallelism; 2 workers is the
    # sweet spot (verified empirically — 4 workers triggered 429 on most calls).
    with ThreadPoolExecutor(max_workers=2) as ex:
        futures = [ex.submit(_gen_one, i, meta) for i, meta in enumerate(scenes_meta)]
        for fut in as_completed(futures):
            idx, name = fut.result()
            logger.info("  [%d/%d] %s", idx + 1, len(scenes_meta), name)

    # ============ Phase 6: Render silent scenes ============
    logger.info("=" * 60)
    logger.info("Phase 6: Render silent scenes")
    logger.info("=" * 60)
    scene_video_paths = []
    scene_durations_for_xfade = []
    for i, (img_path, meta, timing) in enumerate(
        zip(image_paths, scenes_meta, scene_timings, strict=True)
    ):
        # Use scene timing's duration (which equals audio duration)
        # But pad slightly for crossfade headroom
        # The xfade overlap means scene N's last 0.4s overlap with scene N+1's first 0.4s
        # So each scene's video should be its audio duration (no extra padding)
        seg_path = work / f"scene_{i:03d}.mp4"
        scene_dur = timing.duration_sec + 0.2  # tiny pad to avoid xfade undershoot
        render_silent_scene(
            SilentScene(
                image_path=img_path,
                duration_sec=scene_dur,
                ken_burns=not meta.get("is_chapter_card", False),
            ),
            seg_path,
            scene_index=i,
        )
        scene_video_paths.append(seg_path)
        scene_durations_for_xfade.append(scene_dur)

    # ============ Phase 7: Cross-fade scenes ============
    logger.info("=" * 60)
    logger.info("Phase 7: Cross-fade scenes")
    logger.info("=" * 60)
    silent_master = work / "silent_master.mp4"
    visual_dur = crossfade_scenes(
        scene_video_paths, scene_durations_for_xfade, silent_master,
    )
    logger.info("Silent master: %.2fs", visual_dur)

    # ============ Phase 8: Burn subtitles ============
    logger.info("=" * 60)
    logger.info("Phase 8: Burn subtitles")
    logger.info("=" * 60)
    subtitled = work / "with_subs.mp4"
    burn_subtitles(silent_master, subtitle_path, subtitled)

    # ============ Phase 8.5: Render avatar PNG sequence (optional) ============
    avatar_pngs_dir = None
    avatar_dir = _assets_dir() / "avatar"
    avatar_disabled = os.environ.get("YOUTUBE_FACTORY_NO_AVATAR", "0") in ("1", "true", "yes")
    avatar_faces_present = (
        avatar_dir.exists()
        and (avatar_dir / "face_closed.png").exists()
        and (avatar_dir / "face_half_open.png").exists()
        and (avatar_dir / "face_wide_open.png").exists()
    )
    if not avatar_disabled and avatar_faces_present:
        logger.info("=" * 60)
        logger.info("Phase 8.5: Render avatar lip-flap PNGs")
        logger.info("=" * 60)
        from youtube_factory.visual.avatar import render_avatar_pngs
        from youtube_factory.audio.voice import WordCue as _WC

        # Flatten per-scene cues to master-timeline absolute coords
        all_cues_master: list[_WC] = []
        for scene_cues, timing in zip(word_cues_per_scene, scene_timings, strict=True):
            for cue in scene_cues:
                all_cues_master.append(_WC(
                    text=cue.text,
                    start_sec=cue.start_sec + timing.start_sec,
                    end_sec=cue.end_sec + timing.start_sec,
                    vowel=cue.vowel,
                ))

        avatar_pngs_dir = work / "avatar_pngs"
        try:
            render_avatar_pngs(
                voice_path=master_voice,
                faces_dir=avatar_dir,
                out_dir=avatar_pngs_dir,
                duration_sec=visual_dur,
                fps=30,
                avatar_size=300,
                word_cues=all_cues_master,
            )
        except Exception as e:
            logger.warning("Avatar render failed (%s); proceeding without avatar", e)
            avatar_pngs_dir = None
    elif avatar_disabled:
        logger.info("Avatar disabled via YOUTUBE_FACTORY_NO_AVATAR")
    else:
        logger.info("Avatar faces not found in assets/avatar/; skipping")

    # ============ Phase 9: Mux audio (+ avatar overlay) ============
    # showwaves is opt-in via YOUTUBE_FACTORY_SHOWWAVES=1
    showwaves_enabled = os.environ.get("YOUTUBE_FACTORY_SHOWWAVES", "0") in ("1", "true", "yes")
    logger.info("=" * 60)
    parts = []
    if avatar_pngs_dir:
        parts.append("avatar")
    if showwaves_enabled:
        parts.append("showwaves")
    extras = (" + " + " + ".join(parts)) if parts else ""
    logger.info("Phase 9: Mux audio%s", extras)
    logger.info("=" * 60)
    output = data / "output.mp4"
    mux_audio(
        subtitled, master_audio, output,
        avatar_pngs_dir=avatar_pngs_dir,
        avatar_position="bottom-right",
        avatar_margin=40,
        showwaves=showwaves_enabled,
    )

    # ============ Phase 10: Thumbnail ============
    logger.info("=" * 60)
    logger.info("Phase 10: Thumbnail")
    logger.info("=" * 60)
    render_thumbnail(data / "thumbnail.jpg", script.thumbnail_text, "AI NEWS DAILY")

    # ============ Phase 11: Validate ============
    logger.info("=" * 60)
    logger.info("Phase 11: Validate")
    logger.info("=" * 60)
    metadata = validate_output(output, min_duration=30.0, max_duration=1200.0)

    final_meta = {
        "title": script.title,
        "description": script.description,
        "tags": script.tags,
        "thumbnail_text": script.thumbnail_text,
        "video": metadata,
    }
    with open(data / "metadata.json", "w", encoding="utf-8") as f:
        json.dump(final_meta, f, ensure_ascii=False, indent=2)

    print()
    print("=" * 60)
    print(f"✅ Video: {output}")
    print(f"   Duration: {metadata['duration_sec']}s")
    print(f"   Size: {metadata['size_mb']}MB")
    print(f"   Title: {script.title}")
    print(f"   Thumbnail: {data / 'thumbnail.jpg'}")
    print("=" * 60)


def phase_demo() -> None:
    """End-to-end demo with built-in sample script."""
    sample = {
        "title": "【今週のAI】OpenAIがAmazonへ・チャットGPTに広告・新ボイスAI登場 / 2026-04-29",
        "description": "毎週のAIニュースまとめ。今週は世界のAI業界で大きな動きがありました。OpenAIのアマゾン進出、ChatGPTの広告表示、Microsoftの音声AI公開など、注目のニュースを3つ厳選してお届けします。",
        "thumbnail_text": "今週のAI",
        "tags": ["AI", "AIニュース", "OpenAI", "ChatGPT", "Microsoft"],
        "intro_narration": "こんにちは、AIニュースデイリーです。今週、AI業界で起きた重要なニュースを3つお届けします。最後までお見逃しなく。",
        "outro_narration": "今週のニュースは以上です。役に立ったらチャンネル登録と高評価をお願いします。それでは、また来週お会いしましょう。",
        "stories": [
            {
                "title": "OpenAIがアマゾンクラウドに登場",
                "source_url": "https://aws.amazon.com/blogs/aws/",
                "importance": "HIGH",
                "shots": [
                    {
                        "narration": "1つ目のニュース。これまでマイクロソフトが独占していたOpenAIのAIモデルが、今月からアマゾンのクラウドでも使えるようになります。",
                        "image_query": "OpenAI logo merging with Amazon Web Services cloud, glowing blue partnership concept, server room",
                        "image_source": "ai",
                        "text_overlay": "OpenAI×Amazon"
                    },
                    {
                        "narration": "理由はシンプル。先月、両社の独占契約が解消されたからです。これでアマゾンを使う多くの企業が、簡単にAIを導入できるようになります。",
                        "image_query": "broken chain breaking apart, end of exclusive contract between two tech giants, dramatic shattering",
                        "image_source": "ai",
                        "text_overlay": "独占契約解消"
                    },
                    {
                        "narration": "私たちユーザーにとっても朗報です。価格競争が激しくなり、AISサービスがもっと安く、もっと使いやすくなる可能性が高まります。",
                        "image_query": "price war between tech companies, downward red arrows, competing AI cloud services, dramatic stock chart",
                        "image_source": "ai",
                        "text_overlay": "価格競争激化"
                    }
                ]
            },
            {
                "title": "ChatGPTに広告が表示される時代へ",
                "source_url": "https://openai.com/blog",
                "importance": "HIGH",
                "shots": [
                    {
                        "narration": "2つ目。ChatGPTが、ついに広告を表示する仕組みをスタートさせます。回答の中に、関連する商品やサービスの広告が出るようになります。",
                        "image_query": "ChatGPT chat interface with sponsored advertisement banner, smartphone screen, modern UI",
                        "image_source": "ai",
                        "text_overlay": "ChatGPTに広告"
                    },
                    {
                        "narration": "OpenAIにとっては大きな収益源になります。一方で、私たち利用者は、回答の中身が広告に影響されていないか、注意して見る必要が出てきました。",
                        "image_query": "balance scale weighing trust against money, AI ethics neutrality, dramatic chiaroscuro lighting",
                        "image_source": "ai",
                        "text_overlay": "中立性に注意"
                    }
                ]
            },
            {
                "title": "マイクロソフト無料音声AI公開",
                "source_url": "https://github.com/microsoft/VibeVoice",
                "importance": "MEDIUM",
                "shots": [
                    {
                        "narration": "3つ目。マイクロソフトが、自然な音声を生成できる新しいAIを完全無料で公開しました。名前はバイブボイス。誰でも自分のパソコンで使えます。",
                        "image_query": "Microsoft VibeVoice AI voice synthesis, glowing blue audio waveform, futuristic microphone, premium tech",
                        "image_source": "ai",
                        "text_overlay": "VibeVoice 登場"
                    },
                    {
                        "narration": "これまで月数千円かかっていた音声AIサービスが、自前のパソコンで無料で動くようになります。ナレーションや読み上げに使う個人や中小企業にとって、大きな助けになりそうです。",
                        "image_query": "free open source AI voice on laptop, soundwave visualization, home studio setup, warm lighting",
                        "image_source": "ai",
                        "text_overlay": "音声AIが無料化"
                    }
                ]
            }
        ]
    }

    data = _data_dir()
    data.mkdir(exist_ok=True)
    sample_path = data / "script.json"
    with open(sample_path, "w", encoding="utf-8") as f:
        json.dump(sample, f, ensure_ascii=False, indent=2)
    logger.info("Sample script written to %s", sample_path)
    phase_render(sample_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="YouTube Factory")
    parser.add_argument("phase", choices=["render", "demo"])
    args = parser.parse_args()

    if args.phase == "render":
        phase_render()
    elif args.phase == "demo":
        phase_demo()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.exception("Fatal: %s", e)
        sys.exit(1)
