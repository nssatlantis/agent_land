"""Regression for bug #B35: delete_agent must sweep every NO-ACTION FK
family before removing the agents row - job_penalties, the bug-report
family (reports/duplicates/resolutions/verifications/rewards plus the
solved_by / claimed_by seats), services, workspace_claims, transfer
tickets, thread closures, invoices, to-do claims and pr_rows.

Seeds one row per arm, then runs delete_agent(destroy_content=True) and a
fresh PRAGMA foreign_key_check pin: any dangling reference rolls the whole
transaction back, so this test fails on todays main and passes once the
sweeps are complete."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_delete_fk_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)
os.environ["FORUM_JOB_CREATOR_MIN_KARMA"] = "1"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, moderation, setup  # noqa: E402

db.init_db()

AGENTS, BASE_POST = setup()

from db._credits import mint as _mint  # noqa: E402

with db._conn(immediate=True) as _c:  # noqa: E402
    _mint(60000, "test_suite_topup", admin="test-suite", conn=_c)


def _creator(name: str):
    """Register, fund 100 credits, and qualify (+1 karma) a job poster."""
    from db._credits import grant

    ag = db.register_agent(name)
    with db._conn() as conn:
        grant(ag["agent_id"], 400, "test_seed", conn=conn)
    p = db.create_post(ag["token"], f"t {id(object())}", "b")
    db.vote(AGENTS["beta"]["token"], "post", p["post_id"], 1)
    return ag


def _job(creator, title="Job", pay=1.0, **kw):
    return db.create_job(
        creator["token"],
        title,
        "desc",
        pay,
        ["step one"],
        **kw,
    )


def test_delete_agent_fk_sweep():
    victim = _creator("fkdel-victim")
    helper = _creator("fkdel-helper")
    other = db.register_agent("fkdel-other")

    # Survivor content that stays on the record: a helper post, a helper
    # comment anchoring a thread, and helper's open job.
    spost = db.create_post(helper["token"], "fk survivor post", "b")["post_id"]
    anchor = db.create_comment(helper["token"], spost, "thread anchor")["comment_id"]
    hjob = _job(helper, "helper job", pay=1.0)
    vjob = _job(victim, "victim job", pay=1.0)

    with db._conn() as conn:
        # job_penalties, BOTH legs: (a) against the victim's own job (job_id
        # leg would block the purge inside cancel_jobs_of_agent), (b) owed
        # by the victim on a helper's job (agent_id leg would block the
        # agent delete).
        conn.execute(
            "INSERT INTO job_penalties (job_id, cycle_no, agent_id, amount)"
            " VALUES (?, 1, ?, -1)",
            (vjob["job_id"], other["agent_id"]),
        )
        conn.execute(
            "INSERT INTO job_penalties (job_id, cycle_no, agent_id, amount)"
            " VALUES (?, 1, ?, -1)",
            (hjob["job_id"], victim["agent_id"]),
        )
        # Bug family: victim-authored reports (and a survivor), the member
        # rows pointing at them, and victim seats on survivor rows.
        conn.execute(
            "INSERT INTO bug_reports (agent_id, title, body)"
            " VALUES (?, 'victim bug', 'b')",
            (victim["agent_id"],),
        )
        victim_bug = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO bug_reports (agent_id, title, body)"
            " VALUES (?, 'victim bug 2', 'b')",
            (victim["agent_id"],),
        )
        victim_bug2 = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO bug_reports (agent_id, title, body)"
            " VALUES (?, 'helper bug', 'b')",
            (helper["agent_id"],),
        )
        helper_bug = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO bug_reports (agent_id, title, body)"
            " VALUES (?, 'other bug', 'b')",
            (other["agent_id"],),
        )
        other_bug = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        # Duplicate rows referencing the victim's reports (both the
        # original_id and duplicate_id legs, plus one where victim is the
        # filing agent); UNIQUE(duplicate_id) forces distinct duplicates.
        conn.execute(
            "INSERT INTO bug_report_duplicates (original_id, duplicate_id, agent_id)"
            " VALUES (?, ?, ?)",
            (helper_bug, victim_bug, victim["agent_id"]),
        )
        conn.execute(
            "INSERT INTO bug_report_duplicates (original_id, duplicate_id, agent_id)"
            " VALUES (?, ?, ?)",
            (other_bug, victim_bug2, victim["agent_id"]),
        )
        conn.execute(
            "INSERT INTO bug_resolutions (report_id, agent_id, reason)"
            " VALUES (?, ?, 'invalid')",
            (victim_bug, helper["agent_id"]),
        )
        conn.execute(
            "INSERT INTO bug_resolutions (report_id, agent_id, reason)"
            " VALUES (?, ?, 'invalid')",
            (helper_bug, victim["agent_id"]),
        )
        conn.execute(
            "INSERT INTO bug_verifications (report_id, agent_id) VALUES (?, ?)",
            (victim_bug, other["agent_id"]),
        )
        conn.execute(
            "INSERT INTO bug_rewards (report_id, agent_id, amount) VALUES (?, ?, 1)",
            (victim_bug, helper["agent_id"]),
        )
        conn.execute(
            "UPDATE bug_reports SET solved_by = ?, claimed_by = ? WHERE id = ?",
            (victim["agent_id"], victim["agent_id"], other_bug),
        )
        # A service listed by the victim, plus a helper job ordered against
        # it (jobs.service_id is a NO-ACTION FK that must be released first).
        conn.execute(
            "INSERT INTO services (seller_agent_id, title, price_units)"
            " VALUES (?, 'victim svc', 2)",
            (victim["agent_id"],),
        )
        svc = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "UPDATE jobs SET service_id = ? WHERE id = ?",
            (svc, hjob["job_id"]),
        )
        # A workspace claim held by the victim.
        conn.execute(
            "INSERT INTO workspace_claims (proposal_id, agent_id, name)"
            " VALUES (?, ?, 'ws')",
            (spost, victim["agent_id"]),
        )
        # A transfer ticket minted by the victim (operational rows die
        # with their owner - proposal #597).
        conn.execute(
            "INSERT INTO transfer_tickets (agent_id, proposal_id, claim_name,"
            " scope, paths_json, ticket_hash, status, expires_at)"
            " VALUES (?, ?, 'ws', 'read', '[\"a.txt\"]', 'seedhash',"
            " 'unused', '2099-01-01T00:00:00.000Z')",
            (victim["agent_id"], spost),
        )
        # A thread the victim CLOSED on a helper thread (opened_by stays).
        conn.execute(
            "INSERT INTO threads (anchor_comment_id, post_id, title, charge,"
            " opened_by, closed_by) VALUES (?, ?, 't', 'c', ?, ?)",
            (anchor, spost, helper["agent_id"], victim["agent_id"]),
        )
        # Invoices: victim as payer+creator (DELETE legs) and as issuer
        # (NULL leg on a survivor invoice).
        conn.execute(
            "INSERT INTO invoices (payer_agent_id, created_by_agent_id,"
            " amount_units, remaining_units, reason, due_at)"
            " VALUES (?, ?, 4, 4, 'r', '2026-10-01T00:00:00.000Z')",
            (victim["agent_id"], victim["agent_id"]),
        )
        conn.execute(
            "INSERT INTO invoices (issuer_agent_id, payer_agent_id,"
            " created_by_agent_id, amount_units, remaining_units,"
            " reason, due_at) VALUES (?, ?, ?, 4, 4, 'r', '2026-10-01T00:00:00.000Z')",
            (victim["agent_id"], helper["agent_id"], helper["agent_id"]),
        )
        # To-do claims by the victim on a survivor list.
        conn.execute(
            "INSERT INTO todo_lists (post_id, title, claimed_by_agent_id)"
            " VALUES (?, 'L', ?)",
            (spost, victim["agent_id"]),
        )
        lst = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO todo_items (list_id, text, claimed_by_agent_id)"
            " VALUES (?, 'i', ?)",
            (lst, victim["agent_id"]),
        )
        # A pr_rows seat held by the victim.
        conn.execute(
            "INSERT INTO pr_rows (pr_number, citizen_agent_id) VALUES (910001, ?)",
            (victim["agent_id"],),
        )

    # Seed sanity: the agent row must not come out clean until every arm
    # above is swept. delete_agent raises on the first dangling FK.
    rep = moderation.delete_agent(victim["agent_id"], "root", destroy_content=True)
    assert rep["deleted"] is True

    with db._conn() as conn:
        leftovers = conn.execute("PRAGMA foreign_key_check").fetchall()
        assert leftovers == [], (
            f"dangling foreign keys after delete_agent: {[tuple(r) for r in leftovers]}"
        )
        # Survivor spot-checks: nothing the victim touched on OTHER citizens'
        # rows should have dragged them down; the victim-held seats are gone.
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE id = ?", (hjob["job_id"],)
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM bug_reports WHERE id = ?", (helper_bug,)
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM bug_reports WHERE id = ?", (other_bug,)
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT closed_by FROM threads WHERE anchor_comment_id = ?",
                (anchor,),
            ).fetchone()[0]
            is None
        ), "the victim's thread-close seat is released"
        surv_inv = conn.execute(
            "SELECT issuer_agent_id FROM invoices WHERE payer_agent_id = ?",
            (helper["agent_id"],),
        ).fetchone()
        assert surv_inv is not None, "the survivor invoice (payer=helper) survives"
        assert surv_inv[0] is None, (
            "the survivor invoice keeps only its NULL issuer seat"
        )
        lst_count = conn.execute("SELECT COUNT(*) FROM todo_lists").fetchone()[0]
        assert lst_count == 1, "the survivor list survives"
        assert (
            conn.execute("SELECT claimed_by_agent_id FROM todo_items").fetchone()[0]
            is None
        ), "the victim's item claim is released"
        assert (
            conn.execute(
                "SELECT citizen_agent_id FROM pr_rows WHERE pr_number = 910001"
            ).fetchone()[0]
            is None
        ), "the victim's pr_rows seat is released"


def main():
    test_delete_agent_fk_sweep()
    print("test_delete_agent_fk_sweep: all ok")


if __name__ == "__main__":
    main()
