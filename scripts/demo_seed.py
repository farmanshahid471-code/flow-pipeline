#!/usr/bin/env python3
"""Seed a demo dataset so the dashboard has something to show.

    FLOWPIPE_DRIVER=mock python scripts/demo_seed.py

Creates two mock accounts and two projects: one fully rendered (with a final
video) and one paused mid-run so the monitor view has live-looking jobs.
Spends no real credits; safe to run repeatedly (it resets mock credit state).
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flowpipe import accounts, assemble, db, projects, runner  # noqa: E402
from flowpipe.config import SETTINGS  # noqa: E402
from flowpipe.flow.mock import reset_state, set_credits  # noqa: E402
from flowpipe.models import Manifest  # noqa: E402

SETTINGS.driver = "mock"
db.init_db()
reset_state()

MANIFEST = {
    "project": {
        "topic": "A lighthouse keeper finds a message in a bottle from a hundred years ago",
        "style": "coastal noir, 35mm, cold blues with a warm lamp accent",
        "tone": "quiet, haunted, hopeful at the end",
        "target_duration": 24,
        "aspect_ratio": "16:9",
        "logline": "An ageing keeper answers a letter written before she was born.",
        "music_prompt": "sparse piano, distant foghorn, sea wind",
        "voiceover": "",
    },
    "characters": [
        {
            "id": "maren",
            "name": "Maren",
            "description": "lighthouse keeper, late sixties, stubborn and precise",
            "face_prompt": "photoreal portrait, weathered woman in her late sixties, grey braid, "
                           "salt-burned cheeks, pale grey eyes, wool collar, soft window light",
            "body_prompt": "tall spare build, oilskin coat worn smooth, cable-knit sweater, "
                           "rubber boots, hands stained with lamp oil",
        }
    ],
    "scenes": [
        {"id": "the-bottle", "order": 1, "title": "The bottle", "character_ids": ["maren"],
         "image_prompt": "Maren crouches on wet black rocks at dawn holding a barnacled green bottle, "
                         "sea spray, cold blue light, 35mm, shallow depth of field",
         "animation_prompt": "slow handheld push-in as she turns the bottle over, mist moving behind her",
         "clip_duration": 8},
        {"id": "the-letter", "order": 2, "title": "The letter", "character_ids": ["maren"],
         "image_prompt": "Maren at the kitchen table, oil lamp lit, a brittle handwritten letter "
                         "unfolded under her hands, dark room, warm lamp glow against blue dusk",
         "animation_prompt": "gentle tilt down from her face to the letter, lamp flicker",
         "clip_duration": 8, "last_frame_note": "settle on the letter"},
        {"id": "the-lamp", "order": 3, "title": "The lamp", "character_ids": ["maren"],
         "image_prompt": "Maren climbing the spiral stairs inside the lighthouse, lamp housing above "
                         "her, dust in a shaft of light, wide lens",
         "animation_prompt": "tracking shot following her up the stairs, camera rising with her",
         "clip_duration": 8},
    ],
}


def main() -> None:
    for label, credits in (("studio-a", 240), ("studio-b", 60)):
        try:
            account = accounts.add_account(label, label, "demo account")
            account_id = account["id"]
        except ValueError:
            account_id = label
        set_credits(account_id, credits)
        db.update_account(account_id, credits=credits, status="active" if credits > 100 else "low")

    # 1. a complete project
    done_id = projects.new_id("lighthouse demo")
    db.create_project(done_id, MANIFEST["project"]["topic"], "cinematic-short")
    projects.project_dir(done_id)
    projects.write_manifest(done_id, Manifest.model_validate(MANIFEST))
    (projects.project_dir(done_id) / "stage-a-prompt.txt").write_text(
        "demo project — prompt omitted\n", encoding="utf-8")
    db.update_project(done_id, status="manifest_ready")

    runner.start(done_id, force=True)
    deadline = time.time() + 240
    while time.time() < deadline:
        status = db.get_project(done_id)["status"]
        if status in {"ready_to_assemble", "needs_review"}:
            break
        time.sleep(1)
    if db.get_project(done_id)["status"] == "ready_to_assemble":
        assemble.assemble(done_id)
        print(f"rendered demo project: {done_id}")

    # 2. a project paused mid-run to populate the monitor view
    live_id = projects.new_id("second demo")
    db.create_project(live_id, "Second demo: a night market in the rain", "cinematic-short")
    projects.project_dir(live_id)
    projects.write_manifest(live_id, Manifest.model_validate(MANIFEST))
    db.update_project(live_id, status="manifest_ready")
    # only the character stage, so stills/clips stay queued for the monitor view
    runner.start(live_id, kinds=["character"])
    deadline = time.time() + 60
    while time.time() < deadline:
        jobs = db.list_jobs(live_id)
        if jobs and all(job["status"] in {"done", "needs_review", "skipped"} for job in jobs):
            break
        time.sleep(0.5)
    runner.pause(live_id, "demo: paused so the monitor has queued work")
    print(f"paused demo project: {live_id}")
    print(json.dumps({"projects": [done_id, live_id]}, indent=2))


if __name__ == "__main__":
    main()
