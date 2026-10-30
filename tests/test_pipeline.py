"""End-to-end tests against the mock driver: stages B-D, accounts, Stage E."""
from __future__ import annotations

from pathlib import Path

import pytest
from conftest import wait_for


def _add_account(label: str = "studio-a", credits: int = 120) -> str:
    import uuid

    from flowpipe import accounts, db
    from flowpipe.flow.mock import set_credits

    unique = f"{label}-{uuid.uuid4().hex[:6]}"
    account = accounts.add_account(unique, unique)
    db.update_account(account["id"], credits=credits, status="active")
    set_credits(account["id"], credits)
    return account["id"]


def test_full_run_and_assembly(project):
    from flowpipe import assemble, db, runner

    account_id = _add_account("studio-main", credits=200)
    runner.start(project, force=True)

    def ready():
        row = db.get_project(project)
        return row["status"] if row["status"] in {"ready_to_assemble", "needs_review", "waiting_approval"} else None

    status = wait_for(ready, timeout=240)
    assert status == "ready_to_assemble", db.list_jobs(project)

    clips = db.list_assets(project, "clip")
    assert len(clips) == 2
    for clip in clips:
        assert Path(clip["path"]).exists()
        assert clip["account_id"] == account_id

    result = assemble.assemble(project)
    final = Path(result["path"])
    assert final.exists() and final.stat().st_size > 0
    assert result["duration"] > 1.5
    assert result["scenes"] == ["scene-1", "scene-2"]
    assert db.get_project(project)["status"] == "complete"

    # the character is stored once, and its reference was uploaded per account
    assert len(db.list_assets(project, "character")) >= 1
    uploads = db.query("SELECT * FROM uploads WHERE project_id=?", (project,))
    assert uploads and all(row["account_id"] == account_id for row in uploads)


def test_low_credits_pauses_for_approval_then_switches(project, pipeline):
    from flowpipe import accounts, db, runner

    first = _add_account("studio-low", credits=30)
    second = _add_account("studio-high", credits=500)
    pipeline.auto_failover = False

    runner.start(project, force=True)

    def pending():
        return db.pending_switch_request(project)

    request = wait_for(pending, timeout=240)
    assert request["status"] == "pending"
    assert db.get_project(project)["status"] == "waiting_approval"
    assert request["to_account"] == second

    accounts.approve_switch(request["id"], approve=True)
    assert db.get_project(project)["active_account"] == second

    def done():
        row = db.get_project(project)
        return row["status"] == "ready_to_assemble" or row["status"] == "needs_review"

    wait_for(done, timeout=240)
    assert db.get_project(project)["status"] == "ready_to_assemble", db.list_jobs(project)
    assert db.get_active_account(project) == second

    # after the switch every asset needed by the new account was re-uploaded
    uploads = db.query("SELECT DISTINCT account_id FROM uploads WHERE project_id=?", (project,))
    assert {row["account_id"] for row in uploads} == {first, second}
    events = [e for e in db.account_events(200) if e["action"] == "switch"]
    assert events, "switch must be logged with a reason"


def test_auto_failover_when_enabled(project, pipeline):
    from flowpipe import db, runner

    _add_account("auto-low", credits=30)
    second = _add_account("auto-high", credits=500)
    pipeline.auto_failover = True
    try:
        runner.start(project, force=True)

        def done():
            row = db.get_project(project)
            return row["status"] if row["status"] in {"ready_to_assemble", "needs_review", "waiting_approval"} else None

        status = wait_for(done, timeout=240)
    finally:
        pipeline.auto_failover = False
    assert status == "ready_to_assemble", db.list_jobs(project)
    assert db.get_active_account(project) == second
    assert not db.pending_switch_request(project)


def test_challenge_pauses_and_never_retries(project, pipeline, monkeypatch):
    from flowpipe import db, runner

    _add_account("challenged", credits=500)
    monkeypatch.setenv("MOCK_CHALLENGE_AT", "1")
    runner.start(project, force=True)

    def waiting():
        return db.get_project(project)["status"] == "waiting_human"

    wait_for(waiting, timeout=120)
    project_row = db.get_project(project)
    assert "verification" in (project_row["error"] or "").lower()
    accounts = db.list_accounts()
    assert accounts[0]["status"] == "needs_human"
    # nothing was generated and no retry loop happened
    assert db.list_assets(project) == []


def test_selector_drift_stops_instead_of_retrying(project, pipeline, monkeypatch):
    from flowpipe import db, runner

    _add_account("drifting", credits=500)
    monkeypatch.setenv("MOCK_DRIFT_AT", "1")
    runner.start(project, force=True)

    def failed():
        return db.get_project(project)["status"] == "needs_review"

    wait_for(failed, timeout=120)
    jobs = db.list_jobs(project)
    assert jobs[0]["status"] == "needs_review"
    assert "ui drifted" in (jobs[0]["error"] or "").lower() or "menu" in (jobs[0]["error"] or "").lower()


def test_job_retry_and_skip_api(project):
    from flowpipe import db, runner

    _add_account("retry-account", credits=500)
    runner.start(project)
    jobs = db.list_jobs(project)
    job = jobs[0]
    runner.skip_job(job["id"], "manual skip")
    assert db.get_job(job["id"])["status"] == "skipped"
    requeued = runner.retry_job(job["id"])
    assert requeued["status"] == "queued"


def test_assembly_refuses_when_clips_missing(project):
    from flowpipe import assemble

    with pytest.raises(assemble.AssemblyError) as exc:
        assemble.assemble(project)
    assert "no rendered clip" in str(exc.value)
