"""Word-level timestamps for narration — the data every later step is built on.

The idea is taken from whisperX: speech is never treated as one opaque blob, it
is a list of words with ``start`` / ``end`` in seconds. With that list you can

* write subtitles that land on the syllable, not "somewhere in the cue",
* know *which* words fit inside a scene's clip and which ones would be cut,
* speed a line up by exactly the amount it overflows, and keep the words legible,
* mix the narration track scene by scene so the voice follows the picture.

Two things produce a :class:`Timeline` here:

* ``align.proportional`` — distributes the script's words across the audio by
  their length. No model, no downloads; exact enough for clean TTS audio.
* ``align.whisperx`` — real forced alignment from whisperX's JSON output, then
  reconciled against the script (TTS may say "twenty two" where the script says
  "22"), so the timestamps always describe *the script*.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

WORD_RE = re.compile(r"\S+")
SENTENCE_END = (".", "!", "?", "…", "。", "！", "？")
CLAUSE_END = (",", ";", ":", "—", "–")
TRAILING = "\"'”’)]}" 


def split_words(text: str) -> list[str]:
    return [word for word in WORD_RE.findall(text or "")]


def normalize_word(word: str) -> str:
    """Lowercase, strip punctuation — for comparing the script with ASR output."""
    return re.sub(r"[^\w']", "", (word or "").lower())


def estimate_seconds(text: str, words_per_minute: float = 150.0, *, minimum: float = 0.4) -> float:
    """Rough spoken length of a script, used by the mock backend and for warnings."""
    words = len(split_words(text))
    if not words:
        return 0.0
    return max(minimum, words / max(1.0, words_per_minute) * 60.0)


@dataclass(frozen=True)
class Word:
    text: str
    start: float
    end: float

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def shifted(self, offset: float) -> Word:
        return Word(self.text, self.start + offset, self.end + offset)

    def scaled(self, factor: float) -> Word:
        return Word(self.text, self.start * factor, self.end * factor)

    def as_dict(self) -> dict:
        return {"text": self.text, "start": round(self.start, 3), "end": round(self.end, 3)}


@dataclass
class Cue:
    """One subtitle line."""

    text: str
    start: float
    end: float
    words: list[Word] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"text": self.text, "start": round(self.start, 3), "end": round(self.end, 3)}


@dataclass
class Timeline:
    words: list[Word] = field(default_factory=list)
    duration: float = 0.0
    source: str = ""
    language: str = ""

    # ------------------------------------------------------------- queries --
    @property
    def text(self) -> str:
        return " ".join(word.text for word in self.words)

    def __len__(self) -> int:
        return len(self.words)

    def shifted(self, offset: float) -> Timeline:
        return Timeline([w.shifted(offset) for w in self.words], offset + self.duration,
                        self.source, self.language)

    def scaled(self, factor: float) -> Timeline:
        return Timeline([w.scaled(factor) for w in self.words],
                        self.duration * factor, self.source, self.language)

    def clipped(self, limit: float) -> Timeline:
        """Only the words that finish before ``limit`` seconds."""
        kept = [w for w in self.words if w.end <= limit + 1e-6]
        return Timeline(kept, min(self.duration, limit), self.source, self.language)

    def words_between(self, start: float, end: float) -> list[Word]:
        return [w for w in self.words if w.start >= start - 1e-6 and w.end <= end + 1e-6]

    # -------------------------------------------------------------- output --
    def as_dict(self) -> dict:
        return {
            "duration": round(self.duration, 3),
            "source": self.source,
            "language": self.language,
            "text": self.text,
            "words": [w.as_dict() for w in self.words],
        }

    @classmethod
    def from_dict(cls, payload: dict | None) -> Timeline:
        payload = payload or {}
        words = [
            Word(str(w.get("text", "")), float(w.get("start", 0.0)), float(w.get("end", 0.0)))
            for w in payload.get("words", [])
        ]
        return cls(words, float(payload.get("duration", 0.0)), str(payload.get("source", "")),
                   str(payload.get("language", "")))

    def srt(self, *, max_chars: int = 42, max_duration: float = 6.0) -> str:
        return to_srt(chunk_cues(self, max_chars=max_chars, max_duration=max_duration))

    def vtt(self, *, max_chars: int = 42, max_duration: float = 6.0) -> str:
        return to_vtt(chunk_cues(self, max_chars=max_chars, max_duration=max_duration))


# --------------------------------------------------------------- fitting ----
@dataclass
class Fitted:
    """What happens to one scene's narration to make it fit its clip."""

    timeline: Timeline
    speedup: float = 1.0
    dropped: str = ""
    overflow: float = 0.0

    @property
    def fits(self) -> bool:
        return self.speedup <= 1.0 + 1e-9 and not self.dropped


def fit(timeline: Timeline, target: float, *, max_speedup: float = 1.15) -> Fitted:
    """Make a narration segment fit ``target`` seconds using its word timings.

    * already short enough -> unchanged (the caller pads with silence)
    * over by a little -> sped up, up to ``max_speedup``, timestamps scaled too
    * over by a lot -> cut at the last word that fits, and say what was dropped
    """
    if target <= 0 or not timeline.words:
        return Fitted(timeline, 1.0, "", 0.0)
    needed = timeline.duration or (timeline.words[-1].end if timeline.words else 0.0)
    if needed <= target + 1e-6:
        return Fitted(timeline, 1.0, "", 0.0)

    ratio = needed / target
    if ratio <= max_speedup:
        # rounding can push the final word a hair past the clip; clamp it
        return Fitted(_clamp(timeline.scaled(1.0 / ratio), target), ratio, "", 0.0)

    kept = timeline.clipped(target)
    dropped_words = [w.text for w in timeline.words[len(kept.words):]]
    dropped = " ".join(dropped_words).strip()
    return Fitted(kept, 1.0, dropped, needed - target)


def _clamp(timeline: Timeline, limit: float) -> Timeline:
    words = [
        Word(w.text, min(w.start, limit), min(w.end, limit)) for w in timeline.words if w.start < limit
    ]
    return Timeline(words, limit, timeline.source, timeline.language)


# --------------------------------------------------------------- captions ---
def chunk_cues(timeline: Timeline, *, max_chars: int = 42, max_duration: float = 6.0) -> list[Cue]:
    """Group words into readable subtitle lines.

    Breaks on sentence ends first, then on long clauses, then on length or
    duration limits — the same order a subtitle editor would use.
    """
    cues: list[Cue] = []
    current: list[Word] = []

    def flush() -> None:
        nonlocal current
        if not current:
            return
        text = " ".join(w.text for w in current).strip()
        cues.append(Cue(text, current[0].start, max(current[-1].end, current[0].start + 0.2),
                        list(current)))
        current = []

    for word in timeline.words:
        if not word.text.strip():
            continue
        candidate = " ".join([w.text for w in current] + [word.text])
        too_long = len(candidate) > max_chars and len(current) >= 1
        too_slow = current and (word.end - current[0].start) > max_duration
        if too_long or too_slow:
            flush()
        current.append(word)
        bare = word.text.rstrip(TRAILING)
        if bare.endswith(SENTENCE_END) or (
            bare.endswith(CLAUSE_END) and len(" ".join(w.text for w in current)) >= max_chars * 0.6
        ):
            flush()
    flush()
    return cues


def _stamp(seconds: float, *, comma: bool) -> str:
    seconds = max(0.0, float(seconds))
    millis = int(round(seconds * 1000))
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    sep = "," if comma else "."
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{sep}{millis:03d}"


def to_srt(cues: list[Cue]) -> str:
    blocks = []
    for index, cue in enumerate(cues, start=1):
        blocks.append(
            f"{index}\n{_stamp(cue.start, comma=True)} --> {_stamp(cue.end, comma=True)}\n{cue.text}\n"
        )
    return "\n".join(blocks)


def to_vtt(cues: list[Cue]) -> str:
    blocks = ["WEBVTT\n"]
    for cue in cues:
        blocks.append(
            f"{_stamp(cue.start, comma=False)} --> {_stamp(cue.end, comma=False)}\n{cue.text}\n"
        )
    return "\n".join(blocks)


def shift_cues(cues: list[Cue], offset: float) -> list[Cue]:
    return [Cue(c.text, c.start + offset, c.end + offset, [w.shifted(offset) for w in c.words])
            for c in cues]


def merge_cues(cues: list[Cue]) -> list[Cue]:
    ordered = sorted(cues, key=lambda c: (c.start, c.end))
    return [cue for cue in ordered if math.isfinite(cue.start) and math.isfinite(cue.end)]
