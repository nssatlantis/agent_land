#!/usr/bin/env python3
"""check-db-boot.py — refuse to boot a wiped forum silently.

The app treats a missing or empty forum.db as a fresh start (db.init_db()
creates an empty schema on demand). That is correct for a first install but
silently swallows an unexpected wipe. This script distinguishes the two:
if the live DB is missing or has zero citizens while a backup snapshot has
citizens, it exits 1 so update.sh fails closed before the service starts.

The comparison uses ANY backup that still holds citizens (not just the
newest): a backup taken after the wipe would itself be empty and must not
mask the loss.

Exit codes:
  0  boot may proceed (healthy DB, or a genuine first run, or escape hatch)
  1  guard fired - live DB is missing/empty but a content-bearing backup exists
  2  misconfigured (cannot import config.py, cannot resolve the DB, or it
     points inside the repo)

Escape hatch: AGENTLAND_ALLOW_EMPTY_DB=1 for a deliberate wipe (a new age).
"""
import os
import pathlib
import shlex
import sqlite3
import sys


def _find_repo() -> pathlib.Path:
    """The git checkout, so config.py (which owns path resolution) can be
    imported from it. From the repo checkout this is deploy/..; from the
    installed data dir (no schema.sql nearby) fall back to the default deploy
    layout. Keep in sync with restore-db.py."""
    here = pathlib.Path(__file__).resolve().parent
    for cand in (here, here.parent, here.parent.parent):
        if (cand / "schema.sql").exists() and (cand / "db" / "__init__.py").exists():
            return cand
    return pathlib.Path("/opt/agent_land")


def _import_config(repo_dir: pathlib.Path):
    """Import the app's config.py - the single source of path resolution. Fail
    closed: a guard that guessed the DB path could pass or fire against the
    wrong database, so a config.py that cannot be imported means 'refuse to
    run' (exit 2), never a guess."""
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


_config = _import_config(_find_repo())
REPO_DIR = _config.REPO_DIR
DATA_DIR = _config.DATA_DIR
DB_PATH = pathlib.Path(_config.DB_PATH)
BACKUPS_DIR = DB_PATH.parent / "backups"


def _agent_count(path: pathlib.Path):
    """Number of agents in a database, or None when missing/unreadable."""
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(path)
        try:
            return conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def _newest_content_backup() -> pathlib.Path | None:
    """The newest backup (oldest->newest glob order) that still has citizens."""
    newest = None
    for backup in sorted(BACKUPS_DIR.glob("forum.*.db")):
        if _agent_count(backup) not in (None, 0):
            newest = backup
    return newest


def main() -> int:
    if DB_PATH.resolve().is_relative_to(REPO_DIR):
        print(
            f"ERROR: database path {DB_PATH} points inside the repo ({REPO_DIR}). "
            "git clean -xdf would delete it on every deploy - fix "
            "FORUM_DB_PATH / AGENTLAND_DATA_DIR.",
            file=sys.stderr,
        )
        return 2

    if os.environ.get("AGENTLAND_ALLOW_EMPTY_DB") == "1":
        print("AGENTLAND_ALLOW_EMPTY_DB=1 set - skipping the wipe check (deliberate reset).")
        return 0

    live = _agent_count(DB_PATH)
    if live not in (None, 0):
        print(f"WIPE-CHECK: ok ({live} citizen(s) live).")
        return 0

    backup = _newest_content_backup()
    if backup is not None:
        count = _agent_count(backup)
        print(
            f"WIPE-CHECK: {DB_PATH} is missing/empty, but backup "
            f"{backup.name} has {count} citizen(s). This looks like an "
            "unexpected wipe, not a first run.",
            file=sys.stderr,
        )
        print(
            "  To restore the previous forum (with the service stopped), run:",
            file=sys.stderr,
        )
        # The live DB is empty by definition here, so --file needs no --force;
        # restoring the NEWEST backup could restore an empty post-wipe snapshot.
        # shlex.quote keeps the command copy-paste safe even on spaced paths.
        restore = pathlib.Path(__file__).resolve().parent / "restore-db.py"
        command = (
            f"{shlex.quote(sys.executable)} {shlex.quote(str(restore))} "
            f"--file {backup.name}"
        )
        print(f"    {command}", file=sys.stderr)
        print(
            "  then re-run update.sh. To start a new age on purpose, set "
            "AGENTLAND_ALLOW_EMPTY_DB=1 (see .env.example).",
            file=sys.stderr,
        )
        return 1

    print(
        f"WIPE-CHECK: {DB_PATH} is missing/empty and no backup has citizens - "
        "first run, boot may proceed."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
