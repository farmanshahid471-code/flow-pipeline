# Compliance, risk, and the official alternative

This project automates a Google product (Flow) through a reverse-engineered CLI on
accounts you own. That is not a neutral engineering choice, and this document is meant to
be read before the first paid run — not after a suspension.

Nothing here is legal advice.

## 1. What the pipeline does, in platform-risk terms

| behaviour | risk | mitigation in this build |
|---|---|---|
| Browser automation of a consumer web app | Moderate: Google's ToS restrict automated access; Flow's UI is explicitly anti-bot hardened | You own the accounts; runs are serialized (one browser session per account at a time); jitter/anti-bot handling is delegated to gflow-cli rather than hand-rolled |
| Multiple Google accounts used for one workflow | **High**: if used to exceed per-account quotas it reads as quota evasion under fraud/abuse review | Human-approved failover is the default; each switch is logged with a reason; no account creation, no session sharing, no parallel multi-account fan-out |
| CAPTCHA / verification solving | **Highest**: circumventing an access control | **Not implemented.** Challenges pause the run and a human completes them |
| Watermark / provenance removal | **Highest**: defeats attribution the platform applies deliberately, and is a regulated act for synthetic media in several jurisdictions | **Not implemented.** No crop/filter/re-render is applied to hide a mark |
| Reselling or redistributing generated media | Depends on Flow's terms and your jurisdiction's disclosure rules | Out of scope; check the terms for your use case |

**The honest summary:** a pool of accounts you genuinely own, used sequentially, with
credits read before each paid call, no fan-out, no solver, and a human approving every
switch, is the least-risky shape this workflow can take. It is still a shape that a
platform's abuse review may not like. Decide that with eyes open.

## 2. The official alternative (price this first)

Google exposes the same underlying models through paid APIs:

- **Vertex AI / Gemini API — Veo** for text-to-video and image-to-video.
- **Vertex AI / Gemini API — Imagen** (and the Gemini image models) for stills.

What you gain:

- A real API key, normal billing, quotas you can request increases for, an SLA, and logs.
- No browser, no Playwright, no UI drift, no CAPTCHA, no multi-account question at all.
- Deterministic retries with documented error codes.

What you lose:

- Flow's character-reference UI conveniences (a saved Character entity that keeps a face
  consistent across generations). You rebuild continuity with reference images and
  prompt discipline — which is what this pipeline already does for cross-account runs
  (`image i2i --ref`, optional end-frame interpolation), so the port is smaller than it
  looks.
- Flow's flat subscription-style credits. Veo is billed per second of generated video.

Per-second API rates, cross-checked against Flow credits with worked per-project
numbers: [`COSTS.md`](COSTS.md).

How to compare: take one finished project from this pipeline, note the credits burned
(`account_events` + Flow's own balance) and the wall-clock time; then price the same
scene count and durations against current Veo/Imagen per-second/ per-image rates for your
region.

**Design note:** the driver interface (`flowpipe/flow/base.py`) exists precisely so a
`VertexDriver` can be added next to `mock` and `gflow` without touching the runner, the
database, the UI or the manifest. If the compliance math points at the API route, that is
a small addition, not a rewrite.

## 3. Built-in safe defaults

- `driver = mock` on a fresh install: nothing touches Google until you change it.
- `auto_failover = false`: low balance pauses the project and asks you.
- `pause_on_challenge = true`: a challenge stops the run, marks the account
  `needs_human`, and waits for you.
- `FLOWPIPE_WORKERS = 1`: one Flow session at a time.
- Every generation call has a timeout and is never blindly resubmitted after an
  ambiguous failure (that is how credits get burned twice).
- Full audit trail: `account_events` (credit checks, switches, auth, actor), `logs`,
  every asset tagged with the account that produced it, every manifest and Claude reply
  kept on disk.

## 4. Operator checklist before scaling

1. Read Google Flow's terms and the terms of the Google account(s) you are using.
2. Confirm you own every account in the pool and that using them this way is acceptable
   to whoever administers them.
3. Run the smallest possible real project (1 character, 1 scene) and check the output.
4. Price the official Veo/Imagen route against your measured burn.
5. Decide the human-in-the-loop policy (default: approve every switch) and write it down.
6. If output will be published, check disclosure requirements for AI-generated media in
   your market, and keep Flow's provenance intact.
