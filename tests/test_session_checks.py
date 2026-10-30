"""Session checks against gflow-cli's real `auth status` / `auth renew` output.

The live run kept failing with 401 on `character create` while `credits` read
1050, and my "renew" never called a renewal at all. These tests pin the actual
shapes of the two commands so that cannot regress:

* `auth status` has **no** `--json` flag and exits 0 only when the session is
  verified; its last prose line is the CLI's own remediation.
* `auth renew --json` prints a **list** of per-profile rows with a `result` field
  (`authenticated`, `in_use`, `engine_downgrade`, ...), not a dict.
"""
from __future__ import annotations

import json

from flowpipe.flow import gflow

VERIFIED = """
  profile: D:\\Tools\\gflow\\profile_farman
  exists: True
  cookies_present: True
  cookies_path: D:\\Tools\\gflow\\profile_farman\\Default\\Cookies
  browser_engine: playwright
Probing Flow session (may take up to ~45s on a slow network)...
Flow session verified as farman@gmail.com.
"""

DEAD = """
  profile: D:\\Tools\\gflow\\profile_farman
  exists: True
  cookies_present: True
  browser_engine: playwright
Probing Flow session (may take up to ~45s on a slow network)...
The session for this profile has expired.
Run gflow auth login --profile farman to refresh the session.
"""

MARKER_MISSING = """
  profile: D:\\Tools\\gflow\\profile_farman
  exists: True
  cookies_present: True
Probing Flow session (may take up to ~45s on a slow network)...
This profile has no .gflow_browser_strategy marker.
Re-run gflow auth login --browser chrome --profile farman to rewrite it.
"""


def test_a_verified_session_carries_the_account():
    status = gflow.parse_auth_status(0, VERIFIED)
    assert status.ok is True
    assert status.account == "farman@gmail.com"
    assert "cookies_present: True" in status.detail
    assert status.hint == ""


def test_a_dead_session_passes_the_cli_advice_through():
    status = gflow.parse_auth_status(1, DEAD)
    assert status.ok is False
    assert "gflow auth login --profile farman" in status.hint


def test_a_missing_browser_strategy_marker_is_reported_as_such():
    """The one that looks like a healthy account but cannot create anything."""
    status = gflow.parse_auth_status(1, MARKER_MISSING)
    assert status.ok is False
    assert "--browser chrome" in status.hint
    assert "strategy marker" in status.hint


def test_exit_zero_without_a_verdict_is_not_success():
    """Fail closed: only an explicit "session verified" counts."""
    status = gflow.parse_auth_status(0, "  exists: True\n  cookies_present: True\n")
    assert status.ok is False


def test_renew_reports_a_list_of_per_profile_rows():
    payload = [{"profile": "farman", "result": "authenticated",
                "email": "farman@gmail.com", "hint": None}]
    status = gflow.parse_auth_renew(0, "", payload)
    assert status.ok is True
    assert status.account == "farman@gmail.com"


def test_a_refused_renewal_is_not_success_even_with_exit_zero():
    payload = [{"profile": "farman", "result": "labs_unverified", "email": None, "hint": None}]
    status = gflow.parse_auth_renew(0, "", payload)
    assert status.ok is False
    assert "labs" in status.hint


def test_every_refusal_result_explains_itself():
    for result in ("no_session", "in_use", "launch_error", "engine_downgrade",
                   "public_landing", "labs_stale", "labs_unverified", "verification_error"):
        status = gflow.parse_auth_renew(1, "", [{"profile": "p", "result": result}])
        assert status.ok is False
        assert status.hint, f"{result} produced no advice"


def test_a_driver_hint_from_the_cli_wins():
    payload = [{"profile": "p", "result": "engine_downgrade",
                "hint": "profile was written by a newer browser; upgrade playwright"}]
    status = gflow.parse_auth_renew(1, "", payload)
    assert "upgrade playwright" in status.hint


def test_renew_without_json_falls_back_to_the_exit_code():
    ok = gflow.parse_auth_renew(0, "farman: session renewed and verified.\n", None)
    assert ok.ok is True and "renewed" in ok.hint
    bad = gflow.parse_auth_renew(1, "farman: session could not be renewed.\n", None)
    assert bad.ok is False


def test_auth_status_asks_without_json(monkeypatch):
    """`auth status` rejects --json; passing it would look like an auth failure."""
    driver = gflow.GflowDriver(binary="gflow")
    seen = {}

    def fake_run(args, **kwargs):
        seen["args"] = args
        seen["profile"] = kwargs.get("profile")
        return 0, VERIFIED, None

    driver.run = fake_run
    status = driver.auth_status("farman")
    assert seen["args"] == ["auth", "status"]
    assert seen["profile"] == "farman"
    assert status.ok is True


def test_renew_asks_for_json(monkeypatch):
    driver = gflow.GflowDriver(binary="gflow")
    seen = {}

    def fake_run(args, **kwargs):
        seen["args"] = args
        return 0, "", json.dumps([{"profile": "farman", "result": "authenticated"}])

    driver.run = fake_run
    assert driver.renew_auth("farman").ok is True
    assert seen["args"] == ["auth", "renew", "--json"]


# --------------------------------------------------------------------------
# where the profiles live: one answer for the app, the login script and a
# hand-typed command. A bare cmd window has no GFLOW_CLI_HOME, so gflow looks in
# %LOCALAPPDATA%\ffroliva\gflow-cli and reports "No profiles found" for a profile
# that is signed in perfectly well under D:\Tools\gflow.
# --------------------------------------------------------------------------
def test_the_profile_store_is_resolved_without_inheriting_the_shell(tmp_path, monkeypatch):
    from flowpipe.config import Settings

    settings = Settings()
    settings.gflow_home = ""
    monkeypatch.setenv("FLOWPIPE_TOOLS_DIR", str(tmp_path))
    assert settings.gflow_cli_home == str(tmp_path / "gflow")

    settings.gflow_home = "D:\\elsewhere"
    assert settings.gflow_cli_home == "D:\\elsewhere"

    monkeypatch.delenv("FLOWPIPE_TOOLS_DIR", raising=False)
    settings.gflow_home = ""
    # no tool root (a plain PATH install): leave the CLI's own default alone
    assert settings.gflow_cli_home in {"", None} or not settings.gflow_cli_home


def test_every_gflow_call_states_the_profile_store(tmp_path, monkeypatch):
    """Not inheriting it is the bug: an app started without run.bat saw no session."""
    from flowpipe.config import SETTINGS

    monkeypatch.setattr(SETTINGS, "gflow_home", str(tmp_path / "gflow"))
    monkeypatch.delenv("GFLOW_CLI_HOME", raising=False)
    assert gflow._env()["GFLOW_CLI_HOME"] == str(tmp_path / "gflow")


def test_health_reports_the_profile_store(tmp_path, monkeypatch):
    from flowpipe import api
    from flowpipe.config import SETTINGS

    monkeypatch.setattr(SETTINGS, "gflow_home", str(tmp_path))
    health = api.health()
    assert health["gflow"]["home"] == str(tmp_path)
    assert health["gflow"]["home_exists"] is True


# --------------------------------------------------------------------------
# flow.google.com cannot create characters: the labs createEntity route was
# retired. That must never be treated as a login problem, and it must not block
# the scenes that referenced the character.
# --------------------------------------------------------------------------
DEPRECATED_ENTITY = """
{"error_class": "FlowRpcDeprecatedError", "problem": {
   "type": "https://gflow-cli.dev/errors/rpc-deprecated",
   "title": "Flow RPCs have been deprecated", "status": 404,
   "detail": "createEntity is no longer served", "route": "createEntity"},
 "cli_command": "character create", "event": "error_raised"}
"""


def test_a_retired_createentity_route_is_not_an_auth_error():
    from flowpipe.flow.base import CharacterUnsupported

    error = gflow.classify_exit(7, DEPRECATED_ENTITY)
    assert isinstance(error, CharacterUnsupported)
    assert error.retryable is False and error.needs_human is False
    assert "flow.google.com" in str(error) or "createEntity" in str(error)


def test_the_401_from_the_same_route_is_also_classified():
    """What this machine actually returned: 401 with route createEntity."""
    payload = """
    {"error_class": "AuthExpiredError", "problem": {
       "title": "Authentication expired", "status": 401, "detail": "UNAUTHORIZED",
       "route": "createEntity"}, "cli_command": "character create"}
    """
    from flowpipe.flow.base import CharacterUnsupported

    assert isinstance(gflow.classify_exit(3, payload), CharacterUnsupported)


def test_a_migrated_session_is_reported():
    migrated = """
      profile: D:\\Tools\\gflow\\profile_farman
      exists: True
      cookies_present: True
      browser_engine: playwright
    2026-10-30T15:40:03Z [warning] auth_migrated_host_fallback_authenticated detail='labs NextAuth absent; flow.google.com OSID session + SSO cookies present'
    Flow session verified as someone@gmail.com.
    """
    status = gflow.parse_auth_status(0, migrated)
    assert status.ok is True
    assert status.migrated is True


def test_a_normal_session_is_not_migrated():
    status = gflow.parse_auth_status(0, VERIFIED)
    assert status.ok is True and status.migrated is False


def test_a_character_free_project_keeps_running(pipeline):
    """The failure must cost a skip, not the whole project."""
    from flowpipe import db, projects, runner
    from flowpipe.models import Manifest

    db.create_project("no-chars", "no chars", "cinematic-short")
    manifest = Manifest.model_validate({
        "project": {"topic": "lighthouse"},
        "characters": [{"name": "Mara", "face_prompt": "a keeper", "body_prompt": "a keeper"}],
        "scenes": [
            {"id": "scene-01", "image_prompt": "a lighthouse", "animation_prompt": "push in",
             "character_ids": ["mara"]},
            {"id": "scene-02", "image_prompt": "a bottle", "animation_prompt": "tilt"},
        ],
    })
    projects.write_manifest("no-chars", manifest)
    runner.prepare_jobs("no-chars")

    dropped = runner.drop_characters("no-chars", "account cannot create characters")
    assert dropped == {"characters": 1, "scenes": 1}

    reread = projects.read_manifest("no-chars")
    assert reread.characters == []
    assert all(scene.character_ids == [] for scene in reread.scenes)
    # the character work is skipped, and the scene jobs are untouched
    kinds = {row["kind"]: row["status"] for row in db.list_jobs("no-chars")}
    assert kinds["character"] == "skipped"
    assert kinds["image"] == "queued"
