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


# Music and voiceover are mixed at this level so neither drowns the other.
AUDIO_VOLUME = 0.6


def _run(args: list[str], what: str, *, timeout: int = 900, cwd: Path | None = None) -> None:
    cmd = [SETTINGS.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", *args]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    except subprocess.TimeoutExpired as exc:
        # subprocess.run already killed it; never leave the request wedged.
        raise AssemblyError(f"{what} timed out after {timeout}s") from exc
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


def _silence(path: Path, seconds: float) -> None:
    """A silent stretch, so scenes without a line still occupy their time."""
    _run(["-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono",
          "-t", f"{max(0.05, seconds):.3f}", "-c:a", "pcm_s16le", str(path)],
         "building silence")


def _build_narration_track(project_id: str, workdir: Path,
                           sequence: list[tuple[str, Path]],
                           durations: dict[str, float]) -> tuple[Path | None, dict[str, Any]]:
    """Fit each scene's narration into its clip and join the pieces.

    This is what the word timestamps are for: a line that is 0.4 s too long is
    sped up (and its timings scaled), a line far too long keeps only the words
    that fit, and the caller is told exactly which words were dropped.
    """
    from .voice import stage as narration_stage
    from .voice import timeline as vt

    plan = narration_stage.load(project_id)
    info: dict[str, Any] = {"entries": {}, "track": None}
    if not plan:
        return None, info
    info["provider"] = plan.get("provider", "")
    info["voice"] = plan.get("voice", "")
    by_scene = {entry["scene_id"]: entry for entry in plan["scenes"]}

    pieces: list[Path] = []
    used = 0
    for index, (scene_id, _path) in enumerate(sequence, start=1):
        target = max(0.1, durations.get(scene_id) or 0.1)
        piece = workdir / f"vo_{index:03d}.wav"
        entry = by_scene.get(scene_id)
        if not entry or not entry.get("exists") or not len(entry["timeline"]):
            _silence(piece, target)
            info["entries"][scene_id] = {"fitted": None, "target": target}
            continue
        fitted = vt.fit(entry["timeline"], target,
                        max_speedup=SETTINGS.narration_max_speedup)
        filters = []
        if fitted.speedup > 1.001:
            filters.append(f"atempo={min(4.0, max(0.5, fitted.speedup)):.4f}")
        filters.append(f"apad=whole_dur={target:.3f},atrim=0:{target:.3f}")
        _run(["-i", entry["audio"], "-filter:a", ",".join(filters),
              "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", str(piece)],
             f"fitting narration for {scene_id}")
        if fitted.dropped:
            db.log(
                f"narration for {scene_id} is {fitted.overflow:.1f}s longer than its "
                f"{target:.1f}s clip; these words are cut: \"{fitted.dropped[:90]}\"",
                project_id=project_id, level="warn",
            )
        elif fitted.speedup > 1.001:
            db.log(f"narration for {scene_id} sped up {fitted.speedup:.2f}x to fit its clip",
                   project_id=project_id)
        info["entries"][scene_id] = {"fitted": fitted, "target": target}
        pieces.append(piece)
        used += 1

    if not pieces:
        return None, info
    listing = workdir / "narration.txt"
    listing.write_text("\n".join(f"file '{piece.as_posix()}'" for piece in pieces) + "\n",
                       encoding="utf-8")
    track = workdir / "narration.wav"
    _run(["-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(track)],
         "joining the narration")
    info["track"] = str(track)
    db.log(f"narration track: {used} scene(s) mixed against the cut", project_id=project_id)
    return track, info


def _captions_srt(sequence: list[tuple[str, Path]], durations: dict[str, float],
                  narration: dict[str, Any]) -> str:
    """Subtitles with global timings: each scene's words shifted to where it starts."""
    from .voice import timeline as vt

    cues: list[vt.Cue] = []
    offset = 0.0
    for scene_id, _path in sequence:
        entry = (narration.get("entries") or {}).get(scene_id)
        fitted = (entry or {}).get("fitted")
        if fitted is not None:
            cues.extend(vt.shift_cues(
                vt.chunk_cues(fitted.timeline, max_chars=SETTINGS.caption_max_chars), offset))
        offset += durations.get(scene_id) or 0.0
    return vt.to_srt(cues) if cues else ""


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
    use_narration: bool = True,
    captions: str | None = None,
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
    subtitle_path: Path | None = None
    workdir = Path(tempfile.mkdtemp(prefix="flowpipe-assemble-"))
    try:
        concat_lines: list[str] = []
        durations: dict[str, float] = {}
        for index, (scene_id, path) in enumerate(sequence, start=1):
            normalized = workdir / f"{index:03d}_{scene_id}.mp4"
            _normalize(path, normalized, fps=fps, width=width, height=height, crf=crf)
            durations[scene_id] = probe_duration(normalized)
            concat_lines.append(f"file '{normalized.as_posix()}'")
        listing = workdir / "concat.txt"
        listing.write_text("\n".join(concat_lines) + "\n", encoding="utf-8")

        silent = workdir / "joined.mp4"
        _run(["-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(silent)],
             "concatenating clips")

        total_duration = probe_duration(silent)
        if total_duration <= 0:
            raise AssemblyError("could not read the duration of the joined clips")

        # ---- narration: one fitted voice clip per scene, joined in order -----
        narration = {"entries": {}}
        audio_inputs: list[str] = []
        if use_narration:
            track, narration = _build_narration_track(project_id, workdir, sequence, durations)
            if track is not None:
                audio_inputs.append(track)
        if music_path and music_path.exists():
            audio_inputs.append(str(music_path))
        if voiceover_path and voiceover_path.exists():
            if audio_inputs and narration.get("track"):
                db.log("an uploaded voiceover replaces the generated narration in this mix",
                       project_id=project_id, level="warn")
                audio_inputs = [str(music_path)] if music_path and music_path.exists() else []
            audio_inputs.append(str(voiceover_path))

        # ---- captions: .srt from the fitted word timings, optionally burned --
        caption_mode = (captions if captions is not None else SETTINGS.captions) or "off"
        srt_text = _captions_srt(sequence, durations, narration) if caption_mode != "off" else ""
        if srt_text:
            subtitle_path = out_dir / Path(filename).with_suffix(".srt").name
            subtitle_path.write_text(srt_text, encoding="utf-8")
            (workdir / "subs.srt").write_text(srt_text, encoding="utf-8")
            db.log(f"subtitles: {subtitle_path.name} ({srt_text.count('-->')} cues)",
                   project_id=project_id)
        elif caption_mode == "burn":
            db.log("nothing to burn in: no narration timings for these scenes",
                   project_id=project_id, level="warn")
        burn = bool(subtitle_path) and caption_mode == "burn"

        if not audio_inputs and not burn:
            shutil.copyfile(silent, final_path)
        else:
            # The silent cut IS the timeline. Narration, music and voiceover are
            # mixed against it, padded when they are shorter and trimmed when
            # they are longer, and `-t` ends the mux. Do not use `-shortest`
            # here: with an endless apad and a stream-copied video ffmpeg never
            # decides it is done and the request hangs forever.
            args = ["-i", str(silent)]
            chains: list[str] = []
            if audio_inputs:
                for path in audio_inputs:
                    args += ["-i", path]
                chains = [
                    f"[{index + 1}:a]aresample=48000,volume={AUDIO_VOLUME}[a{index}]"
                    for index in range(len(audio_inputs))
                ]
                mix_in = "[a0]"
                if len(audio_inputs) > 1:
                    joined = "".join(f"[a{index}]" for index in range(len(audio_inputs)))
                    chains.append(
                        f"{joined}amix=inputs={len(audio_inputs)}:duration=longest[amixed]"
                    )
                    mix_in = "[amixed]"
                chains.append(
                    f"{mix_in}apad=whole_dur={total_duration:.3f},"
                    f"atrim=0:{total_duration:.3f}[aout]"
                )
                args += ["-filter_complex", ";".join(chains), "-map", "0:v:0", "-map", "[aout]"]
            else:
                args += ["-map", "0:v:0"]
            if burn:
                # relative name + cwd: Windows drive letters in the subtitles
                # filter need escaping, a bare filename needs none
                args += ["-vf", "subtitles=subs.srt", "-c:v", "libx264",
                         "-preset", "medium", "-crf", str(crf), "-pix_fmt", "yuv420p"]
            else:
                args += ["-c:v", "copy"]
            if audio_inputs:
                args += ["-c:a", "aac", "-b:a", "192k"]
            args += ["-t", f"{total_duration:.3f}", "-movflags", "+faststart", str(final_path)]
            _run(args, "muxing audio", cwd=workdir if burn else None)
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
        if subtitle_path:
            try:
                shutil.copy2(subtitle_path, download_copy.with_suffix(".srt"))
            except OSError as exc:
                db.log(f"could not copy the subtitles next to the download: {exc}",
                       project_id=project_id, level="warn")
    return {
        "path": str(final_path),
        "download_path": str(download_copy) if download_copy else None,
        "subtitle_path": str(subtitle_path) if subtitle_path else None,
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
