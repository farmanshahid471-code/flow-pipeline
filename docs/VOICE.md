# Narration: text-to-speech + word timestamps

How a script from Claude becomes a voice on the finished video, and where every
piece of it lives on disk.

```
project.json  ──►  TTS (VoiceStudio)  ──►  alignment (whisperX)  ──►  per-scene fit  ──►  final.mp4
 scene.narration     scene-1.wav           words + start/end        voice follows         + final.srt
 (from Stage A)                             for every word           the picture
```

Everything runs locally. No text leaves the machine, no cloud TTS, no per-word
billing.

---

## 1. Where it all lives (nothing on C:)

| Path | What |
|---|---|
| `D:\Tools\voicestudio\` | VoiceStudio checkout + its own `.venv` (AGPL-3.0, ~1.6 GB) |
| `D:\Tools\voicestudio-data\` | its database, settings, engines |
| `D:\Tools\hf-cache\` | model weights for VoiceStudio **and** whisperX (~2.4 GB on first use) |
| `D:\Tools\torch-cache\` | torch checkpoints |
| `D:\Tools\uv\`, `D:\Tools\uv-cache\` | uv (the installer) and its download cache |
| `D:\Tools\Python311\` | the portable Python whisperX is installed into |
| `D:\downloads\flow\<project>\voiceover\<scene>\` | raw TTS output, staged then moved |
| `D:\Tools\data\projects\<id>\voiceover\` | `<scene>.wav`, `narration.json`, `narration.srt` |

`run.bat` exports `HF_HOME`, `HF_HUB_CACHE`, `TORCH_HOME`, `UV_CACHE_DIR` and
`OMNIVOICE_DATA_DIR` so every tool agrees, and `flowpipe/toolchain.py` defaults
the same values for anything launched another way.

---

## 2. Install

`setup.bat` asks once and then runs:

```
"D:\Tools\Python311\python.exe" scripts\win\install_voice.py --tools "D:\Tools"
```

That script:

1. `git clone --depth 1` VoiceStudio into `D:\Tools\voicestudio` (needs Git);
2. gets `uv` (native if on PATH, otherwise `pip install uv` into the portable
   Python) and runs `uv sync` inside the checkout;
3. `pip install whisperx` into the portable Python (CPU torch, ~1 GB).

Every step is idempotent and each one fails independently — VoiceStudio without
whisperX still narrates (word timings fall back to proportional spacing), and
whisperX without VoiceStudio still aligns any voiceover you upload by hand.

### Already have VoiceStudio? Nothing gets cloned twice

This project is only an **HTTP client** for VoiceStudio: it calls
`/health`, `/v1/audio/voices` and `/v1/audio/speech` on loopback. Anything that
answers there is a valid backend, including the **VoiceStudio desktop app**
(`D:\VoiceStudioApp` + `D:\VoiceStudioRuntime`), a Docker container, or a clone
you keep somewhere else.

So before cloning, `install_voice.py` looks for an existing install:

- a running API on the target URL (`--url`, default `http://127.0.0.1:3900`);
- a folder with `backend/main.py` under `D:\Tools\voicestudio`,
  `D:\Tools\VoiceStudioApp`, `D:\VoiceStudioApp`, `%LOCALAPPDATA%\Programs\VoiceStudio`
  or `%PROGRAMFILES%\VoiceStudio`;
- whatever you pass with `--voicestudio-dir "D:\VoiceStudioApp"`.

If it finds one it says so, skips the clone **and the `uv sync`**, and still
installs whisperX (that one belongs to this project's own Python — the desktop
app's bundled runtime cannot provide it). Force a fresh copy with `--force-clone`.

```bat
rem reuse an existing install, only add whisperX
"D:\Tools\Python311\python.exe" scripts\win\install_voice.py --tools "D:\Tools" --skip-voicestudio
```

Then set `voicestudio_url` to wherever that install listens (the desktop app uses
`http://127.0.0.1:3900` while it is open) and `tts_provider = voicestudio`. Note
the desktop app's API only exists while the app is running — if the Narration
panel says *not reachable*, check that first.

Manual run, or to repair a partial install:

```bat
"D:\Tools\Python311\python.exe" scripts\win\install_voice.py --tools "D:\Tools"
...  --skip-whisperx      (no torch download)
...  --plan               (print what would happen, change nothing)
```

## 3. Start the voice server

```bat
scripts\voice\start-voicestudio.bat
```

It sets the D: environment, checks `/health`, and starts the backend on
`127.0.0.1:3900` in that window (loopback only, `VOICE_PORT` to change). Leave it
open while generating narration; Ctrl+C stops it. The first synthesis downloads
the model, which takes a few minutes.

The port is probed **first**: if something is already answering — the VoiceStudio
desktop app, or a server you started earlier — the script says so and exits 0
instead of complaining that nothing is installed. In that case there is nothing
to start: just leave the app open.

### Accepting the model licence (no UI needed)

A `git clone` install has **no web frontend** ("No web UI build at
...frontend/dist"), and the desktop app keeps a different data directory, so
accepting there does not affect the copy this pipeline starts. VoiceStudio
exposes the same gate over HTTP, and the project ships a helper for it:

```bat
rem see what still needs accepting - changes nothing
"D:\Tools\Python311\python.exe" scripts\accept_voice_licence.py --list

rem show the terms and accept them (asks for confirmation)
"D:\Tools\Python311\python.exe" scripts\accept_voice_licence.py
```

It reads `GET /models/licenses/details/<repo>` and posts
`POST /models/licenses/accept` with the exact fingerprint it displayed, so
terms can never be accepted blind. If VoiceStudio changes the terms it answers
409 and the helper tells you to re-read them. The default models in question are
`k2-fsa/OmniVoice` and `eustlb/higgs-audio-v2-tokenizer`; the OmniVoice weights
are **CC-BY-NC** — non-commercial. Read that before publishing.

Until this is done the server logs *"Preload skipped: the OmniVoice model licence
is not accepted yet"*, `/health` still answers, and the first synthesis fails
with the licence hint appended.

**Checkpoint before the dashboard:** these two must both work —

```bat
curl http://127.0.0.1:3900/health
curl http://127.0.0.1:3900/v1/audio/voices
```

The first returns `{"status": "ok", ...}`; the second lists your voice profiles
(an empty list is fine — `voice = default` works, or design one in VoiceStudio).
If the server log says the **model licence is not accepted**, accept it in
VoiceStudio first or the first synthesis will fail.

Then in the dashboard, **Settings → Narration (text to speech)**: set
`tts_provider = voicestudio` and press *Save narration settings*. That panel also
shows whether the server answered (`reachable` / `not reachable`), which engine
and model are in use, whether whisperX was found, and where VoiceStudio is
installed — so you never have to guess which half is missing. With
`tts_provider = mock` you get offline placeholder tones instead, enough to
exercise the whole flow without any model.

---

## 4. The script comes from Stage A

The master prompt asks Claude for narration as part of the manifest:

```json
"scenes": [
  { "id": "the-letter", "image_prompt": "…", "animation_prompt": "…",
    "narration": "Then one morning, the sea returned something it had kept." }
],
"project": {
  "voiceover": "For forty years the lamp burned, and nobody came. Then one morning, …",
  "voice": "warm older female narrator, unhurried",
  "voice_speed": 1.0
}
```

Rules the prompt enforces: 12–30 words per scene (5–12 seconds of speech), no
stage directions, no speaker labels, numbers spelled out ("nineteen twenty two")
because the text goes straight into the TTS engine.

In the **manual / Claude Desktop** path nothing changes: paste the JSON into
*Import manifest*, then check the lines in the **Manifest** tab before pressing
**Generate narration**.

If a manifest only has the global `project.voiceover` (older projects), its
sentences are spread across the scenes in order, and the UI says so.

---

## 5. What "whisperX-style timestamps" means here

After the audio comes back, every word gets a `start` and an `end`:

```json
{ "text": "returned", "start": 8.62, "end": 9.05 }
```

Two providers produce that list (`align_provider` in Settings):

**`whisperx`** — the real thing. whisperX transcribes the WAV and force-aligns it
against a phoneme model, then writes word-level JSON. We then *reconcile* its
words with the script: if the engine said "twenty two" where the script says
"22", or dropped a filler, sequence alignment (difflib) keeps every script word
in place — matched words take the ASR timings, unmatched ones are interpolated
between their neighbours. So the timestamps always index *the script you wrote*,
which is what the subtitles need.

**`proportional`** — no model: the audio's duration is shared out across words by
their length, with a lead-in and a small gap. For clean single-speaker TTS this
is close to reality and it always works, including on a machine that never
installs whisperX (and it is the default, so narration works out of the box).

What the timings buy you:

1. **Per-scene fit.** At assemble time each scene's clip length is known. A line
   that is slightly long is sped up (up to `narration_max_speedup`, default
   1.15×) *and its timings scaled to match*; a line far too long is cut at the
   last word that fits, and the dashboard says exactly which words would go. A
   short line is padded with silence. The picture keeps its length — narration
   never stretches the film.
2. **Subtitles that land on the words.** Cues are built from the word list
   (sentence breaks first, then clause, then a character cap of
   `caption_max_chars`), shifted by each scene's start time in the cut. Written
   as `output/final.srt` next to the video, copied beside the download, and
   optionally burned into the picture.
3. **A voice that follows the picture.** Because each scene's audio starts
   exactly at that scene's first frame, the narration lands on the shot it was
   written for.

---

## 6. Using it

**Final video tab → Narration**

| Control | What it does |
|---|---|
| voice | a VoiceStudio profile id/name (`default` = engine default). The list is fetched from `/v1/audio/voices` |
| speed | 0.5–2.0, passed to the engine |
| backend | `VoiceStudio (local)` or `mock placeholder` |
| Preview voice | speaks the first line so you can audition before spending time |
| Generate / Regenerate narration | TTS + alignment for every scene; runs in the background, progress in the log |
| Download .srt | the subtitle file with the current clip timings |
| the table | per scene: speech length, clip length, word count, fits / sped up / cut, audio present |

**Assemble** then mixes it: subtitles `sidecar` (default) / `burn` / `off`, and a
**use the generated narration** checkbox. A music bed still mixes underneath at
0.6 gain.

API: `GET /api/voice/status`, `GET /api/tts/voices`, `POST /api/tts/preview`,
`GET|POST|DELETE /api/projects/<id>/narration`,
`GET /api/projects/<id>/narration/srt`. Synthesis runs on a worker thread because
the first call loads the model — poll the GET endpoint for `state.state` and
`state.message`.

---

## 7. Settings

| Setting | Default | Meaning |
|---|---|---|
| `tts_provider` | `off` | `off` \| `voicestudio` \| `mock` |
| `voicestudio_url` | `http://127.0.0.1:3900` | the local server |
| `voicestudio_dir` | `<tools>\voicestudio` | where it was installed |
| `tts_model` | `omnivoice` | engine id passed to `/v1/audio/speech` |
| `tts_voice` | `default` | voice profile |
| `tts_speed` | `1.0` | speaking rate |
| `tts_format` | `wav` | wav is what the aligner wants |
| `align_provider` | `proportional` | or `whisperx` |
| `whisperx_model` | `base.en` | passed to whisperX |
| `whisperx_python` | *(this interpreter)* | where whisperX is installed |
| `narration_language` | `en` | for both engines |
| `narration_max_speedup` | `1.15` | how far a line may be squeezed |
| `captions` | `sidecar` | `off` \| `sidecar` \| `burn` |
| `caption_max_chars` | `42` | subtitle line length |

Every one of them has a field in **Settings → Narration**, and the API refuses a
value it cannot use (`HTTP 400`, nothing saved) rather than letting a typo break
the next synthesis. `PATCH /api/settings` is the same endpoint the panel uses.

---

## 8. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| "VoiceStudio is not running at http://127.0.0.1:3900" | start `scripts\voice\start-voicestudio.bat`; check `http://127.0.0.1:3900/health` in a browser |
| the server log says **"Preload skipped: the OmniVoice model licence is not accepted yet"** | the install is fine, the **model licence** is not. Run `scripts\accept_voice_licence.py` (see "Accepting the model licence" above), then re-run the narration job. Until then `/health` answers but synthesis refuses, and the pipeline appends this hint to the error |
| **"Microsoft Visual C++ Redistributable is not installed"** and `torchaudio ... libtorchaudio.pyd` fails to load | install `vc_redist.x64.exe` from <https://aka.ms/vs/16/release/vc_redist.x64.exe>. This is the one thing that legitimately lands on C: - it is an OS runtime in `C:\Windows\System32`, not app data. Without it VoiceStudio runs degraded (its ASR/dictation half); the pipeline's own whisperX is unaffected because it runs on `D:\Tools\Python311` |
| a thread dump appears during the first start ("Event loop has not run for 4.0s") | benign: VoiceStudio is running its alembic migrations synchronously. It resolves by itself once "Deferred startup complete" appears |
| "No web UI build at ...frontend\dist" | expected for the git clone: it serves the API only, which is all this pipeline talks to |
| the panel says the backend is `mock` | Settings → Narration → `tts_provider = voicestudio` |
| "narration is already running" (409) | a job for this project is in flight: watch the message in the panel, or stop the server and reload |
| a setting was refused with "must be between …" | the field had a value the pipeline cannot use; the message names the key and the allowed range, nothing was saved |
| first synthesis takes minutes | the model is downloading into `D:\Tools\hf-cache`; later calls are seconds |
| "no narration script in this manifest" | Stage A did not return `narration` lines — regenerate the manifest, or add them in the Manifest tab |
| a scene is marked **cut** | that line is longer than the clip even at 1.15× — shorten the line, lengthen the clip, or raise `narration_max_speedup` |
| timestamps look evenly spaced | `align_provider = proportional`; install whisperX and switch for real alignment |
| whisperX fails with a CUDA error | it is installed CPU-only here; `pip install whisperx` with a CUDA torch build if you have the GPU |
| burned subtitles show boxes | the font is missing — use `sidecar` and let your player render the .srt |
| disk filling up | delete `D:\Tools\hf-cache` (re-downloads) or run `setup.bat`'s narration step with `--skip-whisperx` |

---

## 9. Licences and posture

- **VoiceStudio** is AGPL-3.0. It is used here as a *separate local server* over
  HTTP, not linked into this codebase — see `docs/COMPLIANCE.md` for why that
  matters if you plan to distribute anything.
- **whisperX** is BSD-2-Clause; the model it downloads has its own licence.
- Both run offline. This project never uploads your script, voice samples or
  audio anywhere.
- Cloning a voice requires the right to use that voice. Nothing here ships a
  voice model or a sample; you choose the profile in VoiceStudio.
