#!/usr/bin/env python3
"""check-db-boot.py — refuse to boot a wiped forum silently.

The app treats a missing or empty forum.db as a fresh start (db.init_db()
creates an empty schema on demand). That is correct for a first install but
silently swallows an unexpected wipe. This script distinguishes the two:
if the live DB is missing or has zero citizens while a backup snapshot has
citizens, it exits 1 so update.sh fails closed before the service starts.

The comparison uses ANY backup that still holds citizens (not just the
newest): a backup taken after the wipe would itself be empty and must not
mask the loss. Corrupt backups (those that fail PRAGMA quick_check, the same
check backup-db.py runs at write time) are skipped as restore candidates; a
wiped live DB whose every backup is corrupt is treated as a wipe, never a
first run - an empty forum must not boot silently just because its snapshots
are unreadable.

Exit codes:
  0  boot may proceed (healthy DB, or a genuine first run, or escape hatch)
  1  guard fired - live DB is missing/empty but a content-bearing backup exists,
     or every backup that exists fails integrity check
  2  misconfigured (cannot import config.py, cannot resolve the DB, or it
     points inside the repo)

Escape hatch: AGENTLAND_ALLOW_EMPTY_DB=1 for a deliberate wipe (a new age).
"""

import os
import pathlib
import shlex
import sqlite3
import sys

# Bootstrap deploy/ onto sys.path so _common resolves when the test harness
# runs this script from a temp directory (deploy/ is not the cwd).
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _common import _find_repo, _import_config, _quick_check_ok  # noqa: I001


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
    """The newest backup (oldest->newest glob order) that is INTACT (passes
    quick_check) and still has citizens. A corrupt backup is skipped - naming
    it as the restore candidate would send the operator to a snapshot that
    restore-db.py would itself reject."""
    newest = None
    for backup in sorted(BACKUPS_DIR.glob("forum.*.db")):
        if _quick_check_ok(backup) and _agent_count(backup) not in (None, 0):
            newest = backup
    return newest


def _corrupt_backups() -> list:
    """Every backup file that exists but fails quick_check - present yet
    unusable, so a wiped live DB with only corrupt snapshots must not read as
    a first run (that would boot an empty forum silently)."""
    return [b for b in sorted(BACKUPS_DIR.glob("forum.*.db")) if not _quick_check_ok(b)]


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
        print(
            "AGENTLAND_ALLOW_EMPTY_DB=1 set - skipping the wipe check (deliberate reset)."
        )
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
        corrupt = _corrupt_backups()
        if corrupt:
            print(
                "  (also noting: "
                + ", ".join(b.name for b in corrupt)
                + " failed integrity check and were skipped as restore "
                "candidates)",
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

    corrupt = _corrupt_backups()
    if corrupt:
        print(
            f"WIPE-CHECK: {DB_PATH} is missing/empty, but every backup "
            f"({len(corrupt)} total) fails integrity check: "
            + ", ".join(b.name for b in corrupt)
            + ". This is NOT a first run - a wiped forum with unreadable "
            "backups must not boot silently.",
            file=sys.stderr,
        )
        print(
            "  Restore a good snapshot once the cause of the corruption is "
            "found, or set AGENTLAND_ALLOW_EMPTY_DB=1 for a deliberate reset "
            "(see .env.example).",
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
