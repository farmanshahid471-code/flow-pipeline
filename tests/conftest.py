"""Test harness: every test gets an isolated data directory and the mock driver."""
from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

import pytest

_TMP = tempfile.mkdtemp(prefix="flowpipe-tests-")
os.environ.setdefault("FLOWPIPE_DATA_DIR", _TMP)
os.environ.setdefault("FLOWPIPE_DRIVER", "mock")
os.environ.setdefault("MOCK_CREDITS", "120")


@pytest.fixture(scope="session")
def pipeline():
    from flowpipe import db
    from flowpipe.config import SETTINGS

    SETTINGS.data_dir = Path(_TMP)
    SETTINGS.driver = "mock"
    SETTINGS.ensure_dirs()
    SETTINGS.output_width = 320
    SETTINGS.output_height = 180
    SETTINGS.output_fps = 10
    SETTINGS.clip_duration = 1
    SETTINGS.credits_per_clip = 20
    SETTINGS.credit_threshold = 20
    SETTINGS.backoff_seconds = 1
    SETTINGS.max_attempts = 2
    db.init_db()
    return SETTINGS


@pytest.fixture(autouse=True)
def clean_pool(pipeline):
    """Each test owns the whole account pool."""
    from flowpipe import db
    from flowpipe.flow.mock import reset_state

    reset_state()
    db.execute("DELETE FROM accounts")
    db.execute("DELETE FROM switch_requests")
    db.execute("DELETE FROM uploads")
    yield


@pytest.fixture()
def project(pipeline):
    """A tiny two-scene project with a manifest already on disk."""
    from flowpipe import projects
    from flowpipe.models import Manifest

    project_id = projects.new_id("test topic")
    from flowpipe import db

    db.create_project(project_id, "test topic", "cinematic-short")
    manifest = Manifest.model_validate({
        "project": {"topic": "test topic", "style": "noir", "target_duration": 4, "aspect_ratio": "16:9"},
        "characters": [{
            "id": "aria", "name": "Aria", "description": "lighthouse keeper",
            "face_prompt": "portrait of a weathered woman, 60s, grey braid, salt wind",
            "body_prompt": "oilskin coat, wool sweater, tall boots",
        }],
        "scenes": [
            {"id": "scene-1", "order": 1, "title": "Arrival", "character_ids": ["aria"],
             "image_prompt": "Aria on the lighthouse steps at dawn", "animation_prompt": "slow push in",
             "clip_duration": 1},
            {"id": "scene-2", "order": 2, "title": "The lamp", "character_ids": ["aria"],
             "image_prompt": "Aria lighting the lamp", "animation_prompt": "tilt up", "clip_duration": 1},
        ],
    })
    projects.write_manifest(project_id, manifest)
    db.update_project(project_id, status="manifest_ready")
    return project_id


def wait_for(predicate, timeout: float = 180.0, interval: float = 0.5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError(f"condition not met within {timeout}s: {predicate}")
