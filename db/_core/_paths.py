"""db._core._paths — path constants + boot-dir helper (split verbatim from db/_core.py)."""

from __future__ import annotations

from pathlib import Path

import config

DATA_DIR = config.DATA_DIR
DB_PATH = config.DB_PATH
SCHEMA_PATH = config.SCHEMA_PATH
REPO_DIR = config.REPO_DIR
REPLY_SEPARATOR = config.REPLY_SEPARATOR


def database_location_note() -> str:
    """One human-readable startup line: where the forum database lives. If the
    path resolves inside the repo, flags it - update.sh's `git clean -xdf`
    deletes gitignored files (forum.db is one), so such a db would be wiped on
    every deploy. Printed by server.py / viewer/ at boot."""
    note = f"forum database: {DB_PATH}"
    if Path(DB_PATH).resolve().is_relative_to(REPO_DIR):
        note += (
            f"  [WARNING: inside the repo {REPO_DIR}; git clean -xdf deletes "
            "gitignored files, so this db is wiped on every deploy]"
        )
    return note


def _ensure_db_dir() -> None:
    """sqlite3 won't create a missing directory - make sure it exists."""
    import db

    _path = getattr(db, "DB_PATH", DB_PATH)
    Path(_path).parent.mkdir(parents=True, exist_ok=True)
