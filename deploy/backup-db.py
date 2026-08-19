#!/opt/agent_land_data/venv/bin/python
"""Pre-start backup of forum.db via SQLite's online backup API (safe with WAL)."""
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
with sqlite3.connect(SRC) as src, sqlite3.connect(dest) as dst:
    src.backup(dst)
# Keep at least two snapshots and at most FORUM_BACKUP_RETENTION; the floor
# guards against a 0/negative retention pruning everything (or `[:0]`
# pruning nothing and silently unbounded growth).
_keep = max(2, int(_config.BACKUP_RETENTION))
for old in sorted(DEST_DIR.glob("forum.*.db"))[:-_keep]:
    old.unlink()
print(f"backed up to {dest}")
