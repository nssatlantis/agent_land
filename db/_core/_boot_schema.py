"""db._core._boot_schema — init_db phase: todo index widen + FTS backfills (moved verbatim from db/_core.py:580-645)."""

from __future__ import annotations


def run(conn) -> None:
    # Widen the todo ordering indexes on databases that predate this
    # change: a pre-upgrade forum.db carries them on (post_id) /
    # (list_id) only, which forces a temp B-tree sort for the docket
    # listers' ORDER BY post_id,position,id / list_id,position,id.
    # Recreate them wider so an existing database matches a fresh schema.
    # No-op once they are already wide (checked via PRAGMA index_info).
    _existing_indexes = {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
    }

    def _ensure_wide_todo_index(name, table, key):
        if name not in _existing_indexes:
            return
        _cols = {r[2] for r in conn.execute(f"PRAGMA index_info({name})")}
        if "position" in _cols:
            return
        conn.execute(f"DROP INDEX IF EXISTS {name}")
        conn.execute(f"CREATE INDEX {name} ON {table}({key}, position, id)")

    _ensure_wide_todo_index("idx_todo_lists_post", "todo_lists", "post_id")
    _ensure_wide_todo_index("idx_todo_items_list", "todo_items", "list_id")
    _one_running_broadcast_index(conn)
    # Backfill the FTS index for databases that predate the search feature:
    # the CREATE ... IF NOT EXISTS above leaves an existing index empty and
    # only newly inserted posts are indexed by the triggers, so search would
    # silently miss every pre-existing post. A no-op on fresh databases.
    # NOTE: can't test emptiness via "COUNT(*) FROM posts_fts" - for an
    # external-content table that counts content rows, not index entries;
    # the posts_fts_idx shadow table is empty while nothing is indexed.
    if (
        conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0] > 0
        and conn.execute("SELECT COUNT(*) FROM posts_fts_idx").fetchone()[0] == 0
    ):
        conn.execute("INSERT INTO posts_fts(posts_fts) VALUES ('rebuild')")
    # Same story for the comment search index: a database that predates it
    # has an empty comments_fts and only newly inserted comments get
    # indexed by the triggers, so comment search would silently miss every
    # pre-existing comment. A no-op on fresh databases.
    if (
        conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0] > 0
        and conn.execute("SELECT COUNT(*) FROM comments_fts_idx").fetchone()[0] == 0
    ):
        conn.execute("INSERT INTO comments_fts(comments_fts) VALUES ('rebuild')")
    # Same story for the to-do search index: a database that predates the
    # index has an empty todo_items_fts and only newly inserted items get
    # indexed by the triggers, so to-do search would silently miss every
    # pre-existing item. Unlike posts/comments, list_title is not a column
    # of todo_items, so the FTS 'rebuild' command cannot derive it - seed
    # the index manually. A non-external FTS table reports its indexed
    # rows via COUNT(*), so a healthy index has exactly one row per
    # todo_item; any count mismatch (empty, partial via an interrupted
    # previous backfill, or stale) triggers a full rebuild from the
    # authoritative tables.
    # domain:never-lose-data - a mismatch rebuilds the index from
    # todo_items/todo_lists and re-running init_db is idempotent (a
    # healthy index has equal counts and this no-ops).
    if (
        conn.execute("SELECT COUNT(*) FROM todo_items").fetchone()[0]
        != conn.execute("SELECT COUNT(*) FROM todo_items_fts").fetchone()[0]
    ):
        conn.execute("DELETE FROM todo_items_fts")
        conn.execute(
            "INSERT INTO todo_items_fts(rowid, text, list_title)"
            " SELECT ti.id, ti.text, tl.title"
            " FROM todo_items ti JOIN todo_lists tl ON tl.id = ti.list_id"
        )


def _one_running_broadcast_index(conn) -> None:
    """The "one real broadcast at a time" invariant, as a DATA constraint.

    Partial unique index on `status WHERE status='running' AND dry_run=0`.
    `create_broadcast` also checks `active_broadcast()` and refuses a second
    real broadcast, but that check reads on one connection and the INSERT
    happens on another with no BEGIN IMMEDIATE between them - the race
    db/_core/_conn.py documents. It held only because the HTTP handler
    happened to call it synchronously on the event loop; a second uvicorn
    worker or a CLI caller would slip a second row past it and fire two full
    fan-outs. The index moves the guarantee from the call graph to the data.

    `AND dry_run = 0` is what keeps the documented preview exemption alive: a
    preview contacts nobody, so it may be queued and previewed while a real
    broadcast is in flight.

    WHY A BOOT MIGRATION AND NOT schema.sql
    ---------------------------------------
    Because a UNIQUE index cannot be created over rows that violate it, and
    the state that violates it is reachable in exactly one place: a
    database that already has this table from an earlier build of the same
    feature, where previews were exempt from the application check and so
    two `running` rows could exist. `CREATE UNIQUE INDEX` would then raise
    inside init_db, and the server would not boot at all - a partial feature
    taking the whole forum down on upgrade.

    So surplus running rows are retired first, then the index is created. A
    running row at boot is stale by definition - that is the same premise
    `repair_running` acts on - so retiring all but the oldest loses no real
    work, only the stranded markers of a process that is no longer running.
    The oldest is kept so the invariant still has something to hold.
    """
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
        " AND name = 'agent_wake_broadcasts'"
    ).fetchone()
    if row is None:
        # Pre-feature database: schema.sql owns the index for fresh installs.
        return
    conn.execute(
        "UPDATE agent_wake_broadcasts SET status = 'abandoned', finished_at = "
        "strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE status = 'running' AND id NOT IN"
        " (SELECT MIN(id) FROM agent_wake_broadcasts"
        "  WHERE status = 'running' AND dry_run = 0)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_wake_broadcasts_one_running"
        " ON agent_wake_broadcasts(status)"
        " WHERE status = 'running' AND dry_run = 0"
    )
