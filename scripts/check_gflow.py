#!/usr/bin/env python3
"""Verify the gflow-cli surface this pipeline depends on, before spending credits.

Run this on the machine that has a signed-in gflow profile:

    python scripts/check_gflow.py --profile my-account

It checks the binary, the model catalog, credits for a profile, and prints the
exact argv each stage will emit (with GFLOW_CLI_DRY_RUN=1 nothing is executed).
It never submits a generation.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flowpipe.config import SETTINGS  # noqa: E402
from flowpipe.flow.base import Account, FlowError  # noqa: E402
from flowpipe.flow.gflow import GflowDriver, preflight  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", help="gflow profile name to check credits for")
    parser.add_argument("--dry-run", action="store_true", help="print argv instead of running")
    args = parser.parse_args()

    if args.dry_run:
        os.environ["GFLOW_CLI_DRY_RUN"] = "1"

    driver = GflowDriver()
    print("== preflight ==")
    report = preflight()
    print(json.dumps(report, indent=2)[:4000])
    if not report.get("binary"):
        print("\nFAIL: gflow-cli is not on PATH. `pipx install gflow-cli`", file=sys.stderr)
        return 2

    caps = driver.capabilities()
    print("\n== model catalog ==")
    print("images:", caps.image_models, "default:", caps.default_image_model)
    print("videos:", caps.video_models, "default:", caps.default_video_model)
    print("video model chosen from preference "
          f"{SETTINGS.video_model_preference!r}: {driver.model_for_video(SETTINGS.video_model_preference)}")

    if args.profile:
        # Prove the session before anything else: a credit reading keeps working
        # from a warm page while creating a character answers 401, so the balance
        # is not evidence that this profile can act.
        print("\n== session (gflow auth status) ==")
        session = driver.auth_status(args.profile)
        print(f"verified: {session.ok}"
              + (f" as {session.account}" if session.account else "")
              + (f"\ndetail:   {session.detail}" if session.detail else "")
              + (f"\nadvice:   {session.hint}" if session.hint else ""))
        if not session.ok:
            print("NOT USABLE: fix this before running a paid project "
                  "(scripts\\win\\flow_login.bat).", file=sys.stderr)

        account = Account(id=args.profile, label=args.profile, profile=args.profile)
        print("\n== credits ==")
        try:
            print(driver.credits(account, refresh=False))
        except FlowError as exc:
            print(f"credits check failed: {type(exc).__name__}: {exc}")

    print("\n== argv preview (no execution) ==")
    account = Account(id="preview", label="preview", profile=args.profile or "default")
    out = Path("/tmp/flowpipe-preview")
    started = {
        "character": [
            "character", "create", "--project", "<flow-project>", "--name", "Aria",
            "--face-prompt", "<face>", "--body-prompt", "<body>", "--json", "--profile", account.cli_profile,
        ],
        "image": [
            "image", "i2i", "<scene image prompt>", "--ref", "<asset-uuid>",
            "--project", "<flow-project>", "--model", caps.default_image_model,
            "--aspect", "16:9", "--count", "1", "--out", str(out), "--json",
            "--profile", account.cli_profile,
        ],
        "clip": [
            "video", "i2v", "--initial-frame", "<asset-uuid>", "--end-frame", "<asset-uuid>",
            "<animation prompt>", "--project", "<flow-project>",
            "--model", caps.default_video_model, "--duration", "8", "--resolution", "720p",
            "--out-dir", str(out), "--json", "--profile", account.cli_profile,
        ],
    }
    for label, args in started.items():
        print(f"\n[{label}] {SETTINGS.gflow_bin} " + " ".join(args))
    print("\nAlso verify by hand in gflow's own docs: `gflow docs USAGE`, `gflow docs CHARACTER`.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
