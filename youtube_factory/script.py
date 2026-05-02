"""Pydantic models for video script JSON.

Used to validate Claude's output before rendering. If validation fails,
the script is rejected with a detailed error so Claude can be retried.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class Shot(BaseModel):
    """One visual unit within a story (~5-15 seconds)."""

    narration: str = Field(..., min_length=1, max_length=200)
    image_query: str = Field(..., description="English vivid keywords for AI image gen / search")
    image_source: str = Field(default="ai", description="ai/og/card/auto")
    text_overlay: str = Field(default="", max_length=18)

    @field_validator("image_source")
    @classmethod
    def _valid_image_source(cls, v: str) -> str:
        v = (v or "ai").lower()
        if v not in {"ai", "og", "card", "auto"}:
            raise ValueError(f"image_source must be one of ai/og/card/auto, got {v}")
        return v

    @field_validator("narration")
    @classmethod
    def narration_no_url(cls, v: str) -> str:
        if "http://" in v or "https://" in v:
            raise ValueError("Narration must not contain URLs (TTS will read them)")
        return v


class Story(BaseModel):
    """One news story consisting of multiple shots."""

    title: str = Field(..., min_length=1, max_length=80)
    source_url: str = Field(default="")
    importance: str = Field(default="MEDIUM")
    shots: list[Shot] = Field(..., min_length=2, max_length=8)


class VideoScript(BaseModel):
    """Top-level script for one video."""

    title: str = Field(..., min_length=10, max_length=100)
    description: str = Field(..., min_length=50, max_length=4000)
    thumbnail_text: str = Field(..., min_length=1, max_length=20)
    tags: list[str] = Field(..., min_length=3, max_length=15)
    intro_narration: str = Field(..., min_length=20, max_length=300)
    outro_narration: str = Field(..., min_length=20, max_length=300)
    stories: list[Story] = Field(..., min_length=3, max_length=7)

    @field_validator("stories")
    @classmethod
    def total_narration_length(cls, v: list[Story]) -> list[Story]:
        """Enforce total narration character count (1800-2500 = ~5-7 min at 6char/sec).

        For demo/short videos, set MIN to 300 (~50 sec).
        For production weekly videos, the script_generator prompt will request 1800-2500.
        """
        total = sum(len(shot.narration) for story in v for shot in story.shots)
        if total < 300:
            raise ValueError(f"Total narration too short: {total} chars (need ≥300)")
        if total > 3500:
            raise ValueError(f"Total narration too long: {total} chars (need ≤3500)")
        return v

    def estimated_duration_sec(self) -> float:
        """Rough estimate at 6 chars/second JST narration speed."""
        intro = len(self.intro_narration)
        outro = len(self.outro_narration)
        body = sum(len(shot.narration) for s in self.stories for shot in s.shots)
        return (intro + outro + body) / 6.0
