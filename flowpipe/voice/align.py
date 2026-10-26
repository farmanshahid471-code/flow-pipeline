"""Where the words actually are in the audio.

This is the whisperX idea applied to narration we generated ourselves: after the
voice server returns a WAV, the text is turned into a list of word timestamps.
Two providers:

``whisperx``
    Runs whisperX (https://github.com/m-bain/whisperX) on the WAV. whisperX
    transcribes, then force-aligns against a phoneme model, and writes word-level
    JSON. Because a TTS engine sometimes says "twenty-two" where the script says
    "22" (or drops a filler word), the ASR words are then *reconciled* with the
    script by sequence alignment: script words keep their position, matched words
    take the ASR timing, unmatched ones are interpolated between their
    neighbours. Result: timestamps that index the script you wrote.

``proportional``
    No model at all: distribute the duration across the words weighted by their
    length, leaving a small gap between words and a lead-in. For clean
    single-speaker TTS audio this is close to reality, and it always works —
    including on a machine that never installs whisperX.

Both return the same :class:`~flowpipe.voice.timeline.Timeline`.
"""
from __future__ import annotations

import difflib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from ..config import SETTINGS
from .base import AlignError
from .timeline import Timeline, Word, normalize_word, split_words

LEAD_IN = 0.10      # a little silence before the first word
WORD_GAP = 0.05     # breath between words
MIN_WORD = 0.08     # never emit a zero-length word


# ------------------------------------------------------------ proportional --
def proportional(text: str, duration: float, *, lead: float = LEAD_IN,
                 gap: float = WORD_GAP) -> Timeline:
    """Spread ``text`` over ``duration`` seconds by word length."""
    words = split_words(text)
    if not words:
        raise AlignError("no words to align")
    if duration <= 0:
        raise AlignError("the audio has no duration")
    usable = max(0.2, duration - lead)
    weights = [max(1, len(re.sub(r"\W", "", word))) for word in words]
    gaps = gap * (len(words) - 1)
    speech = max(0.1, usable - gaps)
    total_weight = sum(weights)
    cursor = lead
    result: list[Word] = []
    for index, (word, weight) in enumerate(zip(words, weights, strict=False)):
        span = max(MIN_WORD, speech * weight / total_weight)
        start = cursor
        end = min(duration, start + span)
        if index == len(words) - 1:
            end = max(end, min(duration, start + MIN_WORD))
        result.append(Word(word, round(start, 3), round(end, 3)))
        cursor = end + gap
    return Timeline(result, duration, "proportional")


# ----------------------------------------------------------------- whisperx --
def whisperx_json_to_timeline(payload: dict, script: str = "") -> Timeline:
    """Turn whisperX's JSON into a timeline that indexes *the script*."""
    asr_words: list[tuple[str, float, float]] = []
    if isinstance(payload.get("word_segments"), list):
        for row in payload["word_segments"]:
            _push(asr_words, row)
    if not asr_words:
        for segment in payload.get("segments") or []:
            for row in segment.get("words") or []:
                _push(asr_words, row)
    if not asr_words:
        raise AlignError("whisperX returned no word timestamps (did alignment fail?)")

    duration = float(
        payload.get("duration")
        or (payload.get("segments") or [{}])[-1].get("end")
        or asr_words[-1][2]
    )
    language = str(payload.get("language") or "")

    script_words = split_words(script)
    if not script_words:
        return Timeline([Word(t, s, e) for t, s, e in asr_words], duration, "whisperx", language)

    result = _reconcile(script_words, asr_words)
    return Timeline(result, duration, "whisperx", language)


def _push(target: list[tuple[str, float, float]], row: dict) -> None:
    text = str(row.get("word") or row.get("text") or "").strip()
    if not text:
        return
    try:
        start = float(row.get("start"))
        end = float(row.get("end"))
    except (TypeError, ValueError):
        return
    if end <= start:
        end = start + MIN_WORD
    target.append((text, start, end))


def _reconcile(script_words: list[str], asr_words: list[tuple[str, float, float]]) -> list[Word]:
    """Give every script word a timing, borrowing from the ASR where it matches."""
    script_keys = [normalize_word(w) for w in script_words]
    asr_keys = [normalize_word(t) for t, _, _ in asr_words]
    matcher = difflib.SequenceMatcher(a=script_keys, b=asr_keys, autojunk=False)

    timed: list[Word | None] = [None] * len(script_words)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for offset in range(min(i2 - i1, j2 - j1)):
                _, start, end = asr_words[j1 + offset]
                timed[i1 + offset] = Word(script_words[i1 + offset], start, end)
        elif tag == "replace":
            # same count on both sides: keep the script wording, take ASR timing
            span_script = i2 - i1
            span_asr = j2 - j1
            if span_script == span_asr:
                for offset in range(span_script):
                    _, start, end = asr_words[j1 + offset]
                    timed[i1 + offset] = Word(script_words[i1 + offset], start, end)
    return _interpolate(script_words, timed)


def _interpolate(script_words: list[str], timed: list[Word | None]) -> list[Word]:
    """Fill the gaps between matched words by sharing the time evenly."""
    words = list(timed)
    index = 0
    total = len(words)
    while index < total:
        if words[index] is not None:
            index += 1
            continue
        start_index = index
        while index < total and words[index] is None:
            index += 1
        end_index = index  # exclusive

        previous_end = words[start_index - 1].end if start_index > 0 and words[start_index - 1] else 0.0
        if end_index < total and words[end_index] is not None:
            next_start = words[end_index].start
        else:
            last_known = next((w.end for w in reversed(words[:start_index]) if w), previous_end)
            next_start = last_known + 0.25 * (end_index - start_index)
        next_start = max(next_start, previous_end + 0.01)
        count = end_index - start_index
        step = max(MIN_WORD, (next_start - previous_end) / max(1, count))
        for offset in range(count):
            start = previous_end + step * offset
            words[start_index + offset] = Word(script_words[start_index + offset],
                                               round(start, 3), round(start + step * 0.9, 3))
    return [word for word in words if word is not None]


# ---------------------------------------------------------------- whisperX ---
def whisperx_command(audio: Path, out_dir: Path, *, model: str, language: str) -> list[str]:
    """The CLI this project drives. Kept in one place so a version bump is a one-liner."""
    # whisperX aligns by default (that is where the word timings come from)
    args = [sys.executable, "-m", "whisperx", str(audio),
            "--model", model or "base.en",
            "--output_format", "json",
            "--output_dir", str(out_dir)]
    if language and language.lower() not in {"auto", "none"}:
        args += ["--language", language]
    return args


def whisperx_available() -> tuple[bool, str]:
    """Is whisperX importable by the interpreter that will run it?"""
    target = SETTINGS.whisperx_python or sys.executable
    try:
        proc = subprocess.run([target, "-c", "import whisperx; print(whisperx.__version__)"],
                              capture_output=True, text=True, timeout=180)
    except Exception as exc:
        return False, f"could not run {target}: {exc}"
    if proc.returncode != 0:
        return False, "whisperX is not installed for this interpreter (pip install whisperx)"
    return True, f"whisperX {proc.stdout.strip()}"


def run_whisperx(audio: Path, *, model: str = "", language: str = "") -> dict:
    """Run whisperX in a scratch folder under the download root and return its JSON."""
    out_dir = Path(SETTINGS.flow_downloads_dir) / "_align"
    out_dir.mkdir(parents=True, exist_ok=True)
    python = SETTINGS.whisperx_python or sys.executable
    command = whisperx_command(audio, out_dir, model=model, language=language)
    command[0] = python
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=3600)
    except FileNotFoundError as exc:
        raise AlignError(f"could not start whisperX: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise AlignError("whisperX timed out") from exc
    produced = sorted(out_dir.glob(f"{audio.stem}*.json"), key=lambda p: p.stat().st_mtime)
    if not produced:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-6:]
        raise AlignError(
            "whisperX produced no JSON. Last lines:\n  " + "\n  ".join(tail)
            if tail else "whisperX produced no JSON"
        )
    payload = json.loads(produced[-1].read_text(encoding="utf-8"))
    shutil.rmtree(out_dir, ignore_errors=True)  # never leave scratch behind
    return payload


# ---------------------------------------------------------------- dispatch ---
def align(audio: Path, text: str, *, provider: str = "", duration: float = 0.0,
          language: str = "") -> Timeline:
    """Produce timestamps for ``audio`` whose script is ``text``."""
    provider = (provider or SETTINGS.align_provider or "proportional").strip().lower()
    if provider in {"whisperx", "whisper-x", "asr"}:
        payload = run_whisperx(audio, model=SETTINGS.whisperx_model, language=language)
        return whisperx_json_to_timeline(payload, text)
    length = duration or _probe(audio)
    return proportional(text, length)


def _probe(audio: Path) -> float:
    from .voicestudio import probe_duration

    seconds = probe_duration(audio)
    if seconds <= 0:
        raise AlignError(f"could not read the duration of {audio.name}")
    return seconds
