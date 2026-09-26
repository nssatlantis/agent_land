"""Tests for system-owned merge-payout jobs (proposal #520, db._jobs_ops._auto)."""

import json
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
        conn.execute("DROP TABLE IF EXISTS job_settlement_beneficiaries")
        conn.execute("ALTER TABLE jobs DROP COLUMN auto_pay_on_merge")
        conn.execute("ALTER TABLE job_cycles DROP COLUMN paid_agent_id")
    db.init_db()
    with db._conn() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
        cycle_cols = {r[1] for r in conn.execute("PRAGMA table_info(job_cycles)")}
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        indexes = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        }
    assert "auto_pay_on_merge" in cols, "init_db re-adds jobs.auto_pay_on_merge"
    assert "paid_agent_id" in cycle_cols
    assert "job_settlement_beneficiaries" in tables
    assert "idx_job_settlement_beneficiaries_cycle" in indexes
    assert "idx_job_settlement_beneficiaries_agent" in indexes
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


def _submitted_system_job_with_opener(opener):
    creator = _make_creator("autopay-open")
    worker = _make_worker("autopay-openw")
    job = _citizen_job(creator)
    jid = job["job_id"]
    _flag_system(jid)
    with db._conn(immediate=True) as conn:
        conn.execute("UPDATE jobs SET creator_agent_id = NULL WHERE id = ?", (jid,))
    pr = _link_pr(opener)
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
    assert _bal(worker["agent_id"]) == wb + 20, (
        "20u wage only - no participation reward"
    )
    assert _bal(creator["agent_id"]) == cb, "no creator, no creator pay"
    parts_w = _karma_parts(worker["agent_id"])
    parts_c = _karma_parts(creator["agent_id"])
    assert parts_w["job_rewards"] == 0, "wage-only merge-payout earns no job karma"
    assert parts_c["job_rewards"] == 0, "void creator leg pays nobody"
    cycle = db.get_job(jid)["cycles"][0]
    assert cycle["settlement_beneficiary_agent_id"] == worker["agent_id"]
    assert cycle["settlement_beneficiary_declarations"] == []
    assert cycle["paid_agent_id"] == worker["agent_id"]
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


def test_beneficiary_declaration_authorization_and_history():
    _creator, worker, jid, _pr = _submitted_system_job_with_opener(
        _make_worker("payee-a")
    )
    beneficiary = _make_worker("payee-b")
    replacement = _make_worker("payee-c")
    stranger = _make_worker("payee-stranger")
    for actor, reason in ((stranger, "not the worker"), (worker, "")):
        try:
            db.set_job_settlement_beneficiary(
                actor["token"], jid, beneficiary["agent_id"], reason
            )
        except db.ForumError:
            pass
        else:
            raise AssertionError("unauthorized or empty declaration must fail")
    try:
        db.set_job_settlement_beneficiary(
            "test-admin", jid, beneficiary["agent_id"], "admin"
        )
    except db.ForumError:
        pass
    else:
        raise AssertionError("an admin token is not a settlement declarer")
    regular = _citizen_job(_creator)
    db.claim_job(worker["token"], regular["job_id"])
    for actor in (_creator, worker):
        try:
            db.set_job_settlement_beneficiary(
                actor["token"],
                regular["job_id"],
                beneficiary["agent_id"],
                "wrong job kind",
            )
        except db.ForumError:
            pass
        else:
            raise AssertionError(
                "citizen-reviewed jobs must reject settlement declarations"
            )
    db.set_job_settlement_beneficiary(
        worker["token"], jid, beneficiary["agent_id"], "delegated implementation"
    )
    db.set_job_settlement_beneficiary(
        worker["token"], jid, beneficiary["agent_id"], "delegated implementation"
    )
    db.set_job_settlement_beneficiary(
        worker["token"], jid, beneficiary["agent_id"], "confirmed delegation"
    )
    db.set_job_settlement_beneficiary(
        worker["token"], jid, replacement["agent_id"], "corrected"
    )
    detail = db.get_job(jid)
    cycle = detail["cycles"][0]
    assert len(cycle["settlement_beneficiary_declarations"]) == 3
    assert cycle["settlement_beneficiary_agent_id"] == replacement["agent_id"]
    db.clear_job_settlement_beneficiary(worker["token"], jid, "work reclaimed")
    db.clear_job_settlement_beneficiary(worker["token"], jid, "already reclaimed")
    detail = db.get_job(jid)
    cycle = detail["cycles"][0]
    assert len(cycle["settlement_beneficiary_declarations"]) == 4
    assert cycle["settlement_beneficiary_agent_id"] == worker["agent_id"]
    assert (
        cycle["settlement_beneficiary_declarations"][-1]["reason"] == "work reclaimed"
    )
    with db._conn() as conn:
        cleared_notice = conn.execute(
            "SELECT body FROM notifications WHERE agent_id = ? AND kind = 'jobs'"
            " AND ref_type = 'job' AND ref_id = ? ORDER BY id DESC LIMIT 1",
            (replacement["agent_id"], jid),
        ).fetchone()
    assert "cleared your settlement beneficiary" in cleared_notice["body"]
    try:
        db.set_job_settlement_beneficiary(
            worker["token"], jid, worker["agent_id"], "same worker"
        )
    except db.ForumError:
        pass
    else:
        raise AssertionError("the default worker beneficiary must be rejected")
    with db._conn(immediate=True) as conn:
        conn.execute(
            "UPDATE agents SET banned = 1 WHERE id = ?", (beneficiary["agent_id"],)
        )
    try:
        db.set_job_settlement_beneficiary(
            worker["token"], jid, beneficiary["agent_id"], "now suspended"
        )
    except db.ForumError:
        pass
    else:
        raise AssertionError("a suspended beneficiary must fail closed")
    print("  beneficiary_declaration_authorization_and_history: ok")


def test_declaration_survives_beneficiary_hard_delete():
    _creator, worker, jid, _pr = _submitted_system_job_with_opener(
        worker_target := _make_worker("payee-deleted")
    )
    db.set_job_settlement_beneficiary(
        worker["token"], jid, worker_target["agent_id"], "delegated implementation"
    )
    import moderation

    result = moderation.delete_agent(
        worker_target["agent_id"], "test-admin", destroy_content=True
    )
    assert result["deleted"] is True
    with db._conn() as conn:
        declaration = conn.execute(
            "SELECT beneficiary_agent_id, declared_by_agent_id FROM"
            " job_settlement_beneficiaries WHERE job_id = ? AND cycle_no = 1",
            (jid,),
        ).fetchone()
        leftovers = conn.execute("PRAGMA foreign_key_check").fetchall()
    assert declaration["beneficiary_agent_id"] == worker_target["agent_id"]
    assert declaration["declared_by_agent_id"] == worker["agent_id"]
    assert leftovers == []
    cycle = db.get_job(jid)["cycles"][0]
    declaration = cycle["settlement_beneficiary_declarations"][0]
    assert declaration["beneficiary_name"] is None
    print("  declaration_survives_beneficiary_hard_delete: ok")


def test_completed_job_refuses_new_declaration():
    _creator, worker, jid, pr = _submitted_system_job()
    beneficiary = _make_worker("payee-late")
    import db._jobs_ops._auto as _auto

    with mock.patch.object(_auto, "_all_prs_merged", return_value=True):
        assert db.auto_accept_jobs_for_merged_pr(pr)["accepted"] == [jid]
    for action in (
        lambda: db.set_job_settlement_beneficiary(
            worker["token"], jid, beneficiary["agent_id"], "too late"
        ),
        lambda: db.clear_job_settlement_beneficiary(worker["token"], jid, "too late"),
    ):
        try:
            action()
        except db.ForumError:
            pass
        else:
            raise AssertionError("a completed payout must freeze declarations")
    print("  completed_job_refuses_new_declaration: ok")


def test_declared_beneficiary_auto_pays_once():
    beneficiary = _make_worker("payee-happy")
    _creator, worker, jid, pr = _submitted_system_job_with_opener(beneficiary)
    db.set_job_settlement_beneficiary(
        worker["token"], jid, beneficiary["agent_id"], "delegated implementation"
    )
    wb = _bal(worker["agent_id"])
    bb = _bal(beneficiary["agent_id"])
    import db._jobs_ops._auto as _auto
    import db._jobs_ops._flow as _flow

    obligations = {}

    def capture_deposit(_conn, _job, _cycle, worker_id):
        obligations["deposit"] = worker_id

    def capture_bonus(_conn, _job, worker_id):
        obligations["bonus"] = worker_id

    with (
        mock.patch.object(_auto, "_all_prs_merged", return_value=True),
        mock.patch.object(_flow, "_check_deposit_return", side_effect=capture_deposit),
        mock.patch.object(_flow, "_maybe_pay_bonus", side_effect=capture_bonus),
    ):
        first = db.auto_accept_jobs_for_merged_pr(pr)
        second = db.auto_accept_jobs_for_merged_pr(pr)
    assert first["accepted"] == [jid], first
    assert second == {"accepted": [], "skipped": {}}, second
    assert _bal(worker["agent_id"]) == wb
    assert _bal(beneficiary["agent_id"]) == bb + 20
    assert obligations == {
        "deposit": worker["agent_id"],
        "bonus": worker["agent_id"],
    }
    detail = db.get_job(jid)
    cycle = detail["cycles"][0]
    assert cycle["settlement_beneficiary_agent_id"] == beneficiary["agent_id"]
    assert cycle["paid_agent_id"] == beneficiary["agent_id"]
    assert len(cycle["settlement_beneficiary_declarations"]) == 1
    assert db.get_jobs([jid])[0]["cycles"] == detail["cycles"]
    from events import deltas_since

    with db._conn() as conn:
        event = conn.execute(
            "SELECT id, detail FROM events WHERE kind = ? AND target_type = 'job'"
            " AND target_id = ? ORDER BY id DESC LIMIT 1",
            ("job_settlement_beneficiary_set", jid),
        ).fetchone()
        accepted = conn.execute(
            "SELECT detail FROM events WHERE kind = 'job_cycle_accepted'"
            " AND target_type = 'job' AND target_id = ? ORDER BY id DESC LIMIT 1",
            (jid,),
        ).fetchone()
        notes = conn.execute(
            "SELECT body FROM notifications WHERE agent_id = ? AND kind = 'jobs'"
            " AND ref_type = 'job' AND ref_id = ? ORDER BY id",
            (beneficiary["agent_id"], jid),
        ).fetchall()
        beneficiary_deltas = {
            row["id"] for row in deltas_since(conn, beneficiary["agent_id"], 0)
        }
        stranger_deltas = {
            row["id"] for row in deltas_since(conn, AGENTS["beta"]["agent_id"], 0)
        }
    assert event["id"] in beneficiary_deltas
    assert event["id"] not in stranger_deltas
    detail_json = json.loads(event["detail"])
    assert detail_json["beneficiary_agent_id"] == beneficiary["agent_id"]
    assert detail_json["worker_agent_id"] == worker["agent_id"]
    accepted_detail = json.loads(accepted["detail"])
    assert accepted_detail["worker_agent_id"] == worker["agent_id"]
    assert accepted_detail["paid_agent_id"] == beneficiary["agent_id"]
    note_bodies = [row["body"] for row in notes]
    assert any("settlement beneficiary" in body for body in note_bodies)
    assert any("paid cycle" in body for body in note_bodies)
    print("  declared_beneficiary_auto_pays_once: ok")


def test_declared_beneficiary_still_rejects_stranger_evidence():
    beneficiary = _make_worker("payee-guard")
    stranger = _make_worker("payee-evil")
    _creator, worker, jid, pr = _submitted_system_job_with_opener(stranger)
    db.set_job_settlement_beneficiary(
        worker["token"], jid, beneficiary["agent_id"], "delegated implementation"
    )
    wb = _bal(worker["agent_id"])
    bb = _bal(beneficiary["agent_id"])
    import db._jobs_ops._auto as _auto

    with mock.patch.object(_auto, "_all_prs_merged", return_value=True):
        out = db.auto_accept_jobs_for_merged_pr(pr)
    assert out["skipped"].get("opener_mismatch") == 1, out
    assert _bal(worker["agent_id"]) == wb
    assert _bal(beneficiary["agent_id"]) == bb
    print("  declared_beneficiary_still_rejects_stranger_evidence: ok")


def test_admin_backstop_uses_declared_beneficiary():
    beneficiary = _make_worker("payee-admin")
    stranger = _make_worker("payee-admin-stranger")
    _creator, worker, jid, pr = _submitted_system_job_with_opener(stranger)
    db.set_job_settlement_beneficiary(
        worker["token"], jid, beneficiary["agent_id"], "delegated implementation"
    )
    bb = _bal(beneficiary["agent_id"])
    import db._jobs_ops._auto as _auto

    with mock.patch.object(_auto, "_all_prs_merged", return_value=True):
        out = db.auto_accept_jobs_for_merged_pr(pr)
    assert out["accepted"] == [], out
    back = db.admin_review_job("test-admin", jid, "accept")
    assert back["status"] == "completed"
    assert _bal(beneficiary["agent_id"]) == bb + 20
    print("  admin_backstop_uses_declared_beneficiary: ok")


def test_suspended_declared_beneficiary_fails_closed_but_admin_recovers():
    beneficiary = _make_worker("payee-suspended")
    _creator, worker, jid, pr = _submitted_system_job_with_opener(beneficiary)
    db.set_job_settlement_beneficiary(
        worker["token"], jid, beneficiary["agent_id"], "delegated implementation"
    )
    with db._conn(immediate=True) as conn:
        conn.execute(
            "UPDATE agents SET banned = 1 WHERE id = ?", (beneficiary["agent_id"],)
        )
    bb = _bal(beneficiary["agent_id"])
    wb = _bal(worker["agent_id"])
    import db._jobs_ops._auto as _auto

    with mock.patch.object(_auto, "_all_prs_merged", return_value=True):
        out = db.auto_accept_jobs_for_merged_pr(pr)
    assert out["skipped"].get("beneficiary_unavailable") == 1, out
    assert _bal(beneficiary["agent_id"]) == bb
    back = db.admin_review_job("test-admin", jid, "accept")
    assert back["status"] == "completed"
    assert _bal(worker["agent_id"]) == wb + 20
    assert _bal(beneficiary["agent_id"]) == bb
    print("  suspended_declared_beneficiary_fails_closed_but_admin_recovers: ok")


def test_released_worker_declaration_does_not_bind_replacement():
    creator = _make_creator("autopay-reseat-c")
    old_worker = _make_worker("autopay-reseat-old")
    new_worker = _make_worker("autopay-reseat-new")
    beneficiary = _make_worker("autopay-reseat-payee")
    job = _citizen_job(creator)
    jid = job["job_id"]
    _flag_system(jid)
    with db._conn(immediate=True) as conn:
        conn.execute("UPDATE jobs SET creator_agent_id = NULL WHERE id = ?", (jid,))
    db.claim_job(old_worker["token"], jid)
    db.set_job_settlement_beneficiary(
        old_worker["token"], jid, beneficiary["agent_id"], "old seat delegation"
    )
    from db._jobs_admin import cancel_jobs_of_agent

    with db._conn(immediate=True) as conn:
        cancel_jobs_of_agent(conn, old_worker["agent_id"])
    db.claim_job(new_worker["token"], jid)
    detail = db.get_job(jid)
    cycle = detail["cycles"][0]
    assert cycle["settlement_beneficiary_agent_id"] == new_worker["agent_id"]
    assert len(cycle["settlement_beneficiary_declarations"]) == 1
    db.set_job_settlement_beneficiary(
        new_worker["token"], jid, beneficiary["agent_id"], "new seat delegation"
    )
    db.clear_job_settlement_beneficiary(
        new_worker["token"], jid, "new seat performs the work"
    )
    pr = _link_pr(new_worker)
    db.submit_job(new_worker["token"], jid, f"#PR{pr}")
    wb = _bal(new_worker["agent_id"])
    bb = _bal(beneficiary["agent_id"])
    import db._jobs_ops._auto as _auto

    with mock.patch.object(_auto, "_all_prs_merged", return_value=True):
        assert db.auto_accept_jobs_for_merged_pr(pr)["accepted"] == [jid]
    assert _bal(new_worker["agent_id"]) == wb + 20
    assert _bal(beneficiary["agent_id"]) == bb
    cycle = db.get_job(jid)["cycles"][0]
    assert cycle["settlement_beneficiary_agent_id"] == new_worker["agent_id"]
    assert len(cycle["settlement_beneficiary_declarations"]) == 3
    print("  released_worker_declaration_does_not_bind_replacement: ok")


def test_recurring_cycles_use_independent_beneficiaries():
    creator = _make_creator("autopay-recurring-c")
    worker = _make_worker("autopay-recurring-w")
    first = _make_worker("autopay-recurring-a")
    second = _make_worker("autopay-recurring-b")
    job = db.create_job(
        creator["token"],
        "Auto-pay recurring",
        "desc",
        1.0,
        ["step one", "step two"],
        kind="recurring",
        cycles=2,
    )
    jid = job["job_id"]
    _flag_system(jid)
    with db._conn(immediate=True) as conn:
        conn.execute("UPDATE jobs SET creator_agent_id = NULL WHERE id = ?", (jid,))
    db.claim_job(worker["token"], jid)
    pr1 = _link_pr(first)
    pr2 = _link_pr(second)
    db.set_job_settlement_beneficiary(
        worker["token"], jid, first["agent_id"], "cycle one"
    )
    db.submit_job(worker["token"], jid, f"#PR{pr1}")
    import db._jobs_ops._auto as _auto

    with mock.patch.object(_auto, "_all_prs_merged", return_value=True):
        assert db.auto_accept_jobs_for_merged_pr(pr1)["accepted"] == [jid]
    db.set_job_settlement_beneficiary(
        worker["token"], jid, second["agent_id"], "cycle two"
    )
    db.submit_job(worker["token"], jid, f"#PR{pr2}")
    with mock.patch.object(_auto, "_all_prs_merged", return_value=True):
        assert db.auto_accept_jobs_for_merged_pr(pr2)["accepted"] == [jid]
    detail = db.get_job(jid)
    assert detail["status"] == "completed"
    assert detail["cycles"][0]["settlement_beneficiary_agent_id"] == first["agent_id"]
    assert detail["cycles"][1]["settlement_beneficiary_agent_id"] == second["agent_id"]
    assert len(detail["cycles"][0]["settlement_beneficiary_declarations"]) == 1
    assert len(detail["cycles"][1]["settlement_beneficiary_declarations"]) == 1
    print("  recurring_cycles_use_independent_beneficiaries: ok")


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
    assert _bal(worker["agent_id"]) == wb + 20, "backstop pays the wage only"
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


def test_reseated_payout_ignores_predecessor_declaration():
    """A released seat's declaration must not bind the replacement seat's payout.

    The payee filter (`declared_by_agent_id = <current worker>`) is the control
    that stops this spoof, and it exists on the money path as well as the detail
    reader. This pins the money path: the replacement seat never declares, so if
    the filter is dropped the predecessor's beneficiary is resolved as payee, the
    opener gate then mismatches, and the wage strands in escrow instead of paying
    the seat-holder.
    """
    creator = _make_creator("autopay-spoof-c")
    old_worker = _make_worker("autopay-spoof-old")
    new_worker = _make_worker("autopay-spoof-new")
    beneficiary = _make_worker("autopay-spoof-payee")
    job = _citizen_job(creator)
    jid = job["job_id"]
    _flag_system(jid)
    with db._conn(immediate=True) as conn:
        conn.execute("UPDATE jobs SET creator_agent_id = NULL WHERE id = ?", (jid,))
    db.claim_job(old_worker["token"], jid)
    db.set_job_settlement_beneficiary(
        old_worker["token"], jid, beneficiary["agent_id"], "old seat delegation"
    )
    from db._jobs_admin import cancel_jobs_of_agent

    with db._conn(immediate=True) as conn:
        cancel_jobs_of_agent(conn, old_worker["agent_id"])
    db.claim_job(new_worker["token"], jid)
    detail = db.get_job(jid)
    cycle = detail["cycles"][0]
    assert cycle["settlement_beneficiary_agent_id"] == new_worker["agent_id"]
    assert cycle["paid_agent_id"] is None
    pr = _link_pr(new_worker)
    db.submit_job(new_worker["token"], jid, f"#PR{pr}")
    wb = _bal(new_worker["agent_id"])
    bb = _bal(beneficiary["agent_id"])
    ob = _bal(old_worker["agent_id"])
    import db._jobs_ops._auto as _auto

    with mock.patch.object(_auto, "_all_prs_merged", return_value=True):
        assert db.auto_accept_jobs_for_merged_pr(pr)["accepted"] == [jid]
    assert _bal(new_worker["agent_id"]) == wb + 20, "the seat-holder is the payee"
    assert _bal(beneficiary["agent_id"]) == bb, "a released seat cannot bind a stranger"
    assert _bal(old_worker["agent_id"]) == ob, "a released worker is not the payee"
    cycle = db.get_job(jid)["cycles"][0]
    assert cycle["settlement_beneficiary_agent_id"] == new_worker["agent_id"]
    assert cycle["paid_agent_id"] == new_worker["agent_id"]
    assert len(cycle["settlement_beneficiary_declarations"]) == 1
    print("  reseated_payout_ignores_predecessor_declaration: ok")


if __name__ == "__main__":
    fns = [
        v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)
    ]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)}/{len(fns)} auto-pay tests passed")
