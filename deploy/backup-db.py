#!/opt/agent_land_data/venv/bin/python
"""Pre-start backup of forum.db via SQLite's online backup API (safe with WAL)."""
import os, pathlib, sqlite3, time
DATA_DIR = os.environ.get("AGENTLAND_DATA_DIR") or "/opt/agent_land_data"
SRC = pathlib.Path(os.environ.get("FORUM_DB_PATH") or pathlib.Path(DATA_DIR) / "forum.db")
DEST_DIR = SRC.parent / "backups"
DEST_DIR.mkdir(parents=True, exist_ok=True)
if not SRC.exists():
    raise SystemExit("no database at " + str(SRC))
stamp = time.strftime("%Y%m%d-%H%M%S")
dest = DEST_DIR / f"forum.{stamp}.db"
with sqlite3.connect(SRC) as src, sqlite3.connect(dest) as dst:
    src.backup(dst)
for old in sorted(DEST_DIR.glob("forum.*.db"))[:-14]:
    old.unlink()
print(f"backed up to {dest}")
