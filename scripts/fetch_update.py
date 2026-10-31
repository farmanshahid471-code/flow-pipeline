"""Download this branch's archive and extract it to a fresh folder.

Exists because doing this by hand goes wrong: a long URL gets mangled when pasted
into cmd.exe, the zip still has to be extracted, and the extracted folder carries
an awkward archive name (`<repo>-<branch>`). One command, nothing to type:

    python scripts\\fetch_update.py --dest D:\\flowtest
"""
from __future__ import annotations

import argparse
import io
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

DEFAULT_URL = (
    "https://github.com/farmanshahid471-code/flow-pipeline/archive/refs/heads/"
    "arena/4dd53cce-flow-pipeline.zip"
)


def download(url: str, timeout: float = 120.0) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "flow-pipeline-update"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def extract(blob: bytes, dest: Path, *, force: bool = False) -> Path:
    """Extract a GitHub archive into `dest`, without the `<repo>-<branch>/` wrapper."""
    if dest.exists() and any(dest.iterdir()):
        if not force:
            raise SystemExit(
                f"{dest} already exists and is not empty.\n"
                "  pick another --dest, or delete that folder first.\n"
                "  (refusing to overwrite: it may be the install you are running)"
            )
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        names = archive.namelist()
        prefix = names[0].split("/", 1)[0] + "/" if names else ""
        for member in names:
            if not member.startswith(prefix):
                continue
            relative = member[len(prefix):]
            if not relative:
                continue
            target = dest / relative
            if member.endswith("/"):
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, open(target, "wb") as sink:
                shutil.copyfileobj(source, sink)
    return dest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="download + extract the latest build")
    parser.add_argument("--dest", default="D:\\flowtest", help="folder to fill")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--force", action="store_true", help="replace a non-empty --dest")
    args = parser.parse_args(argv)

    dest = Path(args.dest)
    print(f"downloading {args.url}")
    try:
        blob = download(args.url)
    except Exception as exc:
        print(f"[ERROR] download failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        print("        check the internet connection and run this again.", file=sys.stderr)
        return 2
    print(f"  {len(blob) / 1024:.0f} KB")

    print(f"extracting to {dest}")
    extract(blob, dest, force=args.force)

    problems = []
    if not (dest / "run.bat").exists():
        problems.append("run.bat is missing - that archive was not the expected project")
    for expected in ("scripts/win/gflow.bat", "scripts/win/reset.bat"):
        if not (dest / expected).exists():
            problems.append(f"{expected} is missing - this looks like an older build")
    if problems:
        for line in problems:
            print(f"[warn] {line}", file=sys.stderr)
        return 1

    print()
    print(f"[ok] {dest} is ready ({len(list(dest.rglob('*')))} files).")
    print(f"     start it with:  {dest}\\run.bat")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
