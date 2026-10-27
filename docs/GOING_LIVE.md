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

## 1. Install gflow-cli (skip if you answered `y` at setup step 5)

```bat
D:\Tools\Python311\python.exe -m pip install --prefer-binary gflow-cli
```

Checkpoint — this must print a version:

```bat
D:\Tools\Python311\Scripts\gflow.exe --version
```

If Windows says it cannot find the file, the package installed without a console
script; use `D:\Tools\Python311\python.exe -m gflow --version` instead and put
that in `gflow_bin` in step 5 (a `.bat`-free wrapper is fine: `gflow_bin` accepts
a full path, not only a name).

gflow drives a real Chromium. If it cannot find a browser, install one on `D:`:

```bat
D:\Tools\Python311\python.exe -m playwright install chromium
```

## 2. Sign in once per Google account

One profile per account. The profile name is what ties a dashboard account to a
browser session, so pick something boring and keep it.

```bat
D:\Tools\Python311\Scripts\gflow.exe auth login --profile studio-a
```

- A real browser window opens. Sign in **by hand**, including any 2FA or
  verification. Nothing in this project ever types a code or solves a challenge.
- Then confirm the session and read the balance:

```bat
D:\Tools\Python311\Scripts\gflow.exe credits user --profile studio-a --json
```

Checkpoint: that command prints a credit number. If it asks you to log in again,
the profile did not persist — fix that now, not mid-project.

Sessions expire. When one does, the run pauses, the account is marked
`needs_auth`, and you repeat this step. That is expected, not a bug.

## 3. Add the account in the dashboard

**Accounts → Add account.** The *profile* field must be exactly the `--profile`
name from step 2 (`studio-a`). The label is free text (`Studio A`).

Then press **Refresh credits** on that row. Checkpoint: the row shows a credit
number and a status (`active` / `low` / `exhausted`) instead of a dash. If it
shows `needs_auth`, step 2 did not fully work.

Adding a second account is optional and is the riskiest part of the whole design —
see `COMPLIANCE.md` §1. Leave `auto_failover` **off** (the default): when a
balance runs out the run pauses and asks you, rather than silently hopping.

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

## 6. First real project — small and cheap

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
| `AuthExpired` | session expired | repeat step 2, then Refresh credits |
| project pauses with a switch request | balance below the threshold | approve the switch (or add credits) — this is the designed behaviour |
| `NoCredits` before a clip | balance re-checked and insufficient | the run refuses **before** spending |
| `FlowError` mentioning timeout / ambiguous | the generation may already be charged | check Flow's own history before retrying; do not blind-retry |
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
