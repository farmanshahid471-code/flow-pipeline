"""FastAPI app: JSON API + the single-page dashboard."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import accounts, assemble, db, projects, runner, stage_a, toolchain
from .config import SETTINGS
from .flow import get_driver
from .models import Manifest, now_iso

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
    manifest = projects.read_manifest(project_id)
    if manifest is None:
        raise HTTPException(409, "this project has no manifest yet (run Stage A)")
    return manifest


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
    }


@app.get("/api/settings")
def get_settings() -> dict[str, Any]:
    return SETTINGS.public()


@app.patch("/api/settings")
def patch_settings(patch: SettingsPatch) -> dict[str, Any]:
    values = dict(patch.values)
    api_key = values.pop("anthropic_api_key", None)
    if api_key:
        # Held in process memory only; never written to settings.json.
        SETTINGS.anthropic_api_key = str(api_key).strip()
    SETTINGS.update(values)
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
        manifest = projects.read_manifest(row["id"])
        row["scene_count"] = len(manifest.scenes) if manifest else 0
        row["character_count"] = len(manifest.characters) if manifest else 0
    return rows


@app.post("/api/projects")
def create_project(payload: ProjectCreate) -> dict[str, Any]:
    project_id = projects.new_id(payload.topic)
    db.create_project(project_id, payload.topic, payload.template_id)
    projects.project_dir(project_id)
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
    (projects.project_dir(project_id) / "stage-a-prompt.txt").write_text(prompt, encoding="utf-8")
    db.log(f"project created: {payload.topic}", project_id=project_id)

    response: dict[str, Any] = {"id": project_id, "mode": payload.mode}
    if payload.mode == "manual":
        path = stage_a.write_manual_request(project_id, prompt)
        db.update_project(project_id, status="awaiting_manifest")
        response["manual_request"] = str(path)
        response["prompt"] = prompt
        return response

    try:
        manifest, raw = stage_a.generate_via_api(prompt, model=SETTINGS.anthropic_model)
    except stage_a.StageAError as exc:
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
    (projects.project_dir(project_id) / "stage-a-reply.txt").write_text(raw, encoding="utf-8")
    return {**response, "manifest": manifest.model_dump()}


@app.get("/api/projects/{project_id}")
def get_project(project_id: str) -> dict[str, Any]:
    project = _project_or_404(project_id)
    manifest = projects.read_manifest(project_id)
    jobs = db.list_jobs(project_id)
    active = project.get("active_account")
    output_url = _media_url(project_id, project["output_path"]) if project.get("output_path") else None
    return {
        "project": project,
        "output_url": output_url,
        "manifest": manifest.model_dump() if manifest else None,
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
    _project_or_404(project_id)
    try:
        manifest = stage_a.import_manifest(project_id, payload.get("raw") or payload.get("manifest"))
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
        manifest, raw = stage_a.generate_via_api(prompt, model=payload.model)
    except stage_a.StageAError as exc:
        raise HTTPException(502, str(exc)) from exc
    projects.write_manifest(project_id, manifest)
    (projects.project_dir(project_id) / "stage-a-reply.txt").write_text(raw, encoding="utf-8")
    db.update_project(project_id, status="manifest_ready", error=None)
    db.log("manifest regenerated", project_id=project_id)
    return {"mode": "api", "manifest": manifest.model_dump()}


# --------------------------------------------------------------------- runs --
@app.post("/api/projects/{project_id}/run")
def run_project(project_id: str, payload: RunRequest) -> dict[str, Any]:
    _project_or_404(project_id)
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
        )
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
    target_dir = projects.project_dir(project_id) / sub
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
    return _gallery(project_id, projects.read_manifest(project_id))


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
