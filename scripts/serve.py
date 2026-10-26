#!/usr/bin/env python3
"""Start the dashboard without the project having to be on ``PYTHONPATH``.

    python scripts/serve.py --host 127.0.0.1 --port 8000 --open

``run.bat`` calls this instead of ``python -m flowpipe.main``. That matters on
Windows: ``setup.bat`` installs the *embeddable* Python, which ships a
``python311._pth`` file. That file switches the interpreter to isolated mode,
where the current directory and ``PYTHONPATH`` are both ignored — so
``-m flowpipe.main`` cannot find the package even when run from the project
folder. Adding the repository root here is all that is needed, and it keeps
working wherever the project folder is moved to.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def _explain(exc: ImportError) -> None:
    """A missing package is an install problem - say so instead of a traceback."""
    missing = getattr(exc, "name", None) or str(exc)
    print("=" * 62, flush=True)
    print(" flow-pipeline could not start", flush=True)
    print("=" * 62, flush=True)
    print(f" project : {REPO}", flush=True)
    print(f" python  : {sys.executable}", flush=True)
    print(f" missing : {missing}", flush=True)
    if missing.split(".")[0] == "flowpipe":
        print("\n The project folder could not be imported. Run setup.bat", flush=True)
        print(" again from this folder, and check that flowpipe\\__init__.py exists.", flush=True)
    else:
        requirements = REPO / "requirements.txt"
        print("\n A package is missing. Install the requirements with:", flush=True)
        print(f'   "{sys.executable}" -m pip install -r "{requirements}"', flush=True)
        print(" or just run setup.bat again - it skips what is already done.", flush=True)
    print("=" * 62, flush=True)


def main() -> None:
    try:
        # imported up front so a missing dependency is reported clearly
        import fastapi  # noqa: F401
        import multipart  # noqa: F401
        import uvicorn  # noqa: F401

        from flowpipe.main import main as run
    except ImportError as exc:
        _explain(exc)
        raise SystemExit(1) from exc

    try:
        run()
    except ImportError as exc:  # a driver-only dependency, e.g. anthropic
        _explain(exc)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
