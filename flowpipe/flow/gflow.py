"""Driver backed by gflow-cli (ffroliva/gflow-cli).

Why shell out instead of importing: gflow-cli is an alpha, reverse-engineered CLI
that tracks Google Flow's UI weekly. Pinning it as a subprocess dependency means
`pip install -U gflow-cli` fixes a breakage without touching this repo, and the
CLI's documented exit codes give us typed failures.

Syntax below was checked against gflow-cli's docs at build time
(docs/USAGE.md, docs/CHARACTER.md). It is *not* guaranteed — Flow and the CLI
both drift. Run `scripts/check_gflow.py` on the machine that has credentials to
verify every flag this module emits before a real run, and keep
`GFLOW_CLI_DRY_RUN=1` handy to print the exact argv.

Credit safety: every generation subcommand is invoked with `--json` and a strict
timeout. A non-zero exit is never retried blindly — see classify_exit() — because
Flow has usually already been charged or the UI has drifted.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from ..config import SETTINGS
from .base import (
    Account,
    AssetMissing,
    AuthExpired,
    Capabilities,
    ChallengeRequired,
    CharacterResult,
    CreditReading,
    DriverUnavailable,
    FlowError,
    NoCredits,
    NoFlowAccess,
    ProfileLocked,
    ReferenceAsset,
    SelectorDrift,
    TransientFlowError,
    pick_model,
)

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
VIDEO_EXTS = {".mp4", ".webm", ".mov"}

# Documented gflow-cli class exit codes -> our error types.
EXIT_MAP: dict[int, type[FlowError]] = {
    3: AuthExpired,
    8: AuthExpired,
    9: AssetMissing,
    11: ProfileLocked,   # ambiguous: also "invalid project"; refined by message
    12: AuthExpired,     # interactive login timed out -> a human is needed
    23: SelectorDrift,
    24: DriverUnavailable,
    28: TransientFlowError,  # UI cohort flapped; retryable by design
    31: TransientFlowError,  # Flow app failed to load
    39: NoFlowAccess,
    7: TransientFlowError,   # wire format change
}

CHALLENGE_HINTS = (
    "captcha",
    "recaptcha",
    "unusual traffic",
    "verify you are human",
    "verification challenge",
    "challenge required",
)

PROFILE_HINTS = ("profilelocked", "profile locked", "processsingleton", "profileaccess")


def _env() -> dict[str, str]:
    env = dict(os.environ)
    if SETTINGS.gflow_home:
        env["GFLOW_CLI_HOME"] = SETTINGS.gflow_home
    env.setdefault("GFLOW_CLI_LOG_FORMAT", "json")
    env.setdefault("PYTHONUNBUFFERED", "1")
    return env


def classify_exit(code: int, output: str) -> FlowError:
    """Turn (exit code, output) into a typed error the runner can act on."""
    text = output.lower()
    if any(hint in text for hint in CHALLENGE_HINTS):
        return ChallengeRequired(
            "Flow is asking for human verification. Solve it in the browser window, "
            "then press Resume. The pipeline never solves these automatically."
        )
    if "insufficient" in text or "no credits" in text or "out of credits" in text:
        return NoCredits(output.strip().splitlines()[-1][:300] if output.strip() else "no credits")
    if code == 11 and any(hint in text for hint in PROFILE_HINTS):
        return ProfileLocked("gflow profile is in use by another process")
    error_cls = EXIT_MAP.get(code, FlowError)
    tail = " | ".join(line for line in output.strip().splitlines()[-6:])[:600]
    return error_cls(f"gflow exit {code}: {tail}")


def _collect_paths(payload: Any, exts: set[str]) -> list[Path]:
    """Find every file path inside a JSON reply, whatever shape it took."""
    found: list[Path] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        elif isinstance(node, str) and Path(node).suffix.lower() in exts:
            found.append(Path(node))

    walk(payload)
    return found


def _newest(directory: Path, exts: set[str], since: float) -> Path | None:
    candidates = [
        p for p in directory.glob("*")
        if p.is_file() and p.suffix.lower() in exts and p.stat().st_mtime >= since - 2
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


class GflowDriver:
    name = "gflow"

    def __init__(self, binary: str | None = None) -> None:
        self.binary = binary or SETTINGS.gflow_bin
        self._capabilities: Capabilities | None = None
        self._dry_run = os.environ.get("GFLOW_CLI_DRY_RUN", "").strip().lower() in {"1", "true", "yes"}

    # ------------------------------------------------------------- plumbing --
    def _resolve_binary(self) -> str:
        path = shutil.which(self.binary)
        if not path:
            raise DriverUnavailable(
                f"`{self.binary}` was not found on PATH. Install gflow-cli "
                "(`pipx install gflow-cli` or `uv tool install gflow-cli`) or switch the "
                "driver to 'mock' in Settings."
            )
        return path

    def build(self, args: list[str], profile: str | None = None) -> list[str]:
        """Full argv for a gflow invocation. `--profile` is per-subcommand."""
        argv = [self._resolve_binary(), *args]
        if profile:
            argv += ["--profile", profile]
        return argv

    def run(self, args: list[str], *, profile: str | None = None, timeout: int | None = None,
            allow_failure: bool = False) -> tuple[int, str, dict[str, Any] | None]:
        argv = self.build(args, profile)
        if self._dry_run:
            print("DRY RUN:", " ".join(argv))
            return 0, "", {"dry_run": True, "argv": argv}
        timeout = timeout or SETTINGS.job_timeout_seconds
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, timeout=timeout, env=_env(),
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired as exc:
            raise TransientFlowError(
                f"gflow timed out after {timeout}s. The generation may still complete in Flow; "
                "check the Flow project before retrying or you may pay twice."
            ) from exc
        output = f"{proc.stdout}\n{proc.stderr}"
        payload = self._parse_json(proc.stdout)
        if proc.returncode != 0 and not allow_failure:
            raise classify_exit(proc.returncode, output)
        return proc.returncode, output, payload

    @staticmethod
    def _parse_json(text: str) -> dict[str, Any] | None:
        text = text.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except Exception:
            pass
        for line in reversed(text.splitlines()):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    return json.loads(line)
                except Exception:
                    continue
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except Exception:
                return None
        return None

    # --------------------------------------------------------- capabilities --
    def capabilities(self) -> Capabilities:
        if self._capabilities is not None:
            return self._capabilities
        # Seed values only. Nothing is chosen from here until the CLI has
        # answered `gflow models --json`; if that fails the list is marked as a
        # guess so the dashboard never presents invented labels as facts.
        caps = Capabilities(
            driver=self.name,
            image_models=["nano2", "nano2-pro", "nano2-lite"],
            video_models=["omni-flash", "veo-quality"],
            default_image_model="nano2",
            default_video_model="omni-flash",
            supports_character_entity=True,
            supports_end_frame=True,
            catalog_source="fallback",
            notes=[
                "Placeholder list: `gflow` did not answer, so these model labels are NOT "
                "verified. The real catalog is read from `gflow models --json` on every run."
            ],
        )
        try:
            _, _, payload = self.run(["models", "--json"], allow_failure=True)
        except FlowError:
            payload = None
        if isinstance(payload, dict):
            caps.catalog_source = "runtime"
            caps.notes = ["Model catalog read from `gflow models --json` at call time."]
            images = self._model_names(payload.get("images") or payload.get("image_models") or [])
            videos = self._model_names(payload.get("videos") or payload.get("video_models") or [])
            if images:
                caps.image_models = images
            if videos:
                caps.video_models = videos
            default_image = payload.get("default_image") or payload.get("default_image_model")
            default_video = payload.get("default_video") or payload.get("default_video_model")
            if isinstance(default_image, str) and default_image in caps.image_models:
                caps.default_image_model = default_image
            if isinstance(default_video, str) and default_video in caps.video_models:
                caps.default_video_model = default_video
        self._capabilities = caps
        return caps

    @staticmethod
    def _model_names(entries: Any) -> list[str]:
        names: list[str] = []
        if isinstance(entries, dict):
            entries = list(entries.values())
        if not isinstance(entries, list):
            return names
        for entry in entries:
            if isinstance(entry, str):
                names.append(entry)
            elif isinstance(entry, dict):
                aliases = entry.get("aliases") or entry.get("cli_aliases") or []
                name = entry.get("name") or entry.get("alias") or (aliases[0] if aliases else None)
                if name:
                    names.append(str(name))
        return names

    def model_for_video(self, preference: str) -> str:
        caps = self.capabilities()
        return pick_model(preference, caps.video_models, caps.default_video_model)

    def model_for_image(self, preference: str) -> str:
        caps = self.capabilities()
        return pick_model(preference, caps.image_models, caps.default_image_model)

    # -------------------------------------------------------------- credits --
    def credits(self, account: Account, *, refresh: bool = False) -> CreditReading:
        args = ["credits", "user", "--json"]
        if refresh:
            args.append("--refresh")
        code, output, payload = self.run(args, profile=account.cli_profile, allow_failure=True,
                                         timeout=180)
        if code != 0:
            error = classify_exit(code, output)
            # Reading the balance is free and side-effect free, so a transient
            # Flow-app failure (exit 28/31: the app loaded but the balance reply
            # was unreadable - gflow calls this retryable) gets exactly one more
            # try. Without it the balance stays unknown, and an unknown balance
            # cannot be checked before spending: `has_capacity()` has to say yes.
            if isinstance(error, TransientFlowError) and not isinstance(error, ChallengeRequired):
                time.sleep(2.0)
                code, output, payload = self.run(args, profile=account.cli_profile,
                                                 allow_failure=True, timeout=180)
                error = classify_exit(code, output) if code != 0 else None
            if error is not None:
                return CreditReading(credits=None,
                                     authenticated=not isinstance(error, AuthExpired),
                                     error=error.kind)
        balance = None
        if isinstance(payload, dict):
            for key in ("credits", "balance", "total_credits", "remaining"):
                value = payload.get(key)
                if isinstance(value, (int, float)):
                    balance = int(value)
                    break
            if balance is None and isinstance(payload.get("profiles"), list):
                for row in payload["profiles"]:
                    if (
                        isinstance(row, dict)
                        and row.get("profile") == account.cli_profile
                        and isinstance(row.get("credits"), (int, float))
                    ):
                        balance = int(row["credits"])
        if balance is None:
            match = re.search(r"(\d+)\s*(?:veo\s*)?credits", output, re.I)
            if match:
                balance = int(match.group(1))
        return CreditReading(credits=balance)

    # -------------------------------------------------------------- project --
    def ensure_project(self, account: Account, name: str) -> str:
        code, output, payload = self.run(
            ["project", "create", "--name", name, "--json"], profile=account.cli_profile,
            allow_failure=True,
        )
        project_id = None
        if isinstance(payload, dict):
            project_id = payload.get("project_id") or payload.get("id") or payload.get("projectId")
        if not project_id:
            match = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", output)
            project_id = match.group(1) if match else None
        if not project_id:
            raise classify_exit(code, output) if code else FlowError(
                f"could not read a project id from gflow project create: {output[:300]}"
            )
        return str(project_id)

    # ----------------------------------------------------------- characters --
    def create_character(self, account: Account, flow_project: str, *, name: str,
                         face_prompt: str, body_prompt: str, out_dir: Path) -> CharacterResult:
        out_dir.mkdir(parents=True, exist_ok=True)
        started = time.time()
        _, output, payload = self.run(
            [
                "character", "create",
                "--project", flow_project,
                "--name", name,
                "--face-prompt", face_prompt,
                "--body-prompt", body_prompt,
                "--json",
            ],
            profile=account.cli_profile,
        )
        images = [p for p in _collect_paths(payload, IMAGE_EXTS) if p.exists()]
        if not images:
            newest = _newest(out_dir, IMAGE_EXTS, started)
            if newest:
                images = [newest]
        if not images:
            raise FlowError(
                "gflow reported success but no downloaded character image was found. "
                "Look for the files under the gflow output directory."
            )
        entity_id = None
        entity_name = name
        if isinstance(payload, dict):
            entity_id = payload.get("entity_id") or payload.get("id") or payload.get("entityId")
        return CharacterResult(images=images, entity_id=entity_id, entity_name=entity_name,
                               raw=payload or {})

    # --------------------------------------------------------------- upload --
    def upload(self, account: Account, flow_project: str, path: Path) -> str:
        _, output, payload = self.run(
            ["image", "upload", str(path), "--project", flow_project, "--json"],
            profile=account.cli_profile,
        )
        asset_id = None
        if isinstance(payload, dict):
            asset_id = payload.get("asset_uuid") or payload.get("asset_id") or payload.get("uuid")
        if not asset_id:
            match = re.search(
                r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", output
            )
            asset_id = match.group(1) if match else None
        if not asset_id:
            raise FlowError(f"could not read an asset uuid from `gflow image upload`: {output[:300]}")
        return str(asset_id)

    # ---------------------------------------------------------------- image --
    def generate_image(self, account: Account, flow_project: str, *, prompt: str, out_dir: Path,
                       aspect: str, model: str | None = None,
                       references: list[ReferenceAsset] | None = None) -> Path:
        out_dir.mkdir(parents=True, exist_ok=True)
        started = time.time()
        refs = references or []
        uploaded = [r.uploaded_id for r in refs if r.uploaded_id]
        # With local asset UUIDs we need `image i2i --ref`; character entities are
        # also accepted there, so one command covers both reference kinds.
        command = "i2i" if uploaded else "t2i"
        args = ["image", command, prompt]
        for asset_id in uploaded:
            args += ["--ref", asset_id]
        args += [
            "--project", flow_project,
            "--model", model or self.model_for_image(SETTINGS.image_model),
            "--aspect", aspect or SETTINGS.aspect_ratio,
            "--count", "1",
            "--out", str(out_dir),
            "--json",
        ]
        for ref in refs:
            if ref.entity_id:
                args += ["--reference-entity", ref.entity_id,
                         "--reference-entity-name", ref.entity_name or ""]
        _, _, payload = self.run(args, profile=account.cli_profile)
        images = [p for p in _collect_paths(payload, IMAGE_EXTS) if p.exists()]
        if not images:
            newest = _newest(out_dir, IMAGE_EXTS, started)
            if newest:
                images = [newest]
        if not images:
            raise FlowError("gflow reported success but no image file was found on disk")
        return images[-1]

    # ----------------------------------------------------------------- clip --
    def generate_clip(self, account: Account, flow_project: str, *, prompt: str, out_dir: Path,
                      start_frame: Path, end_frame: Path | None = None, duration: int = 8,
                      resolution: str = "720p", model: str | None = None,
                      aspect: str = "16:9", start_asset_id: str | None = None) -> Path:
        out_dir.mkdir(parents=True, exist_ok=True)
        started = time.time()
        # Upload the still into this account's Flow project and pass the asset
        # UUID: gflow selects a UUID from the project's media picker, which is
        # more reliable than a local path re-upload. Both accept `--initial-frame`.
        initial: str = start_asset_id or str(start_frame)
        if not start_asset_id:
            try:
                initial = self.upload(account, flow_project, start_frame)
            except FlowError:
                initial = str(start_frame)
        args = ["video", "i2v", "--initial-frame", initial]
        if end_frame is not None:
            end_asset: str | None = None
            try:
                end_asset = self.upload(account, flow_project, end_frame)
            except FlowError:
                end_asset = None
            args += ["--end-frame", end_asset or str(end_frame)]
        args += [
            prompt,
            "--project", flow_project,
            "--model", model or self.model_for_video(SETTINGS.video_model_preference),
            "--duration", str(duration),
            "--resolution", resolution,
            "--out-dir", str(out_dir),
            "--json",
        ]
        _, _, payload = self.run(args, profile=account.cli_profile)
        clips = [p for p in _collect_paths(payload, VIDEO_EXTS) if p.exists()]
        if not clips:
            newest = _newest(out_dir, VIDEO_EXTS, started)
            if newest:
                clips = [newest]
        if not clips:
            raise FlowError("gflow reported success but no mp4 was found on disk")
        return clips[-1]


def preflight() -> dict[str, Any]:
    """Cheap, read-only check used by the Settings page and scripts/check_gflow.py."""
    driver = GflowDriver()
    report: dict[str, Any] = {"binary": shutil.which(SETTINGS.gflow_bin), "checks": []}
    try:
        argv = driver.build(["--version"])
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=60, env=_env())
        report["version"] = (proc.stdout or proc.stderr).strip().splitlines()[0] if proc.returncode == 0 else None
        report["checks"].append({"name": "version", "ok": proc.returncode == 0})
    except FlowError as exc:
        report["checks"].append({"name": "version", "ok": False, "error": str(exc)})
        return report
    caps = driver.capabilities()
    report["capabilities"] = caps.to_dict()
    return report
