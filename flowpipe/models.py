"""Manifest schema — the source of truth for every stage after prompt generation."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(value: str) -> str:
    out = SLUG_RE.sub("-", value.strip().lower()).strip("-")
    return out or "item"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Character(BaseModel):
    id: str
    name: str
    description: str = ""
    face_prompt: str
    body_prompt: str

    @model_validator(mode="before")
    @classmethod
    def _fill(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data = dict(data)
            name = data.get("name") or data.get("id") or "character"
            data.setdefault("id", slugify(str(name)))
            # a single `prompt` field is acceptable; split it into the two Flow slots
            if "face_prompt" not in data:
                base = data.get("prompt") or data.get("description") or name
                data["face_prompt"] = base
            if "body_prompt" not in data:
                data["body_prompt"] = data.get("description") or data["face_prompt"]
        return data

    @field_validator("id")
    @classmethod
    def _slug(cls, value: str) -> str:
        return slugify(value)


class Scene(BaseModel):
    id: str
    order: int = 0
    title: str = ""
    character_ids: list[str] = Field(default_factory=list)
    image_prompt: str
    animation_prompt: str = ""
    clip_duration: int | None = None
    last_frame_note: str = ""
    # What the narrator says over this shot. Stage A writes it from the master
    # prompt; the voice stage turns it into audio plus word timestamps.
    narration: str = ""

    @field_validator("narration", mode="before")
    @classmethod
    def _one_line(cls, value: object) -> str:
        return " ".join(str(value or "").split())

    @model_validator(mode="before")
    @classmethod
    def _fill(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data = dict(data)
            data.setdefault("id", slugify(str(data.get("title") or f"scene-{data.get('order', 0)}")))
            if not data.get("animation_prompt"):
                data["animation_prompt"] = data.get("image_prompt", "")
        return data

    @field_validator("id")
    @classmethod
    def _slug(cls, value: str) -> str:
        return slugify(value)


class ProjectMeta(BaseModel):
    topic: str
    style: str = ""
    tone: str = ""
    target_duration: int = 30
    aspect_ratio: str = "16:9"
    logline: str = ""
    music_prompt: str = ""
    voiceover: str = ""
    # The voice to synthesize with (a VoiceStudio profile id/name, or "default").
    voice: str = ""
    voice_speed: float = 1.0


class Manifest(BaseModel):
    version: int = 1
    generator: str = "anthropic"
    generated_at: str = Field(default_factory=now_iso)
    project: ProjectMeta
    characters: list[Character] = Field(default_factory=list)
    scenes: list[Scene] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> Manifest:
        seen: set[str] = set()
        for c in self.characters:
            if c.id in seen:
                raise ValueError(f"duplicate character id: {c.id}")
            seen.add(c.id)
        known = seen
        seen_scenes: set[str] = set()
        for i, scene in enumerate(self.scenes):
            if scene.id in seen_scenes:
                raise ValueError(f"duplicate scene id: {scene.id}")
            seen_scenes.add(scene.id)
            scene.order = scene.order or i + 1
            unknown = [cid for cid in scene.character_ids if cid not in known]
            if unknown:
                raise ValueError(f"scene {scene.id} references unknown characters: {unknown}")
        if not self.scenes:
            raise ValueError("manifest has no scenes")
        self.scenes.sort(key=lambda s: s.order)
        return self

    def character(self, cid: str) -> Character | None:
        return next((c for c in self.characters if c.id == cid), None)

    def ordered_scenes(self) -> list[Scene]:
        return sorted(self.scenes, key=lambda s: s.order)

    def total_duration(self) -> int:
        return sum(s.clip_duration or self.project.target_duration // max(1, len(self.scenes)) for s in self.scenes)


# Status vocabulary shared by jobs and the UI.
JobStatus = Literal[
    "queued",
    "running",
    "done",
    "failed",
    "needs_review",
    "waiting_account",
    "skipped",
]

AccountStatus = Literal["active", "low", "exhausted", "needs_auth", "needs_human", "disabled"]
