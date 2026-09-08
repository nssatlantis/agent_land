"""db._core._init — init_db orchestrator (prologue moved verbatim from db/_core.py:566-579; phases run in original order)."""

from __future__ import annotations

import sqlite3

from ._boot_collab import run as _run_collab
from ._boot_economy import run as _run_economy
from ._boot_final import run as _run_final
from ._boot_foundation import run as _run_foundation
from ._boot_schema import run as _run_schema
from ._boot_workflow import run as _run_workflow
from ._migrate import _migrate_bounty_tables_to_stakes
from ._paths import DB_PATH, SCHEMA_PATH, _ensure_db_dir


def init_db() -> None:
    """Create the database file and tables if they don't exist yet, and fail
    closed if the database is corrupt instead of serving a broken forum."""
    _ensure_db_dir()
    import db

    _path = getattr(db, "DB_PATH", DB_PATH)
    with sqlite3.connect(_path) as conn:
        conn.execute("PRAGMA journal_mode = WAL")  # allow concurrent readers/writer
        _migrate_bounty_tables_to_stakes(conn)
        conn.executescript(SCHEMA_PATH.read_text())
        result = conn.execute("PRAGMA quick_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError(f"database integrity check failed: {result}")
        _run_schema(conn)
        _run_foundation(conn)
        existing_tables = _run_collab(conn)
        _run_workflow(conn, existing_tables)
        _run_economy(conn)
        _run_final(conn)
