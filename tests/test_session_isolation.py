"""Session-mode DB reuse isolation (D1 regression).

run_all --session shares one DB per parallel worker, truncating data
between files via tests._setup._truncate_all instead of 60 mkdtemp +
full init_db boots. That optimization was shipped (D1, PR #668) and
then reverted (PR #671) because _truncate_all leaked karma/vote daily
caps across files: it failed to delete the top-level `votes` table, so
leftover vote rows from a prior suite inflated the next suite's
_daily_votes_used / votes_cast for agents on the same worker DB.

This test pins the fix: the session truncate path must fully reset
post/comment voting state so a "next file" sees a clean vote budget.
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_session_iso_"))
_db_path = str(_TMP / "forum.db")
os.environ["FORUM_DB_PATH"] = _db_path
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)
# Force the shared-DB session path so init() goes through _truncate_all.
os.environ["AGENTLAND_SESSION"] = "1"
os.environ["AGENTLAND_SESSION_DB_PATH"] = _db_path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, init  # noqa: E402


def _non_empty_tables() -> set:
    """Tables holding rows, FTS shadows folded into their root."""
    with db._conn() as conn:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
        roots = [t for t in tables if t.endswith("_fts")]
        skip = {
            r + s
            for r in roots
            for s in ("_data", "_idx", "_content", "_docsize", "_config")
        }
        out = set()
        for t in tables:
            if t in skip:
                continue
            try:
                n = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            except Exception:
                continue
            if n > 0:
                out.add(t)
        return out


def main():
    # First "file" on this worker DB: an agent casts post/comment votes.
    init()
    a = db.register_agent("session-iso-a")
    b = db.register_agent("session-iso-b")
    p = db.create_post(a["token"], "isolation post", "body")
    c = db.create_comment(b["token"], p["post_id"], "hello")
    db.vote(a["token"], "comment", c["comment_id"], 1)
    db.vote(b["token"], "post", p["post_id"], 1)
    from db import _agent as _ag

    with db._conn() as conn:
        assert _ag._daily_votes_used(conn, a["agent_id"]) >= 1, (
            "precondition: the vote budget reflects cast votes"
        )
        assert _ag._daily_votes_used(conn, b["agent_id"]) >= 1

    # Second "file" on the same worker DB: init() truncates and reseeds -
    # the vote budget must come back clean, not carry the prior file's votes.
    init()
    a2 = db.register_agent("session-iso-a")
    with db._conn() as conn:
        votes_after = conn.execute("SELECT COUNT(*) FROM votes").fetchone()[0]
        assert votes_after == 0, (
            f"votes table must be fully truncated between session files, "
            f"got {votes_after} residual rows"
        )
        assert _ag._daily_votes_used(conn, a2["agent_id"]) == 0, (
            "a freshly-registered agent in the next session file must see a "
            "clean vote budget (D1 regression)"
        )
    # votes_cast for the new agent is drawn from votes + proposal_votes,
    # so the same truncation keeps it at zero until the agent votes again.
    assert db.my_profile(a2["token"])["votes_cast"] == 0
    # Dynamic-truncate pin (session-default program): the post-init
    # non-empty set must equal the genesis baseline no matter what the
    # prior "file" wrote - no hardcoded table list left to drift. Dirty
    # the tables the old hardcoded list missed (tags, polls) plus the
    # classic surfaces, then re-init on the same worker DB.
    init()
    baseline = _non_empty_tables()
    c = db.register_agent("session-iso-c")
    d = db.register_agent("session-iso-d")
    p2 = db.create_post(c["token"], "third file post", "body")
    c2 = db.create_comment(d["token"], p2["post_id"], "hello again")
    db.vote(c["token"], "comment", c2["comment_id"], 1)
    # Tag creation needs 2 effective karma: earn it with two post votes.
    p3 = db.create_post(c["token"], "third file second post", "body")
    db.vote(d["token"], "post", p2["post_id"], 1)
    db.vote(d["token"], "post", p3["post_id"], 1)
    db.create_tag(c["token"], "session-iso-tag")
    db.create_poll(c["token"], p2["post_id"], "third file poll?", ["yes", "no"], 1.0)
    dirtied = _non_empty_tables()
    assert "tags" in dirtied and "polls" in dirtied, (
        "precondition: the third file must dirty tags + polls"
    )
    assert dirtied > baseline, "precondition: the third file must add rows"
    init()
    after = _non_empty_tables()
    assert after == baseline, (
        "session truncate leaked tables across files: "
        f"extra={sorted(after - baseline)} missing={sorted(baseline - after)}"
    )


if __name__ == "__main__":
    main()
    print("test_session_isolation: all ok")
