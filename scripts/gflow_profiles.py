"""Print the gflow profiles this pipeline's accounts point at, one per line.

Exists so `flow_login.bat` can default to the profile the dashboard actually
uses instead of asking for a name and quietly creating a *different* profile: the
browser then shows the same Google account (Chrome's cookies are shared), the
login looks successful, and the run keeps using the old, dead session.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flowpipe import db  # noqa: E402


def main() -> int:
    try:
        db.init_db()                  # creates the schema on a first run
        rows = db.list_accounts()
    except Exception as exc:          # no data dir yet, or a broken database
        print(f"# {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    seen = set()
    for row in rows:
        profile = (row.get("profile") or row.get("id") or "").strip()
        if profile and profile not in seen:
            seen.add(profile)
            print(profile)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
