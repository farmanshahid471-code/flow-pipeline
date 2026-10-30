"""Offline driver: the whole pipeline end-to-end with no Google account.

It renders real PNGs and real MP4s with ffmpeg (colour fields derived from the
prompt text), spends fake credits, and can simulate the failure modes the runner
has to cope with, so Stages B–E and every UI panel are testable before a single
real credit is spent.

Environment knobs (all optional):
    MOCK_CREDITS=120          starting credit balance per account
    MOCK_FAIL_RATE=0.0        fraction of calls that fail transiently
    MOCK_DRIFT_AT=0           fail call N with selector drift (0 = never)
    MOCK_CHALLENGE_AT=0       raise a human challenge at call N (0 = never)
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import subprocess
from pathlib import Path

from ..config import SETTINGS
from .base import (
    Account,
    Capabilities,
    ChallengeRequired,
    CharacterResult,
    CreditReading,
    FlowError,
    NoCredits,
    ReferenceAsset,
    SelectorDrift,
    TransientFlowError,
)

IMAGE_MODELS = ["nano2", "nano2-pro", "nano2-lite"]
VIDEO_MODELS = ["omni-flash", "veo-quality", "veo-fast"]


def _state_path() -> Path:
    path = SETTINGS.data_dir / "mock"
    path.mkdir(parents=True, exist_ok=True)
    return path / "state.json"


def _load_state() -> dict:
    path = _state_path()
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return {"calls": 0, "credits": {}, "uploaded": {}}


def _save_state(state: dict) -> None:
    _state_path().write_text(json.dumps(state, indent=2))


def reset_state() -> None:
    """Clear mock counters/credits (tests)."""
    path = _state_path()
    if path.exists():
        path.unlink()


def set_credits(account_id: str, credits: int) -> None:
    """Seed a mock balance (used by tests and by the demo seed script)."""
    state = _load_state()
    state.setdefault("credits", {})[account_id] = int(credits)
    _save_state(state)


def _palette(text: str) -> tuple[str, str]:
    digest = hashlib.sha256(text.encode()).hexdigest()
    warm = int(digest[0:2], 16)
    cool = int(digest[2:4], 16)
    c0 = f"0x{(warm // 3 + 60):02x}{(cool // 4 + 30):02x}{(warm // 2 + 40):02x}"
    c1 = f"0x{(cool // 3 + 20):02x}{(warm // 4 + 50):02x}{(cool // 2 + 70):02x}"
    return c0, c1


def _ffmpeg(args: list[str]) -> None:
    cmd = [SETTINGS.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", *args]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise FlowError(f"mock render failed: {result.stderr.strip()[:400]}")


class MockDriver:
    name = "mock"

    #: no CLI, no sessions: the runner keeps its old "trust the balance" behaviour
    supports_auth = False

    # ------------------------------------------------------------ internal --
    def _simulate(self, label: str) -> None:
        state = _load_state()
        state["calls"] = state.get("calls", 0) + 1
        calls = state["calls"]
        _save_state(state)
        drift_at = int(os.environ.get("MOCK_DRIFT_AT", "0") or 0)
        challenge_at = int(os.environ.get("MOCK_CHALLENGE_AT", "0") or 0)
        if challenge_at and calls == challenge_at:
            raise ChallengeRequired(
                "Mock driver: Flow is showing a verification challenge. "
                "Finish it in the browser window, then press Resume."
            )
        if drift_at and calls == drift_at:
            raise SelectorDrift("Mock driver: the video model menu no longer matches the driver.")
        rate = float(os.environ.get("MOCK_FAIL_RATE", "0") or 0)
        if rate and random.random() < rate:
            raise TransientFlowError(f"mock transient failure during {label}")

    # ------------------------------------------------------------- driver --
    def capabilities(self) -> Capabilities:
        return Capabilities(
            driver=self.name,
            image_models=IMAGE_MODELS,
            video_models=VIDEO_MODELS,
            default_image_model=IMAGE_MODELS[0],
            default_video_model=VIDEO_MODELS[0],
            supports_character_entity=True,
            supports_end_frame=True,
            notes=["Offline mock: renders placeholder media with ffmpeg, spends fake credits."],
        )

    def credits(self, account: Account, *, refresh: bool = False) -> CreditReading:
        state = _load_state()
        start = int(os.environ.get("MOCK_CREDITS", "120"))
        return CreditReading(credits=int(state.get("credits", {}).get(account.id, start)))

    def ensure_project(self, account: Account, name: str) -> str:
        return "mock-" + hashlib.sha1(f"{account.id}:{name}".encode()).hexdigest()[:12]

    def upload(self, account: Account, flow_project: str, path: Path) -> str:
        self._simulate("upload")
        state = _load_state()
        key = f"{account.id}:{flow_project}:{path.name}:{hashlib.sha1(path.read_bytes()).hexdigest()[:8]}"
        asset_id = "asset-" + hashlib.sha1(key.encode()).hexdigest()[:12]
        state.setdefault("uploaded", {})[asset_id] = str(path)
        _save_state(state)
        return asset_id

    def _spend(self, account: Account, amount: int) -> None:
        state = _load_state()
        start = int(os.environ.get("MOCK_CREDITS", "120"))
        current = int(state.get("credits", {}).get(account.id, start))
        if current < amount:
            raise NoCredits(f"{account.label} has {current} credits, needs {amount}")
        state.setdefault("credits", {})[account.id] = current - amount
        _save_state(state)

    def create_character(self, account: Account, flow_project: str, *, name: str,
                         face_prompt: str, body_prompt: str, out_dir: Path) -> CharacterResult:
        self._simulate(f"character {name}")
        out_dir.mkdir(parents=True, exist_ok=True)
        face = out_dir / "face.png"
        body = out_dir / "body_triptych.png"
        c0, c1 = _palette(face_prompt)
        _ffmpeg(["-f", "lavfi", "-i", f"gradients=s=768x1024:c0={c0}:c1={c1}:x0=0:y0=0:x1=768:y1=1024:d=1",
                 "-frames:v", "1", str(face)])
        c0, c1 = _palette(body_prompt)
        _ffmpeg(["-f", "lavfi", "-i", f"gradients=s=1280x720:c0={c1}:c1={c0}:x0=0:y0=0:x1=1280:y1=720:d=1",
                 "-frames:v", "1", str(body)])
        return CharacterResult(
            images=[face, body],
            entity_id="entity-" + hashlib.sha1(f"{flow_project}:{name}".encode()).hexdigest()[:10],
            entity_name=name,
            raw={"mock": True},
        )

    def generate_image(self, account: Account, flow_project: str, *, prompt: str, out_dir: Path,
                       aspect: str, model: str | None = None,
                       references: list[ReferenceAsset] | None = None) -> Path:
        self._simulate("image")
        out_dir.mkdir(parents=True, exist_ok=True)
        width, height = (1280, 720) if aspect in {"16:9", ""} else (
            (720, 1280) if aspect == "9:16" else (1024, 1024)
        )
        target = out_dir / "still.png"
        c0, c1 = _palette(prompt)
        _ffmpeg(["-f", "lavfi", "-i",
                 f"gradients=s={width}x{height}:c0={c0}:c1={c1}:x0=0:y0=0:x1={width}:y1={height}:d=1",
                 "-frames:v", "1", str(target)])
        return target

    def generate_clip(self, account: Account, flow_project: str, *, prompt: str, out_dir: Path,
                      start_frame: Path, end_frame: Path | None = None, duration: int = 8,
                      resolution: str = "720p", model: str | None = None,
                      aspect: str = "16:9", start_asset_id: str | None = None) -> Path:
        self._simulate("clip")
        # Mirrors the real driver: the still must exist inside the acting
        # account's Flow project before i2v, so a failover re-uploads it.
        if not start_asset_id:
            self.upload(account, flow_project, start_frame)
        self._spend(account, SETTINGS.credits_per_clip)
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / "clip.mp4"
        fps = SETTINGS.output_fps
        frames = max(2, duration * fps)
        vf = (
            f"scale={SETTINGS.output_width}:{SETTINGS.output_height}:force_original_aspect_ratio=decrease,"
            f"pad={SETTINGS.output_width}:{SETTINGS.output_height}:(ow-iw)/2:(oh-ih)/2,"
            f"zoompan=z='min(zoom+0.0006,1.15)':d={frames}:s={SETTINGS.output_width}x{SETTINGS.output_height}:fps={fps},"
            "format=yuv420p"
        )
        _ffmpeg(["-loop", "1", "-i", str(start_frame), "-t", str(duration),
                 "-vf", vf, "-r", str(fps), "-c:v", "libx264", "-preset", "veryfast",
                 "-crf", str(SETTINGS.output_crf), str(target)])
        return target

