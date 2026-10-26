"""Locate the project's external toolchain.

On Windows, ``setup.bat`` installs everything the project needs under a single
root (``D:\\Tools`` by default) so nothing lands on the C: drive:

    D:\\Tools\\Python311\\python.exe          portable Python
    D:\\Tools\\Python311\\Scripts\\gflow.exe  Flow driver (optional)
    D:\\Tools\\ffmpeg\\bin\\ffmpeg.exe         ffmpeg / ffprobe
    D:\\Tools\\gflow\\                        gflow profiles (browser sessions)
    D:\\Tools\\playwright-browsers\\          Playwright browsers
    D:\\Tools\\pip-cache\\  D:\\Tools\\tmp\\    caches, so installs stay off C:
    D:\\Tools\\voicestudio\\                    VoiceStudio (local text-to-speech server)
    D:\\Tools\\uv\\  D:\\Tools\\uv-cache\\   uv, used to install VoiceStudio
    D:\\Tools\\hf-cache\\ torch-cache\\   model weights for TTS + whisperX
    D:\\Tools\\data\\                          pipeline data (projects, DB)

This module makes the app *find* those tools no matter how it was launched:
at import time the known subdirectories are prepended to ``PATH`` and the
tool-owned state directories are defaulted. It is idempotent, so calling
``apply()`` again never duplicates PATH entries.

Override the root with ``FLOWPIPE_TOOLS_DIR``; on other platforms the default
root only applies if the directory actually exists (so Linux/macOS installs are
untouched and simply use PATH).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

DEFAULT_WINDOWS_ROOT = "D:/Tools"

# (relative path, description) — order matters: ffmpeg first so a bundled build
# wins over anything else on PATH.
_SUBDIRS: Sequence[tuple[tuple[str, ...], str]] = (
    (("ffmpeg", "bin"), "ffmpeg"),
    (("Python311",), "python"),
    (("Python311", "Scripts"), "python-scripts"),
    (("git", "bin"), "git"),
)

_STATE_DIRS: Sequence[tuple[str, str]] = (
    ("GFLOW_CLI_HOME", "gflow"),
    ("PLAYWRIGHT_BROWSERS_PATH", "playwright-browsers"),
    ("PIP_CACHE_DIR", "pip-cache"),
    # text-to-speech + alignment models: gigabytes, never on C:
    ("HF_HOME", "hf-cache"),
    ("HF_HUB_CACHE", "hf-cache"),
    ("TORCH_HOME", "torch-cache"),
    ("UV_CACHE_DIR", "uv-cache"),
    ("OMNIVOICE_DATA_DIR", "voicestudio-data"),
)

# created on demand: model caches are empty until the first synthesis
_CACHE_SUBDIRS = {"hf-cache", "torch-cache", "uv-cache"}

_applied = False
_added: list[str] = []


# --------------------------------------------------------------------- root --
def tools_root() -> Path | None:
    """The tool root in use, or None if this is a plain PATH-based install."""
    explicit = os.environ.get("FLOWPIPE_TOOLS_DIR", "").strip()
    if explicit:
        return Path(explicit)
    default = Path(DEFAULT_WINDOWS_ROOT)
    if default.is_dir():
        return default
    return None


def candidate_dirs(root: Path | None = None) -> list[Path]:
    root = root or tools_root()
    if root is None:
        return []
    return [root.joinpath(*parts) for parts, _ in _SUBDIRS if root.joinpath(*parts).is_dir()]


# --------------------------------------------------------------------- path --
def _key(path: str) -> str:
    """PATH entries compare case-insensitively on Windows."""
    return path.rstrip("\\/").lower() if os.name == "nt" else path.rstrip("/")


def merge_path(entries: Iterable[Path | str], current: str | None = None) -> str:
    """Prepend `entries` to `current`, dropping duplicates. Pure function."""
    current = os.environ.get("PATH", "") if current is None else current
    existing = [p for p in current.split(os.pathsep) if p]
    seen = {_key(p) for p in existing}
    prefix: list[str] = []
    for entry in entries:
        text = str(entry)
        key = _key(text)
        if key in seen:
            continue
        seen.add(key)
        prefix.append(text)
    return os.pathsep.join(prefix + existing)


def apply() -> dict:
    """Make the tool root visible to this process. Safe to call repeatedly."""
    global _applied
    root = tools_root()
    info: dict = {"tools_dir": str(root) if root else None, "added_paths": [], "state_dirs": {}}
    if root is None:
        info["mode"] = "path"
        return info

    resolved: dict[str, str] = {}
    for parts, label in _SUBDIRS:
        directory = root.joinpath(*parts)
        if directory.is_dir():
            resolved[label] = str(directory)
    info["resolved"] = resolved

    if resolved:
        before = os.environ.get("PATH", "")
        after = merge_path(resolved.values(), before)
        if after != before:
            os.environ["PATH"] = after
            info["added_paths"] = list(resolved.values())
            if not _applied:
                _added.extend(resolved.values())

    for env, sub in _STATE_DIRS:
        target = root / sub
        if not os.environ.get(env) and (target.is_dir() or sub in _CACHE_SUBDIRS):
            os.environ[env] = str(target)
            info["state_dirs"][env] = str(target)

    info["mode"] = "tools"
    _applied = True
    return info


# ------------------------------------------------------------------- status --
def _binary(name: str) -> str | None:
    return shutil.which(name)


def _first_line(args: list[str]) -> str | None:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=25)
    except (OSError, subprocess.SubprocessError):
        return None
    text = (proc.stdout or proc.stderr or "").strip()
    return text.splitlines()[0].strip() if text else None


def status() -> dict:
    """What the app actually resolved — surfaced on /api/health and Settings."""
    apply()  # idempotent: makes PATH truthful even if called before config import
    root = tools_root()
    ffmpeg = _binary("ffmpeg")
    ffmpeg_source = "PATH" if ffmpeg else None
    if ffmpeg is None:
        # the bundled wheel build is a valid, offline fallback
        try:
            import imageio_ffmpeg

            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
            ffmpeg_source = "imageio-ffmpeg"
        except Exception:  # pragma: no cover - depends on the wheel
            ffmpeg = None
    ffprobe = _binary("ffprobe")
    gflow = _binary("gflow")
    return {
        "tools_dir": str(root) if root else None,
        "mode": "tools" if root else "path",
        "python": sys.executable,
        "python_version": sys.version.split()[0],
        "platform": sys.platform,
        "ffmpeg": ffmpeg,
        "ffmpeg_source": ffmpeg_source,
        "ffmpeg_version": _first_line([ffmpeg, "-version"]) if ffmpeg else None,
        "ffprobe": ffprobe,
        "gflow": gflow,
        "gflow_home": os.environ.get("GFLOW_CLI_HOME") or None,
        "playwright_browsers": os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or None,
        "data_dir_env": os.environ.get("FLOWPIPE_DATA_DIR") or None,
        "voicestudio_dir": str(root / "voicestudio") if root else None,
        "voicestudio_installed": bool(root and (root / "voicestudio").is_dir()),
        "uv": _binary("uv"),
        "hf_cache": os.environ.get("HF_HOME") or None,
        "added_paths": list(_added),
    }
