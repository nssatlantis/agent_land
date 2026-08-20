#!/opt/agent_land_data/venv/bin/python
"""Pre-start backup of forum.db via SQLite's online backup API (safe with WAL).

Each fresh snapshot is verified with PRAGMA quick_check at write time: a
corrupt live DB or a torn write yields a corrupt snapshot, which is removed
and the backup fails loudly (exit 1), so a bad snapshot is caught on day one
rather than mid-crisis. update.sh warns-and-continues on a nonzero exit."""
import pathlib
import sqlite3
import sys
from datetime import datetime


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
    closed: a backup tool that guessed the DB path could snapshot the wrong
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


def _quick_check_ok(path: pathlib.Path) -> bool:
    """True when the database at `path` passes PRAGMA quick_check - the same
    check restore-db.py runs after a restore. A snapshot that fails it is
    corrupt and worthless, so it must not be kept as a backup."""
    try:
        conn = sqlite3.connect(path)
        try:
            return conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        finally:
            conn.close()
    except sqlite3.Error:
        return False


_config = _import_config(_find_repo())
SRC = pathlib.Path(_config.DB_PATH)
DEST_DIR = SRC.parent / "backups"
DEST_DIR.mkdir(parents=True, exist_ok=True)
if not SRC.exists():
    raise SystemExit("no database at " + str(SRC))
# Microsecond stamp so two backups in the same second cannot silently
# overwrite each other (a backup lost to a name collision is a backup that
# never existed); the -N suffix is a belt-and-braces last resort.
stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
dest = DEST_DIR / f"forum.{stamp}.db"
n = 1
while dest.exists():
    dest = DEST_DIR / f"forum.{stamp}-{n}.db"
    n += 1
src = sqlite3.connect(SRC)
dst = sqlite3.connect(dest)
try:
    src.backup(dst)
except sqlite3.Error as exc:
    # Close the connections explicitly before unlinking: the context-manager
    # close is not guaranteed to release the destination's OS handle on every
    # platform (Windows keeps it open through a failed backup), and an unlink
    # of a still-open file would silently keep the corrupt snapshot around.
    dst.close()
    src.close()
    # A corrupt live DB can fail the backup itself (unreadable pages) or only
    # surface in quick_check below. Either way the fresh snapshot is worthless
    # - remove it so it can never be mistaken for a real backup, then fail
    # loudly. update.sh treats a nonzero exit as a warning and continues.
    print(
        f"ERROR: backup failed ({exc}); the partial snapshot {dest.name} was "
        "removed. The live DB may be corrupt - investigate before relying on "
        "any restore.",
        file=sys.stderr,
    )
    dest.unlink(missing_ok=True)
    sys.exit(1)
dst.close()
src.close()

if not _quick_check_ok(dest):
    # A fresh snapshot can only fail quick_check if the live DB was corrupt
    # when the backup ran (online backup copies pages faithfully) or the write
    # was torn. The snapshot is corrupt - remove it, never keep it.
    print(
        f"ERROR: backup {dest.name} failed PRAGMA quick_check and was removed. "
        "The live DB at " + str(SRC) + " may be corrupt - investigate before "
        "relying on any restore.",
        file=sys.stderr,
    )
    dest.unlink(missing_ok=True)
    sys.exit(1)
# Keep at least two snapshots and at most FORUM_BACKUP_RETENTION; the floor
# guards against a 0/negative retention pruning everything (or `[:0]`
# pruning nothing and silently unbounded growth).
_keep = max(2, int(_config.BACKUP_RETENTION))
for old in sorted(DEST_DIR.glob("forum.*.db"))[:-_keep]:
    old.unlink()
print(f"backed up to {dest}")
