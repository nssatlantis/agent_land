#!/usr/bin/env python3
"""check-record-size.py — keep the record .md files within their budget.

The record (CHARTER.md Article VIII) must stay readable across ages, and
its files are kept at the shortest true version (AGENTS.md). This script
makes their size visible: it prints a WARNING for every record file over
the budget, then exits 0 — a nudge, not a gate.

Exit codes (so the same file can become a CI gate without a rewrite):
  0  checked (any over-budget file is only warned about)
  1  at least one record file over budget, under --strict
  2  the --repo directory is missing, or holds no record files

--strict turns the warning into an error for manual / test use. Stdlib only.
"""
import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MAX = 65536  # 64 KiB

RECORD_FILES = [
    "CHARTER.md",
    "AGENTS.md",
    "HISTORY.md",
    "CITIZENS.md",
    "REASONING.md",
    "README.md",
    "deploy/README.md",
    "deploy/disaster-drill.md",
]


def record_sizes(repo_root: Path):
    """(name, size) for every record file present; absent files are skipped."""
    sizes = []
    for name in RECORD_FILES:
        path = repo_root / name
        if path.is_file():
            sizes.append((name, path.stat().st_size))
    return sizes


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Warn when a record .md file outgrows its budget."
    )
    ap.add_argument("--repo", type=Path, default=REPO_ROOT,
                    help="Repository root (default: the repo this script lives in).")
    ap.add_argument("--max", type=int, default=DEFAULT_MAX, metavar="BYTES",
                    help=f"Budget per record file in bytes (default: {DEFAULT_MAX}).")
    ap.add_argument("--strict", action="store_true",
                    help="Exit 1 when any record file is over the budget.")
    args = ap.parse_args()

    if not args.repo.is_dir():
        print(f"RECORD-SIZE: no such repository directory: {args.repo}",
              file=sys.stderr)
        return 2

    sizes = record_sizes(args.repo)
    if not sizes:
        print("RECORD-SIZE: no record files found (all absent?)", file=sys.stderr)
        return 2

    over = [name for name, size in sizes if size > args.max]
    for name, size in sizes:
        mark = "WARNING" if size > args.max else "ok"
        print(f"RECORD-SIZE: {name} {size} bytes {mark}")
    for name, size in sizes:
        if size > args.max:
            print(
                f"RECORD-SIZE: {name} is {size} bytes, over the {args.max}-byte "
                f"budget - the shortest true version wins (compress it).",
                file=sys.stderr,
            )
    return 1 if over and args.strict else 0


if __name__ == "__main__":
    sys.exit(main())
