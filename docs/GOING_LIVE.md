# Going live: switching from the mock driver to real Google Flow

The install starts on `driver = mock`: the whole pipeline runs offline with
placeholder media so you can learn the UI without spending a credit. This page is
the exact path from mock to a real Flow run, in order, with the checkpoint that
tells you each step worked.

Read [`COMPLIANCE.md`](COMPLIANCE.md) first. You are automating a Google product
on accounts you own; that is a decision, not a setting.

---

## 0. What you need before you start

- The install finished (`setup.bat` ran to the end) and `run.bat` opens the dashboard.
- A Google account **with Flow access and credits** (the free daily 50 work too).
- About 20 minutes: the browser sign-in is interactive and cannot be scripted.
- Nothing on `C:`. Everything below stays under `D:\Tools`.

The dashboard pill in the header reads `driver: mock` while you are offline. It
turns green and reads `driver: gflow` when you are live. Treat that pill as the
source of truth, not your memory of a setting.

## 1–2. Install gflow-cli, the browser, and sign in

`setup.bat` step 5 installs gflow-cli and then offers to finish the job. If you
skipped it, declined, or a session has expired since, run the one file that does
all of it — it is safe to run any number of times:

```bat
scripts\win\flow_login.bat studio-a
```

(Double-clicking it works too; it asks for the profile name, default `studio-a`.)

What it does, in order:

| step | what happens | why it is needed |
|---|---|---|
| 1 | `pip install gflow-cli` if missing | the CLI is what drives Flow |
| 2 | `python -m playwright install chromium` into `D:\Tools\playwright-browsers` | gflow drives a real Chromium; by default Playwright puts it on **C:**, so the path is set first |
| 3 | `gflow auth login --profile <name>` | a real browser window opens — **you** sign in, including 2FA |
| 4 | `gflow credits user --profile <name> --json` | proves the session persisted and shows the balance |

Checkpoint: step 4 prints a credit number. If it asks you to log in again, the
profile did not persist — run the file again before doing anything else.

Nothing in this project types a password, solves a CAPTCHA or stores cookies
itself: the CLI keeps the browser profile under `D:\Tools\gflow`.

To do it by hand instead:

```bat
D:\Tools\Python311\python.exe -m pip install --prefer-binary gflow-cli
set GFLOW_CLI_HOME=D:\Tools\gflow
set PLAYWRIGHT_BROWSERS_PATH=D:\Tools\playwright-browsers
D:\Tools\Python311\python.exe -m playwright install chromium
D:\Tools\Python311\Scripts\gflow.exe auth login --profile studio-a
D:\Tools\Python311\Scripts\gflow.exe credits user --profile studio-a --json
```

Sessions expire. When one does, the run pauses, the account is marked
`needs_auth`, and you run the same file again. That is expected, not a bug.

## 3. Add the account in the dashboard

**Accounts → Add account.** The *profile* field must be exactly the `--profile`
name from step 1–2 (`studio-a`). The label is free text (`Studio A`).

Then press **Refresh credits** on that row. Checkpoint: the row shows a credit
number and a status (`active` / `low` / `exhausted`) instead of a dash. If it
shows `needs_auth`, the sign-in did not take — re-run `flow_login.bat`.

Adding a second account is optional and is the riskiest part of the whole design —
see `COMPLIANCE.md` §1. To add one, run
`scripts\win\flow_login.bat studio-b` and use `studio-b` as its profile. Leave
`auto_failover` **off** (the default): when a balance runs out the run pauses and
asks you, rather than silently hopping.

## 4. Verify the CLI surface before spending credits

Run this on the machine with the signed-in profile. It never submits a
generation; it prints the exact argv each stage will emit.

```bat
D:\Tools\Python311\python.exe scripts\check_gflow.py --profile studio-a
```

Checkpoint — all three must hold:

1. `preflight` shows a non-null `binary`.
2. The **model catalog** section lists real image/video models (not the
   placeholder list).
3. `credits` prints a reading rather than an error.

To see the commands without running them: add `--dry-run`
(or set `GFLOW_CLI_DRY_RUN=1` in the environment).

## 5. Point the app at the driver

**Settings → Driver**, then save:

| setting | value |
|---|---|
| `driver` | `gflow` |
| `gflow_bin` | `gflow`, or `D:\Tools\Python311\Scripts\gflow.exe` if not on PATH |
| `gflow_home` | `D:\Tools\gflow` (where the profiles live) |
| `image_model` / `video_model_preference` | leave **empty** — the driver resolves the real catalog at runtime |
| `credits_per_clip` | what one clip actually costs you (Veo 3.1 Fast = 20, Lite = 10, Quality = 100, Ultra halves Fast/Lite) |
| `credit_threshold` | the balance at which you want a warning, e.g. `credits_per_clip` |
| `auto_failover` | `false` |
| `clip_duration` | `8` (Flow's image-to-video is fixed at 8 s) |

Checkpoint: reload the dashboard. The header pill turns green
(`driver: gflow`), and **Settings → Capabilities** now shows the model list.

**If Settings shows a yellow "model labels are not verified" banner**, the CLI did
not answer `gflow models --json` — go back to step 4. Do not run a paid project
while that banner is up; the model names it is showing are placeholders.

**A balance of "unknown" is not an error, but it does disable a guard.** When the
balance cannot be read (exit 31), the account stays `active` with `credits = null`.
`has_capacity()` then has to assume yes — it cannot refuse a clip it knows nothing
about — so the pre-spend check is effectively off for that account. Read the balance
in Flow's own UI before the first paid project, and keep `auto_failover` off.

## 6. The test run (one shot, ~20 credits)

The first real run should prove three things and cost almost nothing: the CLI
drives Flow, media lands on disk, and the credit number drops by what you expect.
`docs/examples/test-one-scene.json` is built for exactly that — **one scene, no
character** (a Character costs 2 generations; skip it until the pipe works).

1. Credit the account and set the driver first (sections 3 and 5 above). The
   header pill must read `driver: gflow`.
2. **Settings → Driver**: `credits_per_clip = 20` (Veo Fast) and `clip_duration = 8`.
3. **New project** → topic `test`, **1 scene**, `mode = manual` → **Create**.
4. On the project page, **Import a manifest** → paste
   `docs/examples/test-one-scene.json` → **Import**. Checkpoint: one job row per
   scene, all `queued`.
5. Press **Run pipeline**. Because the manifest has no characters, that queues
   exactly two jobs: `image:test-shot` and `clip:test-shot`.
   Watch the log: `project create` → `image t2i` → `image upload` → `video i2v`.
6. **Checkpoint 1 — the still.** When `image:test-shot` is `done`, open the
   **Gallery**. A real generated still (not a flat placeholder) means the browser,
   the profile and the model label are all working.
7. **Checkpoint 2 — the clip.** When `clip:test-shot` is `done`, the **Assets**
   tab has an mp4 you can play, and **Accounts** shows roughly 20 credits less
   than before (1050 → ~1030 for Veo Fast; Lite is ~10, Quality ~100).
8. **Checkpoint 3 — assembly.** **Assemble** → a `final.mp4` appears and
   `D:\downloads\final\` gets a copy.

If the still works but the clip fails, the problem is credits or the model label;
if the clip works, the whole Flow path is proven and you can move to a real
2-scene project with a character.

**Cheapest way to re-test one stage:** **Re-run…** takes a stage list, e.g. `clip`
(one clip only) or `image,clip`. It forces those jobs to run again, so use it
deliberately.

**Costs, so nothing surprises you:** in this pipeline images come from Flow's
separate daily image quota, while **clips are the only thing that spends
credits** — which is why the test is one clip. A Character is 2 generations.
With 1,050 credits you have room for roughly 50 Veo Fast clips (or ~100 Lite).

## 6b. First real project — small and cheap

Do not run your 20-scene idea as the first test.

1. **New project** → topic, **2 scenes**, 8 s each, `mode = manual` (unless
   `ANTHROPIC_API_KEY` is set, in which case API mode writes the manifest itself).
2. Paste the prompt into Claude, import the returned JSON (or edit the manifest in
   the UI).
3. Per scene, check the image prompt and the animation prompt before running. The
   image becomes the clip's first frame; a wrong still wastes two generations.
4. **Run.** Watch the Logs pane: `project create` → `character create` → `image
   i2i` → `image upload` → `video i2v`.
5. Costs at that point: one character (2 generations) plus 2 stills plus 2 clips.

Checkpoint: the credit balance on the Accounts page drops by roughly what you
expected, and 2 mp4s appear under `D:\downloads\flow\<project>\clips\`.

## 7. What the run does when something goes wrong

| what you see | what it means | what to do |
|---|---|---|
| run paused, account `needs_human`, log says "human verification" | Flow wants a CAPTCHA | solve it yourself in the browser window, then **Resume** |
| `SelectorDrift` in the log, run stopped | Flow's UI changed and gflow no longer matches it | stop. Update gflow (`pip install -U gflow-cli`), then retry; nothing is retried blindly on purpose |
| `AuthExpired` | session expired | run `scripts\win\flow_login.bat <profile>`, then Refresh credits |
| project pauses with a switch request | balance below the threshold | approve the switch (or add credits) — this is the designed behaviour |
| `NoCredits` before a clip | balance re-checked and insufficient | the run refuses **before** spending |
| `FlowError` mentioning timeout / ambiguous | the generation may already be charged | check Flow's own history before retrying; do not blind-retry |
| exit 31 / `FlowAppError` "no readable nzlxg reply" | the Flow page loaded but the **balance reply** could not be read — your session is fine | retry once (the driver does this automatically for credits); if it persists read the balance in Flow's own account menu, try `pip install -U gflow-cli`, and note that the dashboard will show the balance as unknown |
| 502 from a run start | Stage A (Claude) failed | use `mode = manual` with the generated request file |

There is exactly one Flow session at a time (`FLOWPIPE_WORKERS=1`). Running a
second project in parallel on the same profile will lock it, by design.

If you start a run from a script (`demo_seed.py`, your own automation) while the
dashboard is open on the same data dir, they no longer fight: each project is
locked to one process for the duration of a job. A second process skips that
project until the lock is released, so job order (character → image → clip) is
never broken and nothing is generated twice.

## 8. Going back to mock

Set `driver = mock` in Settings. Nothing else changes: existing projects, assets,
manifests and the final videos stay. Switch back whenever you want to
experiment without spending credits.

## 9. Things that are easy to get wrong

- **`FLOWPIPE_DRIVER` in the environment overrides Settings.** If you ever set it
  (for a smoke test, say), the dashboard pill keeps showing the old driver no
  matter what you save in Settings. Clear it and restart `run.bat`.
- **`GFLOW_CLI_HOME` must match between the CLI and the app.** `run.bat` sets it
  to `D:\Tools\gflow`, which is where `setup.bat`'s sign-in instructions put the
  profiles. If you signed in with a different shell, the dashboard sees a
  different profile store and every account looks unauthorized.
- **Model labels are never hardcoded.** If Flow renames a model, leave the
  preference empty and the driver picks the closest available one.
- **The credits number is an estimate between refreshes.** The authoritative
  check happens immediately before each paid clip; that is the one that can
  refuse a run.
- **A challenge is never solved for you.** That is deliberate and is the one
  place this tool will always stop and wait for a human.
