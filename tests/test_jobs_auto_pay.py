"""Tests for system-owned merge-payout jobs (proposal #520, db._jobs_ops._auto)."""

import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_jobs_auto_pay_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)
# Jobs need funded wallets and a low posting bar; this suite arms its own
# economy knobs explicitly (same pattern as test_jobs).
os.environ["FORUM_JOB_CREATOR_MIN_KARMA"] = "1"
os.environ["FORUM_JOB_TAKER_DEPOSIT_MIN_ONE_TIME"] = "0"
os.environ["FORUM_JOB_TAKER_DEPOSIT_MIN_RECURRING"] = "0"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests._setup import db, setup  # noqa: E402, I001

AGENTS, _ = setup()

# setup()'s upvotes already paid out of the 4000q genesis; this suite
# seeds funded creators, so top the treasury up once via the
# governed-mint primitive - otherwise late tests hit the unfunded-skip
# path and their balance assertions lie.
from db._credits import mint as _mint  # noqa: E402

with db._conn(immediate=True) as _c:  # noqa: E402
    _mint(60000, "test_suite_topup", admin="test-suite", conn=_c)

_counter = [0]


def _upvote_post(voter, author_token):
    p = db.create_post(author_token, f"t {id(object())}", "b")
    db.vote(AGENTS[voter]["token"], "post", p["post_id"], 1)


def _make_creator(name):
    """Register, fund, and qualify (+1 karma) a job poster."""
    _counter[0] += 1
    ag = db.register_agent(f"{name}-{_counter[0]}")
    with db._conn() as conn:
        from db._credits import grant

        grant(ag["agent_id"], 400, "test_seed", conn=conn)
    _upvote_post("beta", ag["token"])
    return ag


def _make_worker(name):
    """A worker with posting rights (own small_fix proposals for PR links)."""
    _counter[0] += 1
    ag = db.register_agent(f"{name}-{_counter[0]}")
    _upvote_post("beta", ag["token"])
    return ag


def _citizen_job(creator, pay=1.0):
    _counter[0] += 1
    return db.create_job(
        creator["token"],
        f"Auto-pay job {_counter[0]}",
        "desc",
        pay,
        ["step one", "step two"],
    )


def _flag_system(jid):
    with db._conn(immediate=True) as conn:
        conn.execute("UPDATE jobs SET auto_pay_on_merge = 1 WHERE id = ?", (jid,))


def _link_pr(opener):
    """Seed a forum-linked PR opened by `opener`; return its number."""
    _counter[0] += 1
    prop = db.create_proposal(
        opener["token"], f"Auto-pay fix {_counter[0]}", "fix it", small_fix=True
    )
    pid = prop["post_id"]
    pr = 94000 + pid
    db.link_pr_to_proposal(pr, pid, opener["agent_id"])
    return pr


def _bal(agent_id):
    with db._conn() as conn:
        return db.balance_for(conn, agent_id)


def _karma_parts(agent_id):
    with db._conn() as conn:
        return db._karma_parts(conn, agent_id)


def _job_row(jid):
    with db._conn() as conn:
        return conn.execute(
            "SELECT status, creator_agent_id, worker_agent_id,"
            " auto_pay_on_merge, cycles_done FROM jobs WHERE id = ?",
            (jid,),
        ).fetchone()


def test_migration_adds_auto_pay_column():
    with db._conn(immediate=True) as conn:
        conn.execute("ALTER TABLE jobs DROP COLUMN auto_pay_on_merge")
    db.init_db()
    with db._conn() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
    assert "auto_pay_on_merge" in cols, "init_db re-adds jobs.auto_pay_on_merge"
    creator = _make_creator("autopay-mig")
    assert db.get_job(_citizen_job(creator)["job_id"])["auto_pay_on_merge"] is False, (
        "default off after migration"
    )
    print("  migration_adds_auto_pay_column: ok")


def test_submit_skips_hold_on_auto_pay():
    from unittest.mock import call

    creator = _make_creator("autopay-hold")
    worker = _make_worker("autopay-holdw")
    plain = _citizen_job(creator)
    flagged = _citizen_job(creator)
    _flag_system(flagged["job_id"])
    pr_plain = _link_pr(worker)
    pr_flag = _link_pr(worker)
    db.claim_job(worker["token"], plain["job_id"])
    db.claim_job(worker["token"], flagged["job_id"])
    with mock.patch("github.add_pr_label") as lab:
        db.submit_job(worker["token"], plain["job_id"], f"#PR{pr_plain}")
        assert lab.call_count == 1, "ordinary jobs still hold their evidence"
        assert lab.call_args == call(pr_plain, "hold"), lab.call_args
        db.submit_job(worker["token"], flagged["job_id"], f"#PR{pr_flag}")
        assert lab.call_count == 1, "system jobs land no hold labels"
    print("  submit_skips_hold_on_auto_pay: ok")


def _submitted_system_job(pay=1.0):
    """Claimed + submitted flagged job with a worker-opened linked PR.

    Mirrors the prod shape (creatorless bounty job): the flag is set and
    the creator is nulled, so no citizen owes - or earns - a verdict."""
    creator = _make_creator("autopay-flow")
    worker = _make_worker("autopay-floww")
    job = _citizen_job(creator, pay=pay)
    jid = job["job_id"]
    _flag_system(jid)
    with db._conn(immediate=True) as conn:
        conn.execute("UPDATE jobs SET creator_agent_id = NULL WHERE id = ?", (jid,))
    pr = _link_pr(worker)
    db.claim_job(worker["token"], jid)
    db.submit_job(worker["token"], jid, f"#PR{pr}")
    return creator, worker, jid, pr


def test_merge_payout_happy_path():
    creator, worker, jid, pr = _submitted_system_job()
    wb, cb = _bal(worker["agent_id"]), _bal(creator["agent_id"])
    import db._jobs_ops._auto as _auto

    with mock.patch.object(_auto, "_all_prs_merged", return_value=True):
        out = db.auto_accept_jobs_for_merged_pr(pr)
    assert out["accepted"] == [jid], out
    assert db.get_job(jid)["status"] == "completed"
    assert _bal(worker["agent_id"]) == wb + 25, "20u wage + 5u reward"
    assert _bal(creator["agent_id"]) == cb, "no creator, no creator pay"
    parts_w = _karma_parts(worker["agent_id"])
    parts_c = _karma_parts(creator["agent_id"])
    assert parts_w["job_rewards"] == 1, "worker earns the cycle karma"
    assert parts_c["job_rewards"] == 0, "void creator leg pays nobody"
    print("  merge_payout_happy_path: ok")


def test_flag_voids_creator_leg_even_when_set():
    """Belt-and-braces: a flagged job that (against the prod invariant)
    still names a creator pays no creator leg on merge-payout."""
    creator = _make_creator("autopay-belt")
    worker = _make_worker("autopay-beltw")
    job = _citizen_job(creator)
    jid = job["job_id"]
    _flag_system(jid)
    pr = _link_pr(worker)
    db.claim_job(worker["token"], jid)
    db.submit_job(worker["token"], jid, f"#PR{pr}")
    cb = _bal(creator["agent_id"])
    import db._jobs_ops._auto as _auto

    with mock.patch.object(_auto, "_all_prs_merged", return_value=True):
        out = db.auto_accept_jobs_for_merged_pr(pr)
    assert out["accepted"] == [jid], out
    assert _bal(creator["agent_id"]) == cb, "flag voids the leg even when set"
    assert _karma_parts(creator["agent_id"])["job_rewards"] == 0
    print("  flag_voids_creator_leg_even_when_set: ok")


def test_partial_merge_no_pay():
    creator = _make_creator("autopay-part")
    worker = _make_worker("autopay-partw")
    job = _citizen_job(creator)
    jid = job["job_id"]
    _flag_system(jid)
    with db._conn(immediate=True) as conn:
        conn.execute("UPDATE jobs SET creator_agent_id = NULL WHERE id = ?", (jid,))
    pr = _link_pr(worker)
    pr2 = _link_pr(worker)
    db.claim_job(worker["token"], jid)
    db.submit_job(worker["token"], jid, f"#PR{pr} #PR{pr2}")
    wb = _bal(worker["agent_id"])
    import db._jobs_ops._auto as _auto

    with mock.patch.object(_auto, "_all_prs_merged", return_value=False):
        out = db.auto_accept_jobs_for_merged_pr(pr)
    assert out["accepted"] == [], out
    assert out["skipped"].get("awaiting_merges", 0) == 1, out
    assert _job_row(jid)["status"] == "active"
    assert _bal(worker["agent_id"]) == wb, "nothing pays until all merge"
    print("  partial_merge_no_pay: ok")


def test_flagged_resubmit_replaces_evidence():
    """A flagged submission whose evidence died is recoverable by the
    worker alone: resubmitting swaps the evidence (ordinary jobs still
    refuse a second submit while awaiting review)."""
    creator, worker, jid, pr = _submitted_system_job()
    pr2 = _link_pr(worker)
    out = db.submit_job(worker["token"], jid, f"#PR{pr2}")
    assert out["status"] == "active"
    with db._conn() as conn:
        nums = conn.execute(
            "SELECT evidence_pr_numbers FROM job_cycles WHERE job_id = ? AND cycle_no = 1",
            (jid,),
        ).fetchone()[0]
    assert str(pr2) in nums and str(pr) not in nums, nums
    import db._jobs_ops._auto as _auto

    with mock.patch.object(_auto, "_all_prs_merged", return_value=True):
        paid = db.auto_accept_jobs_for_merged_pr(pr2)
    assert paid["accepted"] == [jid], paid
    print("  flagged_resubmit_replaces_evidence: ok")


def test_empty_evidence_never_pays():
    creator = _make_creator("autopay-empty")
    worker = _make_worker("autopay-emptyw")
    job = _citizen_job(creator)
    jid = job["job_id"]
    _flag_system(jid)
    db.claim_job(worker["token"], jid)
    db.submit_job(worker["token"], jid, "")
    out = db.auto_accept_jobs_for_merged_pr(999999)
    assert out == {"accepted": [], "skipped": {}}, out
    assert _job_row(jid)["status"] == "active"
    print("  empty_evidence_never_pays: ok")


def test_opener_mismatch_no_pay_then_admin_backstop():
    creator = _make_creator("autopay-mis")
    worker = _make_worker("autopay-misw")
    stranger = _make_worker("autopay-miss")
    job = _citizen_job(creator)
    jid = job["job_id"]
    _flag_system(jid)
    with db._conn(immediate=True) as conn:
        conn.execute("UPDATE jobs SET creator_agent_id = NULL WHERE id = ?", (jid,))
    pr = _link_pr(stranger)
    db.claim_job(worker["token"], jid)
    db.submit_job(worker["token"], jid, f"#PR{pr}")
    wb = _bal(worker["agent_id"])
    import db._jobs_ops._auto as _auto

    with mock.patch.object(_auto, "_all_prs_merged", return_value=True):
        out = db.auto_accept_jobs_for_merged_pr(pr)
    assert out["accepted"] == [], out
    assert out["skipped"].get("opener_mismatch", 0) == 1, out
    assert _bal(worker["agent_id"]) == wb, "spoofed evidence pays nothing"
    back = db.admin_review_job("test-admin", jid, "accept")
    assert back["status"] == "completed", "admin backstop serves system jobs"
    assert _bal(worker["agent_id"]) == wb + 25, "backstop pays wage + reward"
    print("  opener_mismatch_no_pay_then_admin_backstop: ok")


def test_replay_idempotent():
    _, worker, jid, pr = _submitted_system_job()
    import db._jobs_ops._auto as _auto

    with mock.patch.object(_auto, "_all_prs_merged", return_value=True):
        first = db.auto_accept_jobs_for_merged_pr(pr)
        wb = _bal(worker["agent_id"])
        second = db.auto_accept_jobs_for_merged_pr(pr)
    assert first["accepted"] == [jid], first
    assert second == {"accepted": [], "skipped": {}}, second
    assert _bal(worker["agent_id"]) == wb, "replay pays nothing twice"
    print("  replay_idempotent: ok")


def test_unflagged_job_ignored():
    creator = _make_creator("autopay-plain")
    worker = _make_worker("autopay-plainw")
    job = _citizen_job(creator)
    jid = job["job_id"]
    pr = _link_pr(worker)
    db.claim_job(worker["token"], jid)
    db.submit_job(worker["token"], jid, f"#PR{pr}")
    import db._jobs_ops._auto as _auto

    with mock.patch.object(_auto, "_all_prs_merged", return_value=True):
        out = db.auto_accept_jobs_for_merged_pr(pr)
    assert out == {"accepted": [], "skipped": {}}, out
    assert _job_row(jid)["status"] == "active", "manual review still owns it"
    print("  unflagged_job_ignored: ok")


def test_review_refused_on_system_job():
    creator = _make_creator("autopay-norev")
    worker = _make_worker("autopay-norevw")
    job = _citizen_job(creator)
    jid = job["job_id"]
    with db._conn(immediate=True) as conn:
        conn.execute(
            "UPDATE jobs SET creator_agent_id = NULL,"
            " auto_pay_on_merge = 1 WHERE id = ?",
            (jid,),
        )
    db.claim_job(worker["token"], jid)
    db.submit_job(worker["token"], jid, "done")
    try:
        db.review_job(creator["token"], jid, "accept")
    except Exception as exc:  # noqa: BLE001 - asserting the refusal shape
        assert "creator" in str(exc), exc
    else:
        raise AssertionError("review_job must refuse a creatorless job")
    print("  review_refused_on_system_job: ok")


def test_invalid_input_pays_nothing():
    assert db.auto_accept_jobs_for_merged_pr(0) == {
        "accepted": [],
        "skipped": {"invalid": 1},
    }
    assert db.auto_accept_jobs_for_merged_pr(-12) == {
        "accepted": [],
        "skipped": {"invalid": 1},
    }
    assert db.auto_accept_jobs_for_merged_pr("nope") == {
        "accepted": [],
        "skipped": {"invalid": 1},
    }
    print("  invalid_input_pays_nothing: ok")


if __name__ == "__main__":
    fns = [
        v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)
    ]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)}/{len(fns)} auto-pay tests passed")
