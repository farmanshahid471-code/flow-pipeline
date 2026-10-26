"""Text-to-speech backends.

One protocol, three implementations:

* :class:`flowpipe.voice.voicestudio.VoiceStudioBackend` — the real one. Talks to
  a local VoiceStudio server (``http://127.0.0.1:3900``) over its
  OpenAI-compatible ``POST /v1/audio/speech``, so no VoiceStudio code is imported
  into this project and nothing leaves the machine.
* :class:`flowpipe.voice.mock.MockBackend` — offline placeholder audio, used by
  the mock driver, the tests and the self test. No models, no network.
* **off** — no narration at all (the default until a backend is configured).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


class VoiceError(RuntimeError):
    """Base class for anything that stops narration from happening."""


class TTSUnavailable(VoiceError):
    """The backend is not installed, not running, or not reachable."""


class TTSError(VoiceError):
    """The backend answered, but refused or failed the request."""


class AlignError(VoiceError):
    """Timestamps could not be produced for the synthesized audio."""


@dataclass
class TTSResult:
    path: Path
    duration: float
    backend: str
    voice: str = ""
    model: str = ""
    meta: dict = field(default_factory=dict)


class TTSBackend(Protocol):
    name: str

    def available(self) -> tuple[bool, str]:
        """(ok, human-readable detail) — used by /api/health and the UI."""

    def voices(self) -> list[dict]: ...

    def synthesize(self, text: str, out_path: Path, *, voice: str = "", speed: float = 1.0,
                   model: str = "") -> TTSResult: ...


@dataclass
class BaseBackend:
    """Shared plumbing: the output format, and where the audio ends up."""

    name: str = "base"
    fmt: str = "wav"
    timeout: float = 900.0

    def _target(self, out_path: Path) -> Path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        return out_path

    def available(self) -> tuple[bool, str]:  # pragma: no cover - overridden
        return True, "available"

    def voices(self) -> list[dict]:  # pragma: no cover - overridden
        return []

    def synthesize(self, text: str, out_path: Path, *, voice: str = "", speed: float = 1.0,
                   model: str = "") -> TTSResult:  # pragma: no cover - overridden
        raise NotImplementedError
