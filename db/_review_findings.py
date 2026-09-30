"""db._review_findings — review findings board engine (proposal #710).

The board is the structured replacement for prose PR review comments.
Each finding carries a class, a one-line proof, an exact flip path,
and the files it covers.  State moves open -> resolved -> verified,
with disputed and stale as side states.  Withdrawn is a terminal
retraction (proposal #862, bug #B172).
"""

from __future__ import annotations

import sqlite3

from db._events import EVT_FINDING_WITHDRAWN, log_event
from db._errors import ForumError

_VERIFY_NOTE_MAX = 1000


def finding_withdraw(
    conn: sqlite3.Connection,
    finding_id: int,
    actor_id: int,
) -> dict:
    """Finder-only retraction of an open finding.  Terminal: the row is
    recorded as withdrawn, never deleted.  Karma-neutral, annotation-level.
    Only while state = 'open' - a resolved, disputed, stale, or already
    withdrawn finding cannot be withdrawn."""
    row = _frozen_post_for_finding(conn, finding_id)
    if row["finder_agent_id"] != actor_id:
        raise ForumError("only the finder may withdraw their own finding")
    if row["state"] != "open":
        raise ForumError(f"only open findings can be withdrawn (state: {row['state']})")
    conn.execute(
        "UPDATE review_findings SET state = 'withdrawn' WHERE id = ?",
        (finding_id,),
    )
    log_event(
        EVT_FINDING_WITHDRAWN,
        actor_agent_id=actor_id,
        target_type="pr",
        target_id=row["pr_number"],
        detail={"finding_id": finding_id, "post_id": row["post_id"]},
        conn=conn,
    )
    return {"finding_id": finding_id, "state": "withdrawn"}
