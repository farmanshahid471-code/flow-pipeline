"""Job runner for Stages B–D.

A single DB-backed polling worker (configurable) executes Flow jobs. State lives
in SQLite, never in memory, so stopping the process cannot lose a queue and
resuming never re-submits a completed job.

Per-job error policy:

| error            | what happens                                                  |
|------------------|---------------------------------------------------------------|
| transient/locked | retry with backoff up to `max_attempts`, then needs_review     |
| auth expired     | one headless session renewal, then pause for a human          |
| challenge shown  | pause the project; a human finishes it in a real browser      |
| no credits       | mark account exhausted, request an account switch (§7)        |
| asset missing    | re-upload the reference into the current account and retry     |
| selector drift   | stop: Flow's UI moved and retrying only burns credits         |
"""
from __future__ import annotations

import os
import shutil
import threading
import time
import traceback
from pathlib import Path
from typing import Any

from . import accounts, db, projects
from .config import SETTINGS
from .flow import get_driver
from .flow.base import (
    Account,
    AssetMissing,
    AuthExpired,
    ChallengeRequired,
    FlowError,
    NoCredits,
    NoFlowAccess,
    ProfileLocked,
    ReferenceAsset,
    SelectorDrift,
    TransientFlowError,
)
from .models import Manifest, Scene, now_iso

KIND_ORDER = {"character": 0, "image": 1, "clip": 2}

_STOP = threading.Event()
_THREADS: list[threading.Thread] = []
_OWNER = ""
_LOCK = threading.RLock()
_BUSY: set[int] = set()
_STARTED = False
_LAST_LOOP_ERROR = ""


# ------------------------------------------------------------------ helpers --
def log(msg: str, project_id: str | None = None, job_id: int | None = None, level: str = "info") -> None:
    db.log(msg, project_id=project_id, job_id=job_id, level=level)
    print(f"[{level}] {project_id or '-'} {msg}", flush=True)


def ensure_project(project_id: str, account: Account) -> str:
    """Flow projects are per-account; mint one lazily per (project, account)."""
    existing = db.get_flow_project(project_id, account.id)
    if existing:
        return existing
    driver = get_driver()
    name = f"{SETTINGS.flow_project_prefix}-{project_id}"
    flow_project = driver.ensure_project(account, name)
    db.set_flow_project(project_id, account.id, flow_project)
    log(f"created Flow project {flow_project} on {account.label}", project_id)
    return flow_project


def upload_once(project_id: str, account: Account, path: Path) -> str:
    """Upload a local file into this account's Flow project, cached per account.

    Asset UUIDs do not survive an account switch, so the cache key includes the
    account. A failover therefore re-uploads automatically (step §7).
    """
    key = str(path.resolve())
    cached = db.get_upload(project_id, account.id, key)
    if cached:
        return cached
    driver = get_driver()
    asset_id = driver.upload(account, ensure_project(project_id, account), path)
    db.set_upload(project_id, account.id, key, asset_id)
    log(f"uploaded {path.name} -> {asset_id[:8]}.. on {account.label}", project_id)
    return asset_id


def staging_dir(project_id: str, kind: str, target_id: str) -> Path:
    """Where a Flow download lands first: <downloads>/flow/<project>/<kind>/<target>.

    Everything the pipeline downloads goes through here, so raw media never
    appears in a temp folder, the user profile or on another drive. The finished
    file is moved into the project library afterwards (same drive, so a rename).
    """
    path = SETTINGS.flow_downloads_dir / project_id / kind / target_id
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)  # never adopt a stale download
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cleanup_staging(path: Path, *, warn: bool = True) -> None:
    """Remove the staging folders once their contents were adopted.

    Walks back up to (but never past) ``<downloads>/flow``, so an empty
    ``.../<project>/<kind>/<target>`` chain leaves nothing behind.
    """
    try:
        leftovers = list(path.iterdir())
    except OSError:
        return
    if leftovers:
        # Siblings that are still to be adopted (a character job saves two
        # references) are not leftovers, so only the caller's last file warns.
        if warn:
            log(f"left {len(leftovers)} extra download(s) in {path}", level="warn")
        return
    root = SETTINGS.flow_downloads_dir.resolve()
    current = path
    while current.is_dir() and current.resolve() != root:
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def _adopt_download(source: Path, target: Path, *, project_id: str, job_id: int,
                    last: bool = True) -> Path:
    """Move a downloaded file from the download root into the project library."""
    _adopt(source, target)
    log(f"downloaded {source.name} -> {target}", project_id, job_id)
    _cleanup_staging(source.parent, warn=last)
    return target


def character_reference(project_id: str, account: Account, character_id: str) -> ReferenceAsset | None:
    asset = db.latest_asset(project_id, "character", character_id)
    if not asset:
        return None
    path = Path(asset["path"])
    if not path.exists():
        return None
    uploaded = upload_once(project_id, account, path)
    entity = db.get_entity(project_id, account.id, character_id)
    return ReferenceAsset(
        path=path,
        uploaded_id=uploaded,
        account_id=account.id,
        entity_id=entity["entity_id"] if entity else None,
        entity_name=(entity or {}).get("entity_name") if entity else None,
    )


def scene_asset(project_id: str, scene_id: str) -> dict[str, Any] | None:
    return db.latest_asset(project_id, "image", scene_id)


def clip_asset(project_id: str, scene_id: str) -> dict[str, Any] | None:
    return db.latest_asset(project_id, "clip", scene_id)


# ------------------------------------------------------------- job creation --
def prepare_jobs(project_id: str, kinds: list[str] | None = None) -> dict[str, int]:
    manifest = projects.read_manifest(project_id)
    if manifest is None:
        raise ValueError("this project has no manifest yet - run Stage A first")
    kinds = kinds or ["character", "image", "clip"]
    pairs: list[tuple[str, str]] = []
    if "character" in kinds:
        pairs += [("character", c.id) for c in manifest.characters]
    if "image" in kinds:
        pairs += [("image", s.id) for s in manifest.scenes]
    if "clip" in kinds:
        pairs += [("clip", s.id) for s in manifest.scenes]
    db.upsert_jobs(project_id, pairs)
    counts: dict[str, int] = {}
    for row in db.list_jobs(project_id):
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return counts


def start(project_id: str, kinds: list[str] | None = None, *, force: bool = False) -> dict[str, Any]:
    try:
        manifest = projects.read_manifest_or_none(project_id)
    except projects.ManifestError as exc:  # defensive: the tolerant read never raises
        raise ValueError(str(exc)) from exc
    if manifest is None:
        raise ValueError("no manifest for this project")
    ensure_workers()
    if force:
        db.reset_jobs(project_id, kinds or ["character", "image", "clip"])
    prepare_jobs(project_id, kinds)
    if db.list_accounts(enabled_only=True) and not db.get_active_account(project_id):
        selection = accounts.select(project_id)
        if selection.account:
            db.set_active_account(project_id, selection.account.id)
    db.update_project(project_id, status="running", error=None)
    db.execute(
        "UPDATE jobs SET status='queued', updated_at=? WHERE project_id=? AND status='waiting_account'",
        (now_iso(), project_id),
    )
    log("run started", project_id)
    return {"project_id": project_id, "status": "running"}


def pause(project_id: str, reason: str = "paused by operator") -> None:
    db.update_project(project_id, status="paused")
    db.account_event(None, "paused", reason, project_id=project_id, actor="human")
    log(reason, project_id, level="warn")


def resume(project_id: str) -> None:
    db.update_project(project_id, status="running", error=None)
    db.execute(
        "UPDATE jobs SET status='queued', updated_at=? WHERE project_id=? AND status IN ('waiting_account','needs_review')",
        (now_iso(), project_id),
    )
    log("run resumed", project_id)


def cancel(project_id: str) -> None:
    db.update_project(project_id, status="cancelled")
    db.execute(
        "UPDATE jobs SET status='skipped', updated_at=? WHERE project_id=? AND status IN ('queued','waiting_account')",
        (now_iso(), project_id),
    )
    log("run cancelled", project_id, level="warn")


def retry_job(job_id: int, *, reset_attempts: bool = True) -> dict[str, Any] | None:
    job = db.get_job(job_id)
    if not job:
        return None
    db.execute(
        f"UPDATE jobs SET status='queued', error=NULL, finished_at=NULL"
        f"{', attempts=0' if reset_attempts else ''}, updated_at=? WHERE id=?",
        (now_iso(), job_id),
    )
    db.update_project(job["project_id"], status="running", error=None)
    log(f"job {job['kind']}:{job['target_id']} requeued by operator", job["project_id"], job_id)
    return db.get_job(job_id)


def skip_job(job_id: int, reason: str = "skipped by operator") -> None:
    job = db.get_job(job_id)
    if not job:
        return
    db.set_job_status(job_id, "skipped", error=reason)
    log(f"job {job['kind']}:{job['target_id']} {reason}", job["project_id"], job_id, level="warn")


# ------------------------------------------------------------------ worker ---
def ensure_workers() -> None:
    global _STARTED
    with _LOCK:
        if _STARTED:
            return
        _STARTED = True
        count = max(1, int(os.environ.get("FLOWPIPE_WORKERS", "1")))
        for index in range(count):
            thread = threading.Thread(target=_worker_loop, name=f"flowpipe-worker-{index}", daemon=True)
            thread.start()
            _THREADS.append(thread)


def _next_job() -> dict[str, Any] | None:
    rows = db.query(
        "SELECT j.* FROM jobs j JOIN projects p ON p.id = j.project_id "
        "WHERE j.status='queued' AND p.status='running' "
        "ORDER BY CASE j.kind WHEN 'character' THEN 0 WHEN 'image' THEN 1 ELSE 2 END, j.id "
        "LIMIT 5"
    )
    for row in rows:
        if row["id"] not in _BUSY:
            return row
    return None


def _owner() -> str:
    """Identity of this process for the cross-process project lock."""
    global _OWNER
    if not _OWNER:
        import socket

        _OWNER = f"{socket.gethostname()}:{os.getpid()}"
    return _OWNER


def _heartbeat(project_id: str) -> threading.Event:
    """Keep the project lock fresh while a long generation is running."""
    stop = threading.Event()

    def beat() -> None:
        while not stop.wait(20.0):
            try:
                db.touch_lock(project_id, _owner())
            except Exception:
                return

    threading.Thread(target=beat, name="flowpipe-lock-heartbeat", daemon=True).start()
    return stop


def _worker_loop() -> None:
    global _LAST_LOOP_ERROR
    while not _STOP.is_set():
        try:
            job = _next_job()
        except Exception as exc:
            # A database hiccup (locked file, disk full, bad setting) must never
            # kill the worker: without this, one failed poll stops every future
            # job until the dashboard is restarted.
            message = f"{type(exc).__name__}: {exc}"
            if message != _LAST_LOOP_ERROR:
                _LAST_LOOP_ERROR = message
                try:
                    log(f"worker could not read the queue, retrying: {message}", level="error")
                except Exception:
                    print(f"[error] worker could not read the queue: {message}", flush=True)
            _STOP.wait(2.0)
            continue
        else:
            _LAST_LOOP_ERROR = ""
        if job is None:
            _STOP.wait(1.0)
            continue
        _BUSY.add(job["id"])
        # Only one process may work on a project: the dashboard and a script
        # sharing a data dir used to run the same project's jobs concurrently,
        # which reorders character/image work and can pay for a clip twice.
        if not db.try_lock(job["project_id"], _owner()):
            _BUSY.discard(job["id"])
            _STOP.wait(2.0)
            continue
        beat = _heartbeat(job["project_id"])
        try:
            process_job(job["id"])
        except Exception as exc:  # never kill the worker
            log(f"worker crash on job {job['id']}: {exc}\n{traceback.format_exc(limit=3)}",
                job["project_id"], job["id"], level="error")
            db.set_job_status(job["id"], "needs_review", error=f"internal error: {exc}")
            db.update_project(job["project_id"], status="needs_review", error=str(exc))
        finally:
            beat.set()
            db.release_lock(job["project_id"], _owner())
            _BUSY.discard(job["id"])


def _requeue_with_backoff(job: dict[str, Any], error: str, attempt: int) -> bool:
    """Return True when the job was requeued for another attempt."""
    if attempt >= SETTINGS.max_attempts:
        return False
    delay = min(SETTINGS.backoff_seconds * attempt, 300)
    log(f"retry {attempt + 1}/{SETTINGS.max_attempts} in {delay}s: {error}",
        job["project_id"], job["id"], level="warn")
    time.sleep(delay)
    db.execute(
        "UPDATE jobs SET status='queued', error=?, updated_at=? WHERE id=?",
        (error, now_iso(), job["id"]),
    )
    return True


def process_job(job_id: int) -> None:
    job = db.get_job(job_id)
    if not job:
        return
    project_id = job["project_id"]
    project = db.get_project(project_id)
    if not project or project["status"] not in {"running"}:
        return
    manifest = projects.read_manifest(project_id)
    if manifest is None:
        db.set_job_status(job_id, "needs_review", error="manifest missing")
        return

    attempt = int(job["attempts"]) + 1
    db.set_job_status(job_id, "running", attempts=attempt, error=None)
    log(f"start {job['kind']}:{job['target_id']} (attempt {attempt})", project_id, job_id)

    try:
        _execute(manifest, job, attempt)
    except ChallengeRequired as exc:
        _pause_for_human(project_id, job, exc, "challenge")
    except AuthExpired as exc:
        account_id = job["account_id"] or db.get_active_account(project_id)
        renewed = accounts.renew_session(account_id) if account_id else {}
        if renewed.get("status") in {"active", "low"} and _requeue_with_backoff(job, str(exc), attempt):
            return
        if account_id:
            accounts.mark_needs_human(account_id, "session expired; a Google sign-in is required")
        _pause_for_human(project_id, job, exc, "auth")
    except NoFlowAccess as exc:
        account_id = job["account_id"] or db.get_active_account(project_id)
        if account_id:
            db.update_account(account_id, status="disabled", notes="no Flow access on this account")
            db.account_event(account_id, "disabled", str(exc))
        _pause_for_human(project_id, job, exc, "no_access")
    except NoCredits as exc:
        account_id = job["account_id"] or db.get_active_account(project_id)
        if account_id:
            db.update_account(account_id, status="exhausted")
            db.account_event(account_id, "exhausted", str(exc), project_id=project_id)
        selection = accounts.select(project_id)
        if selection.account and SETTINGS.auto_failover:
            log(f"failover -> {selection.account.label}", project_id, job_id, level="warn")
            db.execute("UPDATE jobs SET status='queued', updated_at=? WHERE id=?", (now_iso(), job_id))
            return
        db.set_job_status(job_id, "waiting_account", error=str(exc))
        db.update_project(project_id, status="waiting_approval", error=str(exc))
        db.execute(
            "UPDATE jobs SET status='waiting_account', updated_at=? WHERE project_id=? AND status='queued'",
            (now_iso(), project_id),
        )
        log(f"paused for an account switch: {exc}", project_id, job_id, level="warn")
    except AssetMissing as exc:
        if _requeue_with_backoff(job, f"asset missing, will re-upload: {exc}", attempt):
            return
        _fail(project_id, job, str(exc))
    except SelectorDrift as exc:
        db.set_job_status(job_id, "needs_review", error=str(exc))
        db.update_project(project_id, status="needs_review", error=str(exc))
        log(
            "Flow's UI drifted from what the driver expects. Update gflow-cli "
            "(`pipx upgrade gflow-cli`) and retry - do NOT keep retrying blindly.",
            project_id, job_id, level="error",
        )
    except (TransientFlowError, ProfileLocked) as exc:
        if _requeue_with_backoff(job, str(exc), attempt):
            return
        _fail(project_id, job, str(exc))
    except FlowError as exc:
        if exc.retryable and _requeue_with_backoff(job, str(exc), attempt):
            return
        _fail(project_id, job, str(exc))
    except Exception as exc:  # pragma: no cover
        _fail(project_id, job, f"{type(exc).__name__}: {exc}")
        log(traceback.format_exc(limit=4), project_id, job_id, level="error")


def _fail(project_id: str, job: dict[str, Any], error: str) -> None:
    db.set_job_status(job["id"], "needs_review", error=error)
    db.update_project(project_id, status="needs_review", error=error)
    log(f"needs review: {error}", project_id, job["id"], level="error")


def _pause_for_human(project_id: str, job: dict[str, Any], exc: FlowError, kind: str) -> None:
    account_id = job["account_id"] or db.get_active_account(project_id)
    if account_id and kind in {"challenge", "auth"}:
        # Take the account out of rotation until a human says it is usable again.
        accounts.mark_needs_human(account_id, f"{kind}: {exc}")
    db.set_job_status(job["id"], "needs_review", error=str(exc))
    db.update_project(project_id, status="waiting_human", error=str(exc))
    db.execute(
        "UPDATE jobs SET status='waiting_account', updated_at=? WHERE project_id=? AND status='queued'",
        (now_iso(), project_id),
    )
    log(
        f"HUMAN ACTION REQUIRED ({kind}): {exc} "
        "Resolve it in the browser window, then press Resume (or switch accounts).",
        project_id, job["id"], level="warn",
    )


# --------------------------------------------------------------- execution ---
def _execute(manifest: Manifest, job: dict[str, Any], attempt: int) -> None:
    project_id = job["project_id"]
    selection = accounts.select(project_id)
    if selection.account is None:
        db.set_job_status(job["id"], "waiting_account", error=selection.reason or "no account available")
        db.update_project(project_id, status="waiting_approval", error=selection.reason or None)
        log(f"waiting for an account decision: {selection.reason}", project_id, job["id"], level="warn")
        return

    account = selection.account
    db.execute("UPDATE jobs SET account_id=? WHERE id=?", (account.id, job["id"]))
    kind = job["kind"]
    driver = get_driver()

    if kind == "character":
        character = manifest.character(job["target_id"])
        if character is None:
            raise FlowError(f"character {job['target_id']} is no longer in the manifest")
        out_dir = staging_dir(project_id, "characters", character.id)
        result = driver.create_character(
            account, ensure_project(project_id, account),
            name=character.name,
            face_prompt=character.face_prompt,
            body_prompt=character.body_prompt,
            out_dir=out_dir,
        )
        saved: list[str] = []
        library = projects.asset_dir(project_id, "characters", character.id)
        for index, image in enumerate(result.images, start=1):
            target = projects.next_versioned_file(library, f"ref{index:02d}", image.suffix or ".png")
            _adopt_download(image, target, project_id=project_id, job_id=job["id"],
                            last=index == len(result.images))
            db.add_asset(project_id, "character", character.id, str(target), account.id, job["id"])
            saved.append(str(target))
        if result.entity_id:
            db.set_entity(project_id, account.id, character.id, result.entity_id, result.entity_name)
        db.set_job_status(job["id"], "done", asset_id=saved[0], error=None)
        log(f"character {character.name}: {len(saved)} reference(s) from {account.label}", project_id, job["id"])

    elif kind == "image":
        scene = _scene(manifest, job["target_id"])
        refs: list[ReferenceAsset] = []
        for character_id in scene.character_ids:
            ref = character_reference(project_id, account, character_id)
            if ref is None:
                raise FlowError(
                    f"scene {scene.id} needs character '{character_id}' but no reference image "
                    "exists yet - run its character job first (or skip the scene)."
                )
            refs.append(ref)
        out_dir = staging_dir(project_id, "images", scene.id)
        image = driver.generate_image(
            account, ensure_project(project_id, account),
            prompt=scene.image_prompt,
            out_dir=out_dir,
            aspect=manifest.project.aspect_ratio or SETTINGS.aspect_ratio,
            references=refs,
        )
        library = projects.asset_dir(project_id, "images", scene.id)
        target = projects.next_versioned_file(library, f"{scene.id}", image.suffix or ".png")
        _adopt_download(image, target, project_id=project_id, job_id=job["id"])
        db.add_asset(project_id, "image", scene.id, str(target), account.id, job["id"])
        db.set_job_status(job["id"], "done", error=None)
        log(f"still for {scene.id} generated on {account.label}", project_id, job["id"])

    elif kind == "clip":
        scene = _scene(manifest, job["target_id"])
        image = scene_asset(project_id, scene.id)
        if not image:
            raise FlowError(f"scene {scene.id} has no still image yet - generate it before the clip")
        accounts.ensure_credits_for_clip(account.id)
        end_frame = _end_frame(manifest, scene, project_id)
        # The still has to live in THIS account's Flow project. Caching the
        # upload per account is what makes a failover re-upload it (§7).
        start_asset_id = upload_once(project_id, account, Path(image["path"]))
        out_dir = staging_dir(project_id, "clips", scene.id)
        clip = driver.generate_clip(
            account, ensure_project(project_id, account),
            prompt=scene.animation_prompt or scene.image_prompt,
            out_dir=out_dir,
            start_frame=Path(image["path"]),
            end_frame=end_frame,
            duration=scene.clip_duration or SETTINGS.clip_duration,
            resolution=SETTINGS.resolution,
            aspect=manifest.project.aspect_ratio or SETTINGS.aspect_ratio,
            start_asset_id=start_asset_id,
        )
        library = projects.asset_dir(project_id, "clips", scene.id)
        target = projects.next_versioned_file(library, f"{scene.id}", ".mp4")
        _adopt_download(clip, target, project_id=project_id, job_id=job["id"])
        db.add_asset(project_id, "clip", scene.id, str(target), account.id, job["id"])
        accounts.spend(account.id)
        db.set_job_status(job["id"], "done", error=None)
        log(f"clip for {scene.id} rendered on {account.label}", project_id, job["id"])
        _maybe_finish(project_id)
    else:
        raise FlowError(f"unknown job kind {kind}")


def _adopt(source: Path, target: Path) -> Path:
    """Move a freshly generated file to its permanent, versioned path."""
    if source.resolve() == target.resolve():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(source), str(target))
    except OSError:
        target.write_bytes(source.read_bytes())
    return target


def _scene(manifest: Manifest, scene_id: str) -> Scene:
    for scene in manifest.scenes:
        if scene.id == scene_id:
            return scene
    raise FlowError(f"scene {scene_id} is no longer in the manifest")


def _end_frame(manifest: Manifest, scene: Scene, project_id: str) -> Path | None:
    """When a scene declares a last-frame note, interpolate toward the next
    scene's still so the cut lands on the right composition."""
    if not scene.last_frame_note:
        return None
    ordered = manifest.ordered_scenes()
    index = ordered.index(scene)
    for later in ordered[index + 1:]:
        asset = scene_asset(project_id, later.id)
        if asset and Path(asset["path"]).exists():
            return Path(asset["path"])
    return None


def _maybe_finish(project_id: str) -> None:
    pending = db.query(
        "SELECT COUNT(*) AS n FROM jobs WHERE project_id=? AND kind IN ('character','image','clip')"
        " AND status NOT IN ('done','skipped')",
        (project_id,),
    )[0]["n"]
    if pending == 0:
        db.update_project(project_id, status="ready_to_assemble")
        log("all clips done - ready to assemble the final video", project_id)


def stats(project_id: str) -> dict[str, Any]:
    rows = db.list_jobs(project_id)
    summary: dict[str, dict[str, int]] = {}
    for row in rows:
        bucket = summary.setdefault(row["kind"], {})
        bucket[row["status"]] = bucket.get(row["status"], 0) + 1
    return {"by_kind": summary, "total": len(rows)}
