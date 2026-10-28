# flow-pipeline

A self-hosted, web-UI-controlled pipeline that turns **a topic + a master prompt**
into **one finished video**:

```
topic ──▶ Stage A  Claude builds a structured manifest   (characters, scenes, prompts)
      ──▶ Stage B  Flow generates character references   (face + body triptych)
      ──▶ Stage C  Flow generates one still per scene    (referencing those characters)
      ──▶ Stage D  Flow animates each still into a clip  (image → video, optional end frame)
      ──▶ Stage E  narration: TTS + word timestamps      (VoiceStudio + whisperX, local)
      ──▶ Stage F  ffmpeg fits the voice, concatenates   (+ subtitles, optional music)
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
| **E** narration | ✅ | Local text-to-speech with [VoiceStudio](https://github.com/debpalash/VoiceStudio) over its OpenAI-compatible API, then word-level timestamps in the spirit of [whisperX](https://github.com/m-bain/whisperX). Each scene's line is fitted to its clip (speed up to `narration_max_speedup`, or cut at the last word that fits) and the timings become an `.srt`. Offline `mock` backend for the mock driver, `proportional` timing when whisperX is not installed. |
| **F** assembly | ✅ | Per-clip normalize (scale/pad/fps/SAR/yuv420p, audio stripped) → per-scene narration fitted and mixed → concat demuxer → subtitles sidecar/burned → optional music (AAC 48 kHz, trimmed to the cut) → thumbnail + duration probe. A voiceover you upload by hand still works. |
| **Multi-account pool** | ✅ | Per-account gflow profiles, credit tracking, low-balance handling, **human-approved failover by default**, automatic reference re-upload after a switch, full switch log. |
| **Dashboard** | ✅ | Project setup, manifest editor, live monitor + log tail, asset gallery with per-item regenerate, account manager, final video player, settings. |
| **Offline mock driver** | ✅ | Renders real placeholder PNG/MP4s with ffmpeg so the whole pipeline and UI are testable with zero accounts and zero credits. |
| **CAPTCHA solving** | ❌ | Deliberately not implemented — see below. |
| **Watermark removal** | ❌ | Deliberately not implemented — see below. |

---

## Quick start

### Windows (everything installs to D:\Tools, nothing on C:)

Double-click **`setup.bat`**, then **`run.bat`**. That is the whole install: it
downloads a portable Python 3.11, ffmpeg and every package into `D:\Tools`, keeps
temp files and caches there too, points the app at them permanently, and finishes
by running a 60-check self test. The window never closes on its own.

Step 6 of `setup.bat` optionally installs the **narration stack** into
`D:\Tools`: VoiceStudio (local text-to-speech) and whisperX (word timestamps),
about 2-4 GB, all of it on D:. Answer `n` and the pipeline still runs — narration
stays off until you install it later.

Every **download** — installers, raw media pulled out of Google Flow, and a copy of
each finished video — goes to **`D:\downloads`** (`flowpipe/setup`, `flow`, `final`),
so nothing large lands on C:. Raw Flow media is staged there and then moved into
the project library on completion.

Windows details, custom drives and troubleshooting: [`docs/WINDOWS_SETUP.md`](docs/WINDOWS_SETUP.md).

### macOS / Linux

```bash
git clone <this repo> && cd flow-pipeline
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m flowpipe.main          # http://localhost:8000
# (on Windows use run.bat: the embeddable Python ignores the cwd, so
#  run.bat starts scripts/serve.py, which fixes sys.path itself)
python scripts/selftest.py       # optional: 60-check install verification
```

The dashboard starts on the **mock driver** — no Google account needed. Seed a demo
project to see the whole thing populated:

```bash
python scripts/demo_seed.py      # 2 mock accounts, one finished project, one paused
```

### Narration (local text-to-speech)

Stage A writes a `narration` line per scene from the master prompt. The
**Final video → Narration** panel turns those lines into speech with
[VoiceStudio](https://github.com/debpalash/VoiceStudio) on `127.0.0.1:3900`, then
gives every word a timestamp the way [whisperX](https://github.com/m-bain/whisperX)
does. Assemble fits each line to its clip (speeding it up to 1.15× or cutting at
the last word that fits — the picture never stretches), mixes it scene by scene,
and writes `final.srt`.

```bash
python scripts/win/install_voice.py --tools "D:/Tools"   # VoiceStudio + whisperX
scripts/voice/start-voicestudio.bat                      # Windows helper
# then: Settings → Narration → tts_provider = voicestudio
```

No voice server? Set `tts_provider = mock` for placeholder audio, or leave
narration off and upload your own voiceover. The **Settings → Narration** panel
also reports whether the server answers and whether whisperX was found. Details, timings and troubleshooting:
[`docs/VOICE.md`](docs/VOICE.md).

### Going live

On Windows, `setup.bat` step 5 does steps 1–2 for you — and you can repeat them at
any time with one double-click:

```bat
scripts\win\flow_login.bat studio-a         :: install CLI + Chromium, sign in, read credits
scripts\win\flow_login.bat studio-b         :: one profile per Google account
```

Full runbook with a checkpoint per step: [`docs/GOING_LIVE.md`](docs/GOING_LIVE.md).

On any platform, by hand:

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
| `FLOWPIPE_TOOLS_DIR` | `D:\Tools` if it exists | tool root; `PATH`, caches and data default inside it |
| `FLOWPIPE_DATA_DIR` | `./data`, or `<tools>\data` | projects, DB, settings, custom templates |
| `FLOWPIPE_DOWNLOADS_DIR` | `D:\downloads`, else `<data>/downloads` | every download: installers, raw Flow media, copies of finished videos |
| `FLOWPIPE_DRIVER` | `mock` | `mock` or `gflow` |
| `HF_HOME` / `TORCH_HOME` | `<tools>\hf-cache`, `<tools>\torch-cache` | narration model weights (VoiceStudio, whisperX) — kept on D: |
| `FLOWPIPE_HOST` / `FLOWPIPE_PORT` | `0.0.0.0` / `8000` | bind address |
| `FLOWPIPE_WORKERS` | `1` | concurrent job workers (keep at 1 for real Flow: one browser profile at a time) |
| `FLOWPIPE_AUTO_FAILOVER` | unset | force-enable silent account failover (prefer the UI toggle) |
| `ANTHROPIC_API_KEY` | unset | headless Stage A |
| `GFLOW_CLI_HOME` | CLI default | where gflow keeps its per-profile Chrome sessions |
| `GFLOW_CLI_DRY_RUN` | unset | print gflow argv, execute nothing |

Key settings: `image_model`, `video_model_preference` (matched against the **runtime**
model catalog — never a hardcoded label), `aspect_ratio`, `resolution`, `clip_duration`,
`max_attempts`, `backoff_seconds`, `credit_threshold`, `credits_per_clip`,
`output_width/height/fps/crf`, `tts_provider`, `voicestudio_url`, `tts_voice`,
`tts_speed`, `align_provider`, `whisperx_model`, `narration_max_speedup`,
`captions`, `caption_max_chars` (see [`docs/VOICE.md`](docs/VOICE.md)).
There is intentionally no CAPTCHA setting: a verification challenge always pauses the project for a human.

---

## Layout

```
setup.bat  run.bat   Windows install + launcher (see docs/WINDOWS_SETUP.md)
flowpipe/
  toolchain.py     finds D:\Tools (PATH, caches, data) on any platform
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
  assemble.py      Stage F: normalize → narration fit → concat → subtitles → mux
  voice/
    base.py        TTS backend protocol + typed errors
    voicestudio.py local VoiceStudio client (OpenAI-compatible /v1/audio/speech)
    mock.py        offline placeholder voices
    align.py       word timestamps: whisperX, or proportional with no model
    timeline.py    words/cues, .srt, and fitting a line to a clip
    stage.py       script → audio → timestamps → narration.json
  db.py            SQLite schema + queries
  api.py           REST API + serves the dashboard
  main.py          `flowpipe` entry point
  web/             dashboard: index.html, styles.css, app.js (no build step)
flowpipe/web/      dashboard (vanilla JS, no build step)
docs/              how-it-works walkthrough, voice/narration, architecture,
                   gflow integration, compliance
scripts/           serve.py (what run.bat starts), check_gflow.py (preflight),
                   demo_seed.py, selftest.py, win/install_ffmpeg.py,
                   win/install_voice.py, win/check_bat.py, voice/start-voicestudio.bat
tests/             112 tests, all offline
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
pytest -q                   # 37 tests, all offline
python scripts/selftest.py  # 50 checks against a real install (ffmpeg, API, UI files)
python scripts/win/check_bat.py   # lint setup.bat / run.bat
```

`pytest` covers: downloads routing (staging, move, final copy), manifest schema, Stage A repair loop, a full mock run to a final
MP4, failover approval, auto-failover, challenge pause, drift stop, assembly
refusal, gflow argv/exit-code mapping, and the D:\Tools toolchain discovery.

## Where to look next

- `docs/HOW_IT_WORKS.md` — the whole lifecycle in plain language: topic → manifest → characters → stills → clips → final cut, what happens on disk, and where credits go.
- `docs/VOICE.md` — text-to-speech + word timestamps: install on D:, timings, fitting, subtitles, troubleshooting.
- `docs/GOING_LIVE.md` — the exact steps from the offline mock driver to a real Flow run, with a checkpoint per step.
- `docs/COSTS.md` — what the official Veo / Gemini-API route would cost, next to Flow credits.
- `docs/ARCHITECTURE.md` — module map, job lifecycle, error policy, why uploads are per-account.
- `docs/GFLOW_INTEGRATION.md` — every gflow command/flag this uses, how the model is chosen at runtime, and the drift checklist.
- `docs/COMPLIANCE.md` — ToS posture, the official Veo/Imagen comparison, and what to decide before scaling.
- `web/app.js` → **Help** tab has the operator-facing version of all of this.
