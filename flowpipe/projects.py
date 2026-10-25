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
    path = SETTINGS.projects_dir / project_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def manifest_path(project_id: str) -> Path:
    return project_dir(project_id) / "project.json"


def subdir(project_id: str, name: str) -> Path:
    path = project_dir(project_id) / name
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


def read_manifest(project_id: str) -> Manifest | None:
    path = manifest_path(project_id)
    if not path.exists():
        return None
    return Manifest.model_validate_json(path.read_text(encoding="utf-8"))


def write_manifest(project_id: str, manifest: Manifest) -> Path:
    path = manifest_path(project_id)
    path.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
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
