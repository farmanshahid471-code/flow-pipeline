"""Regression tests for the ways the dashboard used to break under a bad input.

Every test here failed before the fix it names; together they are the answer to
"can one wrong value take the whole thing down?" - it cannot.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

import pytest

from flowpipe import assemble, db, projects, runner
from flowpipe.config import SETTINGS, Settings, SettingsError, coerce_value


@pytest.fixture()
def client(pipeline, monkeypatch):
    from fastapi.testclient import TestClient

    from flowpipe.api import app

    with TestClient(app) as test_client:
        yield test_client


# --------------------------------------------------------------- settings ----
def test_bad_setting_values_are_rejected_and_nothing_changes(client):
    before = {"output_width": SETTINGS.output_width, "driver": SETTINGS.driver,
              "tts_speed": SETTINGS.tts_speed}
    for payload in ({"output_width": "abc"}, {"output_width": 0}, {"driver": "banana"},
                    {"captions": "sidecar-typo"}, {"tts_speed": 99},
                    {"downloads_dir": ""}, {"max_attempts": 0},
                    {"narration_max_speedup": 0.5}, {"tts_provider": "cloud"},
                    {"auto_failover": "maybe"}):
        response = client.patch("/api/settings", json={"values": payload})
        assert response.status_code == 400, payload
        assert "detail" in response.json()
    for key, value in before.items():
        assert getattr(SETTINGS, key) == value
    # the dashboard still answers everything after all those rejections
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/projects").status_code == 200
    assert client.get("/api/settings").status_code == 200


def test_settings_patch_still_accepts_good_values(client):
    response = client.patch("/api/settings", json={"values": {
        "output_width": 640, "tts_provider": "mock", "captions": "burn",
        "tts_speed": "1.25", "max_attempts": "4",
        "downloads_dir": str(SETTINGS.downloads_dir),
    }})
    assert response.status_code == 200
    body = response.json()
    assert body["output_width"] == 640 and body["tts_speed"] == 1.25
    assert body["captions"] == "burn" and body["max_attempts"] == 4


def test_one_bad_key_rejects_the_whole_patch():
    before = SETTINGS.clip_duration
    with pytest.raises(SettingsError) as excinfo:
        SETTINGS.update({"clip_duration": 6, "output_fps": "fast"})
    assert "output_fps" in str(excinfo.value)
    assert SETTINGS.clip_duration == before  # the good half was not applied


def test_a_corrupt_settings_file_cannot_brick_the_startup(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWPIPE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("FLOWPIPE_TOOLS_DIR", raising=False)
    data = {
        "output_width": "abc",          # wrong type
        "driver": "banana",             # not a choice
        "downloads_dir": "",            # empty path -> used to crash every request
        "tts_speed": 1.5,               # fine, must survive
        "unknown_key": 1,               # ignored
    }
    (tmp_path / "settings.json").write_text(json.dumps(data), encoding="utf-8")
    loaded = Settings.load()
    assert loaded.output_width == Settings().output_width
    assert loaded.driver == Settings().driver
    assert loaded.downloads_dir == Settings().downloads_dir
    assert loaded.tts_speed == 1.5
    assert len(loaded.load_warnings) == 3
    loaded.ensure_dirs()  # and the paths still work


def test_coerce_covers_every_documented_type():
    assert coerce_value("clip_duration", "8") == 8
    assert coerce_value("narration_max_speedup", "1.2") == 1.2
    assert coerce_value("auto_failover", "true") is True
    assert coerce_value("tts_voice", " default ") == "default"
    assert coerce_value("voicestudio_url", "http://127.0.0.1:3900/") == "http://127.0.0.1:3900"
    with pytest.raises(SettingsError):
        coerce_value("voicestudio_url", "127.0.0.1:3900")


def test_tts_api_key_is_never_published_or_persisted():
    published = SETTINGS.public()
    assert "tts_api_key" not in published and "anthropic_api_key" not in published
    SETTINGS.tts_api_key = "secret-value"
    try:
        SETTINGS.save()
        stored = json.loads(SETTINGS.settings_path.read_text(encoding="utf-8"))
        assert "tts_api_key" not in stored
    finally:
        SETTINGS.tts_api_key = ""


# --------------------------------------------------------------- manifests ---
def test_corrupt_manifest_does_not_break_the_dashboard(client, project):
    (projects.project_dir(project) / "project.json").write_text("{not json", encoding="utf-8")

    listing = client.get("/api/projects")
    assert listing.status_code == 200
    row = next(item for item in listing.json() if item["id"] == project)
    assert "not a valid manifest" in row["manifest_error"]
    assert row["scene_count"] == 0

    detail = client.get(f"/api/projects/{project}")
    assert detail.status_code == 200
    assert detail.json()["manifest"] is None
    assert "not a valid manifest" in detail.json()["manifest_error"]

    for path in (f"/api/projects/{project}/manifest", f"/api/projects/{project}/narration",
                 f"/api/projects/{project}/assets", "/api/settings", "/api/health"):
        assert client.get(path).status_code in {200, 422}, path

    # the write path says what is wrong instead of raising a 500
    assert client.put(f"/api/projects/{project}/manifest", json={"project": {}}).status_code == 422
    assert client.post(f"/api/projects/{project}/run", json={}).status_code in {409, 200}


def test_manifest_writes_are_atomic(project):
    original = projects.read_manifest(project)
    projects.write_manifest(project, original)
    assert not list(projects.project_dir(project).glob("*.json.tmp"))
    assert projects.read_manifest(project).scenes[0].id == original.scenes[0].id


# ------------------------------------------------------------------ worker ---
def test_worker_thread_survives_a_failing_queue_read(monkeypatch):
    """One database error used to kill the worker thread for the rest of the run."""
    calls = {"n": 0}
    real_next = runner._next_job

    def flaky_next():
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("database is locked")
        return None

    monkeypatch.setattr(runner, "_next_job", flaky_next)
    monkeypatch.setattr(runner, "_STARTED", False)
    monkeypatch.setattr(runner, "_THREADS", [])
    runner.ensure_workers()
    time.sleep(3.0)
    try:
        assert calls["n"] >= 3, "worker stopped polling after the error"
        assert any(thread.is_alive() for thread in runner._THREADS)
    finally:
        runner._STOP.set()
        for thread in runner._THREADS:
            thread.join(timeout=5)
        runner._STOP.clear()
        monkeypatch.setattr(runner, "_next_job", real_next)
        runner._STARTED = False
        runner._THREADS.clear()


# ---------------------------------------------------------------- assembly ---
def test_assembly_refuses_to_run_twice_for_the_same_project(pipeline, project):
    assemble.claim(project)
    try:
        with pytest.raises(assemble.AssemblyBusy):
            assemble.assemble(project)
    finally:
        assemble.release(project)


def test_a_reassembly_without_captions_removes_the_stale_srt(pipeline, project):
    """Subtitles from an older cut must not be offered next to a newer video."""
    from flowpipe.voice import stage as narration

    _clips(project)
    out = projects.output_dir(project)
    stale = out / "final.srt"
    stale.write_text("1\n00:00:00,000 --> 00:00:01,000\nold words\n", encoding="utf-8")

    result = assemble.assemble(project, captions="off")
    assert result["subtitle_path"] is None
    assert not stale.exists()
    assert narration.load(project) is None


def test_assembly_scratch_space_stays_in_the_data_dir(pipeline, project, monkeypatch):
    _clips(project)
    seen = {}
    real_mkdtemp = assemble.tempfile.mkdtemp

    def spy(prefix=None, dir=None, **kwargs):  # noqa: A002 - mirrors tempfile
        seen["dir"] = dir
        return real_mkdtemp(prefix=prefix, dir=dir, **kwargs)

    monkeypatch.setattr(assemble.tempfile, "mkdtemp", spy)
    assemble.assemble(project)
    assert seen["dir"] is not None
    assert Path(seen["dir"]).resolve() == (Path(SETTINGS.data_dir) / "tmp").resolve()


# --------------------------------------------------------------- narration ---
def test_partial_narration_run_keeps_the_untouched_scenes(pipeline, project, monkeypatch):
    from flowpipe.models import Manifest
    from flowpipe.voice import stage as narration
    from flowpipe.voice.mock import MockBackend

    manifest = projects.read_manifest(project)
    manifest.scenes[0].narration = "The keeper climbs the stairs."
    manifest.scenes[1].narration = "The lamp turns, slow and bright."
    projects.write_manifest(project, manifest)

    narration.synthesize(project, backend=MockBackend(), progress=None)
    first = narration.load(project)
    assert len(first["scenes"]) == 2

    updated = Manifest.model_validate(projects.read_manifest(project).model_dump())
    updated.scenes[1].narration = "A different second line entirely."
    projects.write_manifest(project, updated)
    narration.synthesize(project, backend=MockBackend(), only=["scene-2"])

    plan = narration.load(project)
    by_id = {row["scene_id"]: row for row in plan["scenes"]}
    assert set(by_id) == {"scene-1", "scene-2"}          # scene-1 survived
    assert by_id["scene-1"]["text"] == "The keeper climbs the stairs."
    assert by_id["scene-2"]["text"] == "A different second line entirely."
    assert [row["scene_id"] for row in plan["scenes"]] == ["scene-1", "scene-2"]


def test_reading_narration_does_not_create_folders(pipeline, project):
    voiceover = projects.project_dir(project) / "voiceover"
    if voiceover.exists():
        shutil.rmtree(voiceover)
    from flowpipe.voice import stage as narration

    assert narration.load(project) is None
    narration.describe(project, {})
    assert not voiceover.exists()


def test_voice_status_probe_is_cached(pipeline, monkeypatch):
    from fastapi.testclient import TestClient

    from flowpipe.api import app
    from flowpipe.voice import align, voicestudio

    calls = {"n": 0}
    real_available = voicestudio.VoiceStudioBackend.available

    def counting_available(self):
        calls["n"] += 1
        return True, "cached probe"

    monkeypatch.setattr(voicestudio.VoiceStudioBackend, "available", counting_available)
    monkeypatch.setattr(SETTINGS, "tts_provider", "voicestudio")
    monkeypatch.setattr(align, "_probe_cache", {})
    try:
        with TestClient(app) as client:
            first = client.get("/api/voice/status").json()
            second = client.get("/api/voice/status").json()
            assert first["ok"] is True and second["ok"] is True
            assert calls["n"] == 1, "the voice server was probed on every poll"
            third = client.get("/api/voice/status?refresh=1").json()
            assert third["ok"] is True and calls["n"] == 2
    finally:
        monkeypatch.setattr(voicestudio.VoiceStudioBackend, "available", real_available)


def test_synthesis_survives_a_broken_manifest_file(pipeline, project):
    from flowpipe.voice import stage as narration
    from flowpipe.voice.base import VoiceError

    (projects.project_dir(project) / "project.json").write_text("[]", encoding="utf-8")
    with pytest.raises(VoiceError):
        narration.synthesize(project, backend=None)
    narration.describe(project, {})  # the UI view stays answerable


# ------------------------------------------------------------------ helpers ---
def _clips(project_id: str) -> None:
    """Two tiny real clips, so assembly has something to join."""
    from flowpipe.flow.mock import _ffmpeg  # the mock driver's ffmpeg wrapper

    for scene_id in ("scene-1", "scene-2"):
        directory = projects.asset_dir(project_id, "clips", scene_id)
        target = directory / f"{scene_id}.mp4"
        if not target.exists():
            _ffmpeg(["-f", "lavfi", "-i", "testsrc=size=160x90:rate=10:duration=1",
                     "-pix_fmt", "yuv420p", str(target)])
        db.add_asset(project_id, "clip", scene_id, str(target), None, None)


# ------------------------------------------------------- API smoke: writes ---
def test_every_mutating_endpoint_answers(client, project, monkeypatch):
    """A signature slip in any write handler used to surface as a bare 500.

    Every POST/PATCH/PUT/DELETE the dashboard can send is exercised here; the
    assertion is only that the answer is a *handled* one (2xx/4xx), never a 500.
    """
    from flowpipe import db, projects, stage_a

    # Stage A through the API, with the network call stubbed out
    manifest = projects.read_manifest(project)
    monkeypatch.setattr(
        stage_a, "generate_via_api",
        lambda *, prompt, model=None, **_kwargs: (manifest, "{}"),
    )
    created = client.post("/api/projects", json={
        "topic": "api mode project", "mode": "api", "scene_count": 2, "target_duration": 8})
    assert created.status_code == 200, created.text
    assert created.json()["manifest"]["scenes"], created.json()
    api_project = created.json()["id"]

    # Stage A exploding (not a StageAError) still lands in the manual fallback
    def explode(**_kwargs):
        raise RuntimeError("sdk went sideways")

    monkeypatch.setattr(stage_a, "generate_via_api", explode)
    failed = client.post("/api/projects", json={
        "topic": "api mode failure", "mode": "api", "scene_count": 2, "target_duration": 8})
    assert failed.status_code == 502, failed.text
    assert failed.json()["id"] and failed.json()["manual_request"]
    client.delete(f"/api/projects/{failed.json()['id']}")

    calls = [
        ("post", f"/api/projects/{api_project}/run", {"kinds": None}),
        ("post", f"/api/projects/{api_project}/pause", None),
        ("post", f"/api/projects/{api_project}/resume", None),
        ("post", f"/api/projects/{api_project}/cancel", None),
        ("post", f"/api/projects/{api_project}/stage-a", {"mode": "manual"}),
        ("post", "/api/templates", {"id": "smoke", "body": "topic: {topic}"}),
        ("delete", "/api/templates/smoke", None),
        ("post", "/api/accounts", {"label": "smoke-account"}),
        ("patch", "/api/settings", {"values": {"clip_duration": 2}}),
        ("put", f"/api/projects/{api_project}/manifest", manifest.model_dump()),
        ("post", f"/api/projects/{api_project}/manifest/import", {"raw": "{}"}),
        ("post", "/api/tts/preview", {"text": "hello", "voice": "mock-narrator"}),
        ("delete", f"/api/projects/{api_project}/narration", None),
        ("post", "/api/switch-requests/99999/approve", None),
        ("post", "/api/switch-requests/99999/deny", None),
        ("post", "/api/jobs/99999/retry", None),
        ("post", "/api/jobs/99999/skip", None),
        ("post", "/api/jobs/99999/regenerate", None),
        ("post", "/api/accounts/does-not-exist/refresh", None),
        ("post", "/api/accounts/does-not-exist/renew", None),
        ("post", "/api/accounts/does-not-exist/mark-active", None),
        ("patch", "/api/accounts/does-not-exist", {"label": "x"}),
        ("delete", "/api/accounts/does-not-exist", None),
        ("delete", f"/api/projects/{api_project}/narration/srt", None),
        ("get", "/api/projects/does-not-exist/narration/srt", None),
    ]
    monkeypatch.setattr(SETTINGS, "tts_provider", "mock")
    for method, path, body in calls:
        response = getattr(client, method)(path, json=body) if body is not None \
            else getattr(client, method)(path)
        assert response.status_code < 500, f"{method.upper()} {path} -> {response.status_code} {response.text[:200]}"

    db.execute("DELETE FROM accounts WHERE label='smoke-account'")
    client.delete(f"/api/projects/{api_project}")


def test_narration_rejects_a_second_concurrent_run(client, project, monkeypatch):
    from flowpipe.voice import stage as narration

    # a plan can only be re-run when the manifest has lines to speak
    manifest = projects.read_manifest(project)
    manifest.scenes[0].narration = "One line is enough."
    projects.write_manifest(project, manifest)
    monkeypatch.setattr(SETTINGS, "tts_provider", "mock")
    monkeypatch.setattr(narration, "state", lambda _pid: {"state": "running", "error": "", "message": ""})
    response = client.post(f"/api/projects/{project}/narration", json={})
    assert response.status_code == 409
    assert "already running" in response.json()["detail"]


def test_unknown_setting_keys_are_ignored_not_stored(client):
    response = client.patch("/api/settings", json={"values": {"not_a_setting": "x"}})
    assert response.status_code == 200
    assert "not_a_setting" not in response.json()


def test_manifest_import_accepts_both_shapes(client, project):
    from flowpipe.models import Manifest

    payload = Manifest.model_validate(projects.read_manifest(project).model_dump()).model_dump()
    bare = client.post(f"/api/projects/{project}/manifest/import", json=payload)
    assert bare.status_code == 200, bare.text
    wrapped = client.post(f"/api/projects/{project}/manifest/import", json={"raw": json.dumps(payload)})
    assert wrapped.status_code == 200, wrapped.text
    empty = client.post(f"/api/projects/{project}/manifest/import", json={})
    assert empty.status_code == 422 and "raw" in empty.json()["detail"]

def test_run_without_a_body_is_accepted(client, project):
    """`curl -X POST .../run` must work; the UI sends a body but the API need not."""
    from flowpipe import projects, runner

    manifest = projects.read_manifest(project)
    manifest.scenes[0].narration = ""
    projects.write_manifest(project, manifest)
    response = client.post(f"/api/projects/{project}/run")
    assert response.status_code == 200, response.text
    runner.cancel(project)

def test_a_project_is_locked_to_one_process(client, project):
    """Two processes on one data dir must not run the same project's jobs at once.

    The dashboard plus a script (demo_seed, a second server) used to process the
    same project concurrently, which ran an image job before its character job
    finished and paid for work twice.
    """
    from flowpipe import db, runner

    owner = runner._owner()
    assert db.try_lock(project, owner) is True
    assert db.try_lock(project, owner) is True          # re-entrant for the holder
    assert db.try_lock(project, f"other-host:{os.getpid()}") is False
    db.release_lock(project, owner)
    assert db.try_lock(project, f"other-host:{os.getpid()}") is True


def test_a_dead_process_loses_its_lock(client, project):
    """A crashed run must not block the project forever."""
    from flowpipe import db

    db.execute(
        "INSERT OR REPLACE INTO project_locks (project_id, owner, pid, heartbeat)"
        " VALUES (?,?,?,datetime('now'))",
        (project, "gone-host:999999", 999999),
    )
    assert db.try_lock(project, "new-host:1") is True


def test_the_header_pill_is_repainted_when_settings_are_rendered():
    """Saving driver = gflow must repaint the header pill, not need a reload.

    renderSettings() refetches /api/health, but the pill is drawn by
    renderStatusPills(); without that call the header kept showing the driver
    from page load ("driver: mock") after a successful save.
    """
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent / "flowpipe" / "web" / "app.js").read_text()
    start = source.index("async function renderSettings()")
    body = source[start:source.index("\n}\n", start)]
    assert "state.health = health" in body
    assert "renderStatusPills()" in body, "renderSettings must repaint the status pills"


def test_the_environment_beats_a_saved_driver(tmp_path, monkeypatch):
    """`set FLOWPIPE_DRIVER=mock` must undo a saved driver = gflow.

    docs/GOING_LIVE.md offers exactly that as the way back to the mock driver for
    one session, but load() applied the environment *before* settings.json, so the
    stored value won and the switch silently did nothing.
    """
    from flowpipe.config import Settings

    settings = Settings()
    settings.data_dir = tmp_path
    settings.update({"driver": "gflow"})
    assert json.loads(Path(settings.settings_path).read_text())["driver"] == "gflow"

    monkeypatch.setenv("FLOWPIPE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("FLOWPIPE_DRIVER", "mock")
    loaded = Settings.load()
    assert loaded.driver == "mock"

    monkeypatch.delenv("FLOWPIPE_DRIVER")
    assert Settings.load().driver == "gflow"      # the stored value is still gflow


def test_every_batch_file_passes_the_linter():
    """A new script that is not linted is how a broken `goto` reaches a terminal."""
    import subprocess
    import sys as _sys
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parent.parent
    batches = sorted(root.glob("*.bat")) + sorted((root / "scripts").rglob("*.bat"))
    assert batches, "no batch files found"
    proc = subprocess.run(
        [_sys.executable, str(root / "scripts" / "win" / "check_bat.py")],
        capture_output=True, text=True, timeout=120,
    )
    assert "missing" not in proc.stdout, proc.stdout
    assert proc.returncode == 0, proc.stdout + proc.stderr
    # the linter must cover the same files this test found
    for path in batches:
        assert path.name in proc.stdout, f"{path.name} was not linted"


def test_the_reset_script_refuses_to_delete_anything_but_an_install():
    """It calls rmdir /s /q; a stray FLOWPIPE_TOOLS_DIR must not become a wiped drive."""
    from pathlib import Path as _Path

    text = (_Path(__file__).resolve().parent.parent
            / "scripts" / "win" / "reset.bat").read_text(encoding="utf-8", errors="replace")
    assert "goto :bad_target" in text          # empty path / bare drive
    assert ":bad_tools" in text                # tools dir without Python311
    assert "Python311\\python.exe" in text     # the install marker it insists on
    # only these two targets are ever deleted, and both come from the tools root
    destructive = [line for line in text.splitlines()
                   if "rmdir" in line.lower() and "rem " not in line.lower()]
    assert len(destructive) == 2, destructive
    for line in destructive:
        assert "%TOOLS%" in line or "%FLOWPIPE_DATA_DIR%" in line
