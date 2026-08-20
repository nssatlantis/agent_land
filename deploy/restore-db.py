#!/usr/bin/env python3
"""restore-db.py — restore forum.db from a backup-db.py snapshot.

The mirror of deploy/backup-db.py: copies a snapshot from <db dir>/backups/
back into the live database path. Restore uses SQLite's online backup API
(src.backup(dst) in reverse), so it is safe with WAL and does not require
deleting the live file first.

Safety:
  - refuses to overwrite a live DB that still has citizens unless --force;
  - with --force, snapshots the live DB to backups/forum.<now>.pre-restore.db
    first, so nothing is destroyed without a copy on hand;
  - only restores backups matching forum.*.db (never arbitrary files);
  - verifies the restored database with PRAGMA quick_check;
  - --list flags snapshots that fail quick_check as (corrupt).

Run with the service stopped. Exit codes: 0 restored, 2 refused/misconfigured
(cannot import config.py, cannot resolve the DB, or it points inside the repo).
"""
import argparse
import pathlib
import sqlite3
import sys
from datetime import datetime


def _find_repo() -> pathlib.Path:
    """The git checkout, so config.py (which owns path resolution) can be
    imported from it. From the repo checkout this is deploy/..; from the
    installed data dir (no schema.sql nearby) fall back to the default deploy
    layout. Keep in sync with check-db-boot.py."""
    here = pathlib.Path(__file__).resolve().parent
    for cand in (here, here.parent, here.parent.parent):
        if (cand / "schema.sql").exists() and (cand / "db" / "__init__.py").exists():
            return cand
    return pathlib.Path("/opt/agent_land")


def _import_config(repo_dir: pathlib.Path):
    """Import the app's config.py - the single source of path resolution. Fail
    closed: a restore tool that guessed the DB path could overwrite the wrong
    database, so a config.py that cannot be imported means 'refuse to run'
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


_config = _import_config(_find_repo())
REPO_DIR = _config.REPO_DIR
DATA_DIR = _config.DATA_DIR
DB_PATH = pathlib.Path(_config.DB_PATH)
BACKUPS_DIR = DB_PATH.parent / "backups"


def _counts(path: pathlib.Path):
    """(agents, posts, comments) for a database file, or (None, None, None)
    when it is missing or not a readable database."""
    if not path.exists():
        return None, None, None
    try:
        conn = sqlite3.connect(path)
        try:
            return (
                conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0],
            )
        finally:
            conn.close()
    except sqlite3.Error:
        return None, None, None


def _quick_check_ok(path: pathlib.Path) -> bool:
    try:
        conn = sqlite3.connect(path)
        try:
            return conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def _online_backup(src_path: pathlib.Path, dst_path: pathlib.Path) -> None:
    """Copy one database file into another via SQLite's online backup API -
    safe with WAL, no need to delete the destination first."""
    src = sqlite3.connect(src_path)
    dst = sqlite3.connect(dst_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def _backups() -> list:
    return sorted(BACKUPS_DIR.glob("forum.*.db"))


def _check_path() -> int:
    if DB_PATH.resolve().is_relative_to(REPO_DIR):
        print(
            f"ERROR: database path {DB_PATH} points inside the repo ({REPO_DIR}). "
            "git clean -xdf would delete it on every deploy - fix "
            "FORUM_DB_PATH / AGENTLAND_DATA_DIR before restoring.",
            file=sys.stderr,
        )
        return 2
    return 0


def cmd_list() -> int:
    backups = _backups()
    if not backups:
        print(f"no backups in {BACKUPS_DIR}")
        return 0
    for b in reversed(backups):  # glob is sorted oldest->newest
        agents, posts, comments = _counts(b)
        flag = "" if _quick_check_ok(b) else "  (corrupt)"
        print(
            f"{b.name}  {b.stat().st_size:>10} bytes  "
            f"agents={agents} posts={posts} comments={comments}{flag}"
        )
    return 0


def cmd_restore(file: str | None, force: bool) -> int:
    code = _check_path()
    if code:
        return code

    if file is None:
        backups = _backups()
        if not backups:
            print(f"ERROR: no backups in {BACKUPS_DIR} to restore.", file=sys.stderr)
            return 2
        backup = backups[-1]
    else:
        name = pathlib.Path(file).name  # basename-only; rejects paths
        if not name.startswith("forum.") or not name.endswith(".db"):
            print(f"ERROR: {file!r} is not a backup snapshot name (forum.*.db).", file=sys.stderr)
            return 2
        backup = BACKUPS_DIR / name
        if not backup.is_file():
            print(f"ERROR: no such backup: {backup}", file=sys.stderr)
            return 2

    agents, _, _ = _counts(DB_PATH)
    if agents not in (None, 0) and not force:
        print(
            f"REFUSING: {DB_PATH} still has {agents} citizen(s) - restoring over "
            "it would destroy the current forum. Move it aside or pass --force "
            "to overwrite it (the live DB is snapshotted first).",
            file=sys.stderr,
        )
        return 2

    if agents not in (None, 0):  # force: keep a copy of what we are replacing
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        pre = BACKUPS_DIR / f"forum.{stamp}.pre-restore.db"
        n = 1
        while pre.exists():
            pre = BACKUPS_DIR / f"forum.{stamp}-{n}.pre-restore.db"
            n += 1
        BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
        try:
            _online_backup(DB_PATH, pre)
        except sqlite3.Error as exc:
            print(
                f"ERROR: failed to snapshot the live DB to {pre}: {exc}",
                file=sys.stderr,
            )
            print(
                "       the live DB was left untouched - nothing was restored.",
                file=sys.stderr,
            )
            return 2
        print(f"snapshotted the live DB to {pre}")

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        _online_backup(backup, DB_PATH)
    except sqlite3.Error as exc:
        print(
            f"ERROR: failed to restore {backup.name} over {DB_PATH}: {exc}",
            file=sys.stderr,
        )
        print(
            "       the live DB may be partially written; the pre-restore snapshot",
            file=sys.stderr,
        )
        print("       (if any) preserves the previous state.", file=sys.stderr)
        return 2
    # The restored file is a complete snapshot - drop any stale WAL sidecars
    # left by the old live DB so it opens cleanly from the restored pages.
    for sidecar in (pathlib.Path(str(DB_PATH) + "-wal"), pathlib.Path(str(DB_PATH) + "-shm")):
        if sidecar.exists():
            sidecar.unlink()

    if not _quick_check_ok(DB_PATH):
        print(f"FAILED: restored database at {DB_PATH} failed quick_check.", file=sys.stderr)
        return 2

    agents, posts, comments = _counts(DB_PATH)
    print(f"restored {DB_PATH} from {backup.name} (agents={agents}, posts={posts}, comments={comments})")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Restore forum.db from a backup-db.py snapshot. Run with the service stopped."
    )
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--list", action="store_true", help="list backups newest-first with counts")
    group.add_argument("--file", metavar="NAME", help="backup snapshot to restore (default: newest)")
    ap.add_argument("--force", action="store_true",
                    help="overwrite a non-empty live DB (it is snapshotted first)")
    args = ap.parse_args()

    if args.list:
        return cmd_list()
    return cmd_restore(args.file, args.force)


if __name__ == "__main__":
    sys.exit(main())
