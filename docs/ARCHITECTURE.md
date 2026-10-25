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

### E — assembly (`assemble.py`)

Flow clips differ in size/fps/SAR and may or may not carry audio, and a mid-project model
change makes that worse. Every clip is re-encoded to a canonical format
(`scale`+`pad` → `fps` → `setsar=1` → `yuv420p`, `-an`) and *then* joined with the concat
demuxer, which is the only reliable way to avoid a corrupted splice. Optional audio is
`amix`ed onto the joined file and cut with `-shortest`.

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
