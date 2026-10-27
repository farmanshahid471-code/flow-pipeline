# Architecture

## Data model

One SQLite database (`data/flowpipe.db`) plus one folder per run. The folder is the
durable artifact; the database is an index and a queue.

```
data/
  flowpipe.db
  settings.json
  templates/<id>.json          # custom master prompts override the built-ins
  projects/<project-id>/
    project.json               # the manifest — source of truth for every stage
    stage-a-prompt.txt         # exact prompt sent to Claude
    stage-a-reply.txt          # raw reply kept for audit
    requests/stage-a-prompt.md # manual (Claude Desktop) instruction sheet
    characters/<char-id>/ref01_v01.png, ref02_v01.png, …
    images/<scene-id>/<scene-id>_v01.png
    clips/<scene-id>/<scene-id>_v01.mp4
    inputs/                    # uploaded music / voiceover
    output/final.mp4, thumbnail.jpg
```

Assets are **versioned, never overwritten** (`_v01`, `_v02`, …), so "regenerate just this"
is always reversible and the gallery can show history.

### Tables

| table | purpose |
|---|---|
| `projects` | id, status, active account, flow project, output path |
| `jobs` | one row per (project, kind, target); `kind ∈ {character,image,clip}` |
| `accounts` | label, gflow profile, status, last credit reading |
| `assets` | every produced file: project, kind, target, path, producing account |
| `uploads` | **(project, account, local file) → asset uuid** — the failover cache |
| `flow_projects` | (project, account) → Flow project id (Flow projects are per account) |
| `entities` | (project, account, character) → Flow Character entity id |
| `account_events` | every credit check, switch, auth event, with actor and reason |
| `switch_requests` | human-approval queue for account switches |
| `logs` | the log tail the UI streams |

### Narration on disk

`voiceover/` inside a project holds the generated speech plus `narration.json`
(the word timings) and a preview `.srt`. Raw TTS output is staged under
`<downloads>/flow/<project>/voiceover/<scene>/` and moved in, exactly like Flow
media, so the download root stays the only place large files appear first.
Models live in the tool root (`hf-cache`, `torch-cache`), never in the project.

### Downloads

One download root (`SETTINGS.downloads_dir`: `D:\downloads` on Windows, else
`<data>/downloads`, override `FLOWPIPE_DOWNLOADS_DIR`) holds everything fetched
from outside:

- `setup/` — installers, kept so a re-run of `setup.bat` needs no network;
- `flow/<project>/<kind>/<target>/` — the staging directory a Flow download is
  written into (`runner.staging_dir`). The runner then *moves* the file into the
  project library (same drive, so a rename) and removes the staging folder, so raw
  media never accumulates;
- `final/` — a readable copy of each finished cut (`<topic-slug>-<id>.mp4`),
  toggleable with `copy_final_to_downloads`.

`GFLOW_CLI_OUTPUT_DIR` is pointed at `flow/` so even a gflow download that ignores
our explicit `--out`/`--out-dir` lands on the download drive.

### Tool discovery

`flowpipe/toolchain.py` is imported before settings load. When a tool root is
configured (`FLOWPIPE_TOOLS_DIR`, or `D:\Tools` existing on Windows) it prepends
`ffmpeg/bin`, `Python311`, `Python311/Scripts` and `git/bin` to `PATH`
(de-duplicated, re-runnable), defaults `GFLOW_CLI_HOME`,
`PLAYWRIGHT_BROWSERS_PATH`, `PIP_CACHE_DIR`, `HF_HOME`, `HF_HUB_CACHE`,
`TORCH_HOME`, `UV_CACHE_DIR` and `OMNIVOICE_DATA_DIR` inside the root (creating
the model caches), and points the data directory at `<root>/data`. Without a root
it is a no-op. `/api/health` reports what was resolved, with versions, and
`/api/voice/status` reports the narration chain.

## Stages

### A — manifest (`stage_a.py`, `prompts.py`)

`render_prompt(template, topic, …)` fills a master prompt template. A template is the
"skill" from the brief: topic + template → one JSON manifest. Built-ins:
`cinematic-short`, `documentary`, `product-ad`; any template can be overridden in
`data/templates/`.

`generate_via_api` calls the Anthropic Messages API with a schema contract embedded in
the prompt, strips fences/prose defensively, validates with pydantic, and on failure
feeds the validation error back **once** before raising. `write_manual_request` +
`import_manifest` implement the Claude-Desktop path: the prompt is written to the project
folder, a human runs it, and the JSON is pasted back through the UI.

Nothing downstream reads the prompt again — only `project.json`. Editing the manifest in
the UI is therefore the whole "director's pass".

### B/C/D — Flow (`runner.py` + `flow/`)

The runner is a single DB-polling worker by default (`FLOWPIPE_WORKERS=1`). It picks the
next queued job from any `running` project, ordered `character → image → clip`, and this
ordering *is* the dependency graph: characters have no inputs, stills need character
references, clips need a still.

For each job: select an account → ensure a Flow project for (project, account) → upload
whatever local files this account has not seen → call the driver → copy the result into
the versioned asset path → record `assets` row with the producing account.

### E — narration (`voice/`)

`stage.py` turns `scenes[].narration` into audio plus timings and writes
`data/projects/<id>/voiceover/{<scene>.wav, narration.json, narration.srt}`:

| module | job |
|---|---|
| `voice/base.py` | `TTSBackend` protocol, `VoiceError` / `TTSUnavailable` / `AlignError` |
| `voice/voicestudio.py` | HTTP client for the local VoiceStudio server (`POST /v1/audio/speech`, `GET /v1/audio/voices`, `GET /health`). No import of VoiceStudio, no shared venv. |
| `voice/mock.py` | offline placeholder audio of the right length, for the mock driver and tests |
| `voice/align.py` | `whisperx` (subprocess → JSON → reconciled against the script with difflib) or `proportional` (no model, always available) |
| `voice/timeline.py` | `Word`/`Cue`/`Timeline`, `.srt`/`.vtt`, `chunk_cues`, and `fit()` — the function that decides speed-up vs truncation |
| `voice/stage.py` | run the backend per scene, align, persist the plan, describe/or fit against real clip lengths |

Why the scene is the unit: a Flow clip is 8 s of picture. Fitting is therefore
*per scene*, using each normalized clip's measured duration, so the picture is
never stretched and the words that do not fit are known by name. The same word
list, shifted by the cumulative clip durations, produces the subtitle cues.

Synthesis is triggered from `POST /api/projects/<id>/narration` and runs on a
worker thread (the first call loads the model); `GET` on the same path returns
`state`, the per-scene table and the `.srt`.

### F — assembly (`assemble.py`)

Flow clips differ in size/fps/SAR and may or may not carry audio, and a mid-project model
change makes that worse. Every clip is re-encoded to a canonical format
(`scale`+`pad` → `fps` → `setsar=1` → `yuv420p`, `-an`) and *then* joined with the concat
demuxer, which is the only reliable way to avoid a corrupted splice. Optional audio is
`amix`ed onto the joined file and cut with `-shortest`.

## Failure containment

The dashboard is meant to be left running for hours, so a single bad input must
not be able to take it down. The rules, and the tests that hold them:

| risk | what the code does | test |
|---|---|---|
| a setting with a nonsense value | `Settings.validate()` coerces and range-checks every key and rejects the whole patch with the reason (HTTP 400). Nothing is written unless everything is valid. | `test_bad_setting_values_are_rejected_and_nothing_changes` |
| a hand-edited/broken `settings.json` | values that do not parse are ignored at load, the defaults are kept, and the ignored keys are printed once at startup | `test_a_corrupt_settings_file_cannot_brick_the_startup` |
| a truncated or hand-edited `project.json` | writes go through a `.tmp` + `os.replace`; reads raise `ManifestError`, which the list/detail/gallery/narration views surface as `manifest_error` instead of a 500 | `test_corrupt_manifest_does_not_break_the_dashboard`, `test_manifest_writes_are_atomic` |
| a database hiccup in the worker | `_worker_loop` catches failures from the queue read itself (not just from the job) and keeps polling, logging the message once | `test_worker_thread_survives_a_failing_queue_read` |
| two processes on one data dir (dashboard + `demo_seed.py`/a second server) | a per-project lock row (`project_locks`) is taken before a job runs and released after, with a heartbeat during long generations; a lock is only stolen when its pid is dead or its heartbeat is an hour old. Without it the two workers ran the same project concurrently: an image job could start before its character reference existed, and a paid clip could be requested twice | `test_a_project_is_locked_to_one_process`, `test_a_dead_process_loses_its_lock` |
| two people (or two clicks) assembling at once | `assemble.claim()` allows one assembly per project; the second gets HTTP 409, so two ffmpeg chains never write the same `final.mp4` | `test_assembly_refuses_to_run_twice_for_the_same_project` |
| a stale subtitle next to a new cut | an assembly that produces no cues deletes the previous `.srt` (and its download copy is regenerated on the next sidecar run) | `test_a_reassembly_without_captions_removes_the_stale_srt` |
| polling that fights the operator | a poll only re-renders when the rendered values changed, and a re-render restores video position/playback, focused input text and scroll position | `walk_all.js` (browser walk) |
| a slow/absent voice server | `/api/voice/status` caches the voice-server and whisperX probes (15 s / 5 min, `?refresh=1` bypasses); the poller never overlaps requests | `test_voice_status_probe_is_cached` |
| scratch files on the wrong drive | assembly intermediates are created under `<data>/tmp`, and `toolchain` points `TMP`/`TEMP` at the tool root on Windows | `test_assembly_scratch_space_stays_in_the_data_dir` |

## Per-job error policy

Typed errors live in `flow/base.py`; `gflow.py` maps documented CLI exit codes onto them.

| error | runner action |
|---|---|
| `TransientFlowError`, `ProfileLocked` | requeue with linear backoff up to `max_attempts`, then `needs_review` |
| `AuthExpired` | one headless `gflow credits --refresh`; if that fails, account → `needs_human`, project paused |
| `ChallengeRequired` | account → `needs_human`, project `waiting_human`, **no retry**; a human finishes the challenge |
| `NoCredits` | account → `exhausted`, switch request (or auto-switch if opted in) |
| `AssetMissing` | requeue; the upload cache will re-upload the referenced file |
| `SelectorDrift` | stop. Flow's UI moved; retrying burns credits. Update gflow-cli, then retry |
| `NoFlowAccess` | account disabled with a note |

A project's status is what gates the worker: `running` only. `waiting_approval`,
`waiting_human`, `paused`, `needs_review` and `complete` all stop dispatch, so a paused
project holds its queue across restarts.

## Multi-account design

**Why uploads are per-account.** A Flow asset UUID, a Flow project id and a Flow
Character entity id are all scoped to the Google account that created them. Passing
account A's uuid to account B fails — or worse, silently generates without the reference
and loses character continuity. So the pipeline never stores a *reference id* as if it
were portable. It stores the local file, and `uploads(project, account, file)` caches the
per-account uuid. On failover the cache is simply cold for the new account and the files
are re-uploaded automatically.

**Character continuity across a switch.** Minting a Flow Character costs credits, so
stills reference the character's saved reference image via `image i2i --ref <uuid>`
(plus `--reference-entity` when the entity exists on the acting account). That gives
cross-account continuity without re-minting on every switch. Re-mint explicitly with
**Regenerate** on a character job when you want the stronger, entity-based likeness on a
specific account.

**Selection.** Accounts are considered in creation order, so a project stays on the
account it started with; a switch is an event, not a per-job lottery. Before every paid
(video) call the balance is re-read; below `credit_threshold` the account is marked
`low`/`exhausted` and:

- `auto_failover = false` (**default**) → a `switch_requests` row is created, the project
  becomes `waiting_approval`, and the dashboard shows Approve / Deny.
- `auto_failover = true` → the switch happens immediately and is logged with actor
  `system`.

Every decision is written to `account_events` with reason, timestamp and actor
(`human` / `system`).

## Deliberate non-goals

- No CAPTCHA/verification solving, no watermark removal (see `COMPLIANCE.md`).
- No parallel Flow sessions per account: one browser profile, one job at a time.
- No attempt to reuse credentials or asset ids across accounts.
