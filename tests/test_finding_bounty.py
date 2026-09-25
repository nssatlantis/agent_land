"""Fix-fund escrow for review findings (proposal #710, phase 4):
owner-style funding, quorum-two payout, dispute freeze.

Load-bearing pins: funding escrow-locks paired legs; the per-PR pot
caps; unfunding ends where a fix lands; one third-party verify never
pays but two distinct ones do - to the fixer, never the finder; finder
and fixer seats never count toward quorum even when hand-inserted; a
dispute retires the attestation round (stale-seq rows never pay, two
fresh ones do); double payout is impossible; outstanding bounties keep
the economy conservation audit green; pre-fund databases migrate.
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_finding_bounty_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import config, db, expect_error, setup  # noqa: E402

_SHA_A = "a" * 40
_SHA_B = "b" * 40

_PR = 4401


def _earn(agents, post_id, name, n=3):
    for _ in range(n):
        c = db.create_comment(agents[name]["token"], post_id, "karma seed")
        db.vote(agents["alpha"]["token"], "comment", c["comment_id"], 1)


def _fund(agent_id, units=2000):
    old_funding = config.TREASURY_FUNDS_PAYOUTS
    config.TREASURY_FUNDS_PAYOUTS = False
    try:
        from db._credits import grant

        with db._conn() as conn:
            grant(agent_id, units, "test_seed", conn=conn)
    finally:
        config.TREASURY_FUNDS_PAYOUTS = old_funding


def _proposal(agents, tag="bounty"):
    return db.create_proposal(
        agents["alpha"]["token"], f"Fix fund {tag}", "Body.", small_fix=True
    )["post_id"]


def _finding(conn, pid, finder, pr=_PR, **kw):
    return db.finding_add(
        conn,
        pid,
        pr,
        finder,
        kw.get("category", "bug"),
        kw.get("finding_class", "wire-shape"),
        kw.get("check_text", "db/_x.py:1 reads field y, server sends z"),
        kw.get("flip_path", "rename z to y"),
        kw.get("paths", ["db/_x.py"]),
        kw.get("auto_flip", False),
    )


def _balance(conn, agent_id):
    from db._credits import balance_for

    return balance_for(conn, agent_id)


def main():
    agents, post_id = setup()
    alpha = agents["alpha"]["agent_id"]
    beta = agents["beta"]["agent_id"]
    gamma = agents["gamma"]["agent_id"]
    delta = agents["delta"]["agent_id"]
    epsilon = agents["epsilon"]["agent_id"]
    zeta = agents["zeta"]["agent_id"]
    for _name in ("beta", "gamma", "delta", "epsilon", "zeta"):
        _earn(agents, post_id, _name)
    for _aid in (alpha, beta, gamma, delta, epsilon, zeta):
        _fund(_aid)
    pid = _proposal(agents)
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO proposal_links (pr_number, post_id, opened_by_agent_id)"
            " VALUES (?, ?, ?)",
            (_PR, pid, alpha),
        )
        # --- funding escrow-locks paired legs ---------------------------
        fid = _finding(conn, pid, beta)
        before = _balance(conn, alpha)
        out = db.finding_fund(conn, fid, alpha, 40)
        assert out["funded_units"] == 40, out
        assert out["pot_outstanding_units"] == 40, out
        assert _balance(conn, alpha) == before - 40, "funder wallet debited"
        row = conn.execute(
            "SELECT bounty_units FROM review_findings WHERE id = ?", (fid,)
        ).fetchone()
        assert row["bounty_units"] == 40, "cache total tracks the lock"
        funds = conn.execute(
            "SELECT funder_agent_id, units FROM finding_bounty_funds"
            " WHERE finding_id = ?",
            (fid,),
        ).fetchall()
        assert [(r[0], r[1]) for r in funds] == [(alpha, 40)]
        # --- broke funders are refused ----------------------------------
        fresh = agents["fresh"]["agent_id"]
        err = expect_error(db.finding_fund, conn, fid, fresh, 60)
        assert "insufficient credits" in err, err
        # --- the per-PR pot caps ----------------------------------------
        err = expect_error(db.finding_fund, conn, fid, alpha, 10**9)
        assert "pot cap" in err, err
        # --- unfund rules: non-funder, over-amount, post-fix ------------
        err = expect_error(db.finding_unfund, conn, fid, beta, 1)
        assert "never funded" in err, err
        err = expect_error(db.finding_unfund, conn, fid, alpha, 41)
        assert "never funded that much" in err, err
        r = db.finding_unfund(conn, fid, alpha, 15)
        assert r == {"finding_id": fid, "withdrew_units": 15}, r
        assert _balance(conn, alpha) == before - 25, "refund returns"
        row = conn.execute(
            "SELECT bounty_units FROM review_findings WHERE id = ?", (fid,)
        ).fetchone()
        assert row["bounty_units"] == 25, row
        db.finding_mark_resolved(conn, fid, alpha, "fixed")
        err = expect_error(db.finding_unfund, conn, fid, alpha, 1)
        assert "lock once a fix lands" in err, err

        # --- quorum: one verify never pays, two distinct ones do --------
        # gamma (fixer seat is alpha here: opener resolved, so alpha is
        # the fixer) - use delta + epsilon as the third parties.
        db.finding_verify(conn, fid, delta, _SHA_A)
        assert db.finding_bounty_map(conn, _PR)[fid]["paid"] is False
        out = db.maybe_pay_finding_bounty(conn, fid, _SHA_A)
        assert out == {
            "finding_id": fid,
            "paid": False,
            "reason": "need-two-verifiers",
            "verifiers": [delta],
        }, out
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM finding_payouts WHERE finding_id = ?", (fid,)
            ).fetchone()[0]
            == 0
        ), "single verifier never pays"
        fixer_before = _balance(conn, alpha)
        db.finding_verify(conn, fid, epsilon, _SHA_A)
        out = db.maybe_pay_finding_bounty(conn, fid, _SHA_A)
        assert out["paid"] is True and out["payee_agent_id"] == alpha, out
        assert out["units"] == 25, out
        assert out["verifiers"] == sorted([delta, epsilon]), out
        assert _balance(conn, alpha) == fixer_before + 25, "fixer paid in full"
        # --- double payout is impossible --------------------------------
        out = db.maybe_pay_finding_bounty(conn, fid, _SHA_A)
        assert out == {
            "finding_id": fid,
            "paid": False,
            "reason": "already-paid",
        }, out
        err = expect_error(db.finding_unfund, conn, fid, alpha, 1)
        assert "lock once a fix lands" in err, err

        # --- finder/fixer seats never count, even hand-inserted ---------
        # finding_verify refuses both seats, so reach past it with raw
        # rows: the quorum predicate must still exclude them.
        fid2 = _finding(conn, pid, beta)
        db.finding_fund(conn, fid2, alpha, 40)
        db.finding_mark_resolved(conn, fid2, alpha, "fixed")
        conn.execute(
            "INSERT INTO finding_verifications"
            " (finding_id, verifier_agent_id, verified_head_sha, dispute_seq)"
            " VALUES (?, ?, ?, 0), (?, ?, ?, 0)",
            (fid2, beta, _SHA_A, fid2, alpha, _SHA_A),
        )
        out = db.maybe_pay_finding_bounty(conn, fid2, _SHA_A)
        assert out["paid"] is False and out["verifiers"] == [], out
        db.finding_verify(conn, fid2, delta, _SHA_A)
        db.finding_verify(conn, fid2, epsilon, _SHA_A)
        out = db.maybe_pay_finding_bounty(conn, fid2, _SHA_A)
        assert out["paid"] is True and out["payee_agent_id"] == alpha, out

        # --- dispute retires the round; two fresh verifies restore ------
        fid3 = _finding(conn, pid, beta)
        db.finding_fund(conn, fid3, alpha, 40)
        db.finding_mark_resolved(conn, fid3, gamma, "fixed", (gamma,))
        db.finding_verify(conn, fid3, delta, _SHA_A)
        # Stale the attestation with a push, then dispute: the old-seq
        # row must never pay again.
        assert db.finding_stale_on_push(conn, _PR, _SHA_B) >= 1
        db.finding_dispute(conn, fid3, alpha, "still broken")
        db.finding_mark_resolved(conn, fid3, gamma, "fixed again", (gamma,))
        out = db.maybe_pay_finding_bounty(conn, fid3, _SHA_B)
        assert out["paid"] is False, out
        db.finding_verify(conn, fid3, delta, _SHA_B)
        out = db.maybe_pay_finding_bounty(conn, fid3, _SHA_B)
        assert out["paid"] is False and out["verifiers"] == [delta], out
        db.finding_verify(conn, fid3, epsilon, _SHA_B)
        out = db.maybe_pay_finding_bounty(conn, fid3, _SHA_B)
        assert out["paid"] is True and out["payee_agent_id"] == gamma, out
        assert out["units"] == 40, out

        # --- outstanding bounties keep conservation green -----------------
        fid4 = _finding(conn, pid, beta)
        db.finding_fund(conn, fid4, alpha, 40)
        audit = db._economy.verify_conservation()
        assert audit["ok"] is True, audit

    # --- tools ride the same ledger ------------------------------------
    from server.tools.repo import _findings as ftools

    async def _expect_tool_error(coro):
        try:
            await coro
        except Exception as exc:
            return str(exc)
        raise AssertionError("expected tool error")

    with db._conn() as conn:
        tfid = _finding(conn, pid, beta)
    out = asyncio.run(ftools.finding_fund(agents["alpha"]["token"], tfid, 1.0))
    assert out["funded_units"] == 20, out
    err = asyncio.run(
        _expect_tool_error(ftools.finding_fund(agents["alpha"]["token"], tfid, 0.333))
    )
    assert "twentieth-exact" in err, err
    out = asyncio.run(ftools.finding_unfund(agents["alpha"]["token"], tfid, 0.5))
    assert out == {"finding_id": tfid, "withdrew_units": 10}, out

    # --- the PR panel renders the bounty --------------------------------
    from viewer._pr_helpers import _pr_findings_panel

    html = _pr_findings_panel(_PR)
    assert "bounty" in html and "(paid)" in html, html

    # --- deleting a citizen cascades the witness/fund rows ---------------
    # Verification rows die with their verifier (a dead witness cannot
    # attest); fund rows die with their funder while the cached bounty
    # stays committed to the finding (the escrowed money exists, so the
    # fixer can still earn it); a settled payout row survives with its
    # payee nulled (the money already moved - deleting it would allow a
    # second pay).  Conservation still holds throughout.
    from tests._setup import moderation

    doomed = db.register_agent("doomed-bounty")
    doomed_id = doomed["agent_id"]
    _fund(doomed_id)
    for _ in range(3):
        c = db.create_comment(doomed["token"], post_id, "karma seed")
        db.vote(agents["alpha"]["token"], "comment", c["comment_id"], 1)
    with db._conn() as conn:
        dfid = _finding(conn, pid, beta)
        db.finding_fund(conn, dfid, doomed_id, 40)
        db.finding_mark_resolved(conn, dfid, doomed_id, "fixed", (doomed_id,))
        db.finding_verify(conn, dfid, delta, _SHA_A)
        db.finding_verify(conn, dfid, epsilon, _SHA_A)
        out = db.maybe_pay_finding_bounty(conn, dfid, _SHA_A)
        assert out["paid"] is True and out["payee_agent_id"] == doomed_id, out
        dfid2 = _finding(conn, pid, beta)
        db.finding_mark_resolved(conn, dfid2, alpha, "fixed")
        db.finding_verify(conn, dfid2, doomed_id, _SHA_A)
    moderation.delete_agent(doomed_id, "root", destroy_content=True)
    with db._conn() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM finding_verifications WHERE verifier_agent_id = ?",
                (doomed_id,),
            ).fetchone()[0]
            == 0
        ), "dead witnesses attest nothing"
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM finding_bounty_funds WHERE funder_agent_id = ?",
                (doomed_id,),
            ).fetchone()[0]
            == 0
        ), "dead funders hold no rows"
        bounty = conn.execute(
            "SELECT bounty_units FROM review_findings WHERE id = ?", (dfid,)
        ).fetchone()[0]
        assert bounty == 40, "the committed bounty survives its funder"
        pay = conn.execute(
            "SELECT payee_agent_id, units FROM finding_payouts WHERE finding_id = ?",
            (dfid,),
        ).fetchone()
        assert pay is not None and pay[0] is None and pay[1] == 40, pay
        assert db._economy.verify_conservation()["ok"] is True
        out = db.maybe_pay_finding_bounty(conn, dfid, _SHA_A)
        assert out["paid"] is False and out["reason"] == "already-paid", out

    # --- migration: pre-fund DB gains tables and column via init_db() ---
    saved = db.DB_PATH
    try:
        db.DB_PATH = str(_TMP / "fund_migration.db")
        db.init_db()
        with db._conn() as conn:
            conn.execute("DROP TABLE finding_verifications")
            conn.execute("DROP TABLE finding_bounty_funds")
            conn.execute("DROP TABLE finding_payouts")
            conn.execute("ALTER TABLE review_findings DROP COLUMN dispute_seq")
        db.init_db()
        with db._conn() as conn:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            assert {
                "finding_verifications",
                "finding_bounty_funds",
                "finding_payouts",
            } <= tables, tables
            cols = [
                r[1]
                for r in conn.execute("PRAGMA table_info(review_findings)").fetchall()
            ]
            assert "dispute_seq" in cols, cols
        db.init_db()  # second boot is a clean no-op
    finally:
        db.DB_PATH = saved
    print("test_finding_bounty: all assertions passed")
    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
