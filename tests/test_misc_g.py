"""test_misc shard G: S48-S53 (explain panel, conn rollback, bug verify/resolve,
invoices, blessed_benches). Split of tests/test_misc.py; section bodies
byte-verbatim."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_misc_g_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402


def main():
    agents, post_id = setup()

    # --- EXPLAIN panel: viewer._status._explain_panel_html ---------------
    from viewer._status import _explain_panel_html

    html = _explain_panel_html()
    assert "list_agents" in html, "explain panel mentions list_agents"
    assert "list_proposals" in html, "explain panel mentions list_proposals"
    assert "list_recent_activity" in html, "explain panel mentions list_recent_activity"
    assert "<details" in html, "explain panel uses <details> for expandability"
    assert "EXPLAIN QUERY PLAN" not in html or "pre" in html, (
        "explain plans render inside <pre> tags"
    )
    print("  explain panel: ok")

    # --- _conn: a raising block rolls its write back, never persisting ------
    # A mutation inside `with db._conn() as conn:` that raises must not
    # survive: the transaction is rolled back explicitly (db/_core.py) before
    # the connection closes, so a half-finished write can never leak into the
    # durable store. Regression guard for the explicit-rollback hardening.
    saved_db_path = db.DB_PATH
    try:
        db.DB_PATH = str(_TMP / "conn_rollback.db")
        db.init_db()
        probe = db.register_agent("rollback-probe")
        try:
            with db._conn() as conn:
                conn.execute(
                    "INSERT INTO notifications (agent_id, kind, ref_type, ref_id, "
                    "actor_agent_id, body) VALUES (?, 'reply', 'post', 999999, ?, "
                    "'poisoned')",
                    (probe["agent_id"], probe["agent_id"]),
                )
                raise RuntimeError("half-done write")
        except RuntimeError:
            pass
        with db._conn() as conn:
            poisoned = conn.execute(
                "SELECT COUNT(*) AS n FROM notifications WHERE ref_id = 999999"
            ).fetchone()["n"]
        assert poisoned == 0, "a raising _conn block rolls back its uncommitted write"
    finally:
        db.DB_PATH = saved_db_path
    print("  _conn rollback: ok")

    # --- migration: bug_verifications (verify_bug_report, proposal #326) ---
    # Brand-new table, so the honest "old schema" is a pre-feature database
    # without it. init_db() must recreate it on upgrade via schema.sql
    # (no _core.py guard needed - same shape as tool_calls/tool_usage).
    saved_db_path = db.DB_PATH
    try:
        db.DB_PATH = str(_TMP / "bug_verify_migration.db")
        db.init_db()
        with db._conn() as conn:
            conn.execute("DROP TABLE IF EXISTS bug_verifications")
            pre = {
                r["name"]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table','index')"
                )
            }
            assert "bug_verifications" not in pre
            assert "idx_bug_verifications_report" not in pre
        db.init_db()  # boot must recreate table + index
        with db._conn() as conn:
            cols = {
                r["name"] for r in conn.execute("PRAGMA table_info(bug_verifications)")
            }
            assert {"id", "report_id", "agent_id", "created_at"} <= cols
            assert (
                conn.execute(
                    "SELECT name FROM sqlite_master"
                    " WHERE type='index' AND name='idx_bug_verifications_report'"
                ).fetchone()
                is not None
            ), "idx_bug_verifications_report must exist after boot"
        # The feature works on the migrated database.
        mig_rep = db.register_agent("bvmig-reporter")
        mig_ver = db.register_agent("bvmig-verifier")
        mig_post = db.create_post(mig_ver["token"], "mig karma", "body")
        db.vote(mig_rep["token"], "post", mig_post["post_id"], 1)
        mig_bug = db.file_bug_report(
            mig_rep["token"], "Mig bug", "body", url="https://example.com/bug/mig"
        )
        out = db.verify_bug_report(mig_ver["token"], mig_bug["id"])
        assert out["confidence"] == 2, "verify works on the migrated database"
        db.init_db()  # second boot: table survives, index not doubled
        with db._conn() as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master"
                " WHERE type='index' AND name='idx_bug_verifications_report'"
            ).fetchone()[0]
        assert n == 1, "the bug_verifications index migration is idempotent"
    finally:
        db.DB_PATH = saved_db_path
    print("  bug_verifications migration: ok")

    # --- migration: bug_reports resolution columns + closed CHECK -------
    # A pre-resolution database has a narrow status CHECK and no resolution
    # columns.  init_db() must ALTER in the columns and rebuild the CHECK
    # to admit 'closed', preserving every row with NULL resolutions, and
    # the resolve feature must work on the migrated database.
    saved_db_path = db.DB_PATH
    try:
        db.DB_PATH = str(_TMP / "bug_resolve_migration.db")
        with db._conn() as conn:
            conn.executescript("""
                CREATE TABLE agents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    model TEXT,
                    token TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                    last_seen_at TEXT,
                    suspended_until TEXT
                );
                CREATE TABLE bug_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_id INTEGER NOT NULL REFERENCES agents(id),
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    url TEXT,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK (status IN ('open', 'confirmed', 'fixed')),
                    confidence INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                    decided_at TEXT
                );
                CREATE TABLE bug_report_duplicates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    original_id INTEGER NOT NULL REFERENCES bug_reports(id),
                    duplicate_id INTEGER NOT NULL REFERENCES bug_reports(id),
                    agent_id INTEGER NOT NULL REFERENCES agents(id),
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                    UNIQUE(original_id, duplicate_id),
                    UNIQUE(duplicate_id)
                );
                INSERT INTO agents (name, token) VALUES ('migrep', 'tok1');
                INSERT INTO agents (name, token) VALUES ('migvoter', 'tok2');
                INSERT INTO bug_reports (agent_id, title, body, status, confidence)
                    VALUES (1, 'open bug', 'b', 'open', 1);
                INSERT INTO bug_reports (agent_id, title, body, status, confidence, decided_at)
                    VALUES (1, 'fixed bug', 'b', 'fixed', 3, '2026-01-01T00:00:00.000Z');
                INSERT INTO bug_reports (agent_id, title, body, status, confidence)
                    VALUES (2, 'open dup', 'b', 'open', 1);
                INSERT INTO bug_report_duplicates (original_id, duplicate_id, agent_id)
                    VALUES (1, 3, 2);
            """)
        db.init_db()  # must widen the CHECK and add resolution columns
        with db._conn() as conn:
            check_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='bug_reports'"
            ).fetchone()["sql"]
            assert "'closed'" in check_sql, "init_db widens status CHECK to 'closed'"
            cols = {r[1] for r in conn.execute("PRAGMA table_info(bug_reports)")}
            assert {"resolution", "resolution_note"} <= cols
            rows = {
                r["id"]: (r["status"], r["resolution"])
                for r in conn.execute("SELECT id, status, resolution FROM bug_reports")
            }
            assert rows == {1: ("open", None), 2: ("fixed", None), 3: ("open", None)}
            for idx in (
                "idx_bug_reports_agent",
                "idx_bug_reports_status",
                "idx_bug_reports_url",
                "idx_bug_reports_created",
            ):
                assert (
                    conn.execute(
                        "SELECT name FROM sqlite_master"
                        f" WHERE type='index' AND name='{idx}'"
                    ).fetchone()
                    is not None
                ), f"{idx} survives the CHECK rebuild"
            link = conn.execute(
                "SELECT COUNT(*) FROM bug_report_duplicates"
            ).fetchone()[0]
            assert link == 1, "dup links survive the rebuild"
        # The feature works on the migrated database (reporter withdraw needs
        # no karma, so no karma seeding required here).
        out = db.resolve_bug_report("tok1", 1, "invalid", "stale report")
        assert out["closed"] is True and out["resolution"] == "invalid"
        db.init_db()  # second boot: idempotent, rows keep their resolution
        with db._conn() as conn:
            again = conn.execute(
                "SELECT status, resolution FROM bug_reports WHERE id = 1"
            ).fetchone()
        assert (again["status"], again["resolution"]) == ("closed", "invalid")
    finally:
        db.DB_PATH = saved_db_path
    print("  bug_reports resolution migration: ok")

    # --- migration: invoices (invoiced pull-payments, small_fix #341) -----
    # Brand-new table, so the honest "old schema" is a pre-feature database
    # without it. init_db() must recreate it on upgrade via schema.sql
    # (no _core.py guard needed - same shape as bug_verifications).
    saved_db_path = db.DB_PATH
    try:
        db.DB_PATH = str(_TMP / "invoices_migration.db")
        db.init_db()
        with db._conn() as conn:
            conn.execute("DROP TABLE IF EXISTS invoices")
            pre = {
                r["name"]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table','index')"
                )
            }
            assert "invoices" not in pre
            assert "idx_invoices_payer" not in pre
        db.init_db()  # boot must recreate table + indexes
        with db._conn() as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(invoices)")}
            assert {
                "id",
                "issuer_agent_id",
                "payer_agent_id",
                "created_by_agent_id",
                "amount_units",
                "remaining_units",
                "reason",
                "status",
                "due_at",
            } <= cols
            nullable = {r[1]: r[3] for r in conn.execute("PRAGMA table_info(invoices)")}
            assert nullable["issuer_agent_id"] == 0, (
                "issuer_agent_id must be nullable for Treasury bills"
            )
            assert nullable["created_by_agent_id"] == 1, (
                "created_by_agent_id must be NOT NULL (every notify path addresses it)"
            )
            for idx in (
                "idx_invoices_payer",
                "idx_invoices_issuer",
                "idx_invoices_created_by",
                "idx_invoices_sweep",
            ):
                assert (
                    conn.execute(
                        "SELECT name FROM sqlite_master"
                        f" WHERE type='index' AND name='{idx}'"
                    ).fetchone()
                    is not None
                ), f"{idx} must exist after boot"
        # The feature works on the migrated database.
        mig_issuer = db.register_agent("invmig-issuer")
        mig_payer = db.register_agent("invmig-payer")
        import db._credits as _cr

        with db._conn() as conn:
            assert _cr.grant(mig_issuer["agent_id"], 20, "invmig_seed", conn=conn)
        seed_post = db.create_post(mig_issuer["token"], "mig karma", "body")
        db.vote(mig_payer["token"], "post", seed_post["post_id"], 1)
        mig_inv = db.create_invoice(
            mig_issuer["token"], mig_payer["name"], 1.0, "migrated ask"
        )
        assert mig_inv["status"] == "pending", mig_inv
        db.init_db()  # second boot: table survives, open invoice intact
        with db._conn() as conn:
            again = conn.execute(
                "SELECT status, remaining_units FROM invoices WHERE id = ?",
                (mig_inv["invoice_id"],),
            ).fetchone()
        assert (again["status"], again["remaining_units"]) == ("pending", 20)
    finally:
        db.DB_PATH = saved_db_path
    print("  invoices migration: ok")

    # --- migration: store_entitlements.blessed_benches -------------------
    # Banked blessed runs added a column to the existing store table, so
    # the honest "old schema" is a live database with the column dropped.
    # init_db() must re-add it via _ensure_column, and buying must work
    # against the migrated database.
    saved_db_path = db.DB_PATH
    try:
        db.DB_PATH = str(_TMP / "bench_store_migration.db")
        db.init_db()
        bench_buyer = db.register_agent("benchmig-buyer")
        with db._conn() as conn:
            conn.execute("ALTER TABLE store_entitlements DROP COLUMN blessed_benches")
        db.init_db()
        with db._conn() as conn:
            cols = {
                r["name"] for r in conn.execute("PRAGMA table_info(store_entitlements)")
            }
        assert "blessed_benches" in cols, "init_db() re-adds the bank column"
        import db._credits as _cr2

        with db._conn() as conn:
            assert _cr2.grant(bench_buyer["agent_id"], 200, "benchmig_seed", conn=conn)
        rep = db.buy_store_item(bench_buyer["token"], "blessed_bench")
        assert rep["owned"] == 1, "buying works on the migrated table"
        db.init_db()  # second boot: no crash, bank survives
        with db._conn() as conn:
            bank = conn.execute(
                "SELECT blessed_benches FROM store_entitlements WHERE agent_id = ?",
                (bench_buyer["agent_id"],),
            ).fetchone()
        assert bank["blessed_benches"] == 1, "bank survives a second boot"
    finally:
        db.DB_PATH = saved_db_path
    print("  blessed_benches migration: ok")

    print("test_misc_g: all assertions passed")
    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
