"""Account pool: credit tracking, selection, and the failover decision.

Two rules shape this module:

1. **Credit is checked before money is spent.** Video generation is the only
   step that consumes Veo credits, so the balance is refreshed before every clip.
2. **Switching accounts is a human decision by default.** ``auto_failover`` is off
   unless an operator turns it on, so a low balance produces a review request in
   the dashboard rather than a silent hop to another Google account.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from . import db
from .config import SETTINGS
from .flow import get_driver
from .flow.base import Account, FlowError, NoCredits
from .models import now_iso, slugify

CREDIT_TTL_SECONDS = 600
# A session renewal is cheap and headless; do it at most this often per account.
RENEW_TTL_SECONDS = 3600

_last_check: dict[str, float] = {}
_last_renew: dict[str, float] = {}


def as_account(row: dict[str, Any]) -> Account:
    return Account(id=row["id"], label=row["label"], profile=row["profile"], notes=row.get("notes") or "")


# ------------------------------------------------------------------- CRUD ----
def add_account(label: str, profile: str = "", notes: str = "", account_id: str | None = None) -> dict:
    account_id = account_id or slugify(label)
    if db.get_account(account_id):
        raise ValueError(f"account '{account_id}' already exists")
    db.add_account(account_id, label, profile or account_id, notes)
    db.account_event(account_id, "created", "added to pool", actor="human")
    return db.get_account(account_id) or {}


def remove_account(account_id: str) -> None:
    db.execute("DELETE FROM accounts WHERE id=?", (account_id,))
    db.account_event(account_id, "removed", "removed from pool", actor="human")


def update_account(account_id: str, **fields: Any) -> dict | None:
    if "enabled" in fields:
        fields["enabled"] = 1 if fields["enabled"] else 0
    db.update_account(account_id, **fields)
    return db.get_account(account_id)


def decorated(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add derived numbers the dashboard shows."""
    per_clip = max(1, SETTINGS.credits_per_clip)
    out = []
    for row in rows:
        credits = row.get("credits")
        out.append({
            **row,
            "enabled": bool(row.get("enabled", 1)),
            "estimated_clips": None if credits is None else credits // per_clip,
            "credits_age_seconds": _age(row.get("credits_at")),
        })
    return out


def _age(timestamp: str | None) -> int | None:
    if not timestamp:
        return None
    try:
        from datetime import datetime

        then = datetime.fromisoformat(timestamp)
        return int((datetime.now(then.tzinfo) - then).total_seconds())
    except Exception:
        return None


# ----------------------------------------------------------------- credits ---
def refresh_credits(account_id: str, *, refresh: bool = False, force: bool = False) -> dict[str, Any]:
    row = db.get_account(account_id)
    if not row:
        raise KeyError(account_id)
    last = _last_check.get(account_id, 0)
    if not force and not refresh and time.time() - last < CREDIT_TTL_SECONDS and row.get("credits") is not None:
        return db.get_account(account_id) or row

    account = as_account(row)
    driver = get_driver()
    try:
        reading = driver.credits(account, refresh=refresh)
    except FlowError as exc:
        db.update_account(account_id, credits=None, credits_at=now_iso())
        db.account_event(account_id, "credit_check", f"{type(exc).__name__}: {exc}")
        return db.get_account(account_id) or row

    _last_check[account_id] = time.time()
    fields: dict[str, Any] = {"credits": reading.credits, "credits_at": now_iso()}
    if not reading.authenticated:
        fields["status"] = "needs_auth"
    elif reading.credits is not None:
        # A balance read that came back authenticated *is* proof the session
        # works, so it must also clear a human-action block. It used to reset only
        # low/exhausted/active, which left an account stuck on needs_human for
        # ever: the operator re-signed in, Refresh credits read 1050, and the row
        # still said needs_human so nothing would run.
        recovered = row["status"] in {"needs_human", "needs_auth"}
        if reading.credits <= 0:
            fields["status"] = "exhausted"
        elif reading.credits < SETTINGS.credit_threshold:
            fields["status"] = "low"
        elif row["status"] in {"low", "exhausted", "active"} or recovered:
            fields["status"] = "active"
        if recovered:
            db.account_event(
                account_id, "resumed",
                f"session verified by a credit reading ({reading.credits}); unlocked",
                actor="system",
            )
    db.update_account(account_id, **fields)
    db.account_event(
        account_id, "credit_check",
        f"credits={reading.credits}" + (f" error={reading.error}" if reading.error else ""),
    )
    return db.get_account(account_id) or row


def _driver_supports_auth() -> bool:
    try:
        driver = get_driver()
    except Exception:
        return False
    return hasattr(driver, "renew_auth")


def renew_session(account_id: str) -> dict[str, Any]:
    """Ask gflow to re-rotate cookies headlessly. Never solves a challenge.

    The returned row carries ``renewed``: whether the session is demonstrably
    usable again (a fresh balance reading came back). Without it the caller could
    not tell "renewal worked" from "the read failed but the local status still
    says active" - which is how an expired session used to burn three attempts
    with 20s and 40s backoff against an error that says `"retryable": false`.
    """
    cached = time.time() - _last_renew.get(account_id, 0) < RENEW_TTL_SECONDS
    if cached:
        row = db.get_account(account_id) or {}
        # A renewal this recent that did not restore the session still has not
        # restored it: do not pretend otherwise, or the caller retries forever.
        row["renewed"] = False
        return row
    _last_renew[account_id] = time.time()
    db.account_event(account_id, "auth", "attempting headless session renewal")
    row = db.get_account(account_id) or {}

    renewed_by_cli: bool | None = None
    if _driver_supports_auth():
        driver = get_driver()
        try:
            renewed_by_cli, detail = driver.renew_auth(row.get("profile") or account_id)
        except Exception as exc:  # a broken CLI must not take the dashboard down
            renewed_by_cli, detail = False, f"{type(exc).__name__}: {exc}"
        db.account_event(account_id, "auth", f"gflow auth renew -> {renewed_by_cli} {detail}"[:500])
        if renewed_by_cli:
            # ask the CLI what it thinks, not the balance: credits can read fine
            # while the create routes are still unauthenticated
            try:
                renewed_by_cli, status_detail = driver.auth_status(row.get("profile") or account_id)
                db.account_event(account_id, "auth", f"gflow auth status -> {status_detail}"[:500])
            except Exception:
                pass

    row = refresh_credits(account_id, refresh=True, force=True)
    if renewed_by_cli is True:
        healthy = True          # the CLI says the session is renewed: trust it over a balance read
    elif renewed_by_cli is False:
        # Asked and refused: do NOT fall back to "the balance still reads", which
        # is exactly the false positive that made the runner retry a dead session.
        healthy = False
    else:
        healthy = row.get("status") in {"active", "low", "exhausted"} and row.get("credits_at")
    if not healthy:
        db.update_account(account_id, status="needs_human")
        row = db.get_account(account_id) or row
        db.account_event(account_id, "auth", "headless renewal failed; a human sign-in is required")
    row["renewed"] = bool(healthy)
    return row


def reactivate_after_login(account_id: str) -> bool:
    """The operator says they signed in: re-check, and unlock the account if so.

    Resume used to requeue the jobs while the account stayed `needs_human`, so
    selection refused it and every job bounced straight back to waiting_account -
    the operator did the right thing and nothing ran.
    """
    row = db.get_account(account_id)
    if not row:
        return False
    refreshed = refresh_credits(account_id, refresh=True, force=True)
    if refreshed.get("status") in {"active", "low", "exhausted"} and refreshed.get("credits_at"):
        mark_active(account_id)
        return True
    db.update_account(account_id, status="needs_human")
    return False


def mark_needs_human(account_id: str, reason: str) -> None:
    db.update_account(account_id, status="needs_human")
    db.account_event(account_id, "human_login", reason)


def mark_active(account_id: str) -> None:
    db.update_account(account_id, status="active")
    db.account_event(account_id, "resumed", "operator confirmed the account is usable", actor="human")


def spend(account_id: str, amount: int | None = None) -> None:
    """Local estimate of credits burned, so the UI is right before the next check."""
    amount = SETTINGS.credits_per_clip if amount is None else amount
    row = db.get_account(account_id)
    if not row or row.get("credits") is None:
        return
    remaining = max(0, int(row["credits"]) - amount)
    fields: dict[str, Any] = {"credits": remaining}
    if remaining <= 0:
        fields["status"] = "exhausted"
    elif remaining < SETTINGS.credit_threshold:
        fields["status"] = "low"
    db.update_account(account_id, **fields)


# --------------------------------------------------------------- selection ---
@dataclass
class Selection:
    account: Account | None
    reason: str = ""
    needs_approval: bool = False
    to_account: str | None = None


def usable(row: dict[str, Any]) -> bool:
    if not row.get("enabled", 1):
        return False
    return row.get("status") not in {"needs_auth", "needs_human", "disabled"}


def has_capacity(row: dict[str, Any]) -> bool:
    credits = row.get("credits")
    if row.get("status") == "exhausted":
        return False
    if credits is None:
        return True  # unknown balance: let the driver find out
    return credits >= SETTINGS.credit_threshold


def candidates() -> list[dict[str, Any]]:
    """Usable accounts in a stable order (oldest first).

    Deliberately *not* sorted by balance: a project should stay on the account it
    started on until that account genuinely runs out, so a switch is always an
    event worth logging rather than a per-job lottery.
    """
    return [r for r in db.list_accounts(enabled_only=True) if usable(r)]


def select(project_id: str) -> Selection:
    """Decide which account runs the next paid Flow call for this project."""
    current_id = db.get_active_account(project_id)
    if current_id:
        current = db.get_account(current_id)
        if current and usable(current):
            if current.get("credits") is None or has_capacity(current):
                return Selection(as_account(current))
            filled = refresh_credits(current_id, force=True)
            if has_capacity(filled):
                return Selection(as_account(filled))
            replacement = _first_other(project_id, current_id)
            if replacement is None:
                return Selection(None, "no account has credits left", False, None)
            return _maybe_switch(project_id, current, replacement)

    rows = candidates()
    if not rows:
        pending = db.pending_switch_request(project_id)
        return Selection(None, "no usable account in the pool", pending is not None,
                         pending and pending["to_account"])
    chosen = rows[0]
    db.set_active_account(project_id, chosen["id"])
    db.account_event(chosen["id"], "switch", "first usable account for project", project_id=project_id)
    return Selection(as_account(chosen))


def _first_other(project_id: str, current_id: str) -> dict[str, Any] | None:
    for row in candidates():
        if row["id"] != current_id and has_capacity(row):
            return row
    return None


def _maybe_switch(project_id: str, current: dict[str, Any], replacement: dict[str, Any]) -> Selection:
    reason = (
        f"{current['label']} is at {current.get('credits')} credits "
        f"(threshold {SETTINGS.credit_threshold})"
    )
    if SETTINGS.auto_failover:
        db.set_active_account(project_id, replacement["id"])
        db.account_event(current["id"], "switch", f"auto: {reason} -> {replacement['label']}",
                         project_id=project_id)
        return Selection(as_account(replacement), reason=f"auto-switched: {reason}")

    pending = db.pending_switch_request(project_id)
    if pending:
        return Selection(None, reason, True, pending["to_account"])
    db.create_switch_request(project_id, "", "", current["id"], replacement["id"], reason)
    db.account_event(current["id"], "switch", f"approval requested: {reason} -> {replacement['label']}",
                     project_id=project_id)
    return Selection(None, reason, True, replacement["id"])


def approve_switch(request_id: int, *, approve: bool = True) -> dict[str, Any]:
    request = db.get_switch_request(request_id)
    if not request:
        raise KeyError(request_id)
    project_id = request["project_id"]
    db.resolve_switch(request_id, "approved" if approve else "denied")
    if approve and request["to_account"]:
        db.set_active_account(project_id, request["to_account"])
        db.account_event(
            request["to_account"], "switch",
            f"approved by operator: {request['reason']}", project_id=project_id, actor="human",
        )
        if project_id:
            db.update_project(project_id, status="running")
            db.execute(
                "UPDATE jobs SET status='queued', updated_at=? WHERE project_id=? AND status='waiting_account'",
                (now_iso(), project_id),
            )
    else:
        db.account_event(
            request["from_account"], "switch", "operator denied the account switch",
            project_id=project_id, actor="human",
        )
    return request


def ensure_credits_for_clip(account_id: str) -> None:
    """Refresh right before a paid call and refuse if the balance is gone."""
    row = refresh_credits(account_id, force=True)
    credits = row.get("credits")
    if credits is not None and credits < max(1, SETTINGS.credits_per_clip):
        raise NoCredits(
            f"{row['label']} has {credits} credits left, a clip costs about "
            f"{SETTINGS.credits_per_clip}."
        )
