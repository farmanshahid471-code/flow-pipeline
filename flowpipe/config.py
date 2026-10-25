"""Runtime configuration.

Settings live in ``<data_dir>/settings.json`` and are editable from the dashboard.
Secrets (the Anthropic key) are read from the environment only, never persisted
by the app and never echoed back over the API.
"""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Literal


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    # ---- where things live -------------------------------------------------
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("FLOWPIPE_DATA_DIR", "data")).resolve())

    # ---- drivers -----------------------------------------------------------
    # "mock"   -> deterministic offline driver, no Google account, no credits.
    # "gflow"  -> shells out to gflow-cli (ffroliva/gflow-cli) against real Flow.
    driver: Literal["mock", "gflow"] = "mock"
    gflow_bin: str = "gflow"
    gflow_home: str = ""          # GFLOW_CLI_HOME for per-profile sessions ("" -> CLI default)
    flow_project_prefix: str = "flowpipe"

    # ---- Stage A -----------------------------------------------------------
    anthropic_model: str = "claude-sonnet-4-5"
    prompt_template_id: str = "cinematic-short"
    default_scene_count: int = 5
    default_target_duration: int = 30

    # ---- Stage B-D (Flow) --------------------------------------------------
    image_model: str = ""          # "" -> driver default / runtime catalog default
    video_model_preference: str = ""  # matched against the runtime model catalog, not hardcoded
    aspect_ratio: str = "16:9"
    resolution: str = "720p"
    clip_duration: int = 8         # seconds per generated segment
    max_attempts: int = 3
    backoff_seconds: int = 20
    job_timeout_seconds: int = 900

    # ---- accounts ----------------------------------------------------------
    # Human-approved failover is the default on purpose: switching accounts
    # automatically is the single most ToS-risky behaviour in this pipeline.
    auto_failover: bool = False
    credit_threshold: int = 20
    credits_per_clip: int = 20     # used to project remaining capacity

    # ---- Stage E -----------------------------------------------------------
    output_width: int = 1280
    output_height: int = 720
    output_fps: int = 24
    output_crf: int = 20

    # Deliberately no CAPTCHA/verification settings: a challenge always pauses
    # the project for a human (see docs/COMPLIANCE.md).

    # ---- computed / read-only ---------------------------------------------
    anthropic_api_key: str = field(default="", repr=False)

    # ---------------------------------------------------------------- io ----
    @property
    def db_path(self) -> Path:
        return self.data_dir / "flowpipe.db"

    @property
    def projects_dir(self) -> Path:
        return self.data_dir / "projects"

    @property
    def settings_path(self) -> Path:
        return self.data_dir / "settings.json"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.projects_dir.mkdir(parents=True, exist_ok=True)

    def ffmpeg(self) -> str:
        """Prefer a system ffmpeg, fall back to the bundled imageio-ffmpeg build."""
        system = shutil.which("ffmpeg")
        if system:
            return system
        try:
            import imageio_ffmpeg

            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception as exc:  # pragma: no cover - depends on install
            raise RuntimeError(
                "ffmpeg is required for Stage E. Install ffmpeg or `pip install imageio-ffmpeg`."
            ) from exc

    # ------------------------------------------------------------ persist ---
    def public(self) -> dict[str, Any]:
        """Everything except secrets, with Paths serialised."""
        out = {}
        for f in fields(self):
            if f.name == "anthropic_api_key":
                continue
            value = getattr(self, f.name)
            out[f.name] = str(value) if isinstance(value, Path) else value
        out["anthropic_key_present"] = bool(self.anthropic_api_key)
        return out

    def save(self) -> None:
        self.ensure_dirs()
        payload = {k: v for k, v in self.public().items() if k not in {"anthropic_key_present"}}
        payload.pop("data_dir", None)  # never persist the resolved working dir
        self.settings_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def update(self, changes: dict[str, Any]) -> Settings:
        allowed = {f.name for f in fields(self)} - {"anthropic_api_key", "data_dir"}
        for key, value in changes.items():
            if key in allowed:
                setattr(self, key, value)
        self.save()
        return self

    @classmethod
    def load(cls) -> Settings:
        settings = cls()
        settings.anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if os.environ.get("FLOWPIPE_DRIVER"):
            settings.driver = os.environ["FLOWPIPE_DRIVER"]  # type: ignore[assignment]
        if settings.settings_path.exists():
            stored = json.loads(settings.settings_path.read_text(encoding="utf-8"))
            for key, value in stored.items():
                if key != "data_dir" and hasattr(settings, key):
                    setattr(settings, key, value)
        settings.auto_failover = _env_bool("FLOWPIPE_AUTO_FAILOVER", settings.auto_failover)
        settings.ensure_dirs()
        return settings


SETTINGS = Settings.load()
