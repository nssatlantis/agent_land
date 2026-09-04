"""Shared helpers for deploy/ scripts.

Extracted from backup-db.py, restore-db.py, check-db-boot.py to eliminate
duplication. Each script adds its own deploy/ dir to sys.path before importing
this module so the import works both from the repo checkout and from the
installed data dir (where deploy/ may not be on sys.path yet).
"""

import pathlib
import sqlite3
import sys


def _find_repo() -> pathlib.Path:
    """The git checkout, so config.py (which owns path resolution) can be
    imported from it. From the repo checkout this is deploy/; from the
    installed data dir (no schema.sql nearby) fall back to the default deploy
    layout."""
    here = pathlib.Path(__file__).resolve().parent
    for cand in (here, here.parent, here.parent.parent):
        if (cand / "schema.sql").exists() and (cand / "db" / "__init__.py").exists():
            return cand
    return pathlib.Path("/opt/agent_land")


def _import_config(repo_dir: pathlib.Path):
    """Import the app's config.py - the single source of path resolution. Fail
    closed: a deploy tool that guessed the DB path could snapshot/overwrite the
    wrong database, so a config.py that cannot be imported means 'refuse to run'
    (exit 2), never a guess."""
    sys.path.insert(0, str(repo_dir))
    try:
        import config
    except Exception as exc:
        print(
            f"ERROR: cannot import config.py ({exc}); refusing to run. "
            "Fix config.py before booting.",
            file=sys.stderr,
        )
        sys.exit(2)
    finally:
        sys.path.pop(0)
    return config


def _quick_check_ok(path: pathlib.Path) -> bool:
    """True when the database at `path` passes PRAGMA quick_check - the same
    check backup-db.py runs after a write. A snapshot that fails it is
    corrupt and worthless, so it must not be kept as a backup."""
    try:
        conn = sqlite3.connect(path)
        try:
            return conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        finally:
            conn.close()
    except sqlite3.Error:
        return False
