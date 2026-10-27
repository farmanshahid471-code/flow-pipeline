# What the API route costs (Veo via Gemini API / Vertex AI)

Decision-support notes for adding a second driver beside `mock` and `gflow`.
Everything here is **pricing read in October 2026** — model names and rates move
often, so re-check before committing a budget. Nothing in this document is a
promise about your bill.

Why this exists: Flow credits are cheap per clip but you rent them through a
browser, on one or more Google accounts, with no SLA. The official APIs remove
the browser and the account question entirely. This file prices that trade.

## 1. Two ways to reach the same models

| | **Gemini API** (`ai.google.dev`) | **Vertex AI / Agent Platform** (GCP) |
|---|---|---|
| Auth | one API key (`x-goog-api-key`) | service-account JSON + IAM roles |
| Setup | minutes, no GCP project | GCP project, billing account, APIs enabled |
| Output delivery | the API hands you a file you download | **needs a GCS bucket** (`storageUri`), then download |
| Quota | per-key, request an increase | per-project/per-region; Veo SKUs list fixed quota |
| Enterprise controls, audit, VPC | no | yes |
| Prices | same models, slightly different SKUs (audio always on) | adds a cheaper **silent** SKU |

**For this repo the Gemini API is the simpler driver**: no bucket, no IAM, one
key that fits the existing secret handling (memory only, never in
`settings.json`). Vertex is worth it when you need org billing/audit or the
silent-video SKU.

Because this pipeline **throws generated audio away** (clips are normalized with
`-an` and the voice is synthesized locally by VoiceStudio), the silent SKU is
the right one where it exists — that is a 2.5× saving at the Standard tier.

## 2. Video rates (USD per generated second)

| Model | Gemini API (audio included) | Vertex, with audio | Vertex, silent |
|---|---|---|---|
| Veo 3.1 Lite 720p | $0.05 | $0.05 | **$0.03** |
| Veo 3.1 Lite 1080p | $0.08 | $0.08 | **$0.05** |
| Veo 3.1 Fast 720p | $0.10 | $0.10 | **$0.08** |
| Veo 3.1 Fast 1080p | $0.12 | $0.12 | **$0.10** |
| Veo 3.1 Fast 4K | $0.30 | $0.30 | **$0.25** |
| Veo 3.1 Standard 720p/1080p | $0.40 | $0.40 | **$0.20** |
| Veo 3.1 Standard 4K | $0.60 | $0.60 | **$0.40** |

Constraints that shape the driver:

- Durations are **4, 6 or 8 s**; **image-to-video with 1080p or 4K, and
  first+last-frame interpolation, force 8 s**.
- 24 fps, 16:9 or 9:16 only. Source image ≤ 20 MB.
- Long-running operation: submit → poll → download. Not a single HTTP call.
- Reference-image mode takes up to **3 images**, 8 s only, Standard/Fast (not Lite).

## 3. Stills (character refs and scene images)

Imagen 4 is **retired** (Gemini API shutdown 17 Aug 2026, deprecated on Vertex in
March 2026). Image work now runs on the Gemini image models ("Nano Banana"):

| Model | ≈ per 1K image |
|---|---|
| Gemini 2.5 Flash Image | ~$0.039 |
| Gemini 3.1 Flash Image | ~$0.067 |
| Older Imagen 4 (if still enabled on your project) | $0.02 / $0.04 / $0.06 |

A project here needs 3 character reference images + 1 still per scene, so
8 images ≈ **$0.25–0.55**.

## 4. Per-clip and per-project cost, against this pipeline

One 8 s clip at 720p:

| Route | Per 8 s clip | 5-scene project (40 s) |
|---|---|---|
| Flow, AI Pro $19.99 (1,000 credits, Fast = 20) | ≈ $0.40 | ≈ $2.00 |
| Flow, AI Ultra $99.99 (10,000 credits, Fast = 10) | ≈ $0.10 | ≈ $0.50 |
| **Vertex Veo Lite, silent 720p** | **$0.24** | **$1.20** |
| **Vertex Veo Fast, silent 720p** | **$0.64** | **$3.20** |
| Vertex Veo Fast, with audio 720p | $0.80 | $4.00 |
| Gemini API Veo Fast (audio forced) | $0.80 | $4.00 |
| Vertex Veo Standard, silent 720p | $1.60 | $8.00 |

Add stills (≈ $0.30–0.55) and a realistic **1.3× re-do factor** if you expect to
regenerate a clip in five. A publishable 5-scene video therefore costs roughly:

- **Flow Ultra:** ~$0.50–0.70 in credits
- **Vertex Lite silent:** ~$1.9
- **Vertex Fast silent:** ~$4.7
- **Vertex Standard silent:** ~$11

## 5. Costs that are easy to miss

- **Nothing is a flat fee.** No spend cap by default — hence the
  `vertex_max_spend_usd` guard below. Set a GCP budget alert as well.
- **GCS storage/egress** (Vertex path): a 40 s 720p MP4 is ~10–20 MB, cents per
  hundred projects, but the bucket is a real dependency.
- **Retries are billed work.** A timeout after submit may already be charged;
  the driver must say so instead of silently resubmitting (same rule the gflow
  driver already follows).
- **Safety-filter rejections** are not retryable — they need a prompt change, and
  they should never burn a retry loop.
- **Quota, not money, is the usual first wall**: a per-minute cap turns a big run
  into 429s. Request an increase before a batch, or throttle.
- **Sales tax/VAT** on GCP is charged in your billing country; the prices above
  are pre-tax. A Flow subscription may already be taxed where you are.
- **No free tier for Veo.** A new GCP project may still qualify for trial credit
  — check your own billing page, do not plan around it.

## 6. What the driver would implement

`flowpipe/flow/base.py` already defines the contract; the runner, DB, manifest,
UI and narration do not care which driver is behind it.

| method | what a Google API driver does |
|---|---|
| `capabilities()` | list models **at runtime** (`models.list`), then `pick_model()` fuzzy-match — never hardcode a label |
| `credits()` | no credits exist: return `None` and let the dashboard show local spend instead |
| `ensure_project()` | no Flow project: return the local project id (GCS prefix only for Vertex) |
| `create_character()` | generate 3 reference images with the image model (no Character entity) — continuity comes from `referenceImages` + prompts, which is what this repo already does across accounts |
| `upload()` | unnecessary: images go inline as base64 (≤ 20 MB) |
| `generate_image()` | `:generateContent` on the image model, reference images inline |
| `generate_clip()` | `:predictLongRunning` with first (and optional last) frame, `durationSeconds: 8`, `resolution`, `generateAudio: false`, poll the operation, download the MP4 |
| errors | 429 → `TransientFlowError`, 403/quota → `NoFlowAccess`, safety block → `needs_review` (never retry), operation error → `FlowError`, timeout → ambiguous, say so |

Settings it needs: `driver = google`, `google_api_style = gemini|vertex`,
`google_project`, `google_location`, `google_gcs_bucket` (Vertex only),
video/image model preferences, `vertex_max_spend_usd`, and the key from
`GOOGLE_API_KEY` / `GOOGLE_APPLICATION_CREDENTIALS` — **environment or
`D:\Tools\gcp\`, never `settings.json`**.

## 7. The honest comparison

- **Cheapest per clip: Flow Ultra** (~$0.10–0.20). The API is 3–6× more expensive
  per second and cannot match a flat subscription at volume.
- **Safest and most predictable: the API.** No browser, no CAPTCHA, no UI drift,
  no multi-account policy question, documented errors, an SLA, and a bill you can
  cap. It also removes the second Google account from the design entirely.
- **Crossover:** if a project's API cost is a few dollars and the video is worth
  publishing, reliability wins. If you are burning hundreds of clips a month for
  drafts, the subscription wins.
- **Hybrid is legitimate here:** drafts and bulk on `gflow`, the run that matters
  on `google`. Both write the same manifest, assets and DB rows, so a project can
  start on one driver and finish on the other.
