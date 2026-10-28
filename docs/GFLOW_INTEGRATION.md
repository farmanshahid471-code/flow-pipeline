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
| 31 | Flow app failed to load, or the page loaded but a reply (e.g. the `nzlxg` credit balance) was unreadable | `TransientFlowError` | retry - `credits()` retries once automatically; a job retries with backoff |
| 39 | account has no Flow access | `NoFlowAccess` | disable account |

Challenge text (`recaptcha`, `unusual traffic`, `verification…`) is checked **before** the
exit code and maps to `ChallengeRequired`, which pauses the project for a human. There is
no solver path.

## Credit safety

- Every generation call runs with a timeout (`job_timeout_seconds`, default 900s). A
  timeout is reported as *possibly charged* — check the Flow project before retrying.
- Balance is re-read before **video** calls only (images use separate daily quotas), via
  `gflow credits user --json`, which costs no credits. Because that read is free,
  a transient failure (exit 28/31, which gflow documents as retryable) is retried
  once; if it still fails the balance stays unknown, and an unknown balance cannot
  refuse a clip, so the account is only as protected as its last reading.
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

## Why agent-browser is not the driver (and where it is useful)

[`vercel-labs/agent-browser`](https://github.com/vercel-labs/agent-browser) comes up
whenever "replace Chromium with something agentic" is suggested. It is a good tool,
but it does not swap out the browser — and it cannot drive Flow for this pipeline.

**It is not a browser.** `agent-browser install` downloads *Chrome for Testing* from
Google's official automation channel, and the default engine is `chrome`. Its
`lightpanda` / `obscura` engines are not realistic for Flow: Flow needs a full
GPU-backed Chrome (WebGL, media, WebCrypto) or it will not render the app at all.

**The driver boundary is inside gflow-cli, not here.** `flowpipe/flow/gflow.py` runs
`gflow ...` as a subprocess and parses exit codes. gflow owns the browser, the
persistent profile store (`GFLOW_CLI_HOME`), the session cookies and the selectors.
Its only browser knobs are its internal Playwright/Patchright selection. To use
agent-browser as the driver you would have to reimplement gflow's entire surface —
project create, character create with its 2-generation cost, i2i uploads, i2v,
download polling — against a UI that changes weekly, and lose the exit-code contract
that currently prevents double-charging (`SelectorDrift` = stop, never retry).

**Two different CLIs share the name `gflow-cli`** — worth knowing before following any
guide you find online:

| | `ffroliva/gflow-cli` | `swissmarley/gflow-cli` |
|---|---|---|
| language | Python (`pip install gflow-cli`) | Node.js (`npm i -g`) |
| browser | Patchright/Playwright, bundled Chromium | **real Google Chrome**, not bundled |
| browser override | `GFLOW_CLI_BROWSER` (patchright/playwright) | `GFLOW_CHROME_PATH`, `--browser chrome\|chromium` |
| what this repo targets | **this one** | not this one |

If a guide tells you to install Node and point at Chrome, it is describing the other
project.

### Where agent-browser genuinely earns its place: drift repair

When gflow stops with exit 23 (`SelectorDrift`) the pipeline deliberately halts rather
than guessing. agent-browser is a good *human* tool for that moment, because it can
reuse the Chromium already on `D:` — no second download, nothing new on `C:`:

```bat
rem point it at the browser setup.bat installed, not a fresh copy
agent-browser --executable-path "D:\Tools\playwright-browsers\chromium-XXXX\chrome-win\chrome.exe" open https://labs.google/flow
agent-browser snapshot --json        :: accessibility tree, to find the new selector
agent-browser dashboard start        :: live viewport + command feed on :4848
agent-browser network har start      :: capture the failing request, if it is an API change
```

That is a supervised repair loop: a human (or an agent, with approval) inspects the
real page, the fix lands in `flowpipe/flow/gflow.py`, and the run resumes. It is not
part of the pipeline and never clicks "Generate".

**One thing to avoid:** agent-browser ships stealth-oriented options (`--engine obscura`
stealth mode, `--input-mode human`, fingerprint consistency). Using them to make Flow
automation harder to detect is exactly the evasion this project refuses — no CAPTCHA
solving, no fingerprint spoofing, no provenance removal. The pipeline stays honest
about being automation; that is why challenges pause for a human instead.
