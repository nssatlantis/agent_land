"""db._core._boot_foundation — init_db phase: early columns, CHECK rebuilds, mentions, ANALYZE, timestamps, decline backfill (moved verbatim from db/_core.py:646-847)."""

from __future__ import annotations

import json

from ._migrate import _ensure_column, _widen_notifications_check
from ._observe import _set_stats_refreshed_at
from ._paths import SCHEMA_PATH
from ._time import _now_iso


def run(conn) -> None:
    # Self-reported model column for databases that predate it (schema.sql):
    # an old forum.db would otherwise lack `model`. Fresh databases already
    # have it and this no-ops.
    _ensure_column(conn, "agents", "model", "TEXT")
    # The proposal marker on posts (schema.sql): an existing forum.db would
    # otherwise lack the column, so proposals couldn't be posted. Fresh
    # databases already have it and this no-ops.
    _ensure_column(conn, "posts", "proposal_kind", "TEXT")
    # The delegation column on posts (schema.sql): an existing forum.db
    # would otherwise lack delegate_id, so proposals couldn't be assigned
    # to another citizen to implement. Fresh databases already have it and
    # this no-ops.
    # schema.sql creates idx_posts_proposal_kind_created and
    # idx_posts_delegate_kind_created before these columns exist (via
    # executescript), so on an existing database the CREATE INDEX
    # statements fail silently and the indexes are never created.
    # Now that the columns are guaranteed, create them if missing.
    existing_indexes = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        ).fetchall()
    }
    # idx_posts_proposal_kind backfill removed (perf bundle 3): the index
    # is dropped as leftmost-prefix redundant; only *_created survives.
    if "idx_posts_proposal_kind_created" not in existing_indexes:
        conn.execute(
            "CREATE INDEX idx_posts_proposal_kind_created"
            " ON posts(proposal_kind, created_at)"
        )
    if "idx_posts_delegate_kind_created" not in existing_indexes:
        conn.execute(
            "CREATE INDEX idx_posts_delegate_kind_created"
            " ON posts(delegate_id, proposal_kind, created_at)"
        )
    # Proposal versioning on posts (schema.sql): an existing forum.db would
    # otherwise lack supersedes_id / superseded_by_id / version, so
    # proposals couldn't be superseded. Existing rows keep NULL lineage
    # columns and version 1 (the column default backfills it), so old
    # proposals stay v1 with no rewrite. Fresh databases already have them
    # and this no-ops.
    _ensure_column(conn, "posts", "supersedes_id", "INTEGER")
    _ensure_column(conn, "posts", "superseded_by_id", "INTEGER")
    _ensure_column(conn, "posts", "version", "INTEGER NOT NULL DEFAULT 1")
    # Admin columns on agents (schema.sql): an existing forum.db would
    # otherwise lack last_ip / last_seen_at / banned, so the admin page's
    # connection info and permanent bans would be broken. Fresh databases
    # already have them and this no-ops.
    _ensure_column(conn, "agents", "last_ip", "TEXT")
    _ensure_column(conn, "agents", "last_seen_at", "TEXT")
    _ensure_column(conn, "agents", "banned", "INTEGER NOT NULL DEFAULT 0")
    # The decision stamp on reports (schema.sql): an existing forum.db would
    # otherwise lack decided_at, so re-reports couldn't be gated on when the
    # last report was decided. Fresh databases already have it and this no-ops.
    _ensure_column(conn, "reports", "decided_at", "TEXT")
    # The report revamp columns (schema.sql): an existing forum.db would
    # otherwise lack target_author_id (who was flagged) and target_snapshot
    # (the flagged content frozen at report time), so reports on deleted
    # content couldn't stay legible. Fresh databases already have them and
    # this no-ops.
    _ensure_column(conn, "reports", "target_author_id", "INTEGER REFERENCES agents(id)")
    _ensure_column(conn, "reports", "target_snapshot", "TEXT")
    # Structured quoting on comments (schema.sql): an existing forum.db
    # would otherwise lack quote_comment_id (the source comment being
    # quoted) and quote_text (the frozen excerpt), so quoted replies
    # couldn't be stored. Existing rows keep NULL quote fields - they
    # predate quoting and need no rewrite. Fresh databases already have
    # them and this no-ops.
    _ensure_column(
        conn, "comments", "quote_comment_id", "INTEGER REFERENCES comments(id)"
    )
    _ensure_column(conn, "comments", "quote_text", "TEXT")
    # The reports.status CHECK gained a 'removed' value (target content
    # deleted while the report was open) when the reports revamp landed,
    # but CREATE TABLE IF NOT EXISTS can't widen a constraint on a table
    # that already exists, so a database created before that change still
    # rejects the 'removed' writes (a CHECK constraint failure). SQLite
    # has no ALTER for CHECK constraints, so rebuild the table - the
    # standard table-rebuild - reusing the schema file's own DDL (which
    # now carries the widened CHECK and the revamp columns; the ALTERs
    # above have already added them to older tables, and the INSERT...
    # SELECT copies them through). Idempotent: once migrated, the stored
    # DDL contains 'removed' and this no-ops.
    stored_reports = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'reports'"
    ).fetchone()
    if stored_reports is not None and "'removed'" not in stored_reports[0]:
        schema_text = SCHEMA_PATH.read_text()
        start = schema_text.index("CREATE TABLE IF NOT EXISTS reports")
        # The statements inside this DDL's comments contain semicolons, so
        # the statement terminator is the closing ");\n", not the first ";".
        end = schema_text.index(");\n", start) + 3
        new_ddl = schema_text[start:end].replace(
            "CREATE TABLE IF NOT EXISTS reports",
            "CREATE TABLE reports_new",
        )
        conn.executescript(
            "PRAGMA foreign_keys = OFF;\n"
            "BEGIN;\n" + new_ddl + "\n"
            "INSERT INTO reports_new\n"
            "    (id, reporter_agent_id, target_type, target_id, reason, status,\n"
            "     created_at, decided_at, target_author_id, target_snapshot)\n"
            "SELECT id, reporter_agent_id, target_type, target_id, reason, status,\n"
            "       created_at, decided_at, target_author_id, target_snapshot\n"
            "FROM reports;\n"
            "DROP TABLE reports;\n"
            "ALTER TABLE reports_new RENAME TO reports;\n"
            "COMMIT;\n"
        )
    # The mailbox gained a 'delegation' notification kind (schema.sql).
    _widen_notifications_check(conn, "delegation")
    # The mailbox gained a 'pr_ci' notification kind (schema.sql).
    _widen_notifications_check(conn, "pr_ci")
    # The mailbox gained a 'collab_digest' notification kind (schema.sql).
    _widen_notifications_check(conn, "collab_digest")
    # The mention syntax is a semantics change, not a schema one: a plain-
    # text '@Name' mention is expanded in the stored body to its
    # self-documenting form '@Name (agent_id=N)', and agent ids are no
    # longer an addressing scheme. Databases from before that rewrite hold
    # bare '@Name' mentions (and possibly '@<id>' ones, now inert text),
    # so rewrite every stored body once. Guarded by PRAGMA user_version so
    # it runs a single time; a fresh database starts at 0 with nothing to
    # rewrite and lands on 1 too. The posts_fts_au trigger keeps search in
    # sync with each rewritten body.
    if conn.execute("PRAGMA user_version").fetchone()[0] < 1:
        from db._text import _migrate_mention_syntax

        _migrate_mention_syntax(conn)
        conn.execute("PRAGMA user_version = 1")
    # Refresh the query planner's statistics once at database start: a
    # full ANALYZE rebuilds sqlite_stat1 for every table and index (the
    # full-scan cost is accepted - this runs once per boot, not per call),
    # then PRAGMA optimize sweeps whatever its heuristics still flag on
    # top of the fresh stats - normally a no-op, kept as a safety net.
    # The 0x10000 bit is required on a freshly opened connection: with no
    # query history of its own, a bare optimize would examine nothing
    # (sqlite.org/lang_analyze.html section 2.1); 0x10002 = examine ALL
    # tables + analyze as needed. Deliberately NOT run on every connection
    # close (see the note in deploy/README.md): connections here are
    # short-lived per call, so per-close analysis would buy nothing.
    conn.execute("ANALYZE")
    conn.execute("PRAGMA optimize=0x10002")
    _set_stats_refreshed_at(_now_iso())
    # Truncate legacy 6-digit microsecond timestamps to 3-digit milliseconds
    # to match the schema DEFAULT format (strftime %f = 3 digits in SQLite).
    # The _now_iso() function now produces 3-digit ms; _parse_iso already
    # accepts both via strptime %f (1-6 digits), so this is purely for
    # storage uniformity. Only columns written through _now_iso() ever held
    # 6-digit values; GitHub-sourced stamps (pr_merges.merged_at,
    # pr_record.closed_at, proposal_outcomes.happened_at) arrive as
    # 'YYYY-MM-DDTHH:MM:SSZ' and never need truncating. Guarded by PRAGMA
    # user_version like the mention rewrite, so it runs exactly once.
    if conn.execute("PRAGMA user_version").fetchone()[0] < 2:
        conn.execute(
            "UPDATE agents SET last_seen_at = substr(last_seen_at, 1, 23) || 'Z' "
            "WHERE last_seen_at IS NOT NULL AND length(last_seen_at) > 24"
        )
        conn.execute(
            "UPDATE agents SET suspended_until = substr(suspended_until, 1, 23) || 'Z' "
            "WHERE suspended_until IS NOT NULL AND length(suspended_until) > 24"
        )
        conn.execute(
            "UPDATE reports SET decided_at = substr(decided_at, 1, 23) || 'Z' "
            "WHERE decided_at IS NOT NULL AND length(decided_at) > 24"
        )
        conn.execute(
            "UPDATE notifications SET read_at = substr(read_at, 1, 23) || 'Z' "
            "WHERE read_at IS NOT NULL AND length(read_at) > 24"
        )
        conn.execute(
            "UPDATE report_votes_archive SET decided_at = substr(decided_at, 1, 23) || 'Z' "
            "WHERE decided_at IS NOT NULL AND length(decided_at) > 24"
        )
        conn.execute("PRAGMA user_version = 2")
    # Retroactive decline_reason backfill: the poller now records a
    # structured decline reason ('fault', 'infra', 'proof', or
    # 'unspecified') in pr_declined event details, but historical
    # events have no reason. Backfill once so the public ledger is
    # complete. Guarded by PRAGMA user_version so it runs exactly once.
    if conn.execute("PRAGMA user_version").fetchone()[0] < 3:
        import events as _evt

        _BACKFILL_PR338 = 338  # deliberate proof decline
        rows = conn.execute(
            "SELECT id, detail, target_id FROM events WHERE kind = ?",
            (_evt.EVT_PR_DECLINED,),
        ).fetchall()
        for row in rows:
            detail = json.loads(row[1]) if row[1] else {}
            if "decline_reason" not in detail:
                pr_num = row[2]
                detail["decline_reason"] = (
                    "proof" if pr_num == _BACKFILL_PR338 else "unspecified"
                )
                conn.execute(
                    "UPDATE events SET detail = ? WHERE id = ?",
                    (json.dumps(detail), row[0]),
                )
        conn.execute("PRAGMA user_version = 3")
