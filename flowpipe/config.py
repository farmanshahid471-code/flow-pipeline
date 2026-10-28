"""Runtime configuration.

Settings live in ``<data_dir>/settings.json`` and are editable from the dashboard.
Secrets (the Anthropic key) are read from the environment only, never persisted
by the app and never echoed back over the API.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
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


# --------------------------------------------------------------- validation ---
class SettingsError(ValueError):
    """A setting was rejected. The message is written for the person typing."""


#: accepted range for every integer setting (inclusive)
INT_RANGES: dict[str, tuple[int, int]] = {
    "default_scene_count": (1, 40),
    "default_target_duration": (5, 600),
    "clip_duration": (1, 60),
    "max_attempts": (1, 10),
    "backoff_seconds": (0, 600),
    "job_timeout_seconds": (15, 7200),
    "credit_threshold": (0, 100000),
    "credits_per_clip": (1, 10000),
    "output_width": (64, 3840),
    "output_height": (64, 2160),
    "output_fps": (1, 60),
    "output_crf": (0, 51),
    "caption_max_chars": (10, 200),
}

#: accepted range for every float setting (inclusive)
FLOAT_RANGES: dict[str, tuple[float, float]] = {
    "tts_speed": (0.25, 4.0),
    "narration_max_speedup": (1.0, 2.0),
}

#: settings whose value must come from a fixed vocabulary
CHOICES: dict[str, set[str]] = {
    "driver": {"mock", "gflow"},
    "stage_a_provider": {"anthropic", "deepseek"},
    "tts_provider": {"off", "voicestudio", "mock"},
    "tts_format": {"wav", "mp3", "flac", "ogg", "opus", "aac", "pcm"},
    "align_provider": {"proportional", "whisperx"},
    "captions": {"off", "sidecar", "burn"},
}

BOOLS = {"auto_failover", "copy_final_to_downloads"}

#: strings that may be empty (an empty value means "use the default")
OPTIONAL_STRINGS = {
    "gflow_home", "image_model", "video_model_preference", "voicestudio_dir",
    "whisperx_python", "tts_api_key",
}

#: strings that must have something in them
REQUIRED_STRINGS = {
    "gflow_bin", "flow_project_prefix", "anthropic_model", "prompt_template_id",
    "aspect_ratio", "resolution", "tts_model", "tts_voice", "whisperx_model",
    "narration_language", "voicestudio_url", "deepseek_model", "deepseek_base_url",
}


#: never written to disk, never returned by /api/settings
SECRET_FIELDS = {"anthropic_api_key", "tts_api_key", "deepseek_api_key"}

#: what an operator may change (data_dir is resolved at runtime)
ALLOWED_KEYS = {
    "downloads_dir", "copy_final_to_downloads", "driver", "gflow_bin", "gflow_home",
    "flow_project_prefix", "anthropic_model", "prompt_template_id",
    "stage_a_provider", "deepseek_model", "deepseek_base_url",
    "default_scene_count", "default_target_duration", "image_model",
    "video_model_preference", "aspect_ratio", "resolution", "clip_duration",
    "max_attempts", "backoff_seconds", "job_timeout_seconds", "auto_failover",
    "credit_threshold", "credits_per_clip", "tts_provider", "voicestudio_url",
    "voicestudio_dir", "tts_model", "tts_voice", "tts_speed", "tts_format",
    "align_provider", "whisperx_model", "whisperx_python", "narration_language",
    "narration_max_speedup", "captions", "caption_max_chars", "output_width",
    "output_height", "output_fps", "output_crf",
}


def coerce_value(key: str, value: Any) -> Any:
    """Validate one setting and return it in the type the app expects.

    Raises :class:`SettingsError` with a readable message instead of letting a
    bad value reach the pipeline - a broken setting used to make every endpoint
    fail until the file was edited by hand.
    """
    if key in INT_RANGES:
        low, high = INT_RANGES[key]
        if isinstance(value, bool):
            raise SettingsError(f"{key} must be a whole number between {low} and {high}")
        try:
            number = int(str(value).strip())
        except (TypeError, ValueError) as exc:
            raise SettingsError(f"{key} must be a whole number between {low} and {high}") from exc
        if not low <= number <= high:
            raise SettingsError(f"{key} must be between {low} and {high} (got {number})")
        return number
    if key in FLOAT_RANGES:
        low, high = FLOAT_RANGES[key]
        if isinstance(value, bool):
            raise SettingsError(f"{key} must be a number between {low} and {high}")
        try:
            number = float(str(value).strip())
        except (TypeError, ValueError) as exc:
            raise SettingsError(f"{key} must be a number between {low} and {high}") from exc
        if not low - 1e-9 <= number <= high + 1e-9:
            raise SettingsError(f"{key} must be between {low} and {high} (got {number})")
        return number
    if key in CHOICES:
        text = str(value).strip().lower()
        if text not in CHOICES[key]:
            allowed = ", ".join(sorted(CHOICES[key]))
            raise SettingsError(f"{key} must be one of: {allowed}")
        return text
    if key in BOOLS:
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off", ""}:
            return False
        raise SettingsError(f"{key} must be true or false")
    if key == "downloads_dir":
        text = str(value).strip()
        if not text:
            raise SettingsError("downloads_dir cannot be empty - give it a folder, e.g. D:\\downloads")
        try:
            return Path(text).expanduser().resolve()
        except (OSError, RuntimeError) as exc:
            raise SettingsError(f"downloads_dir is not a usable path: {exc}") from exc
    if key == "voicestudio_url":
        text = str(value).strip()
        if not text.startswith(("http://", "https://")):
            raise SettingsError("voicestudio_url must start with http:// (it is a loopback address)")
        return text.rstrip("/")
    if key in OPTIONAL_STRINGS:
        return str(value).strip()
    if key in REQUIRED_STRINGS:
        text = str(value).strip()
        if not text:
            raise SettingsError(f"{key} cannot be empty")
        return text
    return value


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
    # Stage A can also run on an OpenAI-compatible endpoint. DeepSeek is the one
    # that matters in practice (much cheaper for long manifest prompts), so it is
    # a first-class option rather than a "bring your own client" hack.
    stage_a_provider: str = "anthropic"      # anthropic | deepseek
    deepseek_model: str = "deepseek-chat"    # or deepseek-reasoner
    deepseek_base_url: str = "https://api.deepseek.com"
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

    # ---- narration: text-to-speech + word timestamps -----------------------
    # "off"          -> no narration (the default)
    # "voicestudio"  -> local VoiceStudio server over its OpenAI-compatible API
    # "mock"         -> offline placeholder audio, no model needed
    tts_provider: str = "off"
    voicestudio_url: str = "http://127.0.0.1:3900"
    voicestudio_dir: str = ""      # "" -> <tools>/voicestudio
    tts_model: str = "omnivoice"
    tts_voice: str = "default"
    tts_speed: float = 1.0
    tts_format: str = "wav"        # wav is what the aligner wants
    tts_api_key: str = field(default="", repr=False)
    # "proportional" -> no model, spread the script over the audio by word length
    # "whisperx"     -> real forced alignment (pip install whisperx)
    align_provider: str = "proportional"
    whisperx_model: str = "base.en"
    whisperx_python: str = ""      # "" -> this interpreter
    narration_language: str = "en"
    narration_max_speedup: float = 1.15   # squeeze a scene's line by at most this much
    captions: str = "sidecar"      # off | sidecar (.srt next to the video) | burn
    caption_max_chars: int = 42

    # ---- Stage E -----------------------------------------------------------
    output_width: int = 1280
    output_height: int = 720
    output_fps: int = 24
    output_crf: int = 20

    # Deliberately no CAPTCHA/verification settings: a challenge always pauses
    # the project for a human (see docs/COMPLIANCE.md).

    # ---- computed / read-only ---------------------------------------------
    anthropic_api_key: str = field(default="", repr=False)
    deepseek_api_key: str = field(default="", repr=False)
    #: stored values that were ignored because they made no sense
    load_warnings: list[str] = field(default_factory=list, repr=False)

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
    def public(self, *, include_tools: bool = True) -> dict[str, Any]:
        """Everything except secrets, with Paths serialised."""
        out = {}
        for f in fields(self):
            if f.name in SECRET_FIELDS:
                continue
            value = getattr(self, f.name)
            out[f.name] = str(value) if isinstance(value, Path) else value
        out["anthropic_key_present"] = bool(self.anthropic_api_key)
        out["deepseek_key_present"] = bool(self.deepseek_api_key)
        if include_tools:  # runs `ffmpeg -version`: not worth it on every save
            out["tools"] = toolchain.status()
        return out

    def save(self) -> None:
        self.ensure_dirs()
        payload = {k: v for k, v in self.public(include_tools=False).items()
                   if k not in {"anthropic_key_present", "deepseek_key_present", "tools"}}
        payload.pop("data_dir", None)  # never persist the resolved working dir
        self.settings_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def validate(self, changes: dict[str, Any]) -> dict[str, Any]:
        """Check a patch without applying it. Raises SettingsError listing all problems."""
        problems: list[str] = []
        clean: dict[str, Any] = {}
        for key, value in changes.items():
            if key not in ALLOWED_KEYS:
                continue
            try:
                clean[key] = coerce_value(key, value)
            except SettingsError as exc:
                problems.append(str(exc))
        if problems:
            raise SettingsError("; ".join(problems))
        return clean

    def update(self, changes: dict[str, Any]) -> Settings:
        """Apply a validated patch. Nothing is changed when anything is rejected."""
        for key, value in self.validate(changes).items():
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
        settings.deepseek_api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        settings.tts_api_key = os.environ.get("VOICESTUDIO_API_KEY", "").strip()
        if settings.settings_path.exists():
            stored = json.loads(settings.settings_path.read_text(encoding="utf-8"))
            for key, value in stored.items():
                if key == "data_dir" or key not in ALLOWED_KEYS:
                    continue
                try:
                    value = coerce_value(key, value)
                except SettingsError as exc:
                    # One bad value must never take the dashboard down: keep the
                    # default, remember the complaint, and say so once.
                    settings.load_warnings.append(str(exc))
                    continue
                if isinstance(value, str) and isinstance(getattr(settings, key), Path) and not value.strip():
                    continue
                setattr(settings, key, value)
        # The environment is the escape hatch, so it is applied *after* the stored
        # file: `set FLOWPIPE_DRIVER=mock` then run.bat must switch a saved
        # driver=gflow back to mock for one session. Applying it before the file
        # (as this used to) let settings.json quietly win and the switch did
        # nothing - the header kept saying "driver: gflow".
        env_driver = (os.environ.get("FLOWPIPE_DRIVER") or "").strip().lower()
        if env_driver:
            if env_driver != settings.driver:
                print(
                    f"settings: driver = {env_driver!r} from FLOWPIPE_DRIVER "
                    f"(overrides settings.json = {settings.driver!r})",
                    file=sys.stderr, flush=True,
                )
            settings.driver = env_driver
        settings.auto_failover = _env_bool("FLOWPIPE_AUTO_FAILOVER", settings.auto_failover)
        # gflow-cli's own download root: anything it saves without an explicit
        # --out/--out-dir still lands on the project's download drive.
        os.environ.setdefault("GFLOW_CLI_OUTPUT_DIR", str(settings.flow_downloads_dir))
        settings.ensure_dirs()
        if settings.load_warnings:
            print(
                "settings: ignored " + str(len(settings.load_warnings)) + " unusable stored value(s): "
                + "; ".join(settings.load_warnings),
                file=sys.stderr, flush=True,
            )
        return settings


SETTINGS = Settings.load()
