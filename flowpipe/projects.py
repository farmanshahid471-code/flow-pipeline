"""On-disk layout for one production run.

    data/projects/<project-id>/
        project.json          # the manifest (source of truth)
        requests/             # manual Claude-Desktop prompts
        characters/<id>/      # reference images
        images/<scene-id>/    # stills
        clips/<scene-id>/     # animated segments
        output/final.mp4
        exports/
"""
from __future__ import annotations

import json
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

from .config import SETTINGS
from .models import Manifest


def new_id(topic: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")[:40] or "project"
    return f"{stamp}-{slug}-{secrets.token_hex(2)}"


def project_dir(project_id: str) -> Path:
    """Pure path — reading a project must never create directories on disk."""
    return SETTINGS.projects_dir / project_id


def ensure_project_dir(project_id: str) -> Path:
    """Create the project folder. Used only when something is about to be written."""
    path = project_dir(project_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def manifest_path(project_id: str) -> Path:
    return project_dir(project_id) / "project.json"


def subdir(project_id: str, name: str) -> Path:
    path = ensure_project_dir(project_id) / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def asset_dir(project_id: str, kind: str, target_id: str) -> Path:
    """kind: characters | images | clips."""
    path = subdir(project_id, kind) / target_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def output_dir(project_id: str) -> Path:
    return subdir(project_id, "output")


def requests_dir(project_id: str) -> Path:
    return subdir(project_id, "requests")


class ManifestError(ValueError):
    """project.json is missing fields, is not JSON, or does not match the schema."""


def read_manifest(project_id: str) -> Manifest | None:
    """Parse the manifest. Raises ManifestError when the file is unusable.

    Callers that must keep working (the project list, the gallery) use
    :func:`read_manifest_or_none` instead, so one hand-edited file cannot take
    the whole dashboard down.
    """
    path = manifest_path(project_id)
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestError(f"cannot read {path}: {exc}") from exc
    try:
        return Manifest.model_validate_json(raw)
    except Exception as exc:  # pydantic ValidationError, JSONDecodeError, ...
        first = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        raise ManifestError(f"{path.name} is not a valid manifest ({first})") from exc


def read_manifest_or_none(project_id: str) -> Manifest | None:
    """Like :func:`read_manifest` but returns None instead of raising."""
    try:
        return read_manifest(project_id)
    except ManifestError:
        return None


def manifest_error(project_id: str) -> str:
    """Empty when the manifest is fine (or absent) - the message otherwise."""
    try:
        read_manifest(project_id)
    except ManifestError as exc:
        return str(exc)
    return ""


def write_manifest(project_id: str, manifest: Manifest) -> Path:
    """Write atomically: a crash mid-write must never leave a truncated JSON."""
    ensure_project_dir(project_id)
    path = manifest_path(project_id)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    os.replace(temporary, path)
    return path


def next_versioned_file(directory: Path, stem: str, suffix: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for index in range(1, 1000):
        candidate = directory / f"{stem}_v{index:02d}{suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"too many versions in {directory}")


def save_json(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path
