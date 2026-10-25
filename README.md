# flow-pipeline

A self-hosted, web-UI-controlled pipeline that turns **a topic + a master prompt**
into **one finished video**:

```
topic ──▶ Stage A  Claude builds a structured manifest   (characters, scenes, prompts)
      ──▶ Stage B  Flow generates character references   (face + body triptych)
      ──▶ Stage C  Flow generates one still per scene    (referencing those characters)
      ──▶ Stage D  Flow animates each still into a clip  (image → video, optional end frame)
      ──▶ Stage E  ffmpeg normalizes + concatenates      (+ optional music/voiceover)
```

Everything is driven from one local dashboard: edit the manifest before a single
credit is spent, watch the queue, retry or regenerate individual items, manage the
account pool, and preview/download the final MP4.

> **Read [`docs/COMPLIANCE.md`](docs/COMPLIANCE.md) first.** This pipeline drives a
> Google product through browser automation on accounts you own. That carries real
> ToS and account risk. The doc explains the trade-offs, the safe defaults, and the
> official Veo/Imagen API route — which you should price before a production run.

---

## What's implemented

| Stage | Status | Notes |
|---|---|---|
| **A** manifest generation | ✅ | Anthropic Messages API **and** a Claude-Desktop manual hand-off (assembled prompt file + import box). Strict JSON validation, one automatic repair attempt. |
| **B** characters | ✅ | `gflow character create`; references saved per character id, tagged with the account that made them. |
| **C** stills | ✅ | Character images are uploaded into the *acting* account's Flow project and passed as `--ref`, optionally plus `--reference-entity`. |
| **D** clips | ✅ | `gflow video i2v` with runtime model selection, optional end-frame interpolation toward the next scene. Credits re-checked before every paid call. |
| **E** assembly | ✅ | Per-clip normalize (scale/pad/fps/SAR/yuv420p, audio stripped) → concat demuxer → optional music/voiceover mix → thumbnail + duration probe. |
| **Multi-account pool** | ✅ | Per-account gflow profiles, credit tracking, low-balance handling, **human-approved failover by default**, automatic reference re-upload after a switch, full switch log. |
| **Dashboard** | ✅ | Project setup, manifest editor, live monitor + log tail, asset gallery with per-item regenerate, account manager, final video player, settings. |
| **Offline mock driver** | ✅ | Renders real placeholder PNG/MP4s with ffmpeg so the whole pipeline and UI are testable with zero accounts and zero credits. |
| **CAPTCHA solving** | ❌ | Deliberately not implemented — see below. |
| **Watermark removal** | ❌ | Deliberately not implemented — see below. |

---

## Quick start

```bash
git clone <this repo> && cd flow-pipeline
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"          # or: pip install -r requirements.txt
python -m flowpipe.main          # http://localhost:8000
```

The dashboard starts on the **mock driver** — no Google account needed. Seed a demo
project to see the whole thing populated:

```bash
python scripts/demo_seed.py      # 2 mock accounts, one finished project, one paused
```

### Going live

```bash
# 1. Flow driver
pipx install gflow-cli                       # or: uv tool install gflow-cli
gflow auth login --profile studio-a          # one persistent Chrome profile per account
gflow auth login --profile studio-b

# 2. Prove the surface before spending anything
python scripts/check_gflow.py --profile studio-a

# 3. Claude (headless Stage A)
export ANTHROPIC_API_KEY=sk-ant-...

# 4. Point the app at the real driver
FLOWPIPE_DRIVER=gflow python -m flowpipe.main
```

Then in the UI: **Accounts → Add account** (one per gflow profile) → refresh credits →
**Projects → New project** → review/edit the manifest → **Run pipeline** → **Assemble**.

Run one scene first (`Re-run… → image,clip`) and inspect real output before launching a
whole project. `GFLOW_CLI_DRY_RUN=1` prints every gflow argv without executing it.

---

## Configuration

Settings live in `data/settings.json` and are editable in the dashboard. Secrets are
environment-only (`ANTHROPIC_API_KEY` is never written to disk; a key pasted in the UI
lives in process memory until restart).

| Env var | Default | Meaning |
|---|---|---|
| `FLOWPIPE_DATA_DIR` | `./data` | projects, DB, settings, custom templates |
| `FLOWPIPE_DRIVER` | `mock` | `mock` or `gflow` |
| `FLOWPIPE_HOST` / `FLOWPIPE_PORT` | `0.0.0.0` / `8000` | bind address |
| `FLOWPIPE_WORKERS` | `1` | concurrent job workers (keep at 1 for real Flow: one browser profile at a time) |
| `FLOWPIPE_AUTO_FAILOVER` | unset | force-enable silent account failover (prefer the UI toggle) |
| `ANTHROPIC_API_KEY` | unset | headless Stage A |
| `GFLOW_CLI_HOME` | CLI default | where gflow keeps its per-profile Chrome sessions |
| `GFLOW_CLI_DRY_RUN` | unset | print gflow argv, execute nothing |

Key settings: `image_model`, `video_model_preference` (matched against the **runtime**
model catalog — never a hardcoded label), `aspect_ratio`, `resolution`, `clip_duration`,
`max_attempts`, `backoff_seconds`, `credit_threshold`, `credits_per_clip`,
`output_width/height/fps/crf`.
There is intentionally no CAPTCHA setting: a verification challenge always pauses the project for a human.

---

## Layout

```
flowpipe/
  config.py        settings (env + data/settings.json)
  models.py        manifest schema (pydantic) — the source of truth
  prompts.py       master prompt templates ("skills") + manifest contract
  stage_a.py       Claude API path, repair loop, manual hand-off, import
  flow/
    base.py        driver contract + typed errors + runtime model matching
    gflow.py       gflow-cli subprocess driver (exit-code → typed error mapping)
    mock.py        offline ffmpeg-backed driver
  accounts.py      credit tracking, selection, human-approved failover
  runner.py        DB-backed job worker, retry/backoff, per-job error policy
  assemble.py      Stage E: normalize → concat → mux → thumbnail
  db.py            SQLite schema + queries
  api.py           REST API + serves the dashboard
  main.py          `flowpipe` entry point
web/               dashboard (vanilla JS, no build step)
docs/              architecture, gflow integration, compliance
scripts/           check_gflow.py (preflight), demo_seed.py
tests/             24 tests, all offline
```

`data/projects/<project-id>/` holds `project.json` (the manifest) plus
`characters/ images/ clips/ output/ requests/`. Losing the database does not lose a
project's media; losing the folder does.

---

## Two things the brief asked for that this build does not do

The brief asked to also *"add in captcha-solving and watermark removal from the
referenced GitHub repos."* Neither is here, on purpose:

- **CAPTCHA / verification solving.** Google's reCAPTCHA-Enterprise gates exist to
  establish that a human is operating the account. Automating around them is a direct
  circumvention of an access control, and it is exactly the signal that turns "a niche
  workflow tool" into an abuse investigation. Instead: when Flow serves a challenge the
  project **pauses**, the account is marked `needs_human`, and the UI tells you to
  finish the challenge yourself in the browser window gflow opened, then press Resume.
  (`ChallengeRequired` in `flowpipe/flow/base.py`; there is no solver and no hook to add
  one silently.)
- **Watermark / SynthID removal.** Flow watermarks are provenance labels, not cosmetic
  overlays. Removing them defeats the attribution the platform applies on purpose, and
  in several jurisdictions removing provenance from synthetic media is itself the
  regulated act. Clips are concatenated as delivered — no cropping, filtering or
  re-rendering is applied to hide a mark (the Stage E normalize step changes
  resolution/fps for splice safety only).

The multi-worker / round-robin load-balancing *pattern* from `kodelyx/flow-agent` is
used (the account pool). Its CAPTCHA-solving and watermark-removal code paths were not
ported. If either capability is a hard requirement, the honest path is the official
Gemini API / Vertex AI route (`docs/COMPLIANCE.md` §2), where you get keys, billing and
support instead of an arms race.

---

## API surface (short)

```
GET    /api/health | /api/settings | /api/capabilities | /api/templates
POST   /api/projects                     { topic, template_id, mode: api|manual… }
POST   /api/projects/{id}/manifest/import
PUT    /api/projects/{id}/manifest
POST   /api/projects/{id}/stage-a | /run | /pause | /resume | /cancel | /assemble
POST   /api/jobs/{id}/retry | /skip | /regenerate
GET    /api/projects/{id}/{jobs,assets,logs,prompt}
GET    /api/media/{project_id}/{relative path}          # guarded file serving
GET    /api/accounts · POST /api/accounts · POST /api/accounts/{id}/{refresh,renew,mark-active}
GET    /api/switch-requests · POST /api/switch-requests/{id}/approve|deny
```

## Tests

```bash
pytest -q          # 24 tests: manifest schema, Stage A repair, full mock run,
                   # failover approval, auto-failover, challenge pause, drift stop,
                   # assembly refusal, gflow argv/exit-code mapping
```

## Where to look next

- `docs/ARCHITECTURE.md` — module map, job lifecycle, error policy, why uploads are per-account.
- `docs/GFLOW_INTEGRATION.md` — every gflow command/flag this uses, how the model is chosen at runtime, and the drift checklist.
- `docs/COMPLIANCE.md` — ToS posture, the official Veo/Imagen comparison, and what to decide before scaling.
- `web/app.js` → **Help** tab has the operator-facing version of all of this.
