# How it works, end to end

Everything the pipeline does, in the order it does it — from typing a topic to a
video sitting in `D:\downloads\final`.

- **Dashboard:** http://localhost:8000 (`run.bat`)
- **Data:** `D:\Tools\data` — SQLite database, project folders, settings, templates
- **Downloads:** `D:\downloads` — installers, raw Flow media, finished cuts

---

## 0. The shape of the thing

```
topic + template ──► Stage A ──► project.json (manifest)  ← you edit this
                                     │
                    ┌────────────────┼────────────────┐
                    ▼                ▼                ▼
                 Stage B          Stage C          Stage D
               characters  ───►  scene stills ───►  image-to-video clips
              (2 refs each)     (character refs)    (still as 1st frame)
                    │                │                │
                    └────────────────┴────────────────┘
                                     ▼
                                 Stage E: ffmpeg
                          normalize → concat → final.mp4
                                     │
                    ┌────────────────┴────────────────┐
                    ▼                                 ▼
        data/projects/<id>/output/final.mp4   D:\downloads\final\<topic>-<id>.mp4
```

Four ideas do all the work:

1. **The manifest is the contract.** Nothing after Stage A talks to Claude.
   Every job is "generate this scene's still", "animate this scene", "create this
   character" — all read from `project.json`. Edit it, re-run, and only what you
   changed is regenerated.
2. **Every step is a row in SQLite.** A job is `queued → running → done`, or
   `needs_review` when something needs a human. Kill the server mid-run and the
   queue is still there; nothing is submitted twice.
3. **Every paid call goes through an account from your pool**, with the credit
   balance checked first and each switch written to the event log.
4. **Media only ever lands in two places:** staged under `D:\downloads\flow`,
   then moved into the project library; the finished cut is *copied* to
   `D:\downloads\final`.

---

## 1. Create a project (Stage A — Claude writes the plan)

**Projects → + New project.** You fill in:

| Field | Meaning |
|---|---|
| topic | one sentence: what the video is about |
| template | the "skill": `cinematic-short`, `documentary`, `product-ad`, or your own |
| scenes | how many shots (default 5) |
| target duration | seconds (default 30) |
| style / tone | visual + emotional direction, injected into the prompt |
| extra | free-text direction (exact characters, things to avoid…) |
| Stage A path | **Claude API (headless)** or **Claude Desktop (manual)** |

That text is rendered into a master prompt (`data/templates/<id>.json`, editable
in Settings → Master prompt templates) together with the JSON schema Claude must
answer with. Then:

- **API mode** (`ANTHROPIC_API_KEY` present): the server calls the Anthropic
  Messages API, strips fences/prose, validates the JSON against the manifest
  schema, and — if validation fails — feeds the error back for **one** repair
  attempt before giving up. Everything Claude returns is saved to
  `stage-a-reply.txt` next to the project.
- **Manual mode** (no key): the same prompt is written to
  `data/projects/<id>/requests/stage-a-prompt.md`. Copy it into Claude Desktop,
  paste the JSON reply into the Manifest tab → **Import JSON**, and it is
  validated and saved the same way.

The result is `data/projects/<id>/project.json` — characters with a *face*
prompt and a *body* prompt, scenes with an *image* prompt and an *animation*
prompt, `character_ids` links, `clip_duration`, aspect ratio, optional music /
voiceover notes. Validation is strict: duplicate ids, scenes pointing at a
character that does not exist, or an empty scene list are rejected with the
reason shown in the UI.

**This is the checkpoint.** Nothing has been sent to Flow, no credits spent. Read
it, edit the JSON in the Manifest tab (Save manifest), or regenerate the whole
thing with new direction.

---

## 2. Add accounts (only needed for the real driver)

Settings → `driver = gflow`, then **Accounts**:

- **Add account** — label + the gflow *profile* name (that profile must already be
  signed in: `gflow auth login --profile <name>`).
- **Refresh** — reads the live credit balance (`gflow credits user --json`),
  cached for 10 minutes, forced before every paid clip.
- **Renew** — one headless session renewal (at most hourly, per account).
- **Mark active** — pin this account to a project.
- Status machine: `active` / `low` / `exhausted` / `needs_auth` / `needs_human` /
  `disabled`. Only usable accounts are ever selected.

The **event log** on that page is the switch history: who, when, why, and whether
a human or the system did it.

In mock mode none of this is needed — the mock driver pretends to have credits.

---

## 3. Run it (Stages B → D)

**Run** on the project (you can run one kind at a time: characters, stills, clips,
or everything). The runner creates the job rows in this order and works on them
in the same order — characters first, because scenes reference them:

| Stage | Job | What the driver does (real mode) | Cost |
|---|---|---|---|
| B | `character:<id>` | `gflow character create --name --face-prompt --body-prompt` → 2 reference images + a reusable **entity id** | image credits |
| C | `image:<scene>` | uploads that scene's character references into *this account's* Flow project, then `gflow image i2i <prompt> --ref <asset>` (or `t2i` when there are no references) | image credits |
| D | `clip:<scene>` | uploads the still, then `gflow video i2v --initial-frame <asset> <animation_prompt> --duration 8 --resolution 720p` | **Veo credits** (`credits_per_clip`) |
| E | *(you press Assemble)* | ffmpeg | free |

Details that matter:

- **Model names are never hardcoded.** The catalog comes from `gflow models --json`
  at call time; your `image_model` / `video_model_preference` are matched against
  what Flow currently offers.
- **Assets are per account.** Character entity ids and uploaded asset UUIDs are
  cached per (project, account) — so if the project moves to another account, the
  driver re-uploads the reference into *that* account's Flow project instead of
  reusing a dead id. That is the "asset-carrying failover".
- **Downloads are staged.** Flow's output lands in
  `D:\downloads\flow\<project>\<kind>\<target>\`, then moves into
  `data\projects\<id>\{characters,images,clips}\<target>\`. New versions are
  numbered (`scene-1_02.mp4`); older ones stay as history.

### What can interrupt a run, and what happens

| Situation | Behaviour |
|---|---|
| transient error / locked profile | retry with backoff, up to `max_attempts` (default 3), then `needs_review` |
| session expired | one automatic headless renewal, then pause for a human |
| CAPTCHA / verification | project **pauses** (`waiting_human`), account marked `needs_human`. You finish it in a real browser, then **Resume**. No solving, no bypass. |
| out of credits | account → `exhausted`; a **switch request** appears (unless you opted into `auto_failover`), project → `waiting_approval` |
| Flow has no access on the account | account disabled, project paused |
| reference asset missing | re-upload into the current account and retry |
| Flow's UI changed (selector drift) | **stop** — retrying would only burn credits; upgrade gflow-cli and retry |
| you skipped a job | clip missing → Assemble tells you exactly which scenes |

Every one of those writes a line to the log (Monitor tab streams it live).

### Approving a switch

When an account runs dry the project pauses and the dashboard shows the request:
*"A is at 14 credits (threshold 20) → move to B?"* Approve → the project's active
account becomes B, queued jobs are released, and the event is logged against both
accounts with the reason. Deny → it stays paused and you can top up or add an
account.

---

## 4. Check the gallery

**Gallery** shows every character reference, every still and every clip, with its
job status. Per item: **Regenerate** (re-queues just that job; the clip button
warns that it spends credits). Every version stays on disk — the newest one is
what gets used. This
is the loop you use to fix one bad shot without re-running the project.

---

## 5. Assemble the final cut (Stage E)

**Final video → Assemble.** Requires every scene to have a clip. Then:

1. each clip is re-encoded to a canonical format — `scale + pad` to exact
   dimensions, `fps`, `yuv420p`, CRF 20 — because Flow returns slightly different
   sizes/frame rates and a raw concat would glitch;
2. clips are joined with the concat demuxer in **manifest order** (`scene.order`);
3. optional **music bed** and/or **voiceover** are mixed in (`amix`, 48 kHz,
   0.6 volume) against the length of the cut — see *Audio and voiceover* below;
4. `+faststart` so it streams/seeks properly in the browser; a thumbnail is
   grabbed at 0.5 s;
5. the result is written to `data\projects\<id>\output\final.mp4` **and copied**
   to `D:\downloads\final\<topic-slug>-<id6>.mp4`.

The project flips to `complete`. The Final video tab plays it, offers
**Download MP4**, and shows *also saved to:* the downloads copy.

### Audio and voiceover

Audio is **uploaded at assemble time**, in the Final video tab: a *music bed*
and/or a *voiceover* file (any format ffmpeg can read — mp3, wav, m4a, ogg). The
two pickers store the file in `data/projects/<id>/music/` and
`data/projects/<id>/voiceover/`.

**The video is the timeline.** Both tracks are mixed, then padded with silence
when shorter than the cut and trimmed when longer — so the picture is never
shortened by a voiceover and a long one never stretches the file. Output audio is
AAC 48 kHz, 192 kbit/s, mixed at 0.6 so music does not drown the narration.

The narration **script** is a field in the manifest (`project.voiceover`, written
by Claude in Stage A and editable in the Manifest JSON). The UI shows it with a
*Copy script* button next to both the Manifest tab and the assemble panel. There
is **no text-to-speech in the pipeline**: record the script (or paste it into any
TTS tool) and upload the resulting audio. If you leave both slots empty the cut is
silent.

Not implemented (say so if you need it): per-scene voiceover timing, subtitles/
burned captions, automatic ducking of music under the narration, and looping a
short music bed to fill the cut.

---

## 6. Where every file actually is

```
D:\Tools\
  Python311\ ffmpeg\bin\ gflow\ playwright-browsers\ pip-cache\ tmp\
  data\                      FLOWPIPE_DATA_DIR
    flowpipe.db              projects, jobs, assets, accounts, logs, switches
    settings.json            driver, models, thresholds, downloads_dir…
    templates\<id>.json      your edited master prompts
    projects\<id>\
      project.json           the manifest
      stage-a-prompt.txt     what Claude was asked
      stage-a-reply.txt      what Claude answered (raw)
      requests\              manual-mode prompt for Claude Desktop
      characters\ images\ clips\    generated media (versioned)
      output\final.mp4       the cut, + thumbnail.jpg
      music\ voiceover\      audio you upload at assemble time
      inputs\               anything else you upload
D:\downloads\
  setup\                     installers (python zip, ffmpeg zip, pip)
  flow\<id>\<kind>\<target>\   staging for Flow output (emptied after each move)
  final\<topic>-<id6>.mp4    one copy of each finished video
```

Nothing is written to C:. Kill the server at any point — the database has the
truth, and **Resume** continues where it stopped.

---

## 7. From mock to real

Mock is the default and needs nothing: it renders gradient stills and 1-second
clips with ffmpeg so you can exercise every button for free. To go live:

1. `pip install gflow-cli` (setup.bat already tries).
2. Per Google account: `gflow auth login --profile <name>` — sign in once in the
   browser window it opens. That profile lives in `D:\Tools\gflow`.
3. **Accounts → Add account**, label + that profile name, then **Refresh** to pull
   the real balance.
4. `ANTHROPIC_API_KEY` in the environment (or Settings → paste a key for this
   process only) if you want headless Stage A.
5. Settings → `driver = gflow`, model preferences (blank = whatever Flow's
   catalog defaults to), `clip_duration`, `resolution`.
6. `python scripts/check_gflow.py --profile <name>` — preflight: binary, model
   catalog, credits, and the exact argv each stage will emit without running it.
7. Run one small project: 2 scenes, 1 character. Watch the Monitor tab, confirm
   the credits go down by what you expect, then scale up.

**Rate / ToS honesty:** accounts belong to you, switching is logged, a challenge
always stops the queue for a human, and there is no CAPTCHA solving or
watermark removal anywhere in this codebase — see `docs/COMPLIANCE.md`.

---

## 8. Controls you will actually use

| Control | Where | Effect |
|---|---|---|
| Run / Pause / Resume / Cancel | project header | start jobs, stop after the current one, continue, or mark everything skipped |
| Retry | Monitor, per job | re-queue that job (attempts reset) |
| Skip | Monitor, per job | accept the gap; Assemble will name it |
| Regenerate | Gallery, per item | new version of one character/still/clip |
| Validate & save | Manifest | edit prompts in JSON, then run only the changed jobs |
| Regenerate with API | Manifest | fresh manifest from Claude for the same topic |
| Import manifest | Manifest | paste Claude Desktop output in manual mode (also *Show / copy prompt*) |
| Assemble | Final video | build final.mp4 + the downloads copy (mix in the uploaded music/voiceover) |
| Copy script | Manifest / Final video | copy the narration script to record |
| Approve / Deny switch | Accounts, and the project banner | human decision on moving accounts |

---

## 9. Settings worth knowing

`driver`, `downloads_dir`, `copy_final_to_downloads`, `image_model`,
`video_model_preference`, `aspect_ratio`, `resolution`, `clip_duration`,
`max_attempts`, `backoff_seconds`, `job_timeout_seconds`, `credit_threshold`,
`credits_per_clip`, `output_width/height/fps/crf`, `prompt_template_id`,
`flow_project_prefix`, `gflow_bin`, `gflow_home`, `anthropic_model`,
`auto_failover` (off by default).

Saved to `D:\Tools\data\settings.json`; the API key is never written to disk.
