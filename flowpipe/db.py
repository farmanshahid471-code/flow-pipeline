"""SQLite state store.

One database is enough at this scale, but the layout deliberately keeps
projects self-contained on disk (``data/projects/<id>/project.json`` plus
``/characters /images /clips /output``) so a project folder remains usable even
if the database is deleted.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .config import SETTINGS
from .models import now_iso

_LOCK = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id            TEXT PRIMARY KEY,
    topic         TEXT NOT NULL,
    template_id   TEXT,
    status        TEXT NOT NULL DEFAULT 'draft',
    flow_project  TEXT,
    active_account TEXT,
    output_path   TEXT,
    error         TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,              -- character | image | clip
    target_id   TEXT NOT NULL,              -- character id or scene id
    status      TEXT NOT NULL DEFAULT 'queued',
    attempts    INTEGER NOT NULL DEFAULT 0,
    account_id  TEXT,
    asset_id    TEXT,
    error       TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    started_at  TEXT,
    finished_at TEXT,
    UNIQUE (project_id, kind, target_id)
);

CREATE TABLE IF NOT EXISTS accounts (
    id           TEXT PRIMARY KEY,
    label        TEXT NOT NULL,
    profile      TEXT NOT NULL,             -- gflow-cli profile name (own storageState)
    status       TEXT NOT NULL DEFAULT 'active',
    credits      INTEGER,
    credits_at   TEXT,
    notes        TEXT,
    enabled      INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS account_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT,
    project_id TEXT,
    action     TEXT NOT NULL,               -- switch | skip | credit_check | auth | human_login
    reason     TEXT,
    actor      TEXT NOT NULL DEFAULT 'system',  -- system | human
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    kind       TEXT NOT NULL,
    target_id  TEXT NOT NULL,
    path       TEXT NOT NULL,
    account_id TEXT,
    job_id     INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS switch_requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  TEXT,
    kind        TEXT,
    target_id   TEXT,
    from_account TEXT,
    to_account  TEXT,
    reason      TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | denied
    created_at  TEXT NOT NULL,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS flow_projects (
    project_id     TEXT NOT NULL,
    account_id     TEXT NOT NULL,
    flow_project_id TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    PRIMARY KEY (project_id, account_id)
);

-- Flow Character entities are per-account: an id minted on account A is
-- meaningless (and unusable) to account B.
CREATE TABLE IF NOT EXISTS entities (
    project_id   TEXT NOT NULL,
    account_id   TEXT NOT NULL,
    character_id TEXT NOT NULL,
    entity_id    TEXT NOT NULL,
    entity_name  TEXT,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (project_id, account_id, character_id)
);

-- Local files uploaded into a specific account's Flow project. Asset ids are
-- account-scoped, so this cache is keyed by account and is what makes a
-- failover re-upload automatic instead of reusing a dead id.
CREATE TABLE IF NOT EXISTS uploads (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  TEXT NOT NULL,
    account_id  TEXT NOT NULL,
    source_path TEXT NOT NULL,
    asset_id    TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    UNIQUE (project_id, account_id, source_path)
);

-- One owner per project at a time, across processes. The in-process _BUSY set
-- in runner.py only covers one process; the dashboard and a script (demo_seed,
-- selftest, a manual run) sharing a data dir used to process the same project's
-- jobs concurrently, which reorders character/image work and can pay twice.
CREATE TABLE IF NOT EXISTS project_locks (
    project_id TEXT PRIMARY KEY,
    owner      TEXT NOT NULL,
    pid        INTEGER NOT NULL,
    heartbeat  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT,
    job_id     INTEGER,
    level      TEXT NOT NULL DEFAULT 'info',
    message    TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_project ON jobs(project_id, kind);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_logs_project ON logs(project_id, id);
CREATE INDEX IF NOT EXISTS idx_assets_project ON assets(project_id, kind);
CREATE INDEX IF NOT EXISTS idx_logs_job ON logs(job_id);
"""


def db_path() -> Path:
    return SETTINGS.db_path


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    SETTINGS.ensure_dirs()
    conn = sqlite3.connect(db_path(), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    with _LOCK, connect() as conn:
        conn.executescript(SCHEMA)


def query(sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]


def one(sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
    rows = query(sql, params)
    return rows[0] if rows else None


# ----------------------------------------------------- cross-process locks ---
def _pid_alive(pid: int) -> bool:
    """Best-effort: is this pid still running (on this machine)?"""
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        if os.name == "nt":  # Windows has no signal 0
            import ctypes

            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return int(ctypes.windll.kernel32.GetLastError()) == 5  # access denied: exists
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return True  # unknown: assume alive, never steal a live lock


def _lock_age(heartbeat: str, stale_seconds: int) -> bool:
    try:
        from datetime import datetime

        then = datetime.fromisoformat(heartbeat)
        return (datetime.now(then.tzinfo) - then).total_seconds() > stale_seconds
    except Exception:
        return True


def try_lock(project_id: str, owner: str, *, stale_seconds: int = 3600) -> bool:
    """Take the project lock for this process, stealing it if the holder is gone."""
    pid = os.getpid()
    with _LOCK, connect() as conn:
        rows = conn.execute("SELECT project_id, owner, pid, heartbeat FROM project_locks").fetchall()
        for row in rows:
            if row["owner"] == owner:
                continue
            # Same pid means another owner string from this very process; treat it
            # as live. Only a dead pid or a stale heartbeat may be stolen.
            alive = int(row["pid"]) == pid or _pid_alive(int(row["pid"]))
            if alive and not _lock_age(row["heartbeat"], stale_seconds):
                continue
            conn.execute("DELETE FROM project_locks WHERE project_id=?", (row["project_id"],))
        conn.execute(
            "INSERT OR IGNORE INTO project_locks (project_id, owner, pid, heartbeat) VALUES (?,?,?,?)",
            (project_id, owner, pid, now_iso()),
        )
        row = conn.execute("SELECT owner FROM project_locks WHERE project_id=?", (project_id,)).fetchone()
        return bool(row) and row["owner"] == owner


def touch_lock(project_id: str, owner: str) -> None:
    execute("UPDATE project_locks SET heartbeat=? WHERE project_id=? AND owner=?",
            (now_iso(), project_id, owner))


def release_lock(project_id: str, owner: str) -> None:
    execute("DELETE FROM project_locks WHERE project_id=? AND owner=?", (project_id, owner))


def lock_holder(project_id: str) -> dict[str, Any] | None:
    return one("SELECT * FROM project_locks WHERE project_id=?", (project_id,))


def execute(sql: str, params: Iterable[Any] = ()) -> int:
    with _LOCK, connect() as conn:
        cur = conn.execute(sql, tuple(params))
        return cur.lastrowid or 0


# --------------------------------------------------------------------- logs --
def log(message: str, *, project_id: str | None = None, job_id: int | None = None, level: str = "info") -> None:
    execute(
        "INSERT INTO logs (project_id, job_id, level, message, created_at) VALUES (?,?,?,?,?)",
        (project_id, job_id, level, message, now_iso()),
    )


def tail(project_id: str | None = None, limit: int = 200, after_id: int = 0) -> list[dict[str, Any]]:
    if project_id:
        rows = query(
            "SELECT * FROM logs WHERE project_id=? AND id>? ORDER BY id DESC LIMIT ?",
            (project_id, after_id, limit),
        )
    else:
        rows = query("SELECT * FROM logs WHERE id>? ORDER BY id DESC LIMIT ?", (after_id, limit))
    return list(reversed(rows))


# ----------------------------------------------------------------- projects --
def create_project(project_id: str, topic: str, template_id: str) -> None:
    ts = now_iso()
    execute(
        "INSERT INTO projects (id, topic, template_id, status, created_at, updated_at) VALUES (?,?,?,?,?,?)",
        (project_id, topic, template_id, "draft", ts, ts),
    )


def update_project(project_id: str, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = now_iso()
    sets = ", ".join(f"{k}=?" for k in fields)
    execute(f"UPDATE projects SET {sets} WHERE id=?", (*fields.values(), project_id))


def get_project(project_id: str) -> dict[str, Any] | None:
    return one("SELECT * FROM projects WHERE id=?", (project_id,))


def list_projects() -> list[dict[str, Any]]:
    return query("SELECT * FROM projects ORDER BY created_at DESC")


# --------------------------------------------------------------------- jobs --
def upsert_jobs(project_id: str, jobs: list[tuple[str, str]]) -> None:
    """Idempotently create (kind, target) jobs; existing rows keep their status."""
    ts = now_iso()
    with _LOCK, connect() as conn:
        for kind, target in jobs:
            conn.execute(
                "INSERT OR IGNORE INTO jobs (project_id, kind, target_id, status, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?)",
                (project_id, kind, target, "queued", ts, ts),
            )


def set_job_status(job_id: int, status: str, **fields: Any) -> None:
    ts = now_iso()
    fields["status"] = status
    fields["updated_at"] = ts
    if status == "running":
        fields.setdefault("started_at", ts)
    if status in {"done", "failed", "skipped", "needs_review"}:
        fields.setdefault("finished_at", ts)
    sets = ", ".join(f"{k}=?" for k in fields)
    execute(f"UPDATE jobs SET {sets} WHERE id=?", (*fields.values(), job_id))


def get_job(job_id: int) -> dict[str, Any] | None:
    return one("SELECT * FROM jobs WHERE id=?", (job_id,))


def job_for(project_id: str, kind: str, target_id: str) -> dict[str, Any] | None:
    return one(
        "SELECT * FROM jobs WHERE project_id=? AND kind=? AND target_id=?",
        (project_id, kind, target_id),
    )


def list_jobs(project_id: str | None = None) -> list[dict[str, Any]]:
    if project_id:
        return query("SELECT * FROM jobs WHERE project_id=? ORDER BY id", (project_id,))
    return query("SELECT * FROM jobs ORDER BY id")


def reset_jobs(project_id: str, kinds: Iterable[str]) -> None:
    kinds = list(kinds)
    marks = ",".join("?" for _ in kinds)
    execute(
        f"UPDATE jobs SET status='queued', attempts=0, error=NULL, finished_at=NULL,"
        f" asset_id=NULL WHERE project_id=? AND kind IN ({marks})",
        (project_id, *kinds),
    )


# ----------------------------------------------------------------- accounts --
def add_account(account_id: str, label: str, profile: str, notes: str = "") -> None:
    execute(
        "INSERT OR REPLACE INTO accounts (id, label, profile, notes, created_at) VALUES (?,?,?,?,?)",
        (account_id, label, profile, notes, now_iso()),
    )


def list_accounts(enabled_only: bool = False) -> list[dict[str, Any]]:
    sql = "SELECT * FROM accounts"
    if enabled_only:
        sql += " WHERE enabled=1"
    return query(sql + " ORDER BY created_at")


def get_account(account_id: str) -> dict[str, Any] | None:
    return one("SELECT * FROM accounts WHERE id=?", (account_id,))


def update_account(account_id: str, **fields: Any) -> None:
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    execute(f"UPDATE accounts SET {sets} WHERE id=?", (*fields.values(), account_id))


def account_event(account_id: str | None, action: str, reason: str, *, project_id: str | None = None,
                  actor: str = "system") -> None:
    execute(
        "INSERT INTO account_events (account_id, project_id, action, reason, actor, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (account_id, project_id, action, reason, actor, now_iso()),
    )


def account_events(limit: int = 100) -> list[dict[str, Any]]:
    return query("SELECT * FROM account_events ORDER BY id DESC LIMIT ?", (limit,))


# --------------------------------------------------- per-account Flow state --
def get_flow_project(project_id: str, account_id: str) -> str | None:
    row = one(
        "SELECT flow_project_id FROM flow_projects WHERE project_id=? AND account_id=?",
        (project_id, account_id),
    )
    return row["flow_project_id"] if row else None


def set_flow_project(project_id: str, account_id: str, flow_project_id: str) -> None:
    execute(
        "INSERT OR REPLACE INTO flow_projects (project_id, account_id, flow_project_id, created_at)"
        " VALUES (?,?,?,?)",
        (project_id, account_id, flow_project_id, now_iso()),
    )


def get_entity(project_id: str, account_id: str, character_id: str) -> dict[str, Any] | None:
    return one(
        "SELECT * FROM entities WHERE project_id=? AND account_id=? AND character_id=?",
        (project_id, account_id, character_id),
    )


def set_entity(project_id: str, account_id: str, character_id: str, entity_id: str,
               entity_name: str | None) -> None:
    execute(
        "INSERT OR REPLACE INTO entities (project_id, account_id, character_id, entity_id,"
        " entity_name, created_at) VALUES (?,?,?,?,?,?)",
        (project_id, account_id, character_id, entity_id, entity_name, now_iso()),
    )


def get_upload(project_id: str, account_id: str, source_path: str) -> str | None:
    row = one(
        "SELECT asset_id FROM uploads WHERE project_id=? AND account_id=? AND source_path=?",
        (project_id, account_id, source_path),
    )
    return row["asset_id"] if row else None


def set_upload(project_id: str, account_id: str, source_path: str, asset_id: str) -> None:
    execute(
        "INSERT OR REPLACE INTO uploads (project_id, account_id, source_path, asset_id, created_at)"
        " VALUES (?,?,?,?,?)",
        (project_id, account_id, source_path, asset_id, now_iso()),
    )


def set_active_account(project_id: str, account_id: str) -> None:
    update_project(project_id, active_account=account_id)


def get_active_account(project_id: str) -> str | None:
    project = get_project(project_id)
    return project["active_account"] if project else None


def asset_for_account(project_id: str, kind: str, target_id: str, account_id: str) -> dict[str, Any] | None:
    """Latest asset produced for this account (used to key uploads per account)."""
    return one(
        "SELECT * FROM assets WHERE project_id=? AND kind=? AND target_id=? AND account_id=?"
        " ORDER BY id DESC LIMIT 1",
        (project_id, kind, target_id, account_id),
    )


# ----------------------------------------------------------- switch requests --
def create_switch_request(project_id: str, kind: str, target_id: str, from_account: str | None,
                          to_account: str | None, reason: str) -> int:
    return execute(
        "INSERT INTO switch_requests (project_id, kind, target_id, from_account, to_account, reason,"
        " created_at) VALUES (?,?,?,?,?,?,?)",
        (project_id, kind, target_id, from_account, to_account, reason, now_iso()),
    )


def pending_switch_request(project_id: str | None = None) -> dict[str, Any] | None:
    if project_id:
        return one(
            "SELECT * FROM switch_requests WHERE status='pending' AND project_id=? ORDER BY id LIMIT 1",
            (project_id,),
        )
    return one("SELECT * FROM switch_requests WHERE status='pending' ORDER BY id LIMIT 1")


def get_switch_request(request_id: int) -> dict[str, Any] | None:
    return one("SELECT * FROM switch_requests WHERE id=?", (request_id,))


def resolve_switch(request_id: int, status: str) -> None:
    execute(
        "UPDATE switch_requests SET status=?, resolved_at=? WHERE id=?",
        (status, now_iso(), request_id),
    )


def list_switch_requests(limit: int = 50) -> list[dict[str, Any]]:
    return query("SELECT * FROM switch_requests ORDER BY id DESC LIMIT ?", (limit,))


# ------------------------------------------------------------------- assets --
def add_asset(project_id: str, kind: str, target_id: str, path: str, account_id: str | None,
              job_id: int | None) -> None:
    execute(
        "INSERT INTO assets (project_id, kind, target_id, path, account_id, job_id, created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (project_id, kind, target_id, path, account_id, job_id, now_iso()),
    )


def list_assets(project_id: str, kind: str | None = None) -> list[dict[str, Any]]:
    if kind:
        return query(
            "SELECT * FROM assets WHERE project_id=? AND kind=? ORDER BY id", (project_id, kind)
        )
    return query("SELECT * FROM assets WHERE project_id=? ORDER BY id", (project_id,))


def latest_asset(project_id: str, kind: str, target_id: str) -> dict[str, Any] | None:
    return one(
        "SELECT * FROM assets WHERE project_id=? AND kind=? AND target_id=? ORDER BY id DESC LIMIT 1",
        (project_id, kind, target_id),
    )

