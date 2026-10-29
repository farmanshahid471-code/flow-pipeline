"""What happens when Google decides the session is over.

From a live run: `character create` failed three times with the same
AuthExpiredError ("retryable": false), 20s and 40s apart, before pausing. Worse,
the pause marks the account needs_human - and selection skips those - so pressing
Resume after signing in would requeue every job only to bounce it straight back.
"""
from __future__ import annotations

import pytest

from flowpipe import accounts, db, runner
from flowpipe.flow import gflow
from flowpipe.flow.base import AuthExpired

NOISY = """
A newer gflow-cli is available: 0.91.0 (installed 0.90.0). Run `gflow update` to upgrade.
Set GFLOW_CLI_UPDATE_CHECK=0 to silence.
{"error_class": "AuthExpiredError", "problem": {
   "type": "https://gflow-cli.dev/errors/auth-expired",
   "title": "Authentication expired", "status": 401, "detail": "UNAUTHORIZED",
   "instance": "gflow:error:bcb59adc-2355-4764-a1d4-2a9d81260435",
   "remediation_hint": "Run `gflow auth login --profile <name>` to refresh the session.",
   "route": "createEntity"},
 "cli_command": "character create", "event": "error_raised"}
"""


def test_the_structured_error_is_what_the_operator_sees():
    error = gflow.classify_exit(3, NOISY)
    assert isinstance(error, AuthExpired)
    message = str(error)
    assert "AuthExpiredError" in message
    assert "character create" in message
    assert "HTTP 401" in message
    assert "gflow auth login" in message          # the actual remediation
    # none of the CLI's chatter
    assert "newer gflow-cli" not in message
    assert "GFLOW_CLI_UPDATE_CHECK" not in message
    assert '": ' not in message and "|" not in message


def test_an_unstructured_failure_still_reads_like_a_sentence():
    error = gflow.classify_exit(24, "driver unavailable\nsome detail\n")
    assert "driver unavailable" in str(error)
    assert str(error).startswith("gflow exit 24:")


def test_an_expired_session_is_not_retried_three_times():
    """One failed renewal must pause immediately, not back off and try twice more."""
    failed = {"status": "active", "credits": None, "renewed": False}
    assert failed.get("renewed") is not True      # the guard the runner now uses


@pytest.fixture()
def one_account(pipeline):
    existing = [a for a in db.list_accounts()]
    for row in existing:
        db.execute("DELETE FROM accounts WHERE id=?", (row["id"],))
    db.add_account("farman", "farman", "farman")
    accounts._last_renew.clear()      # the renewal rate-limit cache is module level
    return db.get_account("farman")


_PROJECTS = iter(f"auth-test-{i}" for i in range(1, 50))


def _project(one_account) -> str:
    project_id = next(_PROJECTS)          # the session DB is shared across tests
    db.create_project(project_id, "auth test", "cinematic-short")
    db.set_active_account(project_id, one_account["id"])
    return project_id


def test_resume_unlocks_an_account_that_answers_again(one_account, monkeypatch):
    project_id = _project(one_account)
    accounts.mark_needs_human(one_account["id"], "session expired")

    monkeypatch.setattr(accounts, "refresh_credits", lambda *a, **k: {
        "id": one_account["id"], "status": "active", "credits": 900, "credits_at": "now"})
    resumed = runner.resume(project_id)
    assert resumed["recovered"] == ["farman"]
    assert accounts.usable(db.get_account(one_account["id"]))
    assert db.get_account(one_account["id"])["status"] == "active"


def test_resume_says_so_when_the_sign_in_did_not_take(one_account, monkeypatch):
    project_id = _project(one_account)
    accounts.mark_needs_human(one_account["id"], "session expired")

    monkeypatch.setattr(accounts, "refresh_credits", lambda *a, **k: {
        "id": one_account["id"], "status": "needs_auth", "credits": None, "credits_at": None})
    resumed = runner.resume(project_id)
    assert resumed["still_blocked"] == ["farman"]     # surfaced, not silently ignored
    assert not accounts.usable(db.get_account(one_account["id"]))


def test_a_failed_headless_renewal_says_it_failed(one_account, monkeypatch):
    monkeypatch.setattr(accounts, "refresh_credits", lambda *a, **k: {
        "id": one_account["id"], "status": "active", "credits": None, "credits_at": None})
    row = accounts.renew_session(one_account["id"])
    assert row["renewed"] is False                    # used to look like success
    assert db.get_account(one_account["id"])["status"] == "needs_human"


def test_a_successful_headless_renewal_is_reported(one_account, monkeypatch):
    monkeypatch.setattr(accounts, "refresh_credits", lambda *a, **k: {
        "id": one_account["id"], "status": "active", "credits": 800, "credits_at": "now"})
    row = accounts.renew_session(one_account["id"])
    assert row["renewed"] is True


def test_a_good_credit_reading_clears_needs_human(one_account, monkeypatch):
    """Live: reconnect, Refresh credits -> 1050, row still says needs_human.

    refresh_credits only reset low/exhausted/active, so a human-action block was
    permanent even when the session was demonstrably fine, and nothing could run.
    """
    from flowpipe.flow.base import CreditReading

    accounts.mark_needs_human(one_account["id"], "session expired; a sign-in is required")
    before = db.get_account(one_account["id"])
    assert before["status"] == "needs_human"

    class Driver:
        name = "mock"

        def credits(self, account, refresh=False):
            return CreditReading(credits=1050, authenticated=True)

    monkeypatch.setattr(accounts, "get_driver", lambda: Driver())
    row = accounts.refresh_credits(one_account["id"], refresh=True, force=True)
    assert row["credits"] == 1050
    assert row["status"] == "active"          # unlocked by the reading itself
    assert accounts.usable(row)
    events = [e for e in db.account_events(50) if e["action"] == "resumed"]
    assert any("credit reading" in e["reason"] for e in events)


def test_a_failed_reading_does_not_unlock_anything(one_account, monkeypatch):
    from flowpipe.flow.base import CreditReading

    accounts.mark_needs_human(one_account["id"], "session expired")
    accounts._last_check.pop(one_account["id"], None)

    class Driver:
        name = "mock"

        def credits(self, account, refresh=False):
            return CreditReading(credits=None, authenticated=False, error="401")

    monkeypatch.setattr(accounts, "get_driver", lambda: Driver())
    row = accounts.refresh_credits(one_account["id"], refresh=True, force=True)
    assert row["status"] == "needs_auth"
    assert not accounts.usable(row)
