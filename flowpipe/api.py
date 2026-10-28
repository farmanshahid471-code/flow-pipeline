"""FastAPI app: JSON API + the single-page dashboard."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Literal

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import accounts, assemble, db, projects, runner, stage_a, toolchain
from .config import SETTINGS, SettingsError
from .flow import get_driver
from .models import Manifest, now_iso
from .voice import stage as narration

WEB_DIR = Path(__file__).resolve().parent / "web"

app = FastAPI(title="flow-pipeline", version="0.1.0")


@app.on_event("startup")
def _startup() -> None:
    db.init_db()
    SETTINGS.ensure_dirs()
    runner.ensure_workers()


# ------------------------------------------------------------------ schemas --
class ProjectCreate(BaseModel):
    topic: str = Field(min_length=3)
    template_id: str = "cinematic-short"
    scene_count: int = 5
    target_duration: int = 30
    style: str = "cinematic, photoreal, shallow depth of field"
    tone: str = "warm and curious"
    aspect_ratio: str | None = None
    extra: str = ""
    mode: Literal["api", "manual"] = "api"


class StageARequest(BaseModel):
    mode: Literal["api", "manual"] = "api"
    model: str | None = None


class RunRequest(BaseModel):
    kinds: list[Literal["character", "image", "clip"]] | None = None
    force: bool = False
    only: list[str] | None = None  # specific target ids


class AssembleRequest(BaseModel):
    music_file: str | None = None      # relative path inside the project folder
    voiceover_file: str | None = None
    width: int | None = None
    height: int | None = None
    fps: int | None = None
    crf: int | None = None
    use_narration: bool = True         # mix the generated narration, when there is one
    captions: Literal["off", "sidecar", "burn"] | None = None


class NarrationRequest(BaseModel):
    voice: str | None = None
    speed: float | None = None
    aligner: Literal["proportional", "whisperx"] | None = None
    language: str | None = None
    scenes: list[str] | None = None    # re-do only these scenes
    wait: bool = False                 # block until done (tests, scripts)


class PreviewRequest(BaseModel):
    text: str = Field(min_length=1, max_length=600)
    voice: str | None = None
    speed: float | None = None


class AccountCreate(BaseModel):
    label: str
    profile: str = ""
    notes: str = ""


class AccountPatch(BaseModel):
    label: str | None = None
    profile: str | None = None
    notes: str | None = None
    status: str | None = None
    enabled: bool | None = None


class SettingsPatch(BaseModel):
    values: dict[str, Any]


# ------------------------------------------------------------------ helpers --
def _project_or_404(project_id: str) -> dict[str, Any]:
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(404, "project not found")
    return project


def _manifest_or_404(project_id: str) -> Manifest:
    try:
        manifest = projects.read_manifest(project_id)
    except projects.ManifestError as exc:
        raise HTTPException(422, str(exc)) from exc
    if manifest is None:
        raise HTTPException(409, "this project has no manifest yet (run Stage A)")
    return manifest


def _manifest_or_none(project_id: str) -> Manifest | None:
    """Read a manifest for read-only views: a broken file is reported, not fatal."""
    return projects.read_manifest_or_none(project_id)


def _inside(root: Path, candidate: Path) -> bool:
    """Windows-safe containment check (case differences included)."""
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _input_file(root: Path, relative: str | None) -> Path | None:
    if not relative:
        return None
    target = (root / relative).resolve()
    if not _inside(root, target):
        raise HTTPException(400, "audio path escapes the project folder")
    return target


def _media_url(project_id: str, path: str | Path) -> str | None:
    """Turn an absolute asset path into the /api/media/... URL the UI uses."""
    root = projects.project_dir(project_id).resolve()
    try:
        relative = Path(path).resolve().relative_to(root)
    except (ValueError, OSError):
        return None
    return f"/api/media/{project_id}/{relative.as_posix()}"


def _safe_project_path(project_id: str, relative: str) -> Path:
    root = projects.project_dir(project_id).resolve()
    target = (root / relative).resolve()
    if not _inside(root, target):
        raise HTTPException(400, "path escapes the project folder")
    if not target.exists():
        raise HTTPException(404, "file not found")
    return target


# ------------------------------------------------------------------- basics --
# --------------------------------------------------------------- narration ---
def _clip_durations(project_id: str) -> dict[str, float]:
    """How long each scene's clip really is, so narration can be fitted to it."""
    durations: dict[str, float] = {}
    for asset in db.list_assets(project_id, "clip"):
        path = Path(asset["path"])
        if path.exists():
            seconds = assemble.probe_duration(path)
            if seconds > 0:
                durations[asset["target_id"]] = seconds
    return durations


def _subtitle_path(project_id: str, project: dict[str, Any]) -> str | None:
    if not project.get("output_path"):
        return None
    candidate = Path(project["output_path"]).with_suffix(".srt")
    return str(candidate) if candidate.exists() else None


def narration_summary(project_id: str) -> dict[str, Any]:
    """Cheap narration state for the project card (no word lists)."""
    plan = narration.load(project_id)
    state = narration.state(project_id)
    if not plan:
        manifest = _manifest_or_none(project_id)
        return {
            "exists": False,
            "state": state.get("state", "idle"),
            "error": state.get("error", ""),
            "message": state.get("message", ""),
            "script": narration.narration_source(manifest) if manifest else "none",
            "provider": SETTINGS.tts_provider,
            "scenes": 0,
        }
    return {
        "exists": True,
        "state": state.get("state", "idle"),
        "error": state.get("error", ""),
        "message": state.get("message", ""),
        "provider": plan["provider"],
        "voice": plan["voice"],
        "aligner": plan["aligner"],
        "source": plan["source"],
        "scenes": len(plan["scenes"]),
        "missing_audio": [row["scene_id"] for row in plan["scenes"] if not row["exists"]],
        "generated_at": plan["generated_at"],
    }


# The narration panel polls this endpoint, and probing the voice server means an
# HTTP round trip (and probing whisperX means importing torch). Both answers are
# cached briefly; ?refresh=1 forces a real check.
_VOICE_STATUS_TTL = 15.0
_VOICE_STATUS_CACHE: dict[str, Any] = {}
_VOICE_STATUS_LOCK = threading.Lock()


def _engine_status(backend: Any, provider: str) -> dict[str, Any]:
    """Is the engine the settings name actually usable on that server?

    VoiceStudio can be reachable while the configured engine is not: the default
    OmniVoice engine needs torch/torchaudio (a missing VC++ runtime breaks it) and
    an accepted model licence. Reporting that, plus what *is* available, is the
    difference between "reachable" and "will actually speak".
    """
    if provider not in {"voicestudio", "voice-studio", "omnivoice"}:
        return {}
    engines = getattr(backend, "engines", lambda: [])()
    if not engines:
        return {"engines": [], "engine_available": None,
                "engine_hint": "the server did not report any engines"}
    wanted = (SETTINGS.tts_model or "").strip().lower()
    chosen = next((row for row in engines if row["id"].lower() == wanted), None)
    usable = [row["id"] for row in engines if row["available"]]
    out: dict[str, Any] = {
        "engines": [{"id": row["id"], "name": row["name"], "available": row["available"]}
                    for row in engines],
        "available_engines": usable,
        "engine_available": None if chosen is None else chosen["available"],
        "engine_reason": (chosen or {}).get("reason", "")[:400],
    }
    if chosen is None:
        out["engine_hint"] = (f"the server does not know an engine called {SETTINGS.tts_model!r}; "
                              f"it offers: {', '.join(row['id'] for row in engines)}")
    elif not chosen["available"]:
        out["engine_hint"] = (
            f"engine {chosen['id']!r} is not usable on this server"
            + (f": {chosen['reason'][:300]}" if chosen["reason"] else "")
            + (f". Try tts_model = {usable[0]}" if usable else
               ". No engine is available, so narration cannot run yet"))
    return out


@app.get("/api/voice/status")
def voice_status(refresh: bool = Query(False)) -> dict[str, Any]:
    """What the narration stage will use right now — shown in Settings."""
    from .voice import align, voicestudio

    provider = (SETTINGS.tts_provider or "off").lower()
    health: dict[str, Any] = {
        "provider": provider,
        "voice": SETTINGS.tts_voice,
        "model": SETTINGS.tts_model,
        "format": SETTINGS.tts_format,
        "speed": SETTINGS.tts_speed,
        "url": SETTINGS.voicestudio_url,
        "dir": SETTINGS.voicestudio_dir or (
            str(toolchain.tools_root() / "voicestudio") if toolchain.tools_root() else ""),
        "aligner": SETTINGS.align_provider,
        "whisperx_model": SETTINGS.whisperx_model,
        "captions": SETTINGS.captions,
        "narration_max_speedup": SETTINGS.narration_max_speedup,
        "caption_max_chars": SETTINGS.caption_max_chars,
        "language": SETTINGS.narration_language,
    }
    stamp = time.monotonic()
    with _VOICE_STATUS_LOCK:
        cached = dict(_VOICE_STATUS_CACHE)
    usable = (not refresh and cached
              and stamp - cached.get("at", 0.0) < _VOICE_STATUS_TTL
              and cached.get("key") == (provider, SETTINGS.voicestudio_url))
    if usable:
        health.update(cached["backend"])
        health["whisperx"] = cached["whisperx"]
        health["cached"] = True
        return health

    try:
        backend = voicestudio.backend_from_settings()
    except Exception as exc:
        status = {"ok": False, "detail": str(exc), "backend": provider}
    else:
        ok, detail = backend.available()
        status = {"ok": ok, "detail": detail, "backend": getattr(backend, "name", provider)}
        status.update(_engine_status(backend, provider))
    whisper_ok, whisper_detail = align.whisperx_available(refresh=refresh)
    whisper = {"ok": whisper_ok, "detail": whisper_detail}
    health.update(status)
    health["whisperx"] = whisper
    health["cached"] = False
    with _VOICE_STATUS_LOCK:
        _VOICE_STATUS_CACHE.update({"at": stamp, "key": (provider, SETTINGS.voicestudio_url),
                                    "backend": status, "whisperx": whisper})
    return health


@app.get("/api/tts/voices")
def tts_voices() -> dict[str, Any]:
    from .voice import voicestudio

    try:
        backend = voicestudio.backend_from_settings()
    except Exception as exc:
        raise HTTPException(503, str(exc)) from exc
    return {"backend": getattr(backend, "name", ""), "voices": backend.voices()}


@app.post("/api/tts/preview")
def tts_preview(payload: PreviewRequest) -> FileResponse:
    """Say a line with the configured voice, so you can pick one before a run."""
    from .voice import voicestudio

    try:
        backend = voicestudio.backend_from_settings()
    except Exception as exc:
        raise HTTPException(503, str(exc)) from exc
    staging = Path(SETTINGS.flow_downloads_dir) / "_preview"
    staging.mkdir(parents=True, exist_ok=True)
    try:
        result = backend.synthesize(payload.text, staging / "preview.wav",
                                   voice=payload.voice or SETTINGS.tts_voice,
                                   speed=payload.speed or SETTINGS.tts_speed)
    except Exception as exc:  # VoiceError, or a server that went away
        raise HTTPException(502, str(exc)) from exc
    return FileResponse(str(result.path), media_type="audio/wav", filename="preview.wav")


@app.get("/api/projects/{project_id}/narration")
def get_narration(project_id: str) -> dict[str, Any]:
    _project_or_404(project_id)
    payload = narration.describe(project_id, _clip_durations(project_id))
    payload["state"] = narration.state(project_id)
    payload["srt"] = narration.srt_text(project_id, _clip_durations(project_id))
    return payload


@app.post("/api/projects/{project_id}/narration")
def start_narration(project_id: str, payload: NarrationRequest | None = None) -> dict[str, Any]:
    """Text-to-speech + word timestamps for every scene that has a narration line.

    Runs in a background thread: the first call loads the model, which takes a
    while, and the browser polls ``GET /api/projects/{id}/narration``.
    """
    _project_or_404(project_id)
    manifest = _manifest_or_404(project_id)
    payload = payload or NarrationRequest()
    if not narration.script_for(manifest):
        raise HTTPException(
            422,
            "this manifest has no narration script yet. Stage A writes one per scene "
            "from the master prompt - regenerate the manifest, or add `narration` to the "
            "scenes in the Manifest tab.",
        )
    current = narration.state(project_id)
    if current.get("state") == "running" and not payload.wait:
        raise HTTPException(409, "narration is already running for this project")

    from .voice import voicestudio

    try:
        backend = voicestudio.backend_from_settings()
    except Exception as exc:
        raise HTTPException(503, str(exc)) from exc

    def work() -> None:
        narration.mark_running(project_id)
        try:
            narration.synthesize(
                project_id,
                backend=backend,
                voice=payload.voice or SETTINGS.tts_voice,
                speed=payload.speed,
                align_provider=payload.aligner or SETTINGS.align_provider,
                language=payload.language or SETTINGS.narration_language,
                progress=lambda message: narration.mark_progress(project_id, message),
                only=payload.scenes,
            )
        except Exception as exc:  # surfaced through the status endpoint
            db.log(f"narration failed: {exc}", project_id=project_id, level="error")
            narration.mark_done(project_id, error=str(exc))
        else:
            narration.mark_done(project_id)

    if payload.wait:
        work()
        summary = narration_summary(project_id)
        # same shape as GET /narration: `state` is the status object, not a string
        summary["state"] = narration.state(project_id)
        return summary
    narration.mark_running(project_id)
    threading.Thread(target=work, name=f"narration-{project_id}", daemon=True).start()
    return {"state": "running"}


@app.delete("/api/projects/{project_id}/narration")
def delete_narration(project_id: str) -> dict[str, Any]:
    _project_or_404(project_id)
    narration.delete(project_id)
    db.log("narration files removed", project_id=project_id)
    return {"deleted": True}


@app.get("/api/projects/{project_id}/narration/srt", response_class=None)
def download_srt(project_id: str) -> Any:
    _project_or_404(project_id)
    text = narration.srt_text(project_id, _clip_durations(project_id))
    if not text:
        raise HTTPException(404, "no narration timings yet - generate narration first")
    return _plain_text(text, "narration.srt")


def _plain_text(body: str, filename: str):
    from fastapi import Response

    return Response(content=body, media_type="text/plain; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/api/health")
def health() -> dict[str, Any]:
    driver = get_driver()
    try:
        caps = driver.capabilities().to_dict()
    except Exception as exc:  # driver may be unavailable (no gflow on PATH)
        caps = {"driver": driver.name, "error": str(exc)}
    return {
        "ok": True,
        "driver": driver.name,
        "tools": toolchain.status(),
        "capabilities": caps,
        "anthropic_key_present": bool(SETTINGS.anthropic_api_key),
        "data_dir": str(SETTINGS.data_dir),
        "downloads_dir": str(SETTINGS.downloads_dir),
        "downloads": {
            "root": str(SETTINGS.downloads_dir),
            "flow": str(SETTINGS.flow_downloads_dir),
            "final": str(SETTINGS.final_downloads_dir),
            "copy_final": bool(SETTINGS.copy_final_to_downloads),
        },
    }


@app.get("/api/settings")
def get_settings() -> dict[str, Any]:
    return SETTINGS.public()


@app.patch("/api/settings")
def patch_settings(patch: SettingsPatch) -> dict[str, Any]:
    values = dict(patch.values)
    api_key = values.pop("anthropic_api_key", None)
    voice_key = values.pop("tts_api_key", None)
    try:
        SETTINGS.update(values)
    except SettingsError as exc:
        # nothing was changed, so the dashboard keeps working
        raise HTTPException(400, str(exc)) from exc
    if api_key:
        # Held in process memory only; never written to settings.json.
        SETTINGS.anthropic_api_key = str(api_key).strip()
    if voice_key:
        SETTINGS.tts_api_key = str(voice_key).strip()
    return SETTINGS.public()


@app.get("/api/capabilities")
def capabilities() -> dict[str, Any]:
    return get_driver().capabilities().to_dict()


@app.get("/api/templates")
def list_templates() -> list[dict[str, Any]]:
    from .prompts import list_templates as _list

    return [t.to_dict() for t in _list()]


@app.post("/api/templates")
def save_template(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    template_id = str(payload.get("id") or "").strip()
    body = str(payload.get("body") or "").strip()
    if not template_id or not body:
        raise HTTPException(400, "id and body are required")
    from .prompts import templates_dir

    path = templates_dir() / f"{template_id}.json"
    path.write_text(json.dumps({
        "id": template_id,
        "name": payload.get("name") or template_id,
        "description": payload.get("description", ""),
        "body": body,
    }, indent=2), encoding="utf-8")
    return {"id": template_id, "path": str(path)}


@app.delete("/api/templates/{template_id}")
def delete_template(template_id: str) -> dict[str, Any]:
    from .prompts import templates_dir

    path = templates_dir() / f"{template_id}.json"
    if path.exists():
        path.unlink()
    return {"deleted": template_id}


# ----------------------------------------------------------------- projects --
@app.get("/api/projects")
def list_projects() -> list[dict[str, Any]]:
    rows = db.list_projects()
    for row in rows:
        row["stats"] = runner.stats(row["id"])
        manifest = _manifest_or_none(row["id"])
        row["scene_count"] = len(manifest.scenes) if manifest else 0
        row["character_count"] = len(manifest.characters) if manifest else 0
        # a hand-edited or half-written project.json is shown as a warning on
        # the card instead of breaking the whole list
        row["manifest_error"] = projects.manifest_error(row["id"])
    return rows


@app.post("/api/projects")
def create_project(payload: ProjectCreate) -> dict[str, Any]:
    project_id = projects.new_id(payload.topic)
    db.create_project(project_id, payload.topic, payload.template_id)
    projects.ensure_project_dir(project_id)
    prompt = stage_a.build_prompt(
        topic=payload.topic,
        template_id=payload.template_id,
        scene_count=payload.scene_count,
        target_duration=payload.target_duration,
        style=payload.style,
        tone=payload.tone,
        aspect_ratio=payload.aspect_ratio or SETTINGS.aspect_ratio,
        extra=payload.extra,
    )
    (projects.ensure_project_dir(project_id) / "stage-a-prompt.txt").write_text(prompt, encoding="utf-8")
    db.log(f"project created: {payload.topic}", project_id=project_id)

    response: dict[str, Any] = {"id": project_id, "mode": payload.mode}
    if payload.mode == "manual":
        path = stage_a.write_manual_request(project_id, prompt)
        db.update_project(project_id, status="awaiting_manifest")
        response["manual_request"] = str(path)
        response["prompt"] = prompt
        return response

    try:
        manifest, raw = stage_a.generate_via_api(prompt=prompt, model=SETTINGS.anthropic_model)
    except Exception as exc:  # StageAError, or something unexpected inside the SDK
        if not isinstance(exc, stage_a.StageAError):
            exc = stage_a.StageAError(f"Stage A failed: {type(exc).__name__}: {exc}")
        path = stage_a.write_manual_request(project_id, prompt)
        db.update_project(project_id, status="awaiting_manifest", error=str(exc))
        db.log(f"Stage A via API failed: {exc}", project_id=project_id, level="error")
        return JSONResponse(
            status_code=502,
            content={
                "id": project_id,
                "mode": "api",
                "error": str(exc),
                "manual_request": str(path),
                "prompt": prompt,
            },
        )
    projects.write_manifest(project_id, manifest)
    db.update_project(project_id, status="manifest_ready")
    db.log(f"manifest generated: {len(manifest.characters)} characters, {len(manifest.scenes)} scenes",
           project_id=project_id)
    (projects.ensure_project_dir(project_id) / "stage-a-reply.txt").write_text(raw, encoding="utf-8")
    return {**response, "manifest": manifest.model_dump()}


@app.get("/api/projects/{project_id}")
def get_project(project_id: str) -> dict[str, Any]:
    project = _project_or_404(project_id)
    manifest = _manifest_or_none(project_id)
    jobs = db.list_jobs(project_id)
    active = project.get("active_account")
    output_url = _media_url(project_id, project["output_path"]) if project.get("output_path") else None
    subtitle = _subtitle_path(project_id, project)
    download_path = None
    if manifest is not None:
        candidate = SETTINGS.final_downloads_dir / assemble.download_name(project_id, manifest)
        if candidate.exists():
            download_path = str(candidate)
    return {
        "project": project,
        "output_url": output_url,
        "download_path": download_path,
        "subtitle_url": _media_url(project_id, subtitle) if subtitle else None,
        "narration": narration_summary(project_id),
        "manifest": manifest.model_dump() if manifest else None,
        "manifest_error": projects.manifest_error(project_id),
        "jobs": jobs,
        "stats": runner.stats(project_id),
        "assets": _gallery(project_id, manifest),
        "active_account": db.get_account(active) if active else None,
        "pending_switch": db.pending_switch_request(project_id),
    }


@app.delete("/api/projects/{project_id}")
def delete_project(project_id: str) -> dict[str, Any]:
    """Remove a project from the index. Media on disk is left alone by default."""

    _project_or_404(project_id)
    db.execute("DELETE FROM jobs WHERE project_id=?", (project_id,))
    db.execute("DELETE FROM assets WHERE project_id=?", (project_id,))
    db.execute("DELETE FROM uploads WHERE project_id=?", (project_id,))
    db.execute("DELETE FROM entities WHERE project_id=?", (project_id,))
    db.execute("DELETE FROM flow_projects WHERE project_id=?", (project_id,))
    db.execute("DELETE FROM projects WHERE id=?", (project_id,))
    db.log("project deleted from the index; files kept on disk", project_id=None)
    return {"deleted": project_id, "files": str(projects.project_dir(project_id))}


@app.get("/api/projects/{project_id}/manifest")
def get_manifest(project_id: str) -> dict[str, Any]:
    _project_or_404(project_id)
    return _manifest_or_404(project_id).model_dump()


@app.put("/api/projects/{project_id}/manifest")
def put_manifest(project_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    _project_or_404(project_id)
    try:
        manifest = Manifest.model_validate(payload)
    except Exception as exc:
        raise HTTPException(422, f"invalid manifest: {exc}") from exc
    projects.write_manifest(project_id, manifest)
    db.update_project(project_id, status="manifest_ready", error=None)
    db.log("manifest edited by operator", project_id=project_id)
    return manifest.model_dump()


@app.post("/api/projects/{project_id}/manifest/import")
def import_manifest(project_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Accept Claude's raw reply, or a manifest object pasted straight in.

    Both shapes are legitimate: the UI sends {"raw": "<what Claude printed>"},
    a script would post the manifest itself.
    """
    _project_or_404(project_id)
    candidate = payload.get("raw") or payload.get("manifest")
    if candidate is None and ("scenes" in payload or "project" in payload):
        candidate = payload
    if not candidate:
        raise HTTPException(
            422, 'send {"raw": "<the JSON Claude printed>"} or the manifest object itself')
    try:
        manifest = stage_a.import_manifest(project_id, candidate)
    except Exception as exc:
        raise HTTPException(422, f"could not import manifest: {exc}") from exc
    db.update_project(project_id, status="manifest_ready", error=None)
    db.log("manifest imported from a manual Stage A run", project_id=project_id)
    return manifest.model_dump()


@app.get("/api/projects/{project_id}/prompt")
def get_prompt(project_id: str) -> dict[str, Any]:
    _project_or_404(project_id)
    path = projects.project_dir(project_id) / "stage-a-prompt.txt"
    manual = projects.requests_dir(project_id) / "stage-a-prompt.md"
    return {
        "prompt": path.read_text(encoding="utf-8") if path.exists() else "",
        "manual_request": manual.read_text(encoding="utf-8") if manual.exists() else "",
    }


@app.post("/api/projects/{project_id}/stage-a")
def rerun_stage_a(project_id: str, payload: StageARequest) -> dict[str, Any]:
    _project_or_404(project_id)
    path = projects.project_dir(project_id) / "stage-a-prompt.txt"
    if not path.exists():
        raise HTTPException(409, "no stored Stage A prompt for this project")
    prompt = path.read_text(encoding="utf-8")
    if payload.mode == "manual":
        target = stage_a.write_manual_request(project_id, prompt)
        db.update_project(project_id, status="awaiting_manifest")
        return {"mode": "manual", "manual_request": str(target), "prompt": prompt}
    try:
        manifest, raw = stage_a.generate_via_api(prompt=prompt, model=payload.model)
    except stage_a.StageAError as exc:
        db.update_project(project_id, status="awaiting_manifest", error=str(exc))
        raise HTTPException(502, str(exc)) from exc
    except Exception as exc:  # never a bare 500 from the SDK
        message = f"Stage A failed: {type(exc).__name__}: {exc}"
        db.update_project(project_id, status="awaiting_manifest", error=message)
        raise HTTPException(502, message) from exc
    projects.write_manifest(project_id, manifest)
    (projects.ensure_project_dir(project_id) / "stage-a-reply.txt").write_text(raw, encoding="utf-8")
    db.update_project(project_id, status="manifest_ready", error=None)
    db.log("manifest regenerated", project_id=project_id)
    return {"mode": "api", "manifest": manifest.model_dump()}


# --------------------------------------------------------------------- runs --
@app.post("/api/projects/{project_id}/run")
def run_project(project_id: str, payload: RunRequest | None = None) -> dict[str, Any]:
    _project_or_404(project_id)
    # A body is optional: `curl -X POST .../run` and the Run button both work.
    payload = payload or RunRequest()
    try:
        result = runner.start(project_id, payload.kinds, force=payload.force)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return result


@app.post("/api/projects/{project_id}/pause")
def pause_project(project_id: str) -> dict[str, Any]:
    _project_or_404(project_id)
    runner.pause(project_id)
    return {"status": "paused"}


@app.post("/api/projects/{project_id}/resume")
def resume_project(project_id: str) -> dict[str, Any]:
    _project_or_404(project_id)
    runner.resume(project_id)
    return {"status": "running"}


@app.post("/api/projects/{project_id}/cancel")
def cancel_project(project_id: str) -> dict[str, Any]:
    _project_or_404(project_id)
    runner.cancel(project_id)
    return {"status": "cancelled"}


@app.post("/api/projects/{project_id}/assemble")
def assemble_project(project_id: str, payload: AssembleRequest | None = None) -> dict[str, Any]:
    _project_or_404(project_id)
    payload = payload or AssembleRequest()
    root = projects.project_dir(project_id).resolve()
    music = _input_file(root, payload.music_file)
    voice = _input_file(root, payload.voiceover_file)
    try:
        result = assemble.assemble(
            project_id, music_path=music, voiceover_path=voice,
            width=payload.width, height=payload.height, fps=payload.fps, crf=payload.crf,
            use_narration=payload.use_narration, captions=payload.captions,
        )
    except assemble.AssemblyBusy as exc:
        raise HTTPException(409, str(exc)) from exc
    except assemble.AssemblyError as exc:
        db.update_project(project_id, status="needs_review", error=str(exc))
        raise HTTPException(422, str(exc)) from exc
    return result


@app.post("/api/projects/{project_id}/upload")
async def upload_input(project_id: str, request: Request) -> dict[str, Any]:
    """Store a music bed / voiceover / end-frame image inside the project folder."""
    _project_or_404(project_id)
    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "filename"):
        raise HTTPException(400, "multipart field 'file' is required")
    sub = str(form.get("kind") or "inputs")
    if sub not in {"inputs", "music", "voiceover"}:
        raise HTTPException(400, "invalid kind")
    target_dir = projects.ensure_project_dir(project_id) / sub
    target_dir.mkdir(parents=True, exist_ok=True)
    name = Path(upload.filename).name
    target = target_dir / name
    target.write_bytes(await upload.read())
    return {"path": f"{sub}/{name}", "size": target.stat().st_size}


# --------------------------------------------------------------------- jobs --
@app.post("/api/jobs/{job_id}/retry")
def retry_job(job_id: int) -> dict[str, Any]:
    job = runner.retry_job(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return job


@app.post("/api/jobs/{job_id}/skip")
def skip_job(job_id: int) -> dict[str, Any]:
    if not db.get_job(job_id):
        raise HTTPException(404, "job not found")
    runner.skip_job(job_id)
    return {"status": "skipped"}


@app.post("/api/jobs/{job_id}/regenerate")
def regenerate_job(job_id: int) -> dict[str, Any]:
    """Regenerate just this character / still / clip (a new versioned asset)."""
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    runner.retry_job(job_id)
    if job["kind"] == "character":
        # dependent stills were built on the old reference: flag them
        db.execute(
            "UPDATE jobs SET status='queued', updated_at=? WHERE project_id=? AND kind IN ('image','clip')"
            " AND status='done'",
            (now_iso(), job["project_id"]),
        )
    if job["kind"] == "image":
        db.execute(
            "UPDATE jobs SET status='queued', updated_at=? WHERE project_id=? AND kind='clip'"
            " AND target_id=? AND status='done'",
            (now_iso(), job["project_id"], job["target_id"]),
        )
    return {"status": "queued", "job_id": job_id}


@app.get("/api/projects/{project_id}/logs")
def project_logs(project_id: str, after_id: int = Query(0), limit: int = Query(200)) -> list[dict[str, Any]]:
    _project_or_404(project_id)
    return db.tail(project_id, limit=limit, after_id=after_id)


@app.get("/api/logs")
def all_logs(after_id: int = Query(0), limit: int = Query(200)) -> list[dict[str, Any]]:
    return db.tail(None, limit=limit, after_id=after_id)


# ------------------------------------------------------------------- assets --
def _gallery(project_id: str, manifest: Manifest | None) -> dict[str, Any]:
    assets = db.list_assets(project_id)
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for asset in assets:
        latest[(asset["kind"], asset["target_id"])] = asset

    def url(asset: dict[str, Any] | None) -> str | None:
        return _media_url(project_id, asset["path"]) if asset else None

    characters = []
    if manifest:
        for character in manifest.characters:
            asset = latest.get(("character", character.id))
            characters.append({
                "id": character.id, "name": character.name,
                "description": character.description,
                "image": url(asset), "path": asset["path"] if asset else None,
                "account_id": asset["account_id"] if asset else None,
            })
    scenes = []
    if manifest:
        for scene in manifest.ordered_scenes():
            image = latest.get(("image", scene.id))
            clip = latest.get(("clip", scene.id))
            scenes.append({
                "id": scene.id, "order": scene.order, "title": scene.title or scene.id,
                "character_ids": scene.character_ids,
                "image": url(image), "image_path": image["path"] if image else None,
                "clip": url(clip), "clip_path": clip["path"] if clip else None,
                "account_id": (clip or image or {}).get("account_id"),
            })
    return {"characters": characters, "scenes": scenes}


@app.get("/api/projects/{project_id}/assets")
def list_assets(project_id: str) -> dict[str, Any]:
    _project_or_404(project_id)
    return _gallery(project_id, _manifest_or_none(project_id))


@app.get("/api/projects/{project_id}/assets/history")
def asset_history(project_id: str) -> list[dict[str, Any]]:
    _project_or_404(project_id)
    return [{**row, "url": _media_url(project_id, row["path"])}
            for row in db.list_assets(project_id)]


@app.api_route("/api/media/{project_id}/{relative:path}", methods=["GET", "HEAD"])
def media(project_id: str, relative: str) -> FileResponse:
    _project_or_404(project_id)
    path = _safe_project_path(project_id, relative)
    return FileResponse(path)


# ----------------------------------------------------------------- accounts --
@app.get("/api/accounts")
def list_accounts() -> list[dict[str, Any]]:
    rows = accounts.decorated(db.list_accounts())
    usage = {row["id"]: _account_usage(row["id"]) for row in rows}
    for row in rows:
        row.update(usage.get(row["id"], {}))
    return rows


def _account_usage(account_id: str) -> dict[str, Any]:
    rows = db.query(
        "SELECT kind, COUNT(*) AS n FROM assets WHERE account_id=? GROUP BY kind", (account_id,)
    )
    by_kind = {row["kind"]: row["n"] for row in rows}
    events = db.query(
        "SELECT action, created_at, reason FROM account_events WHERE account_id=? ORDER BY id DESC LIMIT 20",
        (account_id,),
    )
    return {"usage": by_kind, "events": events}


@app.post("/api/accounts")
def create_account(payload: AccountCreate) -> dict[str, Any]:
    try:
        return accounts.add_account(payload.label, payload.profile, payload.notes)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.patch("/api/accounts/{account_id}")
def patch_account(account_id: str, payload: AccountPatch) -> dict[str, Any]:
    if not db.get_account(account_id):
        raise HTTPException(404, "account not found")
    values = {k: v for k, v in payload.model_dump().items() if v is not None}
    return accounts.update_account(account_id, **values) or {}


@app.delete("/api/accounts/{account_id}")
def delete_account(account_id: str) -> dict[str, Any]:
    if not db.get_account(account_id):
        raise HTTPException(404, "account not found")
    accounts.remove_account(account_id)
    return {"deleted": account_id}


@app.post("/api/accounts/{account_id}/refresh")
def refresh_account(account_id: str, refresh: bool = Query(False)) -> dict[str, Any]:
    if not db.get_account(account_id):
        raise HTTPException(404, "account not found")
    return accounts.refresh_credits(account_id, refresh=refresh, force=True)


@app.post("/api/accounts/{account_id}/renew")
def renew_account(account_id: str) -> dict[str, Any]:
    if not db.get_account(account_id):
        raise HTTPException(404, "account not found")
    return accounts.renew_session(account_id)


@app.post("/api/accounts/{account_id}/mark-active")
def mark_account_active(account_id: str) -> dict[str, Any]:
    if not db.get_account(account_id):
        raise HTTPException(404, "account not found")
    accounts.mark_active(account_id)
    return db.get_account(account_id) or {}


@app.get("/api/usage")
def usage(days: int = Query(14)) -> dict[str, Any]:
    """Per-day generation counts for the account page chart."""
    rows = db.query(
        "SELECT substr(created_at, 1, 10) AS day, kind, account_id, COUNT(*) AS n "
        "FROM assets GROUP BY day, kind, account_id ORDER BY day"
    )
    by_day: dict[str, dict[str, int]] = {}
    by_account: dict[str, dict[str, int]] = {}
    for row in rows:
        day = by_day.setdefault(row["day"], {"character": 0, "image": 0, "clip": 0})
        day[row["kind"]] = day.get(row["kind"], 0) + row["n"]
        acc = by_account.setdefault(row["account_id"] or "unknown", {})
        acc[row["kind"]] = acc.get(row["kind"], 0) + row["n"]
    series = sorted(by_day.items())[-days:]
    return {
        "series": [{"day": day, **counts} for day, counts in series],
        "by_account": by_account,
        "credits_per_clip": SETTINGS.credits_per_clip,
    }


@app.get("/api/accounts/events")
def account_events(limit: int = Query(100)) -> list[dict[str, Any]]:
    return db.account_events(limit=limit)


@app.get("/api/switch-requests")
def switch_requests(limit: int = Query(50)) -> list[dict[str, Any]]:
    return db.list_switch_requests(limit=limit)


@app.post("/api/switch-requests/{request_id}/approve")
def approve_switch(request_id: int) -> dict[str, Any]:
    try:
        return accounts.approve_switch(request_id, approve=True)
    except KeyError:
        raise HTTPException(404, "switch request not found") from None


@app.post("/api/switch-requests/{request_id}/deny")
def deny_switch(request_id: int) -> dict[str, Any]:
    try:
        return accounts.approve_switch(request_id, approve=False)
    except KeyError:
        raise HTTPException(404, "switch request not found") from None


# ---------------------------------------------------------------------- web --
@app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((WEB_DIR / "index.html").read_text(encoding="utf-8"))


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
