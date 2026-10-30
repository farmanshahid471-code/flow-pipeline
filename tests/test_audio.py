"""Audio stage: a music bed and/or a voiceover mixed into the finished cut.

The regression these tests exist for: with a *single* audio track the filter
chain ended in an endless ``apad`` and the mux still asked for ``-shortest``,
so ffmpeg never considered itself finished and one voiceover hung the request
forever. Every case below runs with a hard timeout so a hang fails loudly.
"""
from __future__ import annotations

import subprocess
import threading
import wave
from pathlib import Path

import pytest


def _tone(path: Path, seconds: float, frequency: int = 220) -> Path:
    from flowpipe.config import SETTINGS

    subprocess.run(
        [SETTINGS.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"sine=frequency={frequency}:duration={seconds}", str(path)],
        check=True, timeout=120,
    )
    return path


def _clipped_project(project):
    """Give the fixture's two scenes real clips, ready to assemble."""
    from flowpipe import db, projects
    from flowpipe.config import SETTINGS

    for scene_id in ("scene-1", "scene-2"):
        target = projects.asset_dir(project, "clips", scene_id) / f"{scene_id}.mp4"
        subprocess.run(
            [SETTINGS.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "testsrc=size=320x180:rate=10:duration=2", str(target)],
            check=True, timeout=180,
        )
        db.add_asset(project, "clip", scene_id, str(target), None, None)
    return project


def _assemble_with_timeout(project, **audio):
    """Run the assembly in a thread: a hang must fail the test, not hang pytest."""
    from flowpipe import assemble

    box: dict[str, object] = {}

    def run():
        try:
            box["result"] = assemble.assemble(project, **audio)
        except BaseException as exc:  # noqa: BLE001 - reported below
            box["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout=180)
    assert not thread.is_alive(), "assembly hung - ffmpeg never finished the audio mux"
    if "error" in box:
        raise box["error"]  # type: ignore[misc]
    return box["result"]


def _audio_seconds(path: Path) -> float:
    from flowpipe.config import SETTINGS

    wav = path.with_suffix(".decoded.wav")
    subprocess.run(
        [SETTINGS.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
         "-i", str(path), "-map", "0:a", str(wav)],
        check=True, timeout=180,
    )
    with wave.open(str(wav)) as handle:
        seconds = handle.getnframes() / handle.getframerate()
    wav.unlink(missing_ok=True)
    return seconds


def _has_audio(path: Path) -> bool:
    from flowpipe.config import SETTINGS

    probe = subprocess.run([SETTINGS.ffmpeg(), "-hide_banner", "-i", str(path)],
                           capture_output=True, text=True, timeout=120).stderr
    return "Stream #0:1" in probe and "Audio:" in probe


def test_a_lone_voiceover_mixes_and_finishes(project, tmp_path):
    """The exact case that used to hang: one audio track, no music."""
    result = _assemble_with_timeout(_clipped_project(project),
                                    voiceover_path=_tone(tmp_path / "vo.mp3", 1))
    final = Path(result["path"])
    assert _has_audio(final)
    # padded to the picture, not left at the voiceover's own 1 second
    assert _audio_seconds(final) == pytest.approx(result["duration"], abs=0.35)


def test_a_long_voiceover_does_not_stretch_the_video(project, tmp_path):
    result = _assemble_with_timeout(_clipped_project(project),
                                    voiceover_path=_tone(tmp_path / "long.mp3", 30))
    assert result["duration"] == pytest.approx(4.0, abs=0.5)
    assert _audio_seconds(Path(result["path"])) == pytest.approx(result["duration"], abs=0.35)


def test_music_and_voiceover_are_mixed_together(project, tmp_path):
    result = _assemble_with_timeout(
        _clipped_project(project),
        music_path=_tone(tmp_path / "music.mp3", 4, frequency=440),
        voiceover_path=_tone(tmp_path / "vo.mp3", 2),
    )
    final = Path(result["path"])
    assert _has_audio(final)
    assert _audio_seconds(final) == pytest.approx(result["duration"], abs=0.35)


def test_silent_assembly_has_no_audio_track(project):
    """No uploads: the cut stays silent, exactly as before."""
    result = _assemble_with_timeout(_clipped_project(project))
    assert not _has_audio(Path(result["path"]))
    assert result["duration"] > 1.5


def test_a_missing_audio_file_is_ignored_not_fatal(project, tmp_path):
    result = _assemble_with_timeout(_clipped_project(project),
                                    voiceover_path=tmp_path / "gone.mp3")
    assert not _has_audio(Path(result["path"]))
    assert Path(result["path"]).exists()
