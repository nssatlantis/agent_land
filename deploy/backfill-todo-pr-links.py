#!/opt/agent_land_data/venv/bin/python
"""One-off backfill: restore pr_number on done to-do items cleared on merge.

Before PR #602, record_proposal_outcome cleared todo_items.pr_number on
merge (done=1, pr_number=NULL). After #602, merged items keep pr_number for
audit (your Plan A: save PR number unless closed/declined). This script
re-links the ~60 done:true items on proposal #237 (and any other) where
pr_number was cleared but the PR is merged.

It walks todo_edits for each post (the edit trail already stores pr_number
per item) and, for each done:true item with pr_number IS NULL, finds the
most recent edit where that item had a non-null pr_number. If that
pr_number is a merged PR for the same post (proposal_outcomes status='merged'),
the item is restored. Declined/closed PRs stay NULL (re-linkable) and are
skipped — matching #602's "anything but merged" rule.

Idempotent and dry-run by default; use --apply to write.

Usage:
    python deploy/backfill-todo-pr-links.py [--post-id 237] [--apply]

Exit codes: 0 backfilled (or dry-run would backfill), 2 refused/misconfigured.
"""

import argparse
import json
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


def _last_pr_for_item(post_id: int, item_id: int, edits: list[dict]) -> int | None:
    # edits are ordered by id ASC (oldest first); walk newest first to find
    # the last time this item had a pr_number.
    for ed in reversed(edits):
        try:
            new_lists = json.loads(ed["new_lists"])
        except Exception:
            continue
        for lst in new_lists:
            for it in lst.get("items") or []:
                if it.get("id") == item_id and it.get("pr_number"):
                    try:
                        return int(it["pr_number"])
                    except Exception:
                        continue
        try:
            old_lists = json.loads(ed["old_lists"])
        except Exception:
            continue
        for lst in old_lists:
            for it in lst.get("items") or []:
                if it.get("id") == item_id and it.get("pr_number"):
                    try:
                        return int(it["pr_number"])
                    except Exception:
                        continue
    return None


def _main() -> int:
    ap = argparse.ArgumentParser(description="Backfill pr_number on done todos cleared on merge.")
    ap.add_argument("--post-id", type=int, default=None, help="only this proposal (default: all)")
    ap.add_argument("--apply", action="store_true", help="write; without flag this is dry-run")
    args = ap.parse_args()

    repo_dir = _find_repo()
    _config = _import_config(repo_dir)
    if pathlib.Path(_config.DB_PATH).resolve().is_relative_to(repo_dir.resolve()):
        print(f"ERROR: DB {_config.DB_PATH} inside repo {repo_dir}; refusing.", file=sys.stderr)
        return 2
    sys.path.insert(0, str(repo_dir))
    try:
        import db
        from db._core import _conn
    finally:
        sys.path.pop(0)

    with _conn() as conn:
        # Find candidate posts: either filtered or all with done:true null pr_number.
        if args.post_id is not None:
            post_ids = [args.post_id]
        else:
            rows = conn.execute(
                "SELECT DISTINCT tl.post_id FROM todo_items ti "
                "JOIN todo_lists tl ON tl.id = ti.list_id "
                "WHERE ti.done = 1 AND ti.pr_number IS NULL"
            ).fetchall()
            post_ids = [r["post_id"] for r in rows]
            if not post_ids:
                print("No done:true items with pr_number IS NULL — nothing to backfill.")
                return 0

        total_would = 0
        total_did = 0
        for pid in post_ids:
            # All done:true null items for this post.
            items = conn.execute(
                "SELECT ti.id, ti.text FROM todo_items ti "
                "JOIN todo_lists tl ON tl.id = ti.list_id "
                "WHERE tl.post_id = ? AND ti.done = 1 AND ti.pr_number IS NULL",
                (pid,),
            ).fetchall()
            if not items:
                continue
            # All edits for this post, ordered.
            edits = conn.execute(
                "SELECT old_lists, new_lists FROM todo_edits WHERE post_id = ? ORDER BY id",
                (pid,),
            ).fetchall()
            # Merged PRs for this post (for audit, only restore if merged).
            merged = {
                r["pr_number"]
                for r in conn.execute(
                    "SELECT po.pr_number FROM proposal_outcomes po "
                    "JOIN proposal_links pl ON pl.pr_number = po.pr_number "
                    "WHERE pl.post_id = ? AND po.status = 'merged'",
                    (pid,),
                ).fetchall()
            }
            for it in items:
                pr = _last_pr_for_item(pid, it["id"], edits)
                if pr is None:
                    continue
                if pr not in merged:
                    # Declined/closed PRs stay NULL per Plan A (anything but merged).
                    continue
                total_would += 1
                if args.apply:
                    conn.execute("UPDATE todo_items SET pr_number = ? WHERE id = ?", (pr, it["id"]))
                    total_did += 1
                    print(f"post {pid} item #{it['id']} ({it['text'][:40]!r}) -> PR #{pr}")
                else:
                    print(f"[dry-run] post {pid} item #{it['id']} ({it['text'][:40]!r}) would -> PR #{pr}")

        if args.apply:
            conn.commit()
            print(f"backfill complete: {total_did} restored ({total_would} candidates).")
        else:
            print(f"dry-run complete: {total_would} would be restored; re-run with --apply to write.")
        return 0


if __name__ == "__main__":
    sys.exit(_main())
