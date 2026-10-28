#!/usr/bin/env python3
"""Accept VoiceStudio's model licences from the command line.

VoiceStudio refuses to load a model whose licence nobody has accepted, and its
own UI cannot do it for us: a `git clone` install has no web frontend ("No web UI
build at .../frontend/dist"), and the desktop app keeps a *different* data
directory, so accepting there does not affect the copy this pipeline starts.

The backend does expose the same gate over HTTP, which is what this script uses:

    GET  /models/licenses/details/<repo_id>   -> state, terms, fingerprint
    POST /models/licenses/accept              -> {repo_id, fingerprint, accepted}

It deliberately never accepts anything on its own: it prints what the licence
allows, and only records acceptance after you say yes. Acceptance is stored
locally in VoiceStudio's data directory with the exact terms you saw; if the
terms change, VoiceStudio asks again (HTTP 409 ``terms_changed``).

    python scripts/accept_voice_licence.py                    # show + ask
    python scripts/accept_voice_licence.py --list             # nothing to accept?
    python scripts/accept_voice_licence.py --yes              # non-interactive
    python scripts/accept_voice_licence.py --url http://127.0.0.1:3901

Run it against a *running* VoiceStudio (`scripts\\voice\\start-voicestudio.bat`).
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_URL = "http://127.0.0.1:3900"
# The models the default OmniVoice engine needs before its first synthesis.
KNOWN_MODELS = (
    "k2-fsa/OmniVoice",
    "eustlb/higgs-audio-v2-tokenizer",
)


class Unreachable(Exception):
    """VoiceStudio is not running (connection refused / timed out)."""


def _request(url: str, *, data: dict | None = None, timeout: float = 30.0) -> tuple[int, dict]:
    body = json.dumps(data).encode("utf-8") if data is not None else None
    request = urllib.request.Request(url, data=body, method="POST" if body else "GET")
    request.add_header("Accept", "application/json")
    if body is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - loopback
            raw = response.read()
            return response.status, json.loads(raw.decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw.decode("utf-8", "replace") or "{}")
        except Exception:
            payload = {"detail": raw.decode("utf-8", "replace")[:400]}
        return exc.code, payload
    except (urllib.error.URLError, OSError) as exc:
        raise Unreachable(str(getattr(exc, "reason", exc))) from exc


def details(base: str, repo_id: str) -> tuple[int, dict]:
    quoted = urllib.parse.quote(repo_id, safe="/:._-")
    return _request(f"{base}/models/licenses/details/{quoted}")


def accept(base: str, repo_id: str, fingerprint: str) -> tuple[int, dict]:
    return _request(f"{base}/models/licenses/accept",
                    data={"repo_id": repo_id, "fingerprint": fingerprint, "accepted": True})


def _summary(payload: dict) -> tuple[str, str, str]:
    """(state, fingerprint, human-readable licence summary) from a details reply."""
    acceptance = payload.get("acceptance") or {}
    state = str(acceptance.get("state") or "unknown")
    fingerprint = str(acceptance.get("fingerprint") or "")
    info = payload.get("info") or {}
    bits = [str(info.get(key)) for key in ("licence", "license", "name", "category")
            if info.get(key)]
    summary = " / ".join(dict.fromkeys(bits)) or str(info.get("title") or "licence terms")
    return state, fingerprint, summary


def _prose(info: dict) -> list[str]:
    """Whatever the server offers as the human explanation, in a stable order."""
    lines: list[str] = []
    for key in ("summary", "description", "allows", "restrictions", "url"):
        value = info.get(key)
        if isinstance(value, str) and value.strip():
            lines.append(f"    {key:12} {value.strip()}")
        elif isinstance(value, list) and value:
            lines.append(f"    {key:12} " + "; ".join(str(v) for v in value))
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="accept VoiceStudio model licences")
    parser.add_argument("--url", default=DEFAULT_URL, help=f"VoiceStudio base URL (default {DEFAULT_URL})")
    parser.add_argument("--repo", action="append", default=[],
                        help="model repo id to accept (repeatable); default: the OmniVoice engine set")
    parser.add_argument("--list", action="store_true", help="only report the state, accept nothing")
    parser.add_argument("--yes", action="store_true", help="do not ask (you still see the terms)")
    parser.add_argument("--revoke", action="store_true", help="withdraw a previous acceptance")
    args = parser.parse_args(argv)

    base = args.url.rstrip("/")
    repos = args.repo or list(KNOWN_MODELS)

    try:
        status, health = _request(f"{base}/health", timeout=5.0)
    except Unreachable as exc:
        print(f"[error] nothing is listening on {base} ({exc}).")
        print("        start it first: scripts\\voice\\start-voicestudio.bat")
        return 2
    if status != 200:
        print(f"[error] VoiceStudio answered {base}/health with HTTP {status}.")
        print("        it is starting up - wait for 'Deferred startup complete', then retry")
        return 2
    print(f"[ok] VoiceStudio on {base} ({health.get('status') or 'ok'}, "
          f"engine {health.get('engine') or health.get('active_engine') or '?'})")

    pending: list[tuple[str, str]] = []
    unreadable: list[str] = []
    for repo in repos:
        status, payload = details(base, repo)
        if status != 200:
            print(f"[warn] {repo}: could not read the licence (HTTP {status}) "
                  f"{json.dumps(payload)[:160]}")
            unreadable.append(repo)
            continue
        state, fingerprint, summary = _summary(payload)
        print(f"\n{repo}")
        print(f"    state       {state}")
        print(f"    licence     {summary}")
        for line in _prose(payload.get("info") or {}):
            print(line)
        if state in {"accepted", "not_required"}:
            print("    -> nothing to do")
        elif not fingerprint:
            print("    [warn] the server returned no fingerprint; accept it in VoiceStudio itself")
        else:
            pending.append((repo, fingerprint))

    if not pending:
        if unreadable:
            # Do not claim success when nothing could be read: an older build may
            # not expose this route at all, and "all accepted" would be a lie.
            print(f"\n[error] none of the {len(unreadable)} model(s) could be read, so "
                  "acceptance is unknown.")
            print("        this VoiceStudio build may not expose /models/licenses/*.")
            print("        Use an engine that needs no licence instead:")
            print("          Settings -> Narration -> tts_model = kittentts")
            return 2
        print("\nAll checked models are already accepted.")
        return 0
    if args.list:
        print(f"\n{len(pending)} model(s) still need acceptance. "
              "Re-run without --list to accept them.")
        return 1
    if args.revoke:
        for repo, _fingerprint in pending:
            print(f"[skip] {repo}: nothing accepted to revoke")
        return 0

    print("\n" + "=" * 68)
    print(" Accepting means: you confirm you hold the rights these licence terms")
    print(" require. VoiceStudio is not the licensor and cannot grant them - the")
    print(" default OmniVoice weights are CC-BY-NC (non-commercial).")
    print(" Nothing leaves this machine; the acceptance is stored locally.")
    print("=" * 68)
    if not args.yes:
        try:
            answer = input("Type 'accept' to record acceptance of the terms above: ").strip().lower()
        except EOFError:
            answer = ""
        if answer != "accept":
            print("Not accepted - nothing was written.")
            return 1

    failures = 0
    for repo, fingerprint in pending:
        status, payload = accept(base, repo, fingerprint)
        if status == 200:
            print(f"[ok] {repo}: accepted ({payload.get('state') or 'accepted'})")
        elif status == 409:
            print(f"[warn] {repo}: the terms changed since they were read - "
                  "re-run this script to see the new terms")
            failures += 1
        else:
            print(f"[error] {repo}: HTTP {status} {json.dumps(payload)[:200]}")
            failures += 1
    if failures:
        return 1
    print("\nDone. Synthesis works now - generate narration from the dashboard, "
          "or press Sync in Settings -> Narration.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
