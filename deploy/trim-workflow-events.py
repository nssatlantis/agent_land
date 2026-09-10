#!/opt/agent_land_data/venv/bin/python
"""One-off compaction: delete the dead workflow_* event ledger rows.

The workflow lifecycle emitted two event kinds - workflow_started (one per
create-pr run) and workflow_closed (one per run closure / sweep) - neither
of which any production reader consumes. workflow_started stopped being
emitted by the CI_RUN_EVENT_TAIL_BYTES-era events prune (proposal #362);
workflow_closed was stopped by the workflow-ledger prune (proposal #387).
Together they were 28,864 of the 44,006 events rows (~66%) on prod - a
write-heavy append ledger where row count dominates the index and scan
surface (list_events / recent_activity).

The authoritative state survives this deletion: each workflow run's
lifecycle - status, decided_at, expires_at, agent_id, proposal_id,
pr_number - lives in the workflow_runs table (schema.sql), which is where
the run board reads it. The event rows are pure enrichment duplicates:
their detail re-states status/reason/proposal_id that workflow_runs
already holds. Deleting them loses nothing the run board or any reader
depends on.

Deletes rows whose kind is exactly workflow_started or workflow_closed
(the two dead kinds - nothing else is touched, kind-agnostically). The
events PRIMARY KEY is AUTOINCREMENT, so ids are never reused by new rows.

Idempotent and dry-run by default; use --apply to write.

Usage:
    python deploy/trim-workflow-events.py [--apply] [--vacuum]

`--apply` deletes the rows (separate step from --vacuum).
`--vacuum` does NOT touch rows, just VACUUMs the file - run it alone, or
after --apply, when the DB is quiet (VACUUM rewrites the whole DB under a
write lock and returns the freed pages to the OS).

Exit codes: 0 ok, 2 refused/misconfigured.
"""

import argparse
import pathlib
import sys

# Bootstrap deploy/ onto sys.path so _common resolves when the test harness
# runs this script from a temp directory (deploy/ is not the cwd).
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _common import _find_repo, _import_config  # noqa: I001

_DEAD_KINDS = ("workflow_started", "workflow_closed")


def _count_rows(conn) -> int:
    return conn.execute(
        f"SELECT COUNT(*) FROM events WHERE kind IN ({', '.join('?' * len(_DEAD_KINDS))})",
        _DEAD_KINDS,
    ).fetchone()[0]


def _main() -> int:
    ap = argparse.ArgumentParser(
        description="Delete the dead workflow_started / workflow_closed event rows."
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
    # Same hazard update.sh guards: a DB inside the repo is wiped by
    # `git clean -xdf` on every deploy, so deleting rows in a database that
    # is about to vanish would be pointless (and destructive if it weren't).
    if pathlib.Path(_config.DB_PATH).resolve().is_relative_to(repo_dir.resolve()):
        print(
            f"ERROR: database path {_config.DB_PATH} points inside the repo "
            f"({repo_dir}); refusing to run (git clean -xdf would wipe it).",
            file=sys.stderr,
        )
        return 2

    sys.path.insert(0, str(repo_dir))
    try:
        from db._core import _conn
    finally:
        sys.path.pop(0)

    if args.apply or not args.vacuum:
        with _conn() as conn:
            before = _count_rows(conn)
            deleted = 0
            if args.apply and before:
                cur = conn.execute(
                    f"DELETE FROM events WHERE kind IN ({', '.join('?' * len(_DEAD_KINDS))})",
                    _DEAD_KINDS,
                )
                deleted = cur.rowcount
            after = _count_rows(conn)
            verb = "deleted" if args.apply else "would delete"
            if args.apply:
                shown = deleted
            else:
                shown = before
            print(
                f"{verb} {shown} of {before} dead workflow event rows; {after} remain."
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
