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

from . import toolchain

# Point PATH at the project's own tool root (D:\Tools on Windows) before any
# tool lookup happens. A no-op on PATH-based installs; safe to repeat.
toolchain.apply()


def pick_downloads_root(*, explicit: str, data_dir: str, is_windows: bool,
                        d_drive: bool) -> Path:
    """Decide the download root (pure, so every branch is testable).

    Precedence: FLOWPIPE_DOWNLOADS_DIR -> D:\downloads on Windows -> <data>/downloads.
    """
    if explicit.strip():
        return Path(explicit.strip()).expanduser().resolve()
    if is_windows and d_drive:
        return Path("D:/downloads")
    return (Path(data_dir) if data_dir else Path("data")).resolve() / "downloads"


def default_downloads_dir() -> Path:
    """Where *every* download goes: installers, raw Flow media, final copies."""
    data = os.environ.get("FLOWPIPE_DATA_DIR", "").strip()
    if not data:
        root = toolchain.tools_root()
        if root is not None:
            data = str(root / "data")
    return pick_downloads_root(
        explicit=os.environ.get("FLOWPIPE_DOWNLOADS_DIR", ""),
        data_dir=data,
        is_windows=os.name == "nt",
        d_drive=Path("D:/").exists(),
    )


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    # ---- where things live -------------------------------------------------
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("FLOWPIPE_DATA_DIR", "data")).resolve())
    # Single download root for the whole project: installers, raw Flow media,
    # browser/ffmpeg downloads and a copy of each finished video.
    downloads_dir: Path = field(default_factory=default_downloads_dir)
    copy_final_to_downloads: bool = True

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
    def flow_downloads_dir(self) -> Path:
        """Raw media pulled out of Google Flow, per project."""
        return self.downloads_dir / "flow"

    @property
    def final_downloads_dir(self) -> Path:
        """Finished cuts, copied here with a readable name."""
        return self.downloads_dir / "final"

    @property
    def settings_path(self) -> Path:
        return self.data_dir / "settings.json"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.projects_dir.mkdir(parents=True, exist_ok=True)
        for path in (self.downloads_dir, self.flow_downloads_dir, self.final_downloads_dir):
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError:  # read-only or missing drive: fail later, not at import
                break

    def ensure_downloads(self) -> Path:
        self.downloads_dir.mkdir(parents=True, exist_ok=True)
        return self.downloads_dir

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
        out["tools"] = toolchain.status()
        return out

    def save(self) -> None:
        self.ensure_dirs()
        payload = {k: v for k, v in self.public().items()
                   if k not in {"anthropic_key_present", "tools"}}
        payload.pop("data_dir", None)  # never persist the resolved working dir
        self.settings_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def update(self, changes: dict[str, Any]) -> Settings:
        allowed = {f.name for f in fields(self)} - {"anthropic_api_key", "data_dir"}
        path_fields = {"downloads_dir"}
        for key, value in changes.items():
            if key not in allowed:
                continue
            if key in path_fields and isinstance(value, str) and value.strip():
                value = Path(value).expanduser().resolve()
            setattr(self, key, value)
        self.save()
        return self

    @classmethod
    def load(cls) -> Settings:
        settings = cls()
        # A configured tool root means "keep everything on that drive": project
        # data goes next to the tools unless FLOWPIPE_DATA_DIR says otherwise
        # (run.bat and setup.bat set that explicitly anyway).
        root = toolchain.tools_root()
        if root is not None and not os.environ.get("FLOWPIPE_DATA_DIR"):
            settings.data_dir = root / "data"
        settings.anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if os.environ.get("FLOWPIPE_DRIVER"):
            settings.driver = os.environ["FLOWPIPE_DRIVER"]  # type: ignore[assignment]
        if settings.settings_path.exists():
            stored = json.loads(settings.settings_path.read_text(encoding="utf-8"))
            for key, value in stored.items():
                if key == "data_dir" or not hasattr(settings, key):
                    continue
                if isinstance(value, str) and isinstance(getattr(settings, key), Path) and value.strip():
                    value = Path(value)
                setattr(settings, key, value)
        settings.auto_failover = _env_bool("FLOWPIPE_AUTO_FAILOVER", settings.auto_failover)
        # gflow-cli's own download root: anything it saves without an explicit
        # --out/--out-dir still lands on the project's download drive.
        os.environ.setdefault("GFLOW_CLI_OUTPUT_DIR", str(settings.flow_downloads_dir))
        settings.ensure_dirs()
        return settings


SETTINGS = Settings.load()
