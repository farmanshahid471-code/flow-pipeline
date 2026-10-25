"""Entry point: `flowpipe` or `python -m flowpipe.main`."""
from __future__ import annotations

import argparse
import os

from .config import SETTINGS


def main() -> None:
    parser = argparse.ArgumentParser(description="Self-hosted Google Flow video pipeline")
    parser.add_argument("--host", default=os.environ.get("FLOWPIPE_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("FLOWPIPE_PORT", "8000")))
    parser.add_argument("--reload", action="store_true", help="dev auto-reload")
    parser.add_argument("--data-dir", help="override the data directory")
    args = parser.parse_args()

    if args.data_dir:
        SETTINGS.data_dir = SETTINGS.data_dir.__class__(args.data_dir).resolve()
        SETTINGS.ensure_dirs()

    import uvicorn

    uvicorn.run("flowpipe.api:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
