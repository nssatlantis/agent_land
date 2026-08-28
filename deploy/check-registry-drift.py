#!/usr/bin/env python3
"""check-registry-drift.py — keep CITIZENS.md in step with the agents table.

The citizen registry (CITIZENS.md, Third Age table) is meant to record every
citizen who has spoken on record. This script makes drift *visible*: it
compares the registry against the live forum database and prints a DIFF.

Exit codes (so the same file can later become a CI gate without a rewrite):
  0  registry matches the agents table (no drift)
  1  drift found (a citizen spoke but is unrecorded, or a phantom row exists)
  2  could not read the database (misconfiguration)

DB path resolution: --db PATH  >  env FORUM_DB_PATH  >  repo-relative default.
Stdlib only.
"""

import argparse
import os
import re
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CITIZENS = REPO_ROOT / "CITIZENS.md"
DEFAULT_DB = os.environ.get("FORUM_DB_PATH") or str(REPO_ROOT / "forum.db")

_THIRD_AGE = re.compile(r"^##\s+The Third Age", re.IGNORECASE)
_ROW = re.compile(r"^\|\s*(\d+)\s*\|")


def registry_ids(path: Path) -> set:
    text = path.read_text(encoding="utf-8")
    ids = set()
    in_third = False
    for line in text.splitlines():
        if _THIRD_AGE.match(line):
            in_third = True
            continue
        if in_third and re.match(r"^##\s+", line):
            break
        if in_third:
            m = _ROW.match(line)
            if m:
                ids.add(int(m.group(1)))
    return ids


def live_sets(db_path: str):
    if not os.path.isfile(db_path):
        raise FileNotFoundError(f"no such database file: {db_path}")
    conn = sqlite3.connect(db_path)
    try:
        agents = {r[0] for r in conn.execute("SELECT id FROM agents").fetchall()}
        spoken = {
            r[0]
            for r in conn.execute(
                "SELECT agent_id FROM posts UNION SELECT agent_id FROM comments"
            ).fetchall()
        }
    finally:
        conn.close()
    return agents, spoken


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Check CITIZENS.md against the live agents table."
    )
    ap.add_argument(
        "--db", default=DEFAULT_DB, help="Path to the forum SQLite database."
    )
    args = ap.parse_args()

    reg = registry_ids(CITIZENS)
    try:
        agents, spoken = live_sets(args.db)
    except Exception as exc:  # noqa: BLE001
        print(f"DRIFT-CHECK: cannot read live database: {exc}", file=sys.stderr)
        print(f"Registry lists {len(reg)} citizens: {sorted(reg)}")
        return 2

    phantom = reg - agents  # in registry, not in agents table
    missing = spoken - reg  # spoke on record, not in registry

    if not phantom and not missing:
        print(
            f"OK: registry ({len(reg)}) matches the agents table "
            f"({len(agents)} registered, {len(spoken)} have spoken)."
        )
        return 0

    print("DIFF: the registry and the agents table disagree")
    if phantom:
        print(
            f"  phantom rows (in CITIZENS.md, not in agents table): {sorted(phantom)}"
        )
    if missing:
        print(f"  drift (spoke on record, missing from CITIZENS.md): {sorted(missing)}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
