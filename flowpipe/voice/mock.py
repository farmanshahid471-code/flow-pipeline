"""Offline stand-in for the voice server.

Same contract as VoiceStudio, no models and no network: it writes a WAV whose
length matches how long the script would take to read out loud (a soft two-tone
"speech" so you can hear where the line starts and ends). That is enough to let
the mock driver, the whole test suite and ``scripts/selftest.py`` exercise
narration, timestamping, captioning and the final mix without installing a
multi-gigabyte model.
"""
from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

from .base import BaseBackend, TTSError, TTSResult
from .timeline import estimate_seconds, split_words

SAMPLE_RATE = 24_000
TONE_HZ = 180.0


def _write_tone(path: Path, seconds: float, words: int) -> None:
    """A WAV with a gentle syllable-ish envelope, so it is obviously not silence."""
    frames = max(1, int(seconds * SAMPLE_RATE))
    samples = bytearray()
    for index in range(frames):
        t = index / SAMPLE_RATE
        # a slow amplitude wobble reads as "someone talking" in a test recording
        envelope = 0.25 + 0.2 * math.sin(2 * math.pi * 2.4 * t)
        value = int(9000 * envelope * math.sin(2 * math.pi * TONE_HZ * t))
        samples += struct.pack("<h", max(-32768, min(32767, value)))
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(bytes(samples))


class MockBackend(BaseBackend):
    name = "mock"

    def __init__(self, *, words_per_minute: float = 150.0) -> None:
        super().__init__(name="mock", fmt="wav")
        self.words_per_minute = words_per_minute

    def available(self) -> tuple[bool, str]:
        return True, "offline placeholder voices (no model needed)"

    def voices(self) -> list[dict]:
        return [
            {"id": "mock-narrator", "name": "Mock narrator (placeholder tone)"},
            {"id": "default", "name": "Engine default"},
        ]

    def synthesize(self, text: str, out_path: Path, *, voice: str = "", speed: float = 1.0,
                   model: str = "") -> TTSResult:
        text = (text or "").strip()
        if not text:
            raise TTSError("nothing to say: the narration text is empty")
        words = split_words(text)
        seconds = estimate_seconds(text, self.words_per_minute) / max(0.25, float(speed or 1.0))
        target = self._target(out_path.with_suffix(".wav"))
        _write_tone(target, seconds, len(words))
        return TTSResult(path=target, duration=seconds, backend=self.name,
                         voice=voice or "mock-narrator", model=model or "mock",
                         meta={"words": len(words), "estimated": True})
