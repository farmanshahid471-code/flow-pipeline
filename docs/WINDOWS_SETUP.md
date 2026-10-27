# Windows setup (everything on the D: drive)

Two files drive this: **`setup.bat`** installs, **`run.bat`** starts the dashboard.
Both keep every byte of the install on one folder — `D:\Tools` by default — so the
C: drive is not touched.

```
D:\Tools\
  Python311\              portable Python 3.11 (+ pip, + this project's packages)
  ffmpeg\bin\             ffmpeg.exe and ffprobe.exe
  gflow\                  gflow browser profiles: one Google session per account
  playwright-browsers\     browsers, if gflow needs one
  pip-cache\  tmp\        caches and temp files
  voicestudio\            VoiceStudio checkout + its .venv (local text-to-speech)
  voicestudio-data\       VoiceStudio's database and engines
  hf-cache\  torch-cache\  uv-cache\  uv\   model weights + installer cache
  data\                   the pipeline's own data:
      flowpipe.db
      settings.json
      projects\<project-id>\project.json, characters\, images\, clips\, output\

D:\downloads\              every download, on the D: drive
  setup\                  installers (python zip, get-pip, ffmpeg) - kept for offline re-runs
  flow\                   raw media downloaded out of Google Flow
  final\                  a plainly named copy of each finished video
```

**Everything downloaded goes to `D:\downloads`** (or `%TOOLS%\downloads` if the
machine has no D: drive): the setup's installers, anything gflow-cli pulls out of
Flow, and a copy of each finished cut. Raw Flow media is staged under
`D:\downloads\flow\<project>\<kind>\<target>\` and then moved into the project
library in `D:\Tools\data`, so nothing is duplicated and nothing is written to
C:. `GFLOW_CLI_OUTPUT_DIR` is pointed at `D:\downloads\flow`, which means even a
gflow download that does not use the pipeline's `--out` flag lands there.

## 1. Run setup

Double-click **`setup.bat`**. It works from a console too, and it is safe to run
again at any time — every step is skipped if it is already done.

What it does, in order:

| step | what | where |
|---|---|---|
| 1 | downloads the **portable** Python 3.11 embeddable zip (no installer, no registry, no admin rights) and switches `site-packages` on | `D:\Tools\Python311` |
| 2 | bootstraps pip and upgrades it | same |
| 3 | installs this project's requirements | same |
| 4 | downloads a static **ffmpeg** (gyan.dev, then BtbN, then the `imageio-ffmpeg` wheel as an offline fallback) | `D:\Tools\ffmpeg` |
| 5 | **asks** before installing **gflow-cli**, the Google Flow driver | `D:\Tools\Python311\Scripts` |
| 6 | **asks** before installing the **narration stack**: VoiceStudio (text-to-speech) + whisperX (word timestamps) | `D:\Tools\voicestudio`, `D:\Tools\Python311` |
| 7 | runs the self test: the full pipeline offline, narration included, then the HTTP API | temporary |

Folder and download roots can be moved independently; both are shown in the
setup banner before anything is downloaded:

```bat
set FLOWPIPE_TOOLS_DIR=E:\Tools
set FLOWPIPE_DOWNLOADS_DIR=E:\downloads
setup.bat
```

During all of this `TMP`, `TEMP`, `PIP_CACHE_DIR`, `PLAYWRIGHT_BROWSERS_PATH`,
`GFLOW_CLI_HOME`, `HF_HOME`, `HF_HUB_CACHE`, `TORCH_HOME`, `UV_CACHE_DIR` and
`OMNIVOICE_DATA_DIR` point inside `D:\Tools`, so pip's cache, temp files, browser
profiles and the multi-gigabyte speech models do not land on C: either.

### Narration (step 6)

Saying yes clones [VoiceStudio](https://github.com/debpalash/VoiceStudio) and
installs [whisperX](https://github.com/m-bain/whisperX) — about 2-4 GB with the
models. It runs in a **separate** console window:

```bat
scripts\voice\start-voicestudio.bat        rem serves 127.0.0.1:3900
```

then **Settings → Narration** → `tts_provider = voicestudio` → *Save narration
settings* (the same panel shows whether the server answered). Saying no costs nothing: narration
stays off, and `tts_provider = mock` still exercises the whole flow with
placeholder tones. Details: [`VOICE.md`](VOICE.md).

The window **always stays open**: every failure path and the success path end in
`pause`, so nothing closes before you have read it.

### Using a different drive or folder

Set `FLOWPIPE_TOOLS_DIR` before running:

```bat
set FLOWPIPE_TOOLS_DIR=E:\Tools
setup.bat
```

or edit the `set "TOOLS=D:\Tools"` line near the top of both `setup.bat` and
`run.bat`. If `D:` does not exist, the setup says so and stops instead of
installing anywhere else.

## 2. Start it

Double-click **`run.bat`**. It prints the paths it is using and opens
<http://localhost:8000>.

It binds to `127.0.0.1` (local only, no Windows firewall prompt). To reach the
dashboard from another machine on your network:

```bat
set FLOWPIPE_HOST=0.0.0.0
run.bat
```

`run.bat` puts `D:\Tools\ffmpeg\bin` and `D:\Tools\Python311` at the front of
`PATH`, so the project always uses its own copies even if another Python or
ffmpeg is installed on the machine.

## 3. How the app finds D:\Tools

`flowpipe/toolchain.py` does this, and every entry point runs it:

- if `FLOWPIPE_TOOLS_DIR` is set, that is the root; otherwise `D:\Tools` when it
  exists; otherwise nothing happens and the app just uses `PATH` (this is what
  happens on Linux/macOS);
- known sub-folders (`ffmpeg\bin`, `Python311`, `Python311\Scripts`, `git\bin`) are
  prepended to `PATH`, de-duplicated, and re-runnable without growing `PATH`;
- `GFLOW_CLI_HOME`, `PLAYWRIGHT_BROWSERS_PATH` and `PIP_CACHE_DIR` default inside
  the root when they are not already set;
- the data directory defaults to `<root>\data` when a root exists.

So future updates (`git pull`) keep using the same install: no re-install, no new
paths to configure. `GET /api/health` and the Settings page show exactly what was
resolved, with versions.

## 4. Updating later

```bat
git pull
run.bat
```

If `requirements.txt` changed, run `setup.bat` again — steps 1, 2 and 4 are
skipped, pip installs only what is missing. To force a fresh ffmpeg, delete
`D:\Tools\ffmpeg` first. To force fresh packages:
`D:\Tools\Python311\python.exe -m pip install -U -r requirements.txt`.

## 5. Checking the install

```bat
D:\Tools\Python311\python.exe scripts\selftest.py --tools D:\Tools
```

It prints one `[ok]` / `[FAIL]` line per check (60 of them) and a summary,
and exits non-zero on failure. `--keep` keeps the temporary data directory,
`--skip-api` skips the HTTP checks.

To verify the Google Flow driver's surface before spending credits:

```bat
D:\Tools\Python311\python.exe scripts\check_gflow.py --profile studio-a
```

## 6. Troubleshooting

| symptom | fix |
|---|---|
| window flashes and closes | it should not — every path pauses. If it does, run it from a console: `cd /d <repo> && setup.bat`, and read the last lines. |
| "Drive D: was not found" | set `FLOWPIPE_TOOLS_DIR` to a folder on a drive you have (see above). |
| pip fails with "no matching distribution" | your network blocks PyPI. Set a proxy for the console, or `pip install --proxy http://host:port ...`. |
| ffmpeg step fails | the fallback uses the `imageio-ffmpeg` wheel, which has no `ffprobe`; durations are then read from `ffmpeg` itself. Delete `D:\Tools\ffmpeg` and re-run to retry the full build. |
| `[FAIL] ffmpeg resolves` in the self test | the ffmpeg binary did not run. Delete `D:\Tools\ffmpeg`, re-run `setup.bat`. |
| port 8000 already in use | `set FLOWPIPE_PORT=8010` before `run.bat`. |
| `No module named 'flowpipe'` from `run.bat` | fixed: `run.bat` starts `scripts\serve.py`, which puts the project folder on `sys.path` itself. The embeddable Python ignores the current folder (`python311._pth`), so `python -m flowpipe.main` only works from an activated venv. Pull the latest `run.bat`, or run the equivalent by hand: `"D:\Tools\Python311\python.exe" "<repo>\scripts\serve.py"`. |
| downloads are filling D: | Settings → *copy finished videos into the downloads folder* → No (the project folder keeps its copy). Delete `D:\downloads\setup` to reclaim installer space; `setup.bat` re-downloads them. |
| want downloads on another drive | `set FLOWPIPE_DOWNLOADS_DIR=E:\downloads` before `setup.bat` / `run.bat`. |
| narration says "VoiceStudio is not running" | run `scripts\voice\start-voicestudio.bat`, then check `http://127.0.0.1:3900/health` in a browser. |
| narration step failed / git missing | install Git for Windows and re-run `setup.bat`, or run `scripts\win\install_voice.py --tools "D:\Tools"` yourself. |
| the voice model download is huge | it is a one-off into `D:\Tools\hf-cache`; delete that folder to reclaim space (it re-downloads on the next synthesis). |
| whisperX install too slow / no GPU | `... install_voice.py --tools "D:\Tools" --skip-whisperx`; word timings fall back to proportional spacing. |
| no subtitles appear | Settings → `captions` = `sidecar` or `burn`, and the project needs generated narration. |
| antivirus quarantines the ffmpeg build | use the `imageio-ffmpeg` fallback (it is already installed) or whitelist `D:\Tools\ffmpeg`. |
| gflow wants Chrome | `D:\Tools\Python311\python.exe -m playwright install chromium` (installs into `D:\Tools\playwright-browsers`). |

## 7. What is *not* installed

- No Google account, no API key, no credits. `run.bat` starts on the **mock**
  driver, which renders placeholder media locally.
- To go live: follow [`GOING_LIVE.md`](GOING_LIVE.md) end to end — install
  gflow-cli (step 5), sign in per account (`gflow auth login --profile <name>`),
  put `ANTHROPIC_API_KEY` in the environment for headless manifest generation,
  then set `driver = gflow` on the Settings page. Read `docs/COMPLIANCE.md` first.

## Note for developers

`scripts/win/check_bat.py` lints both batch files (missing/duplicate labels,
unbalanced quotes and parentheses, missing `pause`, wrong line endings). The batch
files are stored with CRLF endings and marked `-text` in `.gitattributes` so a
checkout or a GitHub zip download gives `cmd.exe` what it expects.
