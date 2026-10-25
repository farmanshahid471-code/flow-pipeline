# gflow-cli integration notes

`ffroliva/gflow-cli` is an **alpha, reverse-engineered** CLI that tracks Google Flow's
UI. Its flags, exit codes and even the Flow hostname change between releases. Treat this
document as a snapshot, not a contract, and re-verify before a paid run:

```bash
gflow --version
gflow docs USAGE          # CLI reference, offline
gflow docs CHARACTER      # character create/list/show/rm
gflow docs CONFIGURATION  # env vars, exit codes
gflow models --json       # the catalog --model will accept *right now*
python scripts/check_gflow.py --profile <name>
```

## Commands emitted by this project

| stage | argv (before `--profile <name>` is appended) |
|---|---|
| credits | `gflow credits user --json [--refresh]` |
| models | `gflow models --json` |
| project | `gflow project create --name <prefix>-<project-id> --json` |
| character | `gflow character create --project <flow> --name <name> --face-prompt <face> --body-prompt <body> --json` |
| upload | `gflow image upload <file> --project <flow> --json` |
| still | `gflow image i2i <prompt> --ref <uuid> [--ref …] --project <flow> --model <m> --aspect <a> --count 1 --out <dir> --json [--reference-entity <id> --reference-entity-name <name>]` |
| still (no upload) | `gflow image t2i …` (same flags) |
| clip | `gflow video i2v --initial-frame <uuid|path> [--end-frame <uuid|path>] <prompt> --project <flow> --model <m> --duration <s> --resolution <360p|720p> --out-dir <dir> --json` |

`--profile` is **per-subcommand** in gflow-cli, not global — `gflow image t2i "…" --profile
studio-a`, never `gflow --profile studio-a image t2i …`. `GflowDriver.build()` appends it
last for exactly this reason.

## How the video model is chosen

Never hardcode a label. On first use the driver reads `gflow models --json` and keeps the
video-model list; `pick_model(preference, catalog, default)` then resolves in this order:

1. exact match, 2. case-insensitive match, 3. substring match in either direction,
4. the catalog default, 5. the first available model.

So a UI rename ("Omni Flash" → whatever it is next quarter) degrades to the closest
match instead of a hard failure, and the Settings page shows what the catalog actually
offers right now.

## Exit codes → typed errors

From gflow's documented classes (`classify_exit` in `flowpipe/flow/gflow.py`):

| code | meaning | our error | runner behaviour |
|---|---|---|---|
| 2 | usage error | `FlowError` | needs_review |
| 3 / 8 | session expired | `AuthExpired` | renewal, then human |
| 7 | wire format change | `TransientFlowError` | retry, then needs_review |
| 9 | asset/poll miss | `AssetMissing` | re-upload + retry |
| 11 | config / invalid project / profile locked | `ProfileLocked` or `FlowError` (message-refined) | retry or needs_review |
| 12 | interactive login timed out | `AuthExpired` | human |
| 23 | selector drift | `SelectorDrift` | **stop**, never retry |
| 24 | patchright extra missing | `DriverUnavailable` | needs_review |
| 28 | UI cohort pinned to the wrong arm | `TransientFlowError` | retry (documented as retryable) |
| 31 | Flow app failed to load | `TransientFlowError` | retry |
| 39 | account has no Flow access | `NoFlowAccess` | disable account |

Challenge text (`recaptcha`, `unusual traffic`, `verification…`) is checked **before** the
exit code and maps to `ChallengeRequired`, which pauses the project for a human. There is
no solver path.

## Credit safety

- Every generation call runs with a timeout (`job_timeout_seconds`, default 900s). A
  timeout is reported as *possibly charged* — check the Flow project before retrying.
- Balance is re-read before **video** calls only (images use separate daily quotas), via
  `gflow credits user --json`, which costs no credits.
- Selector drift, usage errors and auth failures never trigger a blind resubmit.

## Drift checklist (when Flow changes)

1. `pipx upgrade gflow-cli` (or `uv tool upgrade gflow-cli`).
2. `python scripts/check_gflow.py --profile <name>` — binary, catalog, credits, argv.
3. `GFLOW_CLI_DRY_RUN=1` and start a single-scene project.
4. Run one *image* job first (free), then one *clip* job.
5. If the model list changed, no code change is needed — `pick_model` adapts. Only the
   `video_model_preference` string in Settings may need updating.
6. If a flag was renamed, only `flowpipe/flow/gflow.py` needs editing; nothing else
   knows how Flow is driven.

## Multiple accounts / profiles

Each account = one gflow profile = one persistent Chrome profile directory under
`GFLOW_CLI_HOME`. Set `GFLOW_CLI_HOME` on the server if you want them isolated from a
human's interactive gflow use. gflow lends one profile to one process at a time
(`ProfileLockedError`), which is why the default worker count is 1.
