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
