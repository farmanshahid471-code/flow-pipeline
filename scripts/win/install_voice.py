#!/usr/bin/env python3
r"""Install the narration stack into the tool root: VoiceStudio, whisperX, uv.

Driven by ``setup.bat`` (step 7) and safe to re-run — every step is skipped when
it is already done. Standard library only, so it works before any package is
installed.

    python scripts/win/install_voice.py --tools D:\Tools
    python scripts/win/install_voice.py --tools D:\Tools --skip-whisperx
    python scripts/win/install_voice.py --tools D:\Tools --plan     # print, run nothing

What lands where (nothing on C:):

    D:\Tools\voicestudio\            git clone of debpalash/VoiceStudio
    D:\Tools\voicestudio\.venv\      its own virtualenv (uv sync)
    D:\Tools\voicestudio-data\       its database and engines
    D:\Tools\hf-cache\               model weights (VoiceStudio + whisperX)
    D:\Tools\torch-cache\            torch checkpoints
    D:\Tools\uv-cache\               uv's download cache

The pieces are independent, so a failure in one is reported and the rest carry
on: VoiceStudio without whisperX still gives you audio (this project falls back
to proportional word timing), and whisperX without VoiceStudio still aligns any
voiceover you upload by hand.

Disk: VoiceStudio's venv is ~1.6 GB and its model ~2.4 GB on first synthesis;
whisperX pulls cpu torch (~250 MB) plus a small ASR model.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path

VOICESTUDIO_REPO = "https://github.com/debpalash/VoiceStudio.git"
DEFAULT_TOOLS = "D:/Tools" if os.name == "nt" and Path("D:/").exists() else ""


def log(message: str) -> None:
    print(f"[voice] {message}", flush=True)


def which(name: str) -> str | None:
    return shutil.which(name)


def run(args: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None,
        timeout: int = 3600, check: bool = False) -> subprocess.CompletedProcess:
    log("$ " + " ".join(args))
    proc = subprocess.run(args, cwd=str(cwd) if cwd else None, env=env,
                          capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0 and check:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-8:]
        raise RuntimeError(f"{args[0]} failed ({proc.returncode}): " + " / ".join(tail))
    return proc


# --------------------------------------------------------------------- plan ---
def layout(tools: Path) -> dict[str, Path]:
    return {
        "root": tools,
        "voicestudio": tools / "voicestudio",
        "data": tools / "voicestudio-data",
        "hf": tools / "hf-cache",
        "torch": tools / "torch-cache",
        "uv_cache": tools / "uv-cache",
        "python": tools / "Python311" / "python.exe" if os.name == "nt" else tools / "Python311" / "bin" / "python3",
    }


def environment(tools: Path, python: Path) -> dict[str, str]:
    """Everything VoiceStudio and whisperX need to stay on the D: drive."""
    env = dict(os.environ)
    env.update({
        "HF_HOME": str(tools / "hf-cache"),
        "HF_HUB_CACHE": str(tools / "hf-cache"),
        "TORCH_HOME": str(tools / "torch-cache"),
        "UV_CACHE_DIR": str(tools / "uv-cache"),
        "OMNIVOICE_DATA_DIR": str(tools / "voicestudio-data"),
        "OMNIVOICE_CACHE_DIR": str(tools / "hf-cache"),
        "PIP_CACHE_DIR": str(tools / "pip-cache"),
        "TMP": str(tools / "tmp"),
        "TEMP": str(tools / "tmp"),
        "PYTHONUTF8": "1",
    })
    if python.exists():
        env["PATH"] = str(python.parent) + os.pathsep + env.get("PATH", "")
    return env


# ---------------------------------------------------------------- voice studio
def ensure_uv(plan: dict[str, Path], env: dict[str, str],
              python: Path | None) -> tuple[str | None, str]:
    """uv runs the VoiceStudio backend. Prefer a native uv, else pip install it."""
    existing = which("uv")
    if existing:
        return existing, f"uv already on PATH ({existing})"
    if python is None or not python.exists():
        return None, "uv not found and no portable Python to install it with"
    if run([str(python), "-c", "import uv"]).returncode == 0:
        return f'"{python}" -m uv', "uv module already installed"
    proc = run([str(python), "-m", "pip", "install", "--no-warn-script-location", "uv"],
               env=env)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-4:]
        return None, "pip install uv failed: " + " / ".join(tail)
    return f'"{python}" -m uv', "uv installed with pip"


def install_voicestudio(plan: dict[str, Path], env: dict[str, str],
                        python: Path | None, *, plan_only: bool) -> tuple[bool, str]:
    target = plan["voicestudio"]
    if (target / "backend" / "main.py").exists():
        return True, f"already cloned at {target}"
    git = which("git")
    if git is None:
        return False, ("git is not installed. Install Git for Windows, or clone "
                       f"{VOICESTUDIO_REPO} into {target} by hand and re-run setup.bat")
    if plan_only:
        log(f"would clone {VOICESTUDIO_REPO} -> {target}")
        return True, "planned"
    target.parent.mkdir(parents=True, exist_ok=True)
    proc = run([git, "clone", "--depth", "1", VOICESTUDIO_REPO, str(target)], env=env)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-4:]
        return False, "git clone failed: " + " / ".join(tail)

    uv, note = ensure_uv(plan, env, python)
    log(note)
    if uv is None:
        return False, f"cloned, but could not run uv to install dependencies ({note})"
    args = uv.split() + ["sync"] if " " in uv else [uv, "sync"]
    proc = subprocess.run(args, cwd=str(target), env=env, capture_output=True, text=True,
                          timeout=7200)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-8:]
        return False, "uv sync failed: " + " / ".join(tail)
    return True, f"cloned and synced at {target}"


def install_whisperx(env: dict[str, str], python: Path | None, *,
                     plan_only: bool) -> tuple[bool, str]:
    if python is None or not python.exists():
        return False, "no portable Python found - whisperX is skipped"
    if run([str(python), "-c", "import whisperx"]).returncode == 0:
        return True, "whisperX already installed"
    if plan_only:
        log(f'would run "{python}" -m pip install whisperx (cpu torch, ~1 GB)')
        return True, "planned"
    proc = run([str(python), "-m", "pip", "install", "--no-warn-script-location",
                "--prefer-binary", "whisperx"], env=env, timeout=7200)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-6:]
        return False, ("pip install whisperx failed (this project falls back to "
                       "proportional timing): " + " / ".join(tail))
    return True, "whisperX installed"


# --------------------------------------------------------------------- main ---
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="install the local narration stack")
    parser.add_argument("--tools", default=os.environ.get("FLOWPIPE_TOOLS_DIR") or DEFAULT_TOOLS,
                        help="tool root (default D:\\Tools on Windows)")
    parser.add_argument("--skip-voicestudio", action="store_true",
                        help="do not clone VoiceStudio")
    parser.add_argument("--skip-whisperx", action="store_true",
                        help="do not install whisperX (proportional timing still works)")
    parser.add_argument("--plan", action="store_true", help="print what would happen")
    args = parser.parse_args(argv)

    if not args.tools:
        log("no tool root: pass --tools or set FLOWPIPE_TOOLS_DIR")
        return 2
    tools = Path(args.tools).expanduser().resolve()
    plan = layout(tools)
    env = environment(tools, plan["python"])
    for key in ("hf", "torch", "uv_cache", "data"):
        plan[key].mkdir(parents=True, exist_ok=True)
    (tools / "tmp").mkdir(parents=True, exist_ok=True)

    log(f"tool root        : {tools}")
    log(f"voice studio     : {plan['voicestudio']}")
    log(f"model cache      : {plan['hf']}")
    log(f"data             : {plan['data']}")

    results: list[tuple[str, bool, str]] = []
    python = plan["python"] if plan["python"].exists() else None
    if python is None:
        log("portable Python not found - whisperX will be skipped")
    if not args.skip_voicestudio:
        results.append(("VoiceStudio", *install_voicestudio(plan, env, python,
                                                             plan_only=args.plan)))
    if not args.skip_whisperx:
        results.append(("whisperX", *install_whisperx(env, python, plan_only=args.plan)))

    failures = [name for name, ok, _ in results if not ok]
    print("", flush=True)
    for name, ok, detail in results:
        print(f"  [{'ok' if ok else 'fail'}] {name}: {detail}", flush=True)
    if failures:
        log(f"{len(failures)} component(s) incomplete: {', '.join(failures)}")
        log("the pipeline still runs; narration falls back to proportional timings")
        return 1
    if args.plan:
        log("plan only - nothing was changed")
    else:
        log("narration stack ready. Start the voice server with scripts\\voice\\start-voicestudio.bat")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
