"""Stage E — normalize every clip and concatenate them into the final video.

Clips from Flow arrive with slightly different sizes, frame rates and audio
streams (and different ones again after a model change mid-project). Each clip is
therefore re-encoded to a canonical format with ffmpeg *first*, and only then
joined with the concat demuxer, which is the reliable way to get a clean splice.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from . import db, projects
from .config import SETTINGS
from .models import Manifest


class AssemblyError(RuntimeError):
    pass


def _run(args: list[str], what: str) -> None:
    cmd = [SETTINGS.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", *args]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssemblyError(f"{what} failed: {proc.stderr.strip()[:800]}")
    return None


def _normalize(source: Path, target: Path, *, fps: int, width: int, height: int, crf: int) -> None:
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"fps={fps},setsar=1,format=yuv420p"
    )
    _run(
        [
            "-i", str(source),
            "-vf", vf,
            "-an",
            "-c:v", "libx264", "-preset", "medium", "-crf", str(crf),
            "-movflags", "+faststart",
            str(target),
        ],
        f"normalizing {source.name}",
    )


def _clip_sequence(manifest: Manifest, project_id: str) -> list[tuple[str, Path]]:
    sequence: list[tuple[str, Path]] = []
    missing: list[str] = []
    for scene in manifest.ordered_scenes():
        asset = db.latest_asset(project_id, "clip", scene.id)
        if not asset:
            missing.append(scene.id)
            continue
        path = Path(asset["path"])
        if not path.exists():
            missing.append(scene.id)
            continue
        sequence.append((scene.id, path))
    if missing:
        raise AssemblyError(
            "these scenes have no rendered clip yet: " + ", ".join(missing)
        )
    return sequence


def assemble(
    project_id: str,
    *,
    music_path: Path | None = None,
    voiceover_path: Path | None = None,
    width: int | None = None,
    height: int | None = None,
    fps: int | None = None,
    crf: int | None = None,
    filename: str = "final.mp4",
) -> dict[str, Any]:
    manifest = projects.read_manifest(project_id)
    if manifest is None:
        raise AssemblyError("no manifest for this project")

    width = width or SETTINGS.output_width
    height = height or SETTINGS.output_height
    fps = fps or SETTINGS.output_fps
    crf = crf or SETTINGS.output_crf
    # keep dimensions even: yuv420p requires it
    width -= width % 2
    height -= height % 2

    sequence = _clip_sequence(manifest, project_id)
    db.update_project(project_id, status="assembling")
    db.log("assembling final video", project_id=project_id)

    out_dir = projects.output_dir(project_id)
    final_path = out_dir / filename
    workdir = Path(tempfile.mkdtemp(prefix="flowpipe-assemble-"))
    try:
        concat_lines: list[str] = []
        for index, (scene_id, path) in enumerate(sequence, start=1):
            normalized = workdir / f"{index:03d}_{scene_id}.mp4"
            _normalize(path, normalized, fps=fps, width=width, height=height, crf=crf)
            concat_lines.append(f"file '{normalized.as_posix()}'")
        listing = workdir / "concat.txt"
        listing.write_text("\n".join(concat_lines) + "\n", encoding="utf-8")

        silent = workdir / "joined.mp4"
        _run(["-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(silent)],
             "concatenating clips")

        audio_inputs: list[str] = []
        if music_path and music_path.exists():
            audio_inputs.append(str(music_path))
        if voiceover_path and voiceover_path.exists():
            audio_inputs.append(str(voiceover_path))

        if not audio_inputs:
            shutil.copyfile(silent, final_path)
        else:
            args = ["-i", str(silent)]
            for path in audio_inputs:
                args += ["-i", path]
            filters = []
            labels = []
            for index in range(len(audio_inputs)):
                filters.append(
                    f"[{index + 1}:a]aresample=48000,volume=0.6[a{index}]"
                )
                labels.append(f"[a{index}]")
            if len(labels) == 1:
                filters.append(f"{labels[0]}apad[aout]")
            else:
                filters.append(f"{''.join(labels)}amix=inputs={len(labels)}:duration=longest[aout]")
            args += [
                "-filter_complex", ";".join(filters),
                "-map", "0:v:0", "-map", "[aout]",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                "-shortest", "-movflags", "+faststart",
                str(final_path),
            ]
            _run(args, "muxing audio")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    thumbnail = out_dir / "thumbnail.jpg"
    try:
        _run(["-ss", "0.5", "-i", str(final_path), "-frames:v", "1", "-q:v", "3", str(thumbnail)],
             "thumbnail")
    except AssemblyError:
        thumbnail = None  # type: ignore[assignment]

    duration = probe_duration(final_path)
    download_copy = _copy_to_downloads(project_id, manifest, final_path)
    db.update_project(project_id, status="complete", output_path=str(final_path), error=None)
    db.log(f"final video ready: {final_path.name} ({duration:.1f}s)", project_id=project_id)
    if download_copy:
        db.log(f"copy for download: {download_copy}", project_id=project_id)
    return {
        "path": str(final_path),
        "download_path": str(download_copy) if download_copy else None,
        "thumbnail": str(thumbnail) if thumbnail else None,
        "duration": duration,
        "scenes": [scene_id for scene_id, _ in sequence],
        "width": width,
        "height": height,
        "fps": fps,
    }


def download_name(project_id: str, manifest: Manifest) -> str:
    """A readable, collision-free file name for the downloads folder."""
    slug = re.sub(r"[^A-Za-z0-9]+", "-", manifest.project.topic).strip("-") or "project"
    # truncate on a word boundary so the name stays readable
    words, topic = slug.split("-"), ""
    for word in words:
        candidate = f"{topic}-{word}" if topic else word
        if len(candidate) > 60:
            break
        topic = candidate
    suffix = project_id.rsplit("-", 1)[-1] or project_id[-6:]
    return f"{topic or slug[:60]}-{suffix}.mp4"


def _copy_to_downloads(project_id: str, manifest: Manifest, final_path: Path) -> Path | None:
    """One plainly named copy in the download root, so the finished video is
    where the user expects downloads to be. Turn off with
    `copy_final_to_downloads = false` to save disk space."""
    if not SETTINGS.copy_final_to_downloads:
        return None
    try:
        target_dir = SETTINGS.final_downloads_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / download_name(project_id, manifest)
        shutil.copy2(final_path, target)
        return target
    except OSError as exc:
        db.log(f"could not copy the final video to {SETTINGS.downloads_dir}: {exc}",
               project_id=project_id, level="warn")
        return None


def probe_duration(path: Path) -> float:
    """ffprobe may not exist; fall back to parsing ffmpeg's own output."""
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        proc = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of",
             "json", str(path)],
            capture_output=True, text=True,
        )
        if proc.returncode == 0:
            try:
                return float(json.loads(proc.stdout)["format"]["duration"])
            except Exception:
                pass
    proc = subprocess.run([SETTINGS.ffmpeg(), "-i", str(path)], capture_output=True, text=True)
    text = proc.stderr
    marker = "Duration: "
    if marker in text:
        stamp = text.split(marker, 1)[1].split(",", 1)[0]
        try:
            hours, minutes, seconds = stamp.split(":")
            return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        except Exception:
            return 0.0
    return 0.0
