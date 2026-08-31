#!/opt/agent_land_data/venv/bin/python
"""One-off backfill: rewrite #684 compact-snapshot todo_edits rows to the
#713 delta format.

After #713, every to-do mutation writes either a compact delta
({"v":2,"type":"delta","ops":[...]}) or, when the diff cannot round-trip or
is not smaller than a snapshot, a full compact snapshot. But that applies
only to NEW rows: rows written before #713 (as #684 compact snapshots, or
legacy full-size rows) were never re-encoded, so they keep paying the cost
of a full on-disk snapshot of the entire to-do state per edit. For a busy
collaborative proposal that is the bulk of the table's size (seen on prod:
777 rows / 19.7 MB - ~25 KB per row).

This script rewrites existing COMPACT SNAPSHOT rows (post-#684, non-delta)
into lossless delta rows wherever possible, reusing the exact merged helpers
from db._proposal_todos (issue #713): _decode_new_lists, _diff_states,
_apply_ops, _normalize_state, _compact_delta, _has_ids. For each proposal it
walks todo_edits in the SAME (edited_at, id) order the reader derives edits
and, per row:

  * already a delta -> left untouched (idempotent).
  * a compact snapshot (old_lists is the _OLD_DERIVED sentinel) whose
    after-state differs from the previous resolved state in a small, lossless,
    diffable way -> rewritten as a delta (old_lists stays the sentinel,
    new_lists -> compact delta). The round-trip is verified with
    _apply_ops(_diff_states(...)) == normalized after-state, and the delta
    must be strictly smaller than the stored snapshot before it is written.
  * anything else (legacy full-size rows with their own old_lists, diffs the
    encoder does not express, states lacking item/list ids, first rows,
    snapshots no smaller than their delta) -> left byte-for-byte intact.

The public edit trail - what db._proposal_todos._derive_edits returns - is
byte-identical before and after, because the reader normalizes pr_number:null
back in and replays deltas against the same current chain this walk
maintains. The tests assert that before/after equality.

SQLite does not return freed pages to the OS by itself; run the separate
`--vacuum` step at a quiet time to actually shrink the file (VACUUM rewrites
the whole DB under a write lock).

Idempotent and dry-run by default; use --apply to write.

Usage:
    python deploy/delta-todo-edits.py [--post-id 123] [--apply] [--vacuum]

`--apply` writes the row rewrites (separate step from --vacuum). `--vacuum`
does NOT touch rows, just VACUUMs the file - run it alone, or after --apply,
when the DB is quiet. `--post-id` limits the walk to one proposal.

Exit codes: 0 ok, 2 refused/misconfigured.
"""

import argparse
import pathlib
import sys


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


def _delta_proposal(conn, post_id: int, max_ops: int, apply: bool):
    """Rewrite one proposal's compact-snapshot todo_edits rows into deltas.

    Walks rows in the SAME (edited_at, id) order the reader (_derive_edits)
    uses, maintaining exactly the `current` chain the reader holds, so the
    public trail is unchanged. Returns a summary tuple:
    (seen, deltaed, kept_legacy, kept_snapshot, already_delta, bytes_saved)."""
    import copy

    from db._proposal_todos import (
        _OLD_DERIVED,
        _apply_ops,
        _compact_delta,
        _decode_new_lists,
        _diff_states,
        _has_ids,
        _normalize_state,
    )

    rows = conn.execute(
        "SELECT id, old_lists, new_lists FROM todo_edits"
        " WHERE post_id = ? ORDER BY edited_at, id",
        (post_id,),
    ).fetchall()
    seen = deltaed = kept_legacy = kept_snapshot = already_delta = 0
    bytes_saved = 0
    current: list[dict] = []
    for r in rows:
        seen += 1
        old_raw = r["old_lists"]
        new_raw = r["new_lists"]
        if old_raw != _OLD_DERIVED:
            # Legacy full-size row carrying its own before snapshot - the
            # reader passes its old_lists through unchanged, so a delta here
            # would alter the public trail. Leave it entirely intact.
            kept_legacy += 1
            current = _decode_new_lists(new_raw)[1]
            continue
        kind, payload = _decode_new_lists(new_raw)
        if kind == "delta":
            already_delta += 1
            current = _apply_ops(current, payload)
            continue
        # Compact snapshot row (post-#684). Decide whether its after-state can
        # be expressed as a smaller lossless delta from the previous state.
        x_norm = _normalize_state(copy.deepcopy(payload))
        prev_norm = _normalize_state(copy.deepcopy(current))
        use_delta = len(payload) > 0 and _has_ids(prev_norm) and _has_ids(x_norm)
        if use_delta:
            ops = _diff_states(prev_norm, x_norm)
            use_delta = len(ops) > 0
            if max_ops:
                use_delta = use_delta and len(ops) <= max_ops
            if use_delta and _apply_ops([dict(l) for l in prev_norm], ops) != x_norm:
                # Round-trip mismatch - the encoder missed something; the row
                # stays a snapshot (the reader stores it exactly as written).
                use_delta = False
            delta_json = ""
            if use_delta:
                delta_json = _compact_delta(ops)
                # Only rewrite when the delta actually buys storage.
                if len(delta_json) >= len(new_raw):
                    use_delta = False
        if use_delta:
            kept_snapshot_saved = len(new_raw) - len(delta_json)
            if apply:
                conn.execute(
                    "UPDATE todo_edits SET new_lists = ? WHERE id = ?",
                    (delta_json, r["id"]),
                )
            deltaed += 1
            bytes_saved += kept_snapshot_saved
            current = _apply_ops(current, ops)
        else:
            kept_snapshot += 1
            current = payload
    return seen, deltaed, kept_legacy, kept_snapshot, already_delta, bytes_saved


def _main() -> int:
    ap = argparse.ArgumentParser(
        description="Rewrite #684 compact-snapshot todo_edits rows into #713 deltas."
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

    max_ops = getattr(_config, "FORUM_TODO_DELTA_MAX_SNAPSHOT_OPS", 16) or 0

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
                print("No todo_edits rows - nothing to rewrite.")
            else:
                tot_seen = tot_deltaed = 0
                tot_legacy = tot_snapshot = tot_already = 0
                tot_saved = 0
                for pid in post_ids:
                    seen, deltaed, legacy, snapshot, already, saved = _delta_proposal(
                        conn, pid, max_ops, args.apply
                    )
                    tot_seen += seen
                    tot_deltaed += deltaed
                    tot_legacy += legacy
                    tot_snapshot += snapshot
                    tot_already += already
                    tot_saved += saved
                    print(
                        f"post {pid}: seen={seen} delta={deltaed} "
                        f"legacy={legacy} snapshot={snapshot} already={already} "
                        f"bytes_saved={saved}"
                    )
                verb = "rewrote" if args.apply else "would rewrite"
                print(
                    f"{verb} {tot_deltaed} of {tot_seen} rows to deltas "
                    f"(saving ~{tot_saved} bytes); {tot_snapshot} kept as "
                    f"snapshots, {tot_legacy} legacy rows intact, "
                    f"{tot_already} already deltas."
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
