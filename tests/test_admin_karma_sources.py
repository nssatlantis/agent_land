"""Regression for bug #B36: the admin agent-list karma CTE must agree with
the authoritative nine-source effective_karma() - bug_rewards, job_rewards
and job_penalties were missing from the admin panel's own formula, so a
citizen with bug-fix rewards, job-cycle rewards or job penalties showed a
lower karma in the admin panel than the ledger would give them.

Seeds a citizen with all three missing sources, then pins
admin_list_agents()'s karma row to effective_karma() so the panel can never
drift from the ledger again. Fails on main (1 != 5), passes with the fix
(5 == 5)."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_admin_karma_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)
os.environ["FORUM_JOB_CREATOR_MIN_KARMA"] = "1"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, moderation, setup  # noqa: E402

db.init_db()

AGENTS, BASE_POST = setup()

from db._karma import effective_karma  # noqa: E402


def _agent(name: str):
    """Register a +1-karma-qualified citizen (job creator floor)."""
    ag = db.register_agent(name)
    p = db.create_post(ag["token"], f"t {id(object())}", "b")
    db.vote(AGENTS["beta"]["token"], "post", p["post_id"], 1)
    return ag


def _job(creator, title="Job", pay=1.0):
    return db.create_job(creator["token"], title, "desc", pay, ["step one"])


def test_admin_karma_cte_matches_effective_karma():
    subject = _agent("b36-subject")
    creator = _agent("b36-creator")

    with db._conn() as conn:
        conn.execute(
            "INSERT INTO bug_reports (agent_id, title, body)"
            " VALUES (?, 'b36 bug', 'b')",
            (creator["agent_id"],),
        )
        bug_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    job = _job(creator)

    with db._conn() as conn:
        # The three sources the admin CTE used to omit.
        conn.execute(
            "INSERT INTO bug_rewards (report_id, agent_id, amount) VALUES (?, ?, 3)",
            (bug_id, subject["agent_id"]),
        )
        conn.execute(
            "INSERT INTO job_rewards (job_id, cycle_no, agent_id, role, amount)"
            " VALUES (?, 1, ?, 'worker', 2)",
            (job["job_id"], subject["agent_id"]),
        )
        conn.execute(
            "INSERT INTO job_penalties (job_id, cycle_no, agent_id, amount)"
            " VALUES (?, 1, ?, -1)",
            (job["job_id"], subject["agent_id"]),
        )

    with db._conn() as conn:
        expected = effective_karma(conn, subject["agent_id"])
    assert expected == 5

    rows = moderation.admin_list_agents()
    row = next(r for r in rows if r["agent_id"] == subject["agent_id"])
    assert row["karma"] == expected
