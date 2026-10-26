#!/usr/bin/env python3
"""End-to-end self test — proves the install works without spending a cent.

Run by ``setup.bat`` at the end of an install, and safe to run any time:

    python scripts/selftest.py                 # uses the configured data dir
    python scripts/selftest.py --tools D:\\Tools  # also check the tool root

It exercises the real code paths:

  1. interpreter, imports, resolved toolchain
  2. ffmpeg resolution + `-version`
  3. Stage A   manifest validation (no API call)
  4. Stage B-D the mock driver end to end: characters -> stills -> clips
  5. Stage E   ffmpeg normalize + concat -> final.mp4 with a real duration
  6. the HTTP API: create/import/run/assemble via TestClient, media serving
     including HTTP range requests (needed for the <video> player), settings,
     templates, jobs, accounts, usage

Exit code 0 = everything passed. Every check prints [ok] / [FAIL] so a failure
is obvious in the console window.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

PASS, FAIL = "[ok]", "[FAIL]"
ACCOUNT = "selftest-account"
_failures: list[str] = []
_checks = 0


def check(label: str, condition: object, detail: str = "") -> bool:
    global _checks
    _checks += 1
    ok = bool(condition)
    print(f"  {PASS if ok else FAIL} {label}" + (f" — {detail}" if detail else ""), flush=True)
    if not ok:
        _failures.append(label)
    return ok


def section(title: str) -> None:
    print(f"\n== {title} ==", flush=True)


def prepare_environment(tools: str | None, data_dir: str | None) -> None:
    """Set env vars BEFORE flowpipe is imported (its settings load at import)."""
    if tools:
        os.environ["FLOWPIPE_TOOLS_DIR"] = tools
    if data_dir:
        os.environ["FLOWPIPE_DATA_DIR"] = data_dir
    os.environ.setdefault("FLOWPIPE_DRIVER", "mock")
    os.environ["MOCK_CREDITS"] = "400"
    os.environ.setdefault("FLOWPIPE_WORKERS", "1")
    # keep test renders small and quick
    os.environ.setdefault("FLOWPIPE_SELFTEST", "1")


def wait_until(predicate, timeout: float = 180.0, interval: float = 0.4):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


MANIFEST = {
    "project": {
        "topic": "self test: a lighthouse keeper finds a bottle",
        "style": "noir",
        "tone": "quiet",
        "target_duration": 2,
        "aspect_ratio": "16:9",
    },
    "characters": [{
        "id": "aria",
        "name": "Aria",
        "description": "keeper",
        "face_prompt": "portrait of a weathered woman, grey braid, salt wind",
        "body_prompt": "oilskin coat, wool sweater, tall boots",
    }],
    "scenes": [
        {"id": "s1", "order": 1, "title": "Arrival", "character_ids": ["aria"],
         "image_prompt": "Aria on the lighthouse steps at dawn",
         "animation_prompt": "slow push in", "clip_duration": 1},
        {"id": "s2", "order": 2, "title": "Lamp", "character_ids": ["aria"],
         "image_prompt": "Aria lighting the lamp", "animation_prompt": "tilt up", "clip_duration": 1},
    ],
}


# --------------------------------------------------------------------- main --
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="flow-pipeline self test")
    parser.add_argument("--tools", help="tool root to prepend (D:\\Tools on Windows)")
    parser.add_argument("--data-dir", help="data directory for the test run")
    parser.add_argument("--keep", action="store_true", help="keep the temporary data dir")
    parser.add_argument("--skip-api", action="store_true", help="skip the HTTP layer checks")
    args = parser.parse_args(argv)

    temp_data = None
    if not args.data_dir:
        temp_data = tempfile.mkdtemp(prefix="flowpipe-selftest-")
        args_data = temp_data
    else:
        args_data = args.data_dir

    prepare_environment(args.tools, args_data)

    print("flow-pipeline self test")
    print(f"repo: {REPO}")
    print(f"data: {args_data}")

    try:
        run_checks(args)
    except Exception:
        print(FAIL + " unexpected error")
        traceback.print_exc()
        _failures.append("unexpected error")

    print("\n" + "-" * 58)
    if _failures:
        print(f"FAILED {len(_failures)} of {_checks} checks:")
        for item in _failures:
            print(f"  - {item}")
        return 1
    print(f"PASSED all {_checks} checks")
    print("\nDashboard: run.bat, then open http://localhost:8000")
    if not args.keep and temp_data:
        _cleanup(temp_data)
    return 0


def _drop_project(project_id: str, *, keep: bool, announce: bool = True) -> None:
    """Leave the data dir as we found it (unless --keep)."""
    if keep:
        if announce:
            print(f"\nkept self-test project: {project_id}")
        return
    import shutil

    from flowpipe import assemble, db, projects
    from flowpipe.config import SETTINGS

    manifest = projects.read_manifest(project_id)
    if manifest is not None:
        copy = SETTINGS.final_downloads_dir / assemble.download_name(project_id, manifest)
        copy.unlink(missing_ok=True)
        copy.with_suffix(".srt").unlink(missing_ok=True)  # narration subtitles

    for table in ("jobs", "assets", "uploads", "entities", "flow_projects"):
        try:
            db.execute(f"DELETE FROM {table} WHERE project_id=?", (project_id,))
        except Exception:
            pass
    try:
        db.execute("DELETE FROM projects WHERE id=?", (project_id,))
    except Exception:
        pass
    shutil.rmtree(projects.project_dir(project_id), ignore_errors=True)


def _cleanup(path: str) -> None:
    import shutil

    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass


def run_checks(args) -> None:
    # ---------------------------------------------------------------- tools --
    section("interpreter and tools")
    check("Python >= 3.10", sys.version_info >= (3, 10), sys.version.split()[0])

    import subprocess

    import flowpipe
    from flowpipe import toolchain

    check("flowpipe imports", True, f"version {flowpipe.__version__}")
    for module in ("fastapi", "uvicorn", "pydantic", "anthropic", "imageio_ffmpeg"):
        try:
            __import__(module)
            check(f"import {module}", True)
        except Exception as exc:
            check(f"import {module}", False, str(exc))

    status = toolchain.status()
    check("tool root detected", True, str(status["tools_dir"]) if status["tools_dir"] else "none (PATH mode)")

    # The dashboard must start the same way run.bat starts it. On Windows the
    # embeddable Python ignores the current folder, so this catches a launcher
    # that would die with "No module named 'flowpipe'" when double-clicked.
    launcher = REPO / "scripts" / "serve.py"
    try:
        started = subprocess.run(
            [sys.executable, "-I", str(launcher), "--help"],
            capture_output=True, text=True, timeout=120, cwd=str(Path.home()),
        )
        ok = started.returncode == 0 and "usage: serve.py" in (started.stdout or "")
        detail = "" if ok else ((started.stderr or started.stdout or "").strip().splitlines() or [""])[-1]
        check("dashboard launcher (scripts/serve.py) starts", ok, detail[:120])
    except Exception as exc:
        check("dashboard launcher (scripts/serve.py) starts", False, str(exc))

    from flowpipe.config import SETTINGS

    try:
        ffmpeg = SETTINGS.ffmpeg()
        check("ffmpeg resolves", True, ffmpeg)
    except Exception as exc:
        check("ffmpeg resolves", False, str(exc))
        return

    proc = subprocess.run([ffmpeg, "-version"], capture_output=True, text=True)
    first = (proc.stdout or proc.stderr).splitlines()[0] if (proc.stdout or proc.stderr) else ""
    check("ffmpeg runs", proc.returncode == 0, first.strip()[:80])

    from flowpipe import db, projects
    from flowpipe.flow import get_driver

    driver = get_driver()
    caps = driver.capabilities()
    check("driver loads", bool(caps.driver), f"{caps.driver} (image {caps.default_image_model}, "
                                             f"video {caps.default_video_model})")
    db.init_db()
    check("database initialises", Path(SETTINGS.db_path).exists(), str(SETTINGS.db_path))

    # -------------------------------------------------------------- stage A --
    section("stage A: manifest")
    from flowpipe.models import Manifest
    from flowpipe.prompts import render_prompt

    prompt = render_prompt("cinematic-short", topic="lighthouse", scene_count=2, target_duration=16)
    check("master prompt renders", "lighthouse" in prompt and "face_prompt" in prompt)
    try:
        manifest = Manifest.model_validate(MANIFEST)
        check("manifest validates", len(manifest.scenes) == 2)
    except Exception as exc:
        check("manifest validates", False, str(exc))
        return

    # ------------------------------------------------------------ stages B-D --
    section("stages B-D: characters, stills, clips (mock driver)")
    project_id = projects.new_id("selftest")
    db.create_project(project_id, manifest.project.topic, "cinematic-short")
    projects.write_manifest(project_id, manifest)
    db.update_project(project_id, status="manifest_ready")

    from flowpipe import accounts, runner
    from flowpipe.flow.mock import reset_state, set_credits

    reset_state()
    try:
        account = accounts.add_account(ACCOUNT, ACCOUNT, "created by scripts/selftest.py")
    except ValueError:  # re-run against a persistent data dir: reuse it
        account = db.get_account(ACCOUNT) or {}
    check("test account available", bool(account.get("id")), account.get("id", "missing"))
    db.update_account(account["id"], credits=400, status="active")
    set_credits(account["id"], 400)

    runner.start(project_id, force=True)

    def settled():
        row = db.get_project(project_id)
        return row["status"] in {"ready_to_assemble", "needs_review", "waiting_approval"}

    wait_until(settled, timeout=240)
    status_now = db.get_project(project_id)["status"]
    check("pipeline reaches ready_to_assemble", status_now == "ready_to_assemble",
          f"{len(db.list_jobs(project_id))} jobs")

    jobs = db.list_jobs(project_id)
    check("every job done", jobs and all(job["status"] == "done" for job in jobs),
          ", ".join(f"{j['kind']}:{j['status']}" for j in jobs))
    clips = db.list_assets(project_id, "clip")
    check("clips written to disk", len(clips) == 2 and all(Path(c["path"]).exists() for c in clips),
          f"{len(clips)} clip files")
    uploads = db.query("SELECT * FROM uploads WHERE project_id=?", (project_id,))
    check("reference assets uploaded per account", len(uploads) >= 1, f"{len(uploads)} uploads")

    # -------------------------------------------------------------- stage E --
    section("stage E: assembly")
    from flowpipe import assemble

    try:
        result = assemble.assemble(project_id)
        final = Path(result["path"])
        check("final.mp4 written", final.exists() and final.stat().st_size > 1000,
              f"{final.stat().st_size // 1024} KB" if final.exists() else "missing")
        check("final.mp4 has a duration", result["duration"] > 1.0, f"{result['duration']:.1f}s")
        check("clips joined in manifest order", result["scenes"] == ["s1", "s2"], str(result["scenes"]))
        check("thumbnail rendered", bool(result["thumbnail"]) and Path(result["thumbnail"]).exists())
    except Exception as exc:
        check("assembly", False, str(exc))

    # ------------------------------------------------------------ narration --
    section("narration: text to speech + word timestamps")
    # The voice server (VoiceStudio) is optional and may not be installed, so the
    # self test exercises the pipeline with the offline backend: TTS, timestamps,
    # subtitle generation and the final mix are all real code paths.
    from flowpipe.voice import align, timeline
    from flowpipe.voice import stage as narration
    from flowpipe.voice.mock import MockBackend

    try:
        probe = align.proportional("one two three", 2.0)
        check("word timestamps are produced", len(probe) == 3 and probe.words[-1].end <= 2.0,
              f"{len(probe)} words")
        srt = timeline.Timeline(probe.words, 2.0).srt()
        check("subtitles render", "-->" in srt and srt.startswith("1\n00:00:00,100"),
              srt.splitlines()[1] if len(srt.splitlines()) > 1 else "")

        manifest = projects.read_manifest(project_id)
        if manifest is not None and not narration.script_for(manifest):
            manifest.scenes[0].narration = "For forty years the lamp burned."
            manifest.scenes[1].narration = "Then the sea returned what it had kept."
            projects.write_manifest(project_id, manifest)
        plan = narration.synthesize(project_id, backend=MockBackend(), align_provider="proportional")
        check("narration audio generated", all(
            Path(projects.project_dir(project_id) / row["audio"]).exists()
            for row in plan["scenes"]), f"{len(plan['scenes'])} scene(s)")
        check("narration plan saved", narration.plan_path(project_id).exists(),
              str(narration.plan_path(project_id)))

        fitted = timeline.fit(
            timeline.Timeline([timeline.Word("w", 0.0, 2.0), timeline.Word("x", 2.0, 4.0)], 4.0),
            3.6,
        )
        check("narration is fitted to a clip", fitted.speedup > 1.0 and fitted.timeline.duration <= 3.6,
              f"{fitted.speedup:.2f}x")

        mixed = assemble.assemble(project_id, captions="sidecar")
        srt_path = Path(mixed["subtitle_path"]) if mixed.get("subtitle_path") else None
        check("final video mixed with narration", bool(mixed.get("duration")), f"{mixed['duration']:.1f}s")
        check("subtitles written next to the final video", bool(srt_path) and srt_path.exists(),
              str(srt_path or "none"))
        if srt_path:
            check("subtitle copy saved with the download",
                  Path(mixed["download_path"]).with_suffix(".srt").exists(),
                  Path(mixed["download_path"]).with_suffix(".srt").name)
    except Exception as exc:
        check("narration", False, f"{type(exc).__name__}: {exc}")

    if args.skip_api:
        _drop_project(project_id, keep=args.keep)
        return

    # ------------------------------------------------------------ downloads --
    section("downloads")
    check("download folder exists", Path(SETTINGS.downloads_dir).is_dir(), str(SETTINGS.downloads_dir))
    copy = result.get("download_path") if "result" in dir() else None
    check("final video copied into the download folder",
          bool(copy) and Path(copy).exists(), str(copy or "not copied"))
    staging = SETTINGS.flow_downloads_dir / project_id
    leftovers = [p for p in staging.rglob("*") if p.is_file()] if staging.exists() else []
    check("raw Flow downloads are moved out of the staging area", not leftovers,
          f"{len(leftovers)} left")

    # ------------------------------------------------------------ HTTP layer --
    section("HTTP API")
    try:
        from fastapi.testclient import TestClient

        from flowpipe.api import app
    except Exception as exc:
        check("TestClient available", False, str(exc))
        return

    with TestClient(app) as client:
        health = client.get("/api/health")
        check("GET /api/health", health.status_code == 200, health.json().get("driver"))
        check("dashboard html served", client.get("/").status_code == 200)
        check("static js served", client.get("/static/app.js").status_code == 200)

        listing = client.get("/api/projects").json()
        check("GET /api/projects", any(p["id"] == project_id for p in listing))

        fresh = client.post("/api/projects", json={
            "topic": "self test api project", "template_id": "cinematic-short",
            "scene_count": 2, "target_duration": 4, "mode": "manual",
        })
        check("POST /api/projects", fresh.status_code == 200, fresh.json().get("id"))
        api_project = fresh.json()["id"]
        check("manual Stage A prompt written",
              bool(client.get(f"/api/projects/{api_project}/prompt").json()["prompt"]))

        imported = client.post(f"/api/projects/{api_project}/manifest/import",
                               json={"raw": __import__("json").dumps(MANIFEST)})
        check("POST manifest/import", imported.status_code == 200)

        run = client.post(f"/api/projects/{api_project}/run", json={})
        check("POST /run", run.status_code == 200)
        wait_until(lambda: client.get(f"/api/projects/{api_project}").json()["project"]["status"]
                   in {"ready_to_assemble", "needs_review"}, timeout=240)
        detail = client.get(f"/api/projects/{api_project}").json()
        check("API project finishes stages B-D",
              detail["project"]["status"] == "ready_to_assemble", detail["project"]["status"])

        gallery = client.get(f"/api/projects/{api_project}/assets").json()
        check("gallery exposes stills + clips",
              all(scene["image"] and scene["clip"] for scene in gallery["scenes"]))

        assembled = client.post(f"/api/projects/{api_project}/assemble", json={})
        check("POST /assemble", assembled.status_code == 200,
              f"{assembled.json().get('duration')}s" if assembled.status_code == 200
              else assembled.text[:120])

        detail = client.get(f"/api/projects/{api_project}").json()
        url = detail.get("output_url")
        check("final video URL exposed", bool(url), str(url))
        if url:
            head = client.head(url)
            check("HEAD media works", head.status_code == 200, head.headers.get("content-type"))
            ranged = client.get(url, headers={"Range": "bytes=0-1023"})
            check("range requests work", ranged.status_code == 206,
                  f"{len(ranged.content)} bytes (needed for video scrubbing)")

        jobs = detail["jobs"]
        retried = client.post(f"/api/jobs/{jobs[0]['id']}/retry")
        skipped = client.post(f"/api/jobs/{jobs[0]['id']}/skip")
        check("job retry/skip endpoints", retried.status_code == 200 and skipped.status_code == 200)

        templates = client.get("/api/templates").json()
        check("templates listed", len(templates) >= 3, f"{len(templates)} templates")
        check("settings readable", client.get("/api/settings").json()["driver"] == "mock")
        patched = client.patch("/api/settings", json={"values": {"clip_duration": 8}})
        check("settings writable", patched.status_code == 200
              and patched.json()["clip_duration"] == 8)
        before = client.get("/api/settings").json()["output_width"]
        refused = client.patch("/api/settings", json={"values": {"output_width": "abc"}})
        after = client.get("/api/settings").json()["output_width"]
        check("a bad setting is refused, not stored",
              refused.status_code == 400 and after == before,
              f"{refused.status_code}, output_width still {after}")

        # one broken project.json must not take the project list down with it
        manifest_file = projects.manifest_path(project_id)
        original = manifest_file.read_bytes()
        manifest_file.write_bytes(b'{"scenes": [ oops')
        try:
            broken_list = client.get("/api/projects")
            broken_detail = client.get(f"/api/projects/{project_id}")
            check("a broken manifest does not break the dashboard",
                  broken_list.status_code == 200 and broken_detail.status_code == 200
                  and bool(broken_detail.json().get("manifest_error")),
                  broken_detail.json().get("manifest_error", "")[:70])
        finally:
            manifest_file.write_bytes(original)

        check("usage endpoint", client.get("/api/usage").status_code == 200)
        check("accounts endpoint", c_status := client.get("/api/accounts").status_code == 200, str(c_status))
        check("switch requests endpoint", client.get("/api/switch-requests").status_code == 200)

        deleted = client.delete(f"/api/projects/{api_project}")
        check("DELETE /api/projects/{id}", deleted.status_code == 200)
        check("deleted project is gone", client.get(f"/api/projects/{api_project}").status_code == 404)

    # the API delete removes the index entries; clear the files and the copy too
    _drop_project(api_project, keep=args.keep, announce=False)
    _drop_project(project_id, keep=args.keep)


if __name__ == "__main__":
    sys.exit(main())
