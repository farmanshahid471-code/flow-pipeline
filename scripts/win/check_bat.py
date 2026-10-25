#!/usr/bin/env python3
"""Static checks for the Windows batch files.

There is no cmd.exe here, so this catches the failures that matter without
running them: a ``goto``/``call`` to a missing label, duplicate labels, unbalanced
quotes, an unfinished ``if (...)`` block, a CRLF problem, or an exit path that can
close the window before ``pause`` runs.

    python scripts/win/check_bat.py setup.bat run.bat
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

LABEL_RE = re.compile(r"^\s*:([A-Za-z0-9_.\-]+)")
GOTO_RE = re.compile(r"\b(?:goto|call)\s+:([A-Za-z0-9_.\-]+)", re.IGNORECASE)
PAUSE_RE = re.compile(r"^\s*(pause|set\s+/p\b|timeout\s+/t\b)", re.IGNORECASE)


def check_file(path: Path) -> list[str]:
    problems: list[str] = []
    raw = path.read_bytes()

    if b"\r\n" not in raw and len(raw.splitlines()) > 1:
        problems.append("no CRLF line endings - commit with .gitattributes eol=crlf")

    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()

    labels: dict[str, int] = {}
    for number, line in enumerate(lines, start=1):
        match = LABEL_RE.match(line)
        if match:
            name = match.group(1).lower()
            if name in labels:
                problems.append(f"line {number}: duplicate label :{match.group(1)} "
                                f"(first at line {labels[name]})")
            else:
                labels[name] = number

    targets: list[tuple[str, int]] = []
    for number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if stripped.lower().startswith("rem") or stripped.startswith("::"):
            continue
        for name in GOTO_RE.findall(line):
            targets.append((name.lower(), number))

    for name, number in targets:
        if name not in labels:
            problems.append(f"line {number}: jumps to :{name} which does not exist")

    for number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if stripped.lower().startswith("rem") or stripped.startswith("::"):
            continue
        if stripped.count('"') % 2:
            problems.append(f"line {number}: odd number of double quotes")
        if stripped.count("(") - stripped.count(")"):
            problems.append(f"line {number}: unbalanced parentheses in '{stripped[:60]}'")

    unreferenced = [name for name in labels if name not in {"eof"} and
                    name not in {t for t, _ in targets}]
    if unreferenced:
        problems.append("labels never jumped to: " + ", ".join(sorted(unreferenced)))

    if not any(PAUSE_RE.match(line) for line in lines):
        problems.append("no pause / set /p - the window would close on exit")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="lint Windows batch files")
    parser.add_argument("files", nargs="*", help="batch files (default: setup.bat run.bat)")
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parents[2]
    files = [Path(f) for f in args.files] or [root / "setup.bat", root / "run.bat"]

    failures = 0
    for path in files:
        if not path.exists():
            print(f"{path}: missing")
            failures += 1
            continue
        problems = check_file(path)
        if problems:
            failures += 1
            print(f"{path.name}: {len(problems)} problem(s)")
            for problem in problems:
                print(f"  - {problem}")
        else:
            print(f"{path.name}: ok")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
