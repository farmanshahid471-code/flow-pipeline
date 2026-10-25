#!/usr/bin/env python3
"""Install a static ffmpeg build into ``<tools>/ffmpeg``.

Driven by ``setup.bat``. Uses only the standard library so it can run before any
package is installed.

    python scripts/win/install_ffmpeg.py --tools D:\\Tools

Order of attempts:
  1. gyan.dev "release essentials" zip (full build, includes ffprobe)
  2. BtbN FFmpeg-Builds "latest" zip  (full build, includes ffprobe)
  3. copy the ffmpeg binary that ships inside the `imageio-ffmpeg` wheel
     (works offline, no ffprobe — the pipeline falls back to parsing ffmpeg's
     own output for durations)

Downloads and extraction happen under ``<tools>/downloads`` and ``<tools>/tmp``
so nothing is written to the C: drive.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

SOURCES = [
    ("gyan.dev essentials", "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"),
    (
        "BtbN latest win64 gpl",
        "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
        "ffmpeg-master-latest-win64-gpl.zip",
    ),
]

EXE_SUFFIX = ".exe" if os.name == "nt" else ""
# an archive may be inspected on any OS, so look for both spellings
FFMPEG_NAMES = ("ffmpeg.exe", "ffmpeg")
SIBLING_NAMES = ("ffprobe.exe", "ffprobe")


def log(message: str) -> None:
    print(f"[ffmpeg] {message}", flush=True)


def find_bin_dir(extracted: Path) -> Path | None:
    """Locate the ``bin`` directory inside an extracted archive."""
    for name in FFMPEG_NAMES:
        matches = sorted(extracted.rglob(name))
        # Fast path: <archive-root>/bin/ffmpeg[.exe]
        for match in matches:
            if match.is_file() and match.parent.name == "bin":
                return match.parent
        # Slow path: any file named ffmpeg, take its directory
        for match in matches:
            if match.is_file():
                return match.parent
    return None


def installed_binary(tools: Path) -> Path | None:
    """The ffmpeg executable under ``<tools>/ffmpeg`` if it is there."""
    for name in FFMPEG_NAMES:
        candidate = tools / "ffmpeg" / "bin" / name
        if candidate.exists():
            return candidate
    return None


def download(url: str, destination: Path, *, timeout: int = 60) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    log(f"downloading {url}")
    request = urllib.request.Request(url, headers={"User-Agent": "flow-pipeline-setup/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed https URLs
        total = int(response.headers.get("Content-Length") or 0)
        done = 0
        step = 5 * 1024 * 1024
        with destination.open("wb") as handle:
            while True:
                chunk = response.read(1024 * 256)
                if not chunk:
                    break
                handle.write(chunk)
                done += len(chunk)
                if total and done % step < len(chunk):
                    log(f"  {done // (1024 * 1024)} / {total // (1024 * 1024)} MB")
    log(f"saved {destination.name} ({destination.stat().st_size // (1024 * 1024)} MB)")
    return destination


def install_from_zip(archive: Path, tools: Path, *, verify: bool = True) -> Path | None:
    """Extract `archive` and copy its ffmpeg tree to ``<tools>/ffmpeg``."""
    work = tools / "tmp" / "ffmpeg-extract"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(work)
    except (zipfile.BadZipFile, OSError) as exc:
        log(f"could not extract {archive.name}: {exc}")
        return None

    bin_dir = find_bin_dir(work)
    if bin_dir is None:
        log("no ffmpeg executable found in the archive")
        return None

    target = tools / "ffmpeg"
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)
    shutil.copytree(bin_dir.parent, target, dirs_exist_ok=True)
    shutil.rmtree(work, ignore_errors=True)

    binary = installed_binary(tools)
    if binary is None:
        log(f"expected {target / 'bin'} to contain ffmpeg after extraction")
        return None
    if not verify:
        return binary
    if not check_version(binary):
        log("the extracted binary did not run (`ffmpeg -version` failed)")
        return None
    return binary


def check_version(binary: Path) -> bool:
    try:
        proc = subprocess.run([str(binary), "-version"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    if proc.returncode != 0:
        return False
    first = (proc.stdout or proc.stderr).splitlines()[0] if (proc.stdout or proc.stderr) else ""
    log(f"verified: {first.strip()}")
    return True


def install_from_imageio(tools: Path) -> Path | None:
    """Last resort: reuse the ffmpeg binary bundled in the imageio-ffmpeg wheel."""
    try:
        import imageio_ffmpeg
    except ImportError:
        log("imageio-ffmpeg is not installed, nothing to fall back to")
        return None
    try:
        source = Path(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception as exc:  # pragma: no cover - depends on the wheel
        log(f"imageio-ffmpeg could not provide a binary: {exc}")
        return None
    if not source.exists():
        return None

    target = tools / "ffmpeg" / "bin"
    target.mkdir(parents=True, exist_ok=True)
    binary = target / f"ffmpeg{EXE_SUFFIX}"
    shutil.copy2(source, binary)
    if os.name != "nt":
        binary.chmod(0o755)
    log(f"copied the bundled imageio-ffmpeg binary to {binary}")
    log("note: no ffprobe in this fallback; durations are read from ffmpeg itself")
    return binary if check_version(binary) else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install ffmpeg into the project tool root")
    parser.add_argument("--tools", default=os.environ.get("FLOWPIPE_TOOLS_DIR") or "D:/Tools")
    parser.add_argument("--force", action="store_true", help="reinstall even if present")
    parser.add_argument("--keep-download", action="store_true")
    args = parser.parse_args(argv)

    tools = Path(args.tools)
    tools.mkdir(parents=True, exist_ok=True)
    downloads = tools / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    (tools / "tmp").mkdir(parents=True, exist_ok=True)

    binary = installed_binary(tools)
    if binary is not None and not args.force:
        log(f"already installed: {binary}")
        return 0 if check_version(binary) else 1

    for label, url in SOURCES:
        archive = downloads / url.rsplit("/", 1)[-1]
        try:
            if not archive.exists():
                download(url, archive)
            installed = install_from_zip(archive, tools)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            log(f"{label} failed: {exc}")
            continue
        if installed:
            log(f"installed from {label}: {installed}")
            if not args.keep_download:
                archive.unlink(missing_ok=True)
            return 0
        log(f"{label} did not produce a usable build")

    log("falling back to the binary bundled with imageio-ffmpeg")
    if install_from_imageio(tools):
        return 0
    log("FAILED: could not install ffmpeg")
    return 1


if __name__ == "__main__":
    sys.exit(main())
