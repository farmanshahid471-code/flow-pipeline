"""The narration stage: script -> voice audio -> word timestamps -> subtitles.

The script comes from Stage A (Claude writes it from the master prompt, one
``narration`` line per scene, and the whole thing as ``project.voiceover``). This
module turns it into:

    data/projects/<id>/voiceover/<scene>.wav     one clip of speech per scene
    data/projects/<id>/voiceover/narration.json  per-scene words with timings
    data/projects/<id>/voiceover/narration.srt   a preview subtitle file

Nothing here touches Flow or credits, so it can be re-run as often as you like
while you audition a voice. The final, scene-aligned subtitle file and the mixed
voice track are produced by ``assemble`` once the clips exist (only then do we
know how long each scene really is).
"""
from __future__ import annotations

import json
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import db, projects
from ..config import SETTINGS
from ..models import Manifest, now_iso
from . import align, timeline
from .base import TTSError, VoiceError
from .timeline import Cue, Timeline, chunk_cues

PLAN_NAME = "narration.json"
SRT_NAME = "narration.srt"

Progress = Callable[[str], None]


# ------------------------------------------------------------------ paths ---
def narration_dir(project_id: str) -> Path:
    return projects.subdir(project_id, "voiceover")


def plan_path(project_id: str) -> Path:
    return narration_dir(project_id) / PLAN_NAME


def audio_path(project_id: str, scene_id: str) -> Path:
    return narration_dir(project_id) / f"{scene_id}.wav"


# ------------------------------------------------------------- the script ---
def _sentences(text: str) -> list[str]:
    import re

    chunks = re.split(r"(?<=[.!?…])\s+", (text or "").strip())
    return [chunk.strip() for chunk in chunks if chunk.strip()]


def script_for(manifest: Manifest) -> list[tuple[str, str]]:
    """Per-scene narration lines: ``[(scene_id, text), …]``.

    Prefers the per-scene ``narration`` Claude writes. If the manifest only has
    the single ``project.voiceover`` script, its sentences are spread over the
    scenes in order — good enough to get a first cut, and the UI says so.
    """
    scenes = [scene for scene in manifest.ordered_scenes()]
    lines = [(scene.id, (scene.narration or "").strip()) for scene in scenes]
    if any(text for _, text in lines):
        return [(scene_id, text) for scene_id, text in lines if text]
    whole = (manifest.project.voiceover or "").strip()
    if not whole or not scenes:
        return []
    sentences = _sentences(whole)
    if not sentences:
        return []
    per_scene = max(1, len(sentences) // len(scenes))
    result: list[tuple[str, str]] = []
    cursor = 0
    for index, scene in enumerate(scenes):
        remaining_scenes = len(scenes) - index
        take = per_scene if index < len(scenes) - 1 else max(1, len(sentences) - cursor)
        chunk = sentences[cursor:cursor + take]
        cursor += take
        if not chunk and cursor < len(sentences):
            chunk = sentences[cursor:cursor + 1]
            cursor += 1
        if chunk and remaining_scenes:
            result.append((scene.id, " ".join(chunk)))
    return result


def narration_source(manifest: Manifest) -> str:
    """Where the script came from, for the UI."""
    if any((scene.narration or "").strip() for scene in manifest.scenes):
        return "scenes"
    if (manifest.project.voiceover or "").strip():
        return "project"
    return "none"


# ------------------------------------------------------------- synthesizing --
@dataclass
class SceneNarration:
    scene_id: str
    text: str
    audio: str            # path relative to the project folder
    duration: float
    timeline: Timeline

    def as_dict(self) -> dict:
        return {
            "scene_id": self.scene_id,
            "text": self.text,
            "audio": self.audio,
            "duration": round(self.duration, 3),
            "timeline": self.timeline.as_dict(),
        }

    @classmethod
    def from_dict(cls, payload: dict) -> SceneNarration:
        return cls(
            scene_id=str(payload.get("scene_id", "")),
            text=str(payload.get("text", "")),
            audio=str(payload.get("audio", "")),
            duration=float(payload.get("duration", 0.0)),
            timeline=Timeline.from_dict(payload.get("timeline")),
        )


def _stage_dir(project_id: str, scene_id: str) -> Path:
    """Raw TTS output is downloaded-grade media, so it lands under the download root."""
    path = Path(SETTINGS.flow_downloads_dir) / project_id / "voiceover" / scene_id
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cleanup(path: Path) -> None:
    root = Path(SETTINGS.flow_downloads_dir).resolve()
    current = path
    while current.is_dir() and current.resolve() != root:
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def synthesize(project_id: str, *, backend=None, voice: str = "", speed: float | None = None,
               align_provider: str = "", language: str = "", progress: Progress | None = None,
               only: list[str] | None = None) -> dict[str, Any]:
    """Generate the narration for every scene that has a line.

    Returns the plan (also written to ``voiceover/narration.json``).
    """
    manifest = projects.read_manifest(project_id)
    if manifest is None:
        raise VoiceError("this project has no manifest yet - run Stage A first")
    lines = script_for(manifest)
    if only:
        lines = [(sid, text) for sid, text in lines if sid in set(only)]
    if not lines:
        raise VoiceError(
            "there is no narration script in this manifest. Stage A writes one per scene "
            "(the `narration` field) from the master prompt - regenerate the manifest, or "
            "add the lines in the Manifest tab."
        )

    if backend is None:
        from .voicestudio import backend_from_settings

        backend = backend_from_settings()
    voice = voice or SETTINGS.tts_voice
    speed = SETTINGS.tts_speed if speed is None else speed
    language = language or SETTINGS.narration_language

    def say(message: str) -> None:
        db.log(message, project_id=project_id)
        if progress:
            progress(message)

    entries: list[SceneNarration] = []
    say(f"narration: {len(lines)} scene(s) via {getattr(backend, 'name', 'backend')}")
    for scene_id, text in lines:
        staging = _stage_dir(project_id, scene_id)
        target = staging / f"{scene_id}.wav"
        result = backend.synthesize(text, target, voice=voice, speed=speed)
        produced = Path(result.path)
        if not produced.exists():
            raise TTSError(f"the voice backend reported success but {produced} is missing")

        # word timestamps: whisperX when asked, otherwise spread the script over
        # the audio. Either way the result indexes the script we sent.
        try:
            words = align.align(produced, text, provider=align_provider, duration=result.duration,
                                language=language)
        except align.AlignError as exc:
            say(f"alignment failed for {scene_id} ({exc}); falling back to proportional timing")
            words = align.proportional(text, result.duration or _duration(produced))

        destination = audio_path(project_id, scene_id)
        shutil.move(str(produced), destination)
        _cleanup(staging)
        entries.append(SceneNarration(
            scene_id=scene_id,
            text=text,
            audio=str(destination.relative_to(projects.project_dir(project_id))),
            duration=round(result.duration or words.duration, 3),
            timeline=words,
        ))
        say(f"narration: {scene_id} -> {entries[-1].duration:.1f}s, {len(words)} words")

    plan = {
        "version": 1,
        "generated_at": now_iso(),
        "provider": getattr(backend, "name", "unknown"),
        "voice": voice,
        "model": getattr(backend, "model", ""),
        "aligner": (align_provider or SETTINGS.align_provider),
        "language": language,
        "source": narration_source(manifest),
        "scenes": [entry.as_dict() for entry in entries],
    }
    plan_path(project_id).write_text(json.dumps(plan, indent=2), encoding="utf-8")
    # preview subtitles: scene-local timings laid end to end. assemble() writes
    # the authoritative file once the real clip lengths are known.
    preview: list[Cue] = []
    offset = 0.0
    for entry in entries:
        preview.extend(timeline.shift_cues(chunk_cues(entry.timeline,
                                                      max_chars=SETTINGS.caption_max_chars),
                                           offset))
        offset += entry.duration
    (narration_dir(project_id) / SRT_NAME).write_text(timeline.to_srt(preview), encoding="utf-8")
    db.update_project(project_id, status=db.get_project(project_id)["status"])
    say(f"narration ready: {offset:.1f}s of speech in {len(entries)} scene(s)")
    return plan


def _duration(path: Path) -> float:
    from .voicestudio import probe_duration

    return probe_duration(path)


# ------------------------------------------------------------------ loading --
def load(project_id: str) -> dict[str, Any] | None:
    path = plan_path(project_id)
    if not path.exists():
        return None
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    root = projects.project_dir(project_id)
    scenes = []
    for row in plan.get("scenes", []):
        entry = SceneNarration.from_dict(row)
        audio = (root / entry.audio) if entry.audio else None
        scenes.append({
            "scene_id": entry.scene_id,
            "text": entry.text,
            "audio": str(audio) if audio else "",
            "exists": bool(audio and audio.exists()),
            "duration": entry.duration,
            "timeline": entry.timeline,
        })
    return {
        "provider": plan.get("provider", ""),
        "voice": plan.get("voice", ""),
        "model": plan.get("model", ""),
        "aligner": plan.get("aligner", ""),
        "language": plan.get("language", ""),
        "source": plan.get("source", ""),
        "generated_at": plan.get("generated_at", ""),
        "scenes": scenes,
    }


def delete(project_id: str) -> None:
    shutil.rmtree(narration_dir(project_id), ignore_errors=True)


# ------------------------------------------------------------------ summary --
def describe(project_id: str, clip_durations: dict[str, float] | None = None) -> dict[str, Any]:
    """What the UI shows: per-scene length, whether it fits, and the SRT text."""
    manifest = projects.read_manifest(project_id)
    plan = load(project_id)
    script = script_for(manifest) if manifest else []
    data: dict[str, Any] = {
        "script": [{"scene_id": sid, "text": text} for sid, text in script],
        "source": narration_source(manifest) if manifest else "none",
        "provider": SETTINGS.tts_provider,
        "aligner": SETTINGS.align_provider,
        "voice": SETTINGS.tts_voice,
        "captions": SETTINGS.captions,
        "plan": None,
        "scenes": [],
        "warnings": [],
    }
    if plan is None:
        return data
    data["plan"] = {key: plan[key] for key in
                    ("provider", "voice", "model", "aligner", "language", "source", "generated_at")}
    total = 0.0
    for entry in plan["scenes"]:
        words = len(entry["timeline"])
        row = {
            "scene_id": entry["scene_id"],
            "text": entry["text"],
            "duration": round(entry["duration"], 2),
            "words": words,
            "audio": entry["audio"],
            "exists": entry["exists"],
            "clip_duration": "",
            "fits": None,
            "speedup": 1.0,
            "dropped": "",
        }
        if clip_durations and entry["scene_id"] in clip_durations:
            target = clip_durations[entry["scene_id"]]
            fitted = timeline.fit(entry["timeline"], target,
                                  max_speedup=SETTINGS.narration_max_speedup)
            row["clip_duration"] = round(target, 2)
            row["fits"] = fitted.fits
            row["speedup"] = round(fitted.speedup, 3)
            row["dropped"] = fitted.dropped
            if fitted.dropped:
                data["warnings"].append(
                    f"{entry['scene_id']}: the line is {fitted.overflow:.1f}s longer than the clip "
                    f"even at {SETTINGS.narration_max_speedup}x - these words would be cut: "
                    f"\"{fitted.dropped[:80]}\""
                )
            elif fitted.speedup > 1.0:
                data["warnings"].append(
                    f"{entry['scene_id']}: speed up {fitted.speedup:.2f}x to fit the clip"
                )
        total += entry["duration"]
        data["scenes"].append(row)
    data["total_duration"] = round(total, 2)
    return data


def srt_text(project_id: str, clip_durations: dict[str, float] | None = None) -> str:
    """Subtitles for the narration, using real clip lengths when they are known."""
    plan = load(project_id)
    if not plan:
        return ""
    offset = 0.0
    cues: list[Cue] = []
    for entry in plan["scenes"]:
        words: Timeline = entry["timeline"]
        target = (clip_durations or {}).get(entry["scene_id"], 0.0)
        if target:
            fitted = timeline.fit(words, target, max_speedup=SETTINGS.narration_max_speedup)
            words = fitted.timeline
        cues.extend(timeline.shift_cues(
            chunk_cues(words, max_chars=SETTINGS.caption_max_chars), offset))
        offset += target or entry["duration"]
    return timeline.to_srt(cues)


def state(project_id: str) -> dict[str, Any]:
    """In-memory progress for long synthesis runs (see api.py)."""
    return dict(_RUNNING.get(project_id) or {"state": "idle"})


_RUNNING: dict[str, dict[str, Any]] = {}


def mark_running(project_id: str) -> None:
    _RUNNING[project_id] = {"state": "running", "started": time.time(), "message": "starting"}


def mark_done(project_id: str, error: str = "") -> None:
    _RUNNING[project_id] = {"state": "error" if error else "idle", "finished": time.time(),
                            "error": error}


def mark_progress(project_id: str, message: str) -> None:
    entry = _RUNNING.setdefault(project_id, {"state": "running", "started": time.time()})
    entry["message"] = message


def scene_ids_with_lines(manifest: Manifest) -> list[str]:
    return [sid for sid, _ in script_for(manifest)]


__all__ = [
    "PLAN_NAME", "SRT_NAME", "SceneNarration", "audio_path", "delete", "describe", "load",
    "mark_done", "mark_progress", "mark_running", "narration_dir", "plan_path", "scene_ids_with_lines",
    "script_for", "srt_text", "state", "synthesize",
]
