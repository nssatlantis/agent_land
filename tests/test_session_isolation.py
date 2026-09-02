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


if __name__ == "__main__":
    main()
    print("test_session_isolation: all ok")
