"""The VoiceStudio backend (https://github.com/debpalash/VoiceStudio).

VoiceStudio runs a local FastAPI server. It exposes an OpenAI-compatible audio
API, which is exactly what we need and nothing more:

    POST /v1/audio/speech   {"model": "omnivoice", "input": "…", "voice": "…",
                             "response_format": "wav", "speed": 1.0}  -> bytes
    GET  /v1/audio/voices   -> the voice profiles you cloned/designed there
    GET  /health            -> {"status": "ok", …}

Deliberately a plain HTTP client: no VoiceStudio import, no shared virtualenv, no
torch in this process. VoiceStudio lives in its own folder on D: (see
``docs/VOICE.md``) and this project only ever talks to it over loopback.

WAV is requested by default because the aligner wants uncompressed samples;
``response_format`` is configurable for people who want mp3 to save space.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from ..config import SETTINGS
from .base import BaseBackend, TTSError, TTSResult, TTSUnavailable

DEFAULT_URL = "http://127.0.0.1:3900"
HEALTH_TIMEOUT = 5.0


def _request(url: str, *, data: dict | None = None, timeout: float = 60.0,
             headers: dict | None = None) -> bytes:
    body = json.dumps(data).encode("utf-8") if data is not None else None
    request = urllib.request.Request(url, data=body, method="POST" if body else "GET")
    request.add_header("Accept", "application/json")
    if body is not None:
        request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - loopback only
        return response.read()


def _openai_message(raw: bytes, status: int) -> str:
    """VoiceStudio answers errors in OpenAI's shape: {"error": {"message": …}}."""
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return f"HTTP {status}: {raw[:300]!r}"
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict) and error.get("message"):
        code = error.get("code") or error.get("type") or ""
        return f"{error['message']} ({code})" if code else str(error["message"])
    if isinstance(payload, dict) and payload.get("detail"):
        return str(payload["detail"])[:300]
    return f"HTTP {status}: {raw[:300]!r}"


def probe_duration(path: Path) -> float:
    """Length of the synthesized file, via ffmpeg (ffprobe may not exist)."""
    try:
        proc = subprocess.run([SETTINGS.ffmpeg(), "-hide_banner", "-i", str(path)],
                              capture_output=True, text=True, timeout=120)
    except Exception:
        return 0.0
    marker = "Duration: "
    if marker in proc.stderr:
        stamp = proc.stderr.split(marker, 1)[1].split(",", 1)[0]
        try:
            hours, minutes, seconds = stamp.split(":")
            return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        except Exception:
            return 0.0
    return 0.0


class VoiceStudioBackend(BaseBackend):
    name = "voicestudio"

    def __init__(self, url: str = "", *, model: str = "", fmt: str = "wav",
                 timeout: float = 900.0, api_key: str = "") -> None:
        super().__init__(name="voicestudio", fmt=fmt or "wav", timeout=timeout)
        self.url = (url or DEFAULT_URL).rstrip("/")
        self.model = model or "omnivoice"
        self.api_key = api_key

    # ------------------------------------------------------------- probes --
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    def health(self) -> dict:
        try:
            raw = _request(f"{self.url}/health", timeout=HEALTH_TIMEOUT, headers=self._headers())
        except urllib.error.HTTPError as exc:
            raise TTSUnavailable(
                f"VoiceStudio answered HTTP {exc.code} on {self.url}/health"
            ) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise TTSUnavailable(
                f"VoiceStudio is not running at {self.url} ({getattr(exc, 'reason', exc)}). "
                "Start it with scripts\\voice\\start-voicestudio.bat, or run setup.bat again."
            ) from exc
        try:
            return json.loads(raw.decode("utf-8", "replace") or "{}")
        except Exception:
            return {"raw": raw.decode("utf-8", "replace")[:200]}

    def available(self) -> tuple[bool, str]:
        try:
            payload = self.health()
        except TTSUnavailable as exc:
            return False, str(exc)
        status = str(payload.get("status") or payload.get("ok") or "ok")
        engine = payload.get("engine") or payload.get("active_engine") or self.model
        return True, f"{self.url} ({status}, engine {engine})"

    def voices(self) -> list[dict]:
        """Voice profiles available in VoiceStudio (cloned or designed)."""
        try:
            raw = _request(f"{self.url}/v1/audio/voices", timeout=HEALTH_TIMEOUT,
                           headers=self._headers())
            payload = json.loads(raw.decode("utf-8", "replace") or "{}")
        except Exception:
            return []
        rows = payload.get("voices") if isinstance(payload, dict) else payload
        voices: list[dict] = []
        for row in rows or []:
            if isinstance(row, str):
                voices.append({"id": row, "name": row})
            elif isinstance(row, dict):
                voice_id = row.get("id") or row.get("voice_id") or row.get("name") or ""
                if voice_id:
                    voices.append({"id": str(voice_id),
                                   "name": str(row.get("name") or voice_id),
                                   "language": str(row.get("language") or "")})
        return voices

    # --------------------------------------------------------- synthesis ---
    def synthesize(self, text: str, out_path: Path, *, voice: str = "", speed: float = 1.0,
                   model: str = "") -> TTSResult:
        text = (text or "").strip()
        if not text:
            raise TTSError("nothing to say: the narration text is empty")
        payload = {
            "model": model or self.model,
            "input": text,
            "voice": voice or "default",
            "response_format": self.fmt,
            "speed": max(0.25, min(4.0, float(speed or 1.0))),
        }
        target = self._target(out_path)
        try:
            audio = _request(f"{self.url}/v1/audio/speech", data=payload,
                             timeout=self.timeout, headers=self._headers())
        except urllib.error.HTTPError as exc:
            raise TTSError(_openai_message(exc.read(), exc.code)) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise TTSUnavailable(
                f"VoiceStudio stopped answering at {self.url} "
                f"({getattr(exc, 'reason', exc)}). Is the server still up?"
            ) from exc
        if not audio:
            raise TTSError("VoiceStudio returned an empty body")
        target.write_bytes(audio)
        duration = probe_duration(target)
        if duration <= 0:
            raise TTSError(
                f"VoiceStudio wrote {target.name} but it has no readable duration - "
                "check the server log for the engine error"
            )
        return TTSResult(path=target, duration=duration, backend=self.name,
                         voice=payload["voice"], model=payload["model"],
                         meta={"url": self.url, "format": self.fmt})


def backend_from_settings() -> BaseBackend:
    """Pick the backend the settings ask for."""
    provider = (SETTINGS.tts_provider or "off").strip().lower()
    if provider in {"voicestudio", "voice-studio", "omnivoice"}:
        return VoiceStudioBackend(SETTINGS.voicestudio_url, model=SETTINGS.tts_model,
                                  fmt=SETTINGS.tts_format, api_key=SETTINGS.tts_api_key)
    if provider == "mock":
        from .mock import MockBackend

        return MockBackend()
    raise TTSUnavailable(
        "narration is off: set tts_provider to 'voicestudio' in Settings "
        "(or 'mock' to try the pipeline without a voice server)"
    )


def find_voice_python(tools_dir: Path | None = None) -> Path | None:
    """Where VoiceStudio's own interpreter lives, if setup.bat installed it."""
    root = tools_dir or Path(SETTINGS.voicestudio_dir or "voicestudio")
    for candidate in (root / ".venv" / "Scripts" / "python.exe",
                      root / ".venv" / "bin" / "python",
                      root / ".venv" / "Scripts" / "python"):
        if candidate.exists():
            return candidate
    return None


def uv_binary() -> str | None:
    return shutil.which("uv")
