#!/opt/agent_land_data/venv/bin/python
"""One-off compaction: trim oversized ci_* event output_tail values.

The ci_* event ledger rows (repo_ci_run's audit trail) fold a finished
run's output_tail into their detail. Before the CI_RUN_EVENT_TAIL_BYTES
cap, that copy was the full caller-facing 16 KiB tail, so a single event's
detail (~25 KB on prod) spilled across six-odd SQLite overflow pages and
accounted for all 6.6 MB of overflow on the events table. New rows are
capped at write time (events.py log_event / _ci_detail_with_output read
CI_RUN_EVENT_TAIL_BYTES); this script rewrites the pre-cap historical rows.

Per event row:
  * detail is not valid JSON, or is not an object, or has no non-empty
    string `output_tail` -> left byte-for-byte intact (kind-agnostic: any
    detail with an output_tail is by construction a ci_* row).
  * output_tail fits within the cap already -> left byte-for-byte intact
    (idempotency: re-running rewrites nothing).
  * output_tail exceeds the cap -> kept to its last cap bytes (byte-exact,
    like the runtime capper CI_RUN_TAIL_BYTES: the cut may fall inside a
    multi-byte character, which decodes as U+FFFD), `output_truncated`
    set True, and the whole detail re-JSONed compactly. Every other key
    (summary, failed_files, ok, exit_code, ...) is preserved untouched.

The public read surface - events.query_events' parsed detail - is
identical before and after (json.loads of the compact form returns the
same object); only the JSON whitespace and the oversized tail change.

SQLite does not return freed pages to the OS by itself; run the separate
`--vacuum` step at a quiet time to actually shrink the file (VACUUM
rewrites the whole DB under a write lock).

Idempotent and dry-run by default; use --apply to write.

Usage:
    python deploy/trim-ci-events.py [--apply] [--vacuum]

`--apply` writes the row rewrites (separate step from --vacuum).
`--vacuum` does NOT touch rows, just VACUUMs the file - run it alone, or
after --apply, when the DB is quiet.

Exit codes: 0 ok, 2 refused/misconfigured.
"""

import argparse
import json
import pathlib
import sys

# Bootstrap deploy/ onto sys.path so _common resolves when the test harness
# runs this script from a temp directory (deploy/ is not the cwd).
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _common import _find_repo, _import_config  # noqa: I001


def _trim_tail(tail: str, cap: int) -> str:
    """Byte-exact tail cut mirroring the runtime capper: keep the last
    `cap` bytes, decode with replacement so multibyte characters at the
    boundary degrade to U+FFFD rather than crashing the rewrite."""
    tail_bytes = tail.encode("utf-8")
    if len(tail_bytes) <= cap:
        return tail
    return tail_bytes[-cap:].decode("utf-8", errors="replace")


def _rewritten_detail(raw: str, cap: int) -> str | None:
    """The compact, tail-capped re-dump of an event detail, or None when
    the row needs no rewrite (unparseable / not a dict / no oversized
    output_tail) and must be left byte-for-byte intact."""
    try:
        detail = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(detail, dict):
        return None
    tail = detail.get("output_tail")
    if not isinstance(tail, str) or not tail:
        return None
    trimmed = _trim_tail(tail, cap)
    if trimmed == tail:
        return None
    detail["output_tail"] = trimmed
    detail["output_truncated"] = True
    return json.dumps(detail, separators=(",", ":"))


def _main() -> int:
    ap = argparse.ArgumentParser(
        description="Trim oversized ci_* event output_tail values."
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
    # `git clean -xdf` on every deploy, so rewriting a database that is
    # about to vanish would be pointless (and destructive if it weren't).
    if pathlib.Path(_config.DB_PATH).resolve().is_relative_to(repo_dir.resolve()):
        print(
            f"ERROR: database path {_config.DB_PATH} points inside the repo "
            f"({repo_dir}); refusing to run (git clean -xdf would wipe it).",
            file=sys.stderr,
        )
        return 2

    cap = getattr(_config, "CI_RUN_EVENT_TAIL_BYTES", 3072) or 0
    if not cap:
        print(
            "FORUM_CI_RUN_EVENT_TAIL_BYTES is 0 (keep full tails) - nothing to trim.",
            file=sys.stderr,
        )
        return 0

    sys.path.insert(0, str(repo_dir))
    try:
        from db._core import _conn
    finally:
        sys.path.pop(0)

    if args.apply or not args.vacuum:
        with _conn() as conn:
            rows = conn.execute(
                "SELECT id, detail FROM events WHERE detail IS NOT NULL"
            ).fetchall()
            seen = trimmed = kept = bytes_saved = 0
            for r in rows:
                seen += 1
                rewritten = _rewritten_detail(r["detail"], cap)
                if rewritten is None:
                    kept += 1
                    continue
                bytes_saved += len(r["detail"]) - len(rewritten)
                if rewritten == r["detail"]:
                    kept += 1
                    continue
                if args.apply:
                    conn.execute(
                        "UPDATE events SET detail = ? WHERE id = ?",
                        (rewritten, r["id"]),
                    )
                trimmed += 1
            verb = "trimmed" if args.apply else "would trim"
            print(
                f"{verb} {trimmed} of {seen} event detail rows "
                f"(saving ~{bytes_saved} bytes); {kept} left intact."
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
