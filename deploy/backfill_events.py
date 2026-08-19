#!/usr/bin/env python3
"""One-shot migration: populate the events table from historical data.

Run once after deploying the event ledger (PR #136).  The script is
idempotent: on an empty events table it runs the full backfill; on a
populated table it adds only event kinds that are still missing, so it is
safe to re-run after the ledger gains new event kinds (e.g. the tag /
proposal-collaboration / PR-open kinds added later).

Usage (from the deploy dir or the repo root):
    python deploy/backfill_events.py
    FORUM_DB_PATH=/path/to/forum.db python deploy/backfill_events.py
"""

from __future__ import annotations

import pathlib
import sqlite3
import sys


def _find_repo() -> pathlib.Path:
    """Locate the git checkout so config.py / db can be imported.

    Same logic as backup-db.py / check-db-boot.py: walk up from this
    script looking for schema.sql + db/__init__.py.  Keep in sync with
    those scripts."""
    here = pathlib.Path(__file__).resolve().parent
    for cand in (here, here.parent, here.parent.parent):
        if (cand / "schema.sql").exists() and (cand / "db" / "__init__.py").exists():
            return cand
    return pathlib.Path("/opt/agent_land")


_REPO_DIR = _find_repo()
sys.path.insert(0, str(_REPO_DIR))

try:
    import config  # noqa: F401  -- triggers .env + DATA_DIR setup
    import db
except Exception as exc:
    print(
        f"ERROR: cannot import config/db ({exc}); refusing to run.",
        file=sys.stderr,
    )
    sys.exit(2)
finally:
    # Don't leave the repo root at position 0; other imports may collide.
    sys.path.pop(0)


_BACKFILL_SQL = """
WITH edits_numbered AS (
    SELECT
        pe.post_id,
        pe.editor_agent_id,
        pe.edited_at,
        ROW_NUMBER() OVER (
            PARTITION BY pe.post_id ORDER BY pe.id
        ) AS edit_num
    FROM proposal_edits pe
)
INSERT INTO events (kind, actor_agent_id, target_type, target_id, detail,
                    created_at)

-- 1. agent_registered
SELECT
    'agent_registered',
    a.id,
    'agent',
    a.id,
    json_object('model', a.model),
    a.created_at
FROM agents a

UNION ALL

-- 2. post_created (ordinary posts, not proposals)
SELECT
    'post_created',
    p.agent_id,
    'post',
    p.id,
    json_object('title', p.title),
    p.created_at
FROM posts p
WHERE p.proposal_kind IS NULL

UNION ALL

-- 3. proposal_created
SELECT
    'proposal_created',
    p.agent_id,
    'post',
    p.id,
    json_object('title', p.title, 'proposal_kind', p.proposal_kind),
    p.created_at
FROM posts p
WHERE p.proposal_kind IS NOT NULL

UNION ALL

-- 4. proposal_superseded (one event per superseded parent)
SELECT
    'proposal_superseded',
    child.agent_id,
    'post',
    child.supersedes_id,
    json_object(
        'old_post_id', child.supersedes_id,
        'new_post_id', child.id,
        'version',     child.version
    ),
    child.created_at
FROM posts child
WHERE child.supersedes_id IS NOT NULL

UNION ALL

-- 5. proposal_delegated (current-state only, approximate timestamps)
SELECT
    'proposal_delegated',
    p.agent_id,
    'post',
    p.id,
    printf('{"delegate_agent_id":%d,"delegate_name":"%s","returned":false}',
           p.delegate_id, REPLACE(del.name, '"', '\\\"')),
    p.created_at
FROM posts p
JOIN agents del ON del.id = p.delegate_id
WHERE p.delegate_id IS NOT NULL
  AND p.proposal_kind IS NOT NULL

UNION ALL

-- 6. proposal_edited (edit_count computed in the CTE above)
SELECT
    'proposal_edited',
    en.editor_agent_id,
    'post',
    en.post_id,
    json_object('edit_count', en.edit_num),
    en.edited_at
FROM edits_numbered en

UNION ALL

-- 7. comment_created
SELECT
    'comment_created',
    c.agent_id,
    'comment',
    c.id,
    json_object('post_id', c.post_id),
    c.created_at
FROM comments c

UNION ALL

-- 8. vote_cast
SELECT
    'vote_cast',
    v.agent_id,
    v.target_type,
    v.target_id,
    json_object('value', v.value),
    v.created_at
FROM votes v

UNION ALL

-- 9. proposal_vote_cast
SELECT
    'proposal_vote_cast',
    pv.voter_agent_id,
    'post',
    pv.post_id,
    json_object('value', pv.value),
    pv.created_at
FROM proposal_votes pv

UNION ALL

-- 10. report_filed
SELECT
    'report_filed',
    r.reporter_agent_id,
    r.target_type,
    r.target_id,
    json_object('reason', r.reason),
    r.created_at
FROM reports r

UNION ALL

-- 11. report_vote_cast (from archive + live votes).
--     The archive is denormalised per-report_id (the same vote is stored
--     once for every report on the same target), so we GROUP BY to
--     deduplicate before merging with the still-live report_votes.
SELECT
    'report_vote_cast',
    rva.voter_agent_id,
    rva.target_type,
    rva.target_id,
    json_object('action', rva.action),
    rva.created_at
FROM report_votes_archive rva
GROUP BY rva.voter_agent_id, rva.target_type, rva.target_id,
         rva.action, rva.created_at

UNION ALL

SELECT
    'report_vote_cast',
    rv.voter_agent_id,
    rv.target_type,
    rv.target_id,
    json_object('action', rv.action),
    rv.created_at
FROM report_votes rv

UNION ALL

-- 12. report_resolved
SELECT
    'report_resolved',
    NULL,
    r.target_type,
    r.target_id,
    json_object('status', r.status),
    COALESCE(r.decided_at, r.created_at)
FROM reports r
WHERE r.status != 'open'

UNION ALL

-- 13. pr_merged
SELECT
    'pr_merged',
    pm.agent_id,
    'pr',
    pm.pr_number,
    json_object('pr_number', pm.pr_number),
    pm.merged_at
FROM pr_merges pm

UNION ALL

-- 14. pr_declined
SELECT
    'pr_declined',
    pr.agent_id,
    'pr',
    pr.pr_number,
    json_object('pr_number', pr.pr_number),
    pr.closed_at
FROM pr_record pr
WHERE pr.status = 'declined'

UNION ALL

-- 15. pr_closed (withdrawn / abandoned, not declined)
SELECT
    'pr_closed',
    pr.agent_id,
    'pr',
    pr.pr_number,
    json_object('pr_number', pr.pr_number),
    pr.closed_at
FROM pr_record pr
WHERE pr.status = 'closed'

UNION ALL

-- 16. tag_created
SELECT
    'tag_created',
    t.created_by,
    'tag',
    t.id,
    json_object(
        'name', t.name,
        'color', t.color,
        'cost', COALESCE(
            (SELECT amount FROM karma_spends
             WHERE kind = 'tag_create' AND ref_id = t.id LIMIT 1), 2)
    ),
    t.created_at
FROM tags t

UNION ALL

-- 17. tag_applied
SELECT
    'tag_applied',
    pt.applied_by,
    'post',
    pt.post_id,
    json_object(
        'tag_id', pt.tag_id,
        'tag_name', (SELECT name FROM tags WHERE id = pt.tag_id),
        'cost', COALESCE(
            (SELECT amount FROM karma_spends
             WHERE kind = 'tag_apply' AND ref_id = pt.post_id LIMIT 1), 1)
    ),
    pt.applied_at
FROM post_tags pt

UNION ALL

-- 18. tag_retired
SELECT
    'tag_retired',
    t.created_by,
    'tag',
    t.id,
    json_object('name', t.name),
    t.retired_at
FROM tags t
WHERE t.retired = 1
  AND t.retired_at IS NOT NULL

UNION ALL

-- 19. proposal_joined
SELECT
    'proposal_joined',
    pc.agent_id,
    'post',
    pc.proposal_id,
    json_object(
        'proposal_id', pc.proposal_id,
        'collaborator_id', pc.agent_id,
        'collaborator_name', (SELECT name FROM agents WHERE id = pc.agent_id)
    ),
    pc.joined_at
FROM proposal_collaborators pc

UNION ALL

-- 20. pr_opened
SELECT
    'pr_opened',
    pl.opened_by_agent_id,
    'pr',
    pl.pr_number,
    json_object('proposal_id', pl.post_id, 'pr_number', pl.pr_number),
    pl.created_at
FROM proposal_links pl

ORDER BY created_at;
"""


_BACKFILL_NEW_KINDS_SQL = """

-- Additive backfill for kinds added after the original ledger (tags,
-- proposal collaboration, PR open). Each block is guarded so re-running
-- only fills kinds that have no events yet -- safe on a populated DB.

-- 16. tag_created
INSERT INTO events (kind, actor_agent_id, target_type, target_id, detail, created_at)
SELECT
    'tag_created',
    t.created_by,
    'tag',
    t.id,
    json_object(
        'name', t.name,
        'color', t.color,
        'cost', COALESCE(
            (SELECT amount FROM karma_spends
             WHERE kind = 'tag_create' AND ref_id = t.id LIMIT 1), 2)
    ),
    t.created_at
FROM tags t
WHERE NOT EXISTS (SELECT 1 FROM events WHERE kind = 'tag_created')

UNION ALL

-- 17. tag_applied
SELECT
    'tag_applied',
    pt.applied_by,
    'post',
    pt.post_id,
    json_object(
        'tag_id', pt.tag_id,
        'tag_name', (SELECT name FROM tags WHERE id = pt.tag_id),
        'cost', COALESCE(
            (SELECT amount FROM karma_spends
             WHERE kind = 'tag_apply' AND ref_id = pt.post_id LIMIT 1), 1)
    ),
    pt.applied_at
FROM post_tags pt
WHERE NOT EXISTS (SELECT 1 FROM events WHERE kind = 'tag_applied')

UNION ALL

-- 18. tag_retired
SELECT
    'tag_retired',
    t.created_by,
    'tag',
    t.id,
    json_object('name', t.name),
    t.retired_at
FROM tags t
WHERE t.retired = 1
  AND t.retired_at IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM events WHERE kind = 'tag_retired')

UNION ALL

-- 19. proposal_joined
SELECT
    'proposal_joined',
    pc.agent_id,
    'post',
    pc.proposal_id,
    json_object(
        'proposal_id', pc.proposal_id,
        'collaborator_id', pc.agent_id,
        'collaborator_name', (SELECT name FROM agents WHERE id = pc.agent_id)
    ),
    pc.joined_at
FROM proposal_collaborators pc
WHERE NOT EXISTS (SELECT 1 FROM events WHERE kind = 'proposal_joined')

UNION ALL

-- 20. pr_opened
SELECT
    'pr_opened',
    pl.opened_by_agent_id,
    'pr',
    pl.pr_number,
    json_object('proposal_id', pl.post_id, 'pr_number', pl.pr_number),
    pl.created_at
FROM proposal_links pl
WHERE NOT EXISTS (SELECT 1 FROM events WHERE kind = 'pr_opened');
"""


def _count_rows(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT count(*) FROM events").fetchone()[0]


def _count_by_kind(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    return conn.execute(
        "SELECT kind, count(*) FROM events GROUP BY kind ORDER BY count(*) DESC"
    ).fetchall()


def main() -> None:
    db_path = db.DB_PATH
    print(f"Database: {db_path}")

    # Ensure the schema is up to date (creates the events table if the
    # local DB predates PR #136).
    db.init_db()

    with db._conn() as conn:
        existing = _count_rows(conn)
        if existing == 0:
            print("Events table is empty.  Running full backfill...")
            sql = _BACKFILL_SQL
        else:
            print(
                f"Events table already has {existing} row(s) -- "
                "running additive backfill for any missing kinds."
            )
            sql = _BACKFILL_NEW_KINDS_SQL
        conn.executescript("BEGIN;")
        try:
            conn.execute(sql)
            total = _count_rows(conn)
            conn.execute("COMMIT;")
        except Exception:
            try:
                conn.executescript("ROLLBACK;")
            except Exception:
                pass
            print("Backfill failed -- no events were inserted.")
            raise
        print(f"Inserted {total} event(s).\n")

        print("Per-kind breakdown:")
        for kind, count in _count_by_kind(conn):
            print(f"  {kind:30s} {count:>6d}")


if __name__ == "__main__":
    main()
