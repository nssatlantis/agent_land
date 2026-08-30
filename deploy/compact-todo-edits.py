#!/opt/agent_land_data/venv/bin/python
"""One-off compaction: shrink legacy full-size todo_edits rows to the compact
format introduced by PR #684.

Before #684, every to-do mutation wrote BOTH a full `old_lists` (before) and
`new_lists` (after) snapshot as spaced JSON. After #684, rows store only the
after side as compact JSON (separators (",", ":")) and the before side is
derived from the previous row's after side by the reader
(db._proposal_todos._derive_edits). Existing rows were written in the old
format and stay big — nothing regressed (the reader is backward compatible),
but they never shrink on their own.

This script rewrites those legacy rows in place to the compact format,
losslessly, exploiting the invariant that every historical row's `old_lists`
equals the previous row's `new_lists` (the old write paths derived the before
side from the previous after side). For each proposal it walks todo_edits in
the SAME order the reader derives edits (edited_at, id) and, per row:

  * if the row's old_lists decodes to exactly the previous row's new_lists
    (or [] for the first row of a proposal) — the row is REDUNDANT and safe
    to compact: old_lists -> '' (the _OLD_DERIVED sentinel) and new_lists ->
    compact JSON (same decoded value, fewer bytes).
  * otherwise — the row's own snapshot is NOT derivable; it is left untouched
    (the reader passes it through unchanged), so no information is ever lost.

SQLite does not return freed pages to the OS by itself; run the separate
`--vacuum` step at a quiet time to actually shrink the file (VACUUM rewrites
the whole DB under a write lock).

Idempotent and dry-run by default; use --apply to write.

Usage:
    python deploy/compact-todo-edits.py [--post-id 123] [--apply] [--vacuum]

`--apply` writes the row compactions (separate step from --vacuum). `--vacuum`
does NOT touch rows, just VACUUMs the file — run it alone, or after --apply,
when the DB is quiet. `--post-id` limits the walk to one proposal.

Exit codes: 0 ok, 2 refused/misconfigured.
"""

import argparse
import json
import pathlib
import sys

# The exact sentinel and compact writer the app uses (db._proposal_todos.py).
_OLD_DERIVED = ""
_COMPACT_SEPARATORS = (",", ":")


def _find_repo() -> pathlib.Path:
    here = pathlib.Path(__file__).resolve().parent
    for cand in (here, here.parent, here.parent.parent):
        if (cand / "schema.sql").exists() and (cand / "db" / "__init__.py").exists():
            return cand
    return pathlib.Path("/opt/agent_land")


def _import_config(repo_dir: pathlib.Path):
    sys.path.insert(0, str(repo_dir))
    try:
        import config
    except Exception as exc:
        print(f"ERROR: cannot import config.py ({exc}); refusing.", file=sys.stderr)
        sys.exit(2)
    finally:
        sys.path.pop(0)
    return config


def compact_json(state) -> str:
    return json.dumps(state, separators=_COMPACT_SEPARATORS)


def _compact_proposal(conn, post_id: int, apply: bool) -> tuple[int, int, int, int]:
    """Compact one proposal's todo_edits rows. Rows must be walked in the same
    (edited_at, id) order the reader derives edits, so the derived before side
    matches what this walk treated as redundant.

    Returns (rows_seen, rows_compacted, rows_skipped, rows_already_compact)."""
    rows = conn.execute(
        "SELECT id, old_lists, new_lists FROM todo_edits"
        " WHERE post_id = ? ORDER BY edited_at, id",
        (post_id,),
    ).fetchall()
    seen = compacted = skipped = already = 0
    prev_new: str | None = None
    for r in rows:
        seen += 1
        old_raw = r["old_lists"]
        new_raw = r["new_lists"]
        if old_raw == _OLD_DERIVED:
            # Already compact (post-#684 write) — nothing to do.
            already += 1
            prev_new = new_raw
            continue
        try:
            old = json.loads(old_raw)
            new = json.loads(new_raw)
        except Exception:
            # Unparseable row — never touch it; keep its snapshot intact.
            skipped += 1
            prev_new = new_raw
            continue
        expected = json.loads(prev_new) if prev_new is not None else []
        if old != expected:
            # The row's old_lists is NOT derivable from the previous row's
            # after side — blanking it would change what the reader returns.
            # Keep the full snapshot (reader passes it through unchanged).
            skipped += 1
            prev_new = new_raw
            continue
        compacted += 1
        new_compact = compact_json(new)
        if apply:
            conn.execute(
                "UPDATE todo_edits SET old_lists = ?, new_lists = ? WHERE id = ?",
                (_OLD_DERIVED, new_compact, r["id"]),
            )
        prev_new = new_compact
    return seen, compacted, skipped, already


def _main() -> int:
    ap = argparse.ArgumentParser(
        description="Compact legacy todo_edits rows to the #684 compact format."
    )
    ap.add_argument(
        "--post-id", type=int, default=None, help="only this proposal (default: all)"
    )
    ap.add_argument(
        "--apply", action="store_true", help="write; without this flag it is dry-run"
    )
    ap.add_argument(
        "--vacuum",
        action="store_true",
        help="VACUUM the database (does not touch rows); run separately at a quiet time",
    )
    args = ap.parse_args()

    repo_dir = _find_repo()
    _config = _import_config(repo_dir)
    if pathlib.Path(_config.DB_PATH).resolve().is_relative_to(repo_dir.resolve()):
        print(
            f"ERROR: DB {_config.DB_PATH} inside repo {repo_dir}; refusing.",
            file=sys.stderr,
        )
        return 2
    sys.path.insert(0, str(repo_dir))
    try:
        from db._core import _conn
    finally:
        sys.path.pop(0)

    # Compaction goes through _conn (one transaction, committed on clean
    # exit, rolled back on any error) — identical to how deploy/backfill
    # scripts write. VACUUM is separate: it cannot run inside a transaction,
    # so it opens its own raw connection.
    if args.apply or not args.vacuum:
        with _conn() as conn:
            if args.post_id is not None:
                post_ids = [args.post_id]
            else:
                post_ids = [
                    r["post_id"]
                    for r in conn.execute(
                        "SELECT DISTINCT post_id FROM todo_edits ORDER BY post_id"
                    ).fetchall()
                ]

            if not post_ids:
                print("No todo_edits rows — nothing to compact.")
            else:
                tot_seen = tot_compacted = tot_skipped = tot_already = 0
                for pid in post_ids:
                    seen, compacted, skipped, already = _compact_proposal(
                        conn, pid, args.apply
                    )
                    tot_seen += seen
                    tot_compacted += compacted
                    tot_skipped += skipped
                    tot_already += already
                    print(
                        f"post {pid}: seen={seen} compact={compacted} "
                        f"skip={skipped} already={already}"
                    )
                verb = "compacted" if args.apply else "would compact"
                print(
                    f"{verb} {tot_compacted} of {tot_seen} rows "
                    f"({tot_skipped} left intact as non-derivable, "
                    f"{tot_already} already compact)."
                )

    if args.vacuum:
        import sqlite3 as _sqlite3

        print("Running VACUUM...")
        vconn = _sqlite3.connect(_config.DB_PATH)
        try:
            vconn.execute("VACUUM")
        finally:
            vconn.close()
        print("VACUUM complete.")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
