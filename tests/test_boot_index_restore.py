"""Index reconciliation after boot: schema-declared indexes are never lost.

Bugs #B39-#B43: legacy table rebuilds in the boot phases recreate only a
hand-copied subset of schema.sql's indexes, so an index added to schema.sql
later is silently dropped on upgraded databases.  `_restore_schema_indexes`
runs after every boot phase and reconciles the live connection against
schema.sql, the source of truth.
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_boot_index_restore_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import config, db  # noqa: E402

_REPORTS_INDEXES = {
    "idx_reports_status",
    "idx_reports_reporter",
    "idx_reports_target_status",
}


def _index_names(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA index_list({table})")}


def _jobs_worker_sql(conn):
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'idx_jobs_worker'"
    ).fetchone()
    return row[0]


def _legacy_reports_ddl():
    text = config.SCHEMA_PATH.read_text()
    start = text.index("CREATE TABLE IF NOT EXISTS reports")
    end = text.index(");\n", start) + 3
    ddl = text[start:end].replace("IF NOT EXISTS reports", "reports")
    return ddl.replace(", 'removed'", "")


def test_reports_rebuild_restores_declared_indexes():
    db.init_db()
    script = "PRAGMA foreign_keys = OFF;\nBEGIN;\n"
    script += "DROP TABLE IF EXISTS reports;\n"
    script += _legacy_reports_ddl()
    script += "\nCOMMIT;\n"
    with db._conn() as conn:
        conn.executescript(script)
        names = _index_names(conn, "reports")
    assert names == set(), "fixture must leave reports with no indexes"
    db.init_db()
    with db._conn() as conn:
        names = _index_names(conn, "reports")
    missing = _REPORTS_INDEXES - names
    assert not missing, f"reports rebuild lost declared indexes: {missing}"


def test_wrong_shape_jobs_worker_index_is_recreated():
    db.init_db()
    with db._conn() as conn:
        conn.execute("DROP INDEX IF EXISTS idx_jobs_worker")
        conn.execute("CREATE INDEX idx_jobs_worker ON jobs(worker_agent_id)")
        sql = _jobs_worker_sql(conn)
    assert "WHERE" not in sql.upper(), "fixture must arm a plain index"
    db.init_db()
    with db._conn() as conn:
        sql = _jobs_worker_sql(conn)
    assert "WHERE" in sql.upper(), "reconciler must restore a partial index"


def main():
    test_reports_rebuild_restores_declared_indexes()
    test_wrong_shape_jobs_worker_index_is_recreated()
    print("test_boot_index_restore: all assertions passed")
    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
