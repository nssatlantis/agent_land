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

# Bootstrap deploy/ onto sys.path so _common resolves when the test harness
# runs this script from a temp directory (deploy/ is not the cwd).
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _common import _find_repo, _import_config, _quick_check_ok  # noqa: I001


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
