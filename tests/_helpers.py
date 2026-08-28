"""Shared helpers for the tests/ package."""

from tests._setup import expect_error  # noqa: F401


def mail(token, **kw):
    """Fetch notifications for an agent (thin wrapper)."""
    from tests._setup import notifications

    return notifications.notifications(token, **kw)


def assert_upgrade_column(
    table: str, old_create_sql: str, new_col: str, seed=None, verify=None
):
    """House helper for upgrade-path tests: old-shape table -> init_db() -> assert migration fired.
    Creates a fresh DB file, installs the old table shape, optionally seeds via `seed(conn)`,
    runs `init_db()`, asserts `new_col in PRAGMA table_info(table)`, runs `verify(conn)` if given,
    then a second `init_db()` for idempotency. Restores `db.DB_PATH` afterwards."""
    import tempfile
    from pathlib import Path

    from tests._setup import db

    tmp = Path(tempfile.mkdtemp(prefix=f"agentland_test_{table}_upgrade_"))
    saved = db.DB_PATH
    try:
        db.DB_PATH = str(tmp / f"{table}_upgrade.db")
        db.init_db()
        with db._conn() as conn:
            conn.execute(f"DROP TABLE {table}")
            conn.execute(old_create_sql)
            if seed:
                seed(conn)
        db.init_db()
        with db._conn() as conn:
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            assert new_col in cols, f"init_db adds {new_col} to {table} on upgrade"
            if verify:
                verify(conn)
        db.init_db()
        with db._conn() as conn:
            cols2 = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            assert new_col in cols2, f"second init_db keeps {new_col} in {table}"
            if verify:
                verify(conn)
    finally:
        db.DB_PATH = saved
