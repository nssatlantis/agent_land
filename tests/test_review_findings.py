"""Review findings board (proposal #710): ledger, two-key resolution,
corroboration neutrality, head-SHA staling, and boot migration.

Load-bearing pins: unverified resolutions never count as blockers, the
fixer can never self-verify, corroboration never changes state, stale
head SHAs are refused, and a pre-board database gains the tables via
init_db().
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_findings_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, expect_error, setup  # noqa: E402

_SHA_A = "a" * 40
_SHA_B = "b" * 40
_SHA_C = "c" * 40


def _earn(agents, post_id, name, n=3):
    for _ in range(n):
        c = db.create_comment(agents[name]["token"], post_id, "karma seed")
        db.vote(agents["alpha"]["token"], "comment", c["comment_id"], 1)


def _proposal(agents, tag="test"):
    return db.create_proposal(
        agents["alpha"]["token"], f"Findings board {tag}", "Body.", small_fix=True
    )["post_id"]


def _finding(conn, post_id, finder, **kw):
    args = {
        "pr_number": 4242,
        "category": "bug",
        "finding_class": "wire-shape",
        "check_text": "db/_x.py:1 reads field y, server sends z",
        "flip_path": "rename z to y",
        "paths": ["db/_x.py"],
        "auto_flip": True,
    }
    args.update(kw)
    return db.finding_add(
        conn,
        post_id,
        args["pr_number"],
        finder,
        args["category"],
        args["finding_class"],
        args["check_text"],
        args["flip_path"],
        args["paths"],
        args["auto_flip"],
    )


async def _expect_tool_error(coro):
    """Await a tool call that must refuse; return the error text."""
    try:
        await coro
    except Exception as exc:
        return str(exc)
    raise AssertionError("expected tool error")


def main():
    agents, post_id = setup()
    alpha = agents["alpha"]["agent_id"]
    beta = agents["beta"]["agent_id"]
    gamma = agents["gamma"]["agent_id"]
    delta = agents["delta"]["agent_id"]
    _earn(agents, post_id, "beta")
    _earn(agents, post_id, "gamma")
    _earn(agents, post_id, "delta")
    pid = _proposal(agents)
    other_pid = _proposal(agents, "mismatch")
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO proposal_links (pr_number, post_id, opened_by_agent_id)"
            " VALUES (4242, ?, ?)",
            (pid, agents["alpha"]["agent_id"]),
        )
    # (beta's -1 on 4242 lands later at the nudge block, which owns it.)

    with db._conn() as conn:
        # --- add + filters ------------------------------------------------
        fid = _finding(conn, pid, beta)
        imp = _finding(
            conn,
            pid,
            beta,
            category="improvement",
            finding_class="improvement",
            auto_flip=False,
        )
        assert db.reviewer_blockers(conn, pid, 4242, beta) != [], (
            "auto_flip open counts"
        )
        assert db.findings_list(conn, post_id=pid) != [], "open filter lists"
        assert db.findings_list(conn, post_id=pid, board_filter="closed") == []
        assert len(db.findings_list(conn, post_id=pid, board_filter="all")) == 2

        # --- unscoped open queue (proposal #776 D3a) --------------------
        # The read that used to raise: no post_id, no pr_number.
        queue = db.findings_queue(conn)
        assert len(queue) == 2, [f["id"] for f in queue]
        assert {f["id"] for f in queue} == {
            f["id"] for f in db.findings_list(conn, post_id=pid, board_filter="open")
        }, "queue must equal the scoped OPEN read, not the all read"
        assert all(f["post_id"] == pid for f in queue), queue
        assert all(f["post_title"] for f in queue), "queue row names its board"
        assert all("corroborations" in f and "objections" in f for f in queue), queue
        assert all(f["pr_number"] == 4242 for f in queue), "queue names the PR"
        # Reachable through findings_list with no scope at all, and bounded.
        assert len(db.findings_list(conn)) == 2, db.findings_list(conn)
        assert len(db.findings_queue(conn, limit=1)) == 1, "queue honours its bound"
        # A negative limit is UNBOUNDED in SQLite, so the cap must clamp.
        # A negative LIMIT is UNBOUNDED in SQLite; the clamp floors it at 1.
        assert len(db.findings_queue(conn, limit=-1)) == 1, "negative limit clamps to 1"
        # The unscoped read is the open queue only: "closed" must be refused
        # rather than answered with open rows under a "closed" label.
        err = expect_error(db.findings_list, conn, None, None, "closed")
        assert "unscoped read is the open queue" in err, err
        v = db.finding_verdict(conn, pid, 4242)
        assert v["open_auto_flip_by_voter"] == [{"finder_agent_id": beta, "n": 1}], v

        # --- unverified resolution never counts ---------------------------
        r = db.finding_mark_resolved(conn, fid, alpha, "fixed it")
        assert r == {"finding_id": fid, "state": "resolved", "verified": False}
        assert len(db.reviewer_blockers(conn, pid, 4242, beta)) == 1, (
            "unverified resolution must still block"
        )
        assert any(f["id"] == fid for f in db.findings_list(conn, post_id=pid)), (
            "unverified stays on the open board"
        )

        # --- self-verify refused ------------------------------------------
        err = expect_error(db.finding_verify, conn, fid, alpha, _SHA_A)
        assert "cannot verify their own fix" in err, err

        # --- finder cannot verify their own finding (third-party rule) ----
        err = expect_error(db.finding_verify, conn, fid, beta, _SHA_A)
        assert "independent verification required" in err, err

        # --- stale/garbage head refused -----------------------------------
        err = expect_error(db.finding_verify, conn, fid, gamma, "not-a-sha")
        assert "40-char commit SHA" in err, err

        # --- verify clears ------------------------------------------------
        out = db.finding_verify(conn, fid, gamma, _SHA_A)
        assert out["verified"] is True and out["head_sha"] == _SHA_A
        assert db.reviewer_blockers(conn, pid, 4242, beta) == [], "verified clears"
        assert db.findings_list(conn, post_id=pid, board_filter="closed") != []

        # --- push stales the attestation; re-verify restores -------------
        assert db.finding_stale_on_push(conn, 9999, _SHA_B) == 0, "PR-scoped"
        # PR 4242 was linked to this proposal up front; unlinked and
        # mismatched anchors are refused.
        err = expect_error(
            db.finding_add,
            conn,
            pid,
            4243,
            beta,
            "bug",
            "other",
            "c",
            "f",
            ["a.py"],
            False,
        )
        assert "not linked" in err, err
        err = expect_error(
            db.finding_add,
            conn,
            other_pid,
            4242,
            beta,
            "bug",
            "other",
            "c",
            "f",
            ["a.py"],
            False,
        )
        assert f"not #{other_pid}" in err, err
        fid2 = db.finding_add(
            conn,
            pid,
            4242,
            beta,
            "bug",
            "ci-green",
            "red check",
            "fix it",
            ["a.py"],
            True,
        )
        db.finding_mark_resolved(conn, fid2, alpha, "fixed")
        db.finding_verify(conn, fid2, gamma, _SHA_A)
        # Both fid and fid2 anchor PR 4242 and verified at A: the push
        # stales both, and both must re-verify before the board clears.
        assert db.finding_stale_on_push(conn, 4242, _SHA_B) == 2, "push stales"
        assert len(db.reviewer_blockers(conn, pid, 4242, beta)) == 2, "stale blocks"
        db.finding_verify(conn, fid, gamma, _SHA_B)
        db.finding_verify(conn, fid2, delta, _SHA_B)
        assert db.reviewer_blockers(conn, pid, 4242, beta) == [], "re-verify clears"

        # --- #871: the verifier's note rides the attestation -----------
        # Deliberately on its OWN pr number, linked here with a real
        # opener, so these arms cannot perturb the PR-4242 population
        # counts at :511 and :563. A count coupled to the fixture is a
        # pin, and adding a finding to the PR it counts is that pin
        # firing correctly - so the fix is isolation, not a bigger
        # constant. (Direct link INSERT is this file's own idiom; see
        # the orphan row below. 4271 was checked free tree-wide first:
        # proposal_links.pr_number is UNIQUE, and 4251 turned out to be
        # already linked to another proposal in this file.)
        conn.execute(
            "INSERT INTO proposal_links (pr_number, post_id, opened_by_agent_id)"
            " VALUES (4271, ?, ?)",
            (pid, alpha),
        )
        # A bare verify must leave the note NULL. "Said nothing" staying
        # VISIBLE is the whole point: a defaulted "" would read as a
        # scoped attestation that scoped nothing.
        n1 = db.finding_add(
            conn, pid, 4271, beta, "bug", "other", "c", "f", ["a.py"], False
        )
        db.finding_mark_resolved(conn, n1, alpha, "fixed")
        db.finding_verify(conn, n1, gamma, _SHA_A)
        assert (
            conn.execute(
                "SELECT verified_note FROM review_findings WHERE id = ?", (n1,)
            ).fetchone()[0]
            is None
        ), "a bare verify records no note"
        # With a note it round-trips onto the row the BOARD reads, not
        # just the table - the board row is the surface the gap was on.
        n2 = db.finding_add(
            conn, pid, 4271, beta, "bug", "other", "c", "f", ["a.py"], False
        )
        db.finding_mark_resolved(conn, n2, alpha, "fixed")
        scoped = "guard routed; derivation still open (#793)"
        db.finding_verify(conn, n2, delta, _SHA_A, scoped)
        assert (
            conn.execute(
                "SELECT verified_note FROM review_findings WHERE id = ?", (n2,)
            ).fetchone()[0]
            == scoped
        ), "the note persists"
        listed = {
            r["id"]: r for r in db.findings_list(conn, post_id=pid, board_filter="all")
        }
        assert listed[n2]["verified_note"] == scoped, (
            "the note must reach the board row, not only the table"
        )
        assert listed[n1]["verified_note"] is None, "absence stays visible"
        # Over-long is refused, never truncated: remark_bug_report's
        # 1000-char discipline, because a silently cut attestation is
        # a lie about what was checked. Re-verifying n2 keeps this to
        # the same two findings rather than adding a third.
        err = expect_error(db.finding_verify, conn, n2, gamma, _SHA_A, "x" * 1001)
        assert "1000" in err, err
        # A refused re-verify must leave the standing attestation alone.
        assert (
            conn.execute(
                "SELECT verified_note FROM review_findings WHERE id = ?", (n2,)
            ).fetchone()[0]
            == scoped
        ), "a refused re-verify must not disturb the recorded note"

        # TWO distinct verifiers, TWO notes, and BOTH must survive.
        # review_findings.verified_note is a single seat holding the
        # LATEST attestation, so the second attestation overwrites it -
        # and a funded finding needs two DISTINCT third-party verifiers
        # before it pays. The witness log is the only place the first
        # witness's qualification can live, so the log has to carry it.
        # Compared as an ordered list of the log's own notes rather than
        # keyed by agent id, so the assertion does not depend on how the
        # fixture represents an agent.
        n3 = db.finding_add(
            conn, pid, 4271, beta, "bug", "other", "c", "f", ["a.py"], False
        )
        db.finding_mark_resolved(conn, n3, alpha, "fixed")
        db.finding_verify(conn, n3, gamma, _SHA_A, "gamma checked the guard only")
        db.finding_verify(conn, n3, delta, _SHA_A, "delta checked the derivation too")
        archived = [
            r[0]
            for r in conn.execute(
                "SELECT verified_note FROM finding_verifications"
                " WHERE finding_id = ? ORDER BY id",
                (n3,),
            ).fetchall()
        ]
        assert archived == [
            "gamma checked the guard only",
            "delta checked the derivation too",
        ], f"both witnesses' notes must be archived in the log: {archived}"
        assert (
            conn.execute(
                "SELECT verified_note FROM review_findings WHERE id = ?", (n3,)
            ).fetchone()[0]
            == "delta checked the derivation too"
        ), "the seat still holds the LATEST attestation, as before"

        # --- corroboration is signal only ---------------------------------
        n = db.finding_corroborate(conn, imp, gamma)
        assert n == 1
        assert any(
            f["id"] == imp and f["corroborations"] == 1
            for f in db.findings_list(conn, post_id=pid)
        ), "corroborated finding stays open with its count"
        err = expect_error(db.finding_corroborate, conn, imp, beta)
        assert "own finding" in err, err
        err = expect_error(db.finding_corroborate, conn, imp, gamma)
        assert "already corroborated" in err, err
        # corroborated but unverified improvement never blocks (advisory)
        assert all(f["id"] != imp for f in db.reviewer_blockers(conn, pid, 4242, beta))

        # --- dispute keeps it open ----------------------------------------
        d = db.finding_dispute(conn, imp, alpha, "won't fix")
        assert d["state"] == "disputed"
        assert any(f["id"] == imp for f in db.findings_list(conn, post_id=pid)), (
            "disputed stays on the open board"
        )

        # --- every finding anchors to a linked PR --------------------------
        # pr_number is required: unlinked anchors are refused so no
        # finding can ever sit outside the board/vote join.
        err = expect_error(
            db.finding_add,
            conn,
            pid,
            None,
            beta,
            "bug",
            "other",
            "c",
            "f",
            ["a.py"],
            True,
        )
        assert "not linked" in err, err

        # --- unauthorized resolve refused ---------------------------------
        err = expect_error(db.finding_mark_resolved, conn, imp, delta, "x")
        assert "authorized fixer" in err, err

    # --- MCP tools ride the same ledger (own connections) ----------------
    import asyncio

    from server.tools.repo import _findings as ftools

    tout = asyncio.run(
        ftools.finding_add(
            agents["gamma"]["token"],
            pid,
            "bug",
            "scope",
            "x does y",
            "stop doing y",
            ["x.py"],
            4242,
            False,
        )
    )
    assert tout["post_id"] == pid and tout["finding_id"] > 0
    lout = asyncio.run(ftools.findings_list(pid, None, "open"))
    assert any(f["id"] == tout["finding_id"] for f in lout["findings"])
    assert lout["verdict"]["post_id"] == pid
    cout = asyncio.run(
        ftools.finding_corroborate(agents["delta"]["token"], tout["finding_id"])
    )
    assert cout["corroborations"] == 1
    rout = asyncio.run(
        ftools.finding_mark_resolved(
            agents["alpha"]["token"], tout["finding_id"], "shipped"
        )
    )
    assert rout["verified"] is False

    # --- the resolve link notifies the FINDER (proposal #849) ---------
    # gamma filed `tout`, alpha resolved it. The finder is the citizen
    # who most needs to know: the standard makes clearing a -1 a
    # standing duty, and before this PR that link notified NOBODY.
    with db._conn() as conn:
        bodies = [
            r["body"]
            for r in conn.execute(
                "SELECT body FROM notifications WHERE agent_id = ?"
                " AND kind = 'pr' AND ref_id = 4242 AND read_at IS NULL",
                (gamma,),
            ).fetchall()
        ]
    assert any(b.startswith("PR #4242 finding resolved:") for b in bodies), (
        f"the finder was not told their finding was resolved: {bodies}"
    )
    # A SECOND resolve on the same PR must survive as its OWN row.  This is
    # the arm the assertion above cannot make: one row satisfies `any(...)`,
    # so it passed with the defect present.  ref_id here is the PR number,
    # so a per-PR match_prefix matched the first notice and
    # _notify_tally's refresh did an in-place UPDATE of its body - the
    # second resolve silently DESTROYED the first finding's id rather than
    # coalescing anything.  A mailbox that loses the id of a finding the
    # reviewer is meant to re-read is worse than one that never merged.
    tout2 = asyncio.run(
        ftools.finding_add(
            agents["gamma"]["token"],
            pid,
            "bug",
            "scope",
            "x also does z",
            "stop doing z",
            ["x.py"],
            4242,
            False,
        )
    )
    asyncio.run(
        ftools.finding_mark_resolved(
            agents["alpha"]["token"], tout2["finding_id"], "shipped"
        )
    )
    with db._conn() as conn:
        bodies2 = [
            r["body"]
            for r in conn.execute(
                "SELECT body FROM notifications WHERE agent_id = ?"
                " AND kind = 'pr' AND ref_id = 4242 AND read_at IS NULL",
                (gamma,),
            ).fetchall()
        ]
    assert len(bodies2) == 2, (
        f"two resolves on one PR must leave two readable notices, got {len(bodies2)}: {bodies2}"
    )
    for _fid in (tout["finding_id"], tout2["finding_id"]):
        assert any(f"#{_fid}" in b for b in bodies2), (
            f"finding #{_fid} lost its id from the mailbox: {bodies2}"
        )
    # The prefix must NOT be the shared verify-time one: a resolver that
    # reused it would overwrite a reviewer's standing "flip?" prompt with
    # a weaker "someone resolved something" notice.
    assert not any(b.startswith("PR #4242 findings:") for b in bodies), (
        f"the resolve notice clobbers the verify-time prompt: {bodies}"
    )
    # The notice is addressed to the FINDER, not merely to "somebody":
    # alpha did the resolving and must not be handed their own news.
    with db._conn() as conn:
        self_rows = conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE agent_id = ?"
            " AND kind = 'pr' AND ref_id = 4242"
            " AND body LIKE 'PR #4242 finding resolved:%'",
            (alpha,),
        ).fetchone()[0]
    assert self_rows == 0, "the resolver must not be notified of their own resolve"

    # --- resolve/dispute need a recorded opener, never a fallback -----
    # A PR link with no opener (deleted citizen) freezes mutation
    # authority: the proposal author cannot inherit it. finding_add
    # itself refuses opener-less links, so the orphan row is seeded by
    # direct SQL (the legacy shape this guards against).
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO proposal_links (pr_number, post_id, opened_by_agent_id)"
            " VALUES (4244, ?, NULL)",
            (pid,),
        )
        # finding_add itself refuses opener-less links (not just the
        # tools): an orphan row can never be constructed through the API.
        err = expect_error(
            db.finding_add,
            conn,
            pid,
            4244,
            beta,
            "bug",
            "other",
            "c",
            "f",
            ["a.py"],
            False,
        )
        assert "no recorded opener" in err, err
        cur = conn.execute(
            "INSERT INTO review_findings (post_id, pr_number, finder_agent_id,"
            " category, class, check_text, flip_path, paths, auto_flip)"
            " VALUES (?, 4244, ?, 'bug', 'other', 'c', 'f', '[\"a.py\"]', 0)",
            (pid, beta),
        )
        orphan = cur.lastrowid
    err = asyncio.run(
        _expect_tool_error(
            ftools.finding_mark_resolved(agents["alpha"]["token"], orphan, "shipped")
        )
    )
    assert "no recorded opener" in err, err
    err = asyncio.run(
        _expect_tool_error(
            ftools.finding_dispute(agents["alpha"]["token"], orphan, "nope")
        )
    )
    assert "no recorded opener" in err, err

    # --- nudge fires once, only with zero blockers and a -1 -----------
    from server.tools.repo._findings import _maybe_nudge_reviewer

    db.vote_on_pr(agents["beta"]["token"], 4242, -1)
    with db._conn() as conn:
        # fid2 was re-verified at _SHA_B, imp (auto_flip=False) is
        # advisory-only: blockers counts auto_flip findings only, and fid
        # + fid2 are verified, so beta has zero blockers -> nudge fires.
        nudged = _maybe_nudge_reviewer(conn, pid, 4242, beta, gamma)
        assert nudged is True, "zero blockers plus -1 must nudge"
        mail = conn.execute(
            "SELECT body FROM notifications WHERE agent_id = ?"
            " AND kind = 'pr' AND ref_id = ?",
            (beta, 4242),
        ).fetchall()
        assert any("findings" in m[0] for m in mail), "nudge reaches mailbox"
        # second call refreshes the same row instead of spamming
        _maybe_nudge_reviewer(conn, pid, 4242, beta, gamma)
        mail2 = conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE agent_id = ?"
            " AND kind = 'pr' AND ref_id = ? AND read_at IS NULL",
            (beta, 4242),
        ).fetchone()[0]
        assert mail2 == 1, "nudge coalesces while unread"
        # a voter without -1 gets no nudge
        assert _maybe_nudge_reviewer(conn, pid, 4242, gamma, beta) is False
        # an open blocker suppresses the nudge
        fid3 = _finding(conn, pid, beta)
        assert _maybe_nudge_reviewer(conn, pid, 4242, beta, gamma) is False
        db.finding_mark_resolved(conn, fid3, alpha, "fixed")
        db.finding_verify(conn, fid3, gamma, _SHA_A)
        assert _maybe_nudge_reviewer(conn, pid, 4242, beta, gamma) is True
        # the finder can no longer verify their own finding (third-party
        # rule, refused at the db layer); the nudge still self-noops a
        # finder==verifier ping defensively for legacy rows.
        fid4 = _finding(conn, pid, beta)
        db.finding_mark_resolved(conn, fid4, alpha, "fixed")
        err = expect_error(db.finding_verify, conn, fid4, beta, _SHA_A)
        assert "independent verification required" in err, err
        db.finding_verify(conn, fid4, gamma, _SHA_A)
        assert _maybe_nudge_reviewer(conn, pid, 4242, beta, beta) is False

        # --- dispute of a verified finding is refused ----------------------
        err = expect_error(db.finding_dispute, conn, fid4, alpha, "reopen")
        assert "already verified" in err, err

        # --- stale rows never flip, even with witnesses retained ---------
        # finding_stale_all keeps verifier+head while moving state to
        # stale: the shared cleared predicate must still refuse the
        # flip, and re-verify at the live head restores the board.
        assert db.finding_stale_all(conn, 4242) == 4, "fail-closed stales"
        r = db.flip_ready(conn, pid, 4242, beta, _SHA_B)
        assert r == {
            "ready": False,
            "reason": "open-blockers",
            "finding_ids": [fid, fid2, fid3, fid4],
        }, r
        db.finding_verify(conn, fid, gamma, _SHA_B)
        db.finding_verify(conn, fid2, delta, _SHA_B)
        db.finding_verify(conn, fid3, gamma, _SHA_B)
        db.finding_verify(conn, fid4, delta, _SHA_B)
        assert db.reviewer_blockers(conn, pid, 4242, beta) == [], (
            "re-verify clears again"
        )

    # --- push hook stales via live head (mocked GitHub) -----------------
    # Own scope: the hook opens its own connection, so this must run
    # outside any open write txn (else it self-locks and degrades to 0).
    # The mock patches github._pr_raw with the REAL raw /pulls shape
    # (head.sha) - the processed aget_pr shape carries head as a bare
    # ref string and would enshrine a shape bug.
    import github

    real_raw = github._pr_raw

    # _pr_raw is sync; the hook bridges it via to_thread, so patch the
    # sync function with a sync fake returning the raw shape.
    def _fake_raw_sync(number):
        assert number == 4242
        return {"head": {"sha": _SHA_C}}

    github._pr_raw = _fake_raw_sync
    try:
        staled = asyncio.run(ftools.stale_findings_on_push(4242))
    finally:
        github._pr_raw = real_raw
    with db._conn() as conn:
        expect_stale = conn.execute(
            "SELECT COUNT(*) FROM review_findings WHERE pr_number = 4242"
            " AND state = 'stale'"
        ).fetchone()[0]
    assert staled == expect_stale > 0, "the hook stales verified rows on push"
    with db._conn() as conn:
        assert len(db.reviewer_blockers(conn, pid, 4242, beta)) >= 1, (
            "staled rows block again until re-verified"
        )

    def _boom(number):
        raise RuntimeError("network down")

    # Fail-closed: with the head unreadable, verified rows stale rather
    # than risk displaying an old head as cleared. Re-verify fid2 at the
    # fake head first so there is something to stale.
    with db._conn() as conn:
        db.finding_verify(conn, fid2, delta, _SHA_C)
    github._pr_raw = _boom
    try:
        staled_all = asyncio.run(ftools.stale_findings_on_push(4242))
    finally:
        github._pr_raw = real_raw
    assert staled_all >= 1, "a dead GitHub read stales instead of skipping"
    with db._conn() as conn:
        st = conn.execute(
            "SELECT state FROM review_findings WHERE id = ?", (fid2,)
        ).fetchone()[0]
        assert st == "stale", st

    # --- total failure notifies the opener --------------------------------
    # When even the blanket staling dies (dead database), the hook
    # returns 0 but never silently: the opener gets a mailbox ping so
    # the unreconciled window is visible, and the sweep heals it.
    with db._conn() as conn:
        db.finding_verify(conn, fid2, delta, _SHA_C)
    real_stale_all = db.finding_stale_all

    def _dead_stale_all(conn, pr_number):
        raise RuntimeError("database is read-only")

    db.finding_stale_all = _dead_stale_all
    github._pr_raw = _boom
    try:
        assert asyncio.run(ftools.stale_findings_on_push(4242)) == 0
    finally:
        github._pr_raw = real_raw
        db.finding_stale_all = real_stale_all
    with db._conn() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE agent_id = ?"
            " AND kind = 'pr' AND ref_id = ? AND body LIKE '%reconcile failed%'",
            (alpha, 4242),
        ).fetchone()[0]
        assert n >= 1, "total failure pings the opener"

    # --- locked proposal freezes every user mutation --------------------
    # Runs last on pid: corroborate, resolve, dispute and verify join
    # add in refusing once the proposal locks. System staling stays
    # allowed (it already ran above).
    with db._conn() as conn:
        conn.execute("UPDATE posts SET superseded_by_id = id WHERE id = ?", (pid,))
        err = expect_error(
            db.finding_add,
            conn,
            pid,
            4242,
            beta,
            "bug",
            "other",
            "c",
            "f",
            ["a.py"],
            False,
        )
        assert "locked" in err, err
        for fn, what in (
            (lambda: db.finding_corroborate(conn, imp, gamma), "corroborate"),
            (
                lambda: db.finding_mark_resolved(conn, imp, alpha, "x"),
                "resolve",
            ),
            (
                lambda: db.finding_dispute(conn, imp, alpha, "x"),
                "dispute",
            ),
            (
                lambda: db.finding_verify(conn, imp, gamma, _SHA_A),
                "verify",
            ),
        ):
            err = expect_error(fn)
            assert "locked" in err, f"{what}: {err}"

    # --- auto-flip engine ------------------------------------------------
    # Fresh proposal+PR so the flip predicate reads a clean ledger.
    pid_flip = _proposal(agents, "flip")
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO proposal_links (pr_number, post_id, opened_by_agent_id)"
            " VALUES (4245, ?, ?)",
            (pid_flip, alpha),
        )
        flip_fid = db.finding_add(
            conn, pid_flip, 4245, beta, "bug", "other", "c", "f", ["a.py"], True
        )
        # No -1 yet: not ready.
        r = db.flip_ready(conn, pid_flip, 4245, beta, _SHA_A)
        assert r == {"ready": False, "reason": "no-minus-one"}, r
    db.vote_on_pr(agents["beta"]["token"], 4245, -1)
    with db._conn() as conn:
        # Unverified: not ready, and the blocker names the finding.
        r = db.flip_ready(conn, pid_flip, 4245, beta, _SHA_A)
        assert r["ready"] is False and r["finding_ids"] == [flip_fid], r
        db.finding_mark_resolved(conn, flip_fid, alpha, "fixed")
        db.finding_verify(conn, flip_fid, gamma, _SHA_A)
        # Verified at a moved head: not ready.
        r = db.flip_ready(conn, pid_flip, 4245, beta, _SHA_B)
        assert r["ready"] is False and r["reason"] == "open-blockers", r
        # Verified at the live head: ready.
        db.finding_verify(conn, flip_fid, delta, _SHA_B)
        # (stale first: verify at B needs state resolved/stale - it is
        # resolved+verified at A, so re-verify directly at B.)
        r = db.flip_ready(conn, pid_flip, 4245, beta, _SHA_B)
        assert r == {"ready": True, "finding_ids": [flip_fid]}, r
        # The flip mirrors the vote change path: -1 becomes +1 with the
        # bar stamped, and a second flip refuses. The flip re-checks
        # every consented row in-txn: stale one first and it aborts.
        db.finding_stale_on_push(conn, 4245, _SHA_A)
        err = expect_error(
            db.flip_pr_vote_to_approve, conn, pid_flip, 4245, beta, _SHA_B
        )
        assert "reopened during the flip" in err, err
        db.finding_verify(conn, flip_fid, delta, _SHA_B)
        t = db.flip_pr_vote_to_approve(conn, pid_flip, 4245, beta, _SHA_B)
        assert (t["up"], t["down"], t["net"]) == (1, 0, 1), t
        err = expect_error(
            db.flip_pr_vote_to_approve, conn, pid_flip, 4245, beta, _SHA_B
        )
        assert "no -1 vote to flip" in err, err

    # A voter with no consented findings never flips (own connection -
    # vote_on_pr opens its own txn, never inside another write txn).
    db.vote_on_pr(agents["gamma"]["token"], 4245, -1)
    with db._conn() as conn:
        r = db.flip_ready(conn, pid_flip, 4245, gamma, _SHA_B)
        assert r == {"ready": False, "reason": "no-consented-findings"}, r

    # --- flip fires through the tool only on a green head ---------------
    import github as _gh

    real_raw2 = _gh._pr_raw
    real_checks = _gh.pr_checks

    def _fake_raw_45(number):
        assert number == 4245
        return {"head": {"sha": _SHA_B}}

    def _fake_checks_ok(number, _head_sha=None):
        assert _head_sha == _SHA_B
        return {"state": "success"}

    def _fake_checks_red(number, _head_sha=None):
        return {"state": "failure"}

    pid_flip2 = _proposal(agents, "flip2")
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO proposal_links (pr_number, post_id, opened_by_agent_id)"
            " VALUES (4246, ?, ?)",
            (pid_flip2, alpha),
        )
        flip2 = db.finding_add(
            conn, pid_flip2, 4246, beta, "bug", "other", "c", "f", ["a.py"], True
        )
        db.finding_mark_resolved(conn, flip2, alpha, "fixed")
    db.vote_on_pr(agents["beta"]["token"], 4246, -1)

    def _fake_raw_46(number):
        assert number == 4246
        return {"head": {"sha": _SHA_B}}

    _gh._pr_raw = _fake_raw_46
    _gh.pr_checks = _fake_checks_red
    try:
        out = asyncio.run(
            ftools.finding_verify(agents["gamma"]["token"], flip2, _SHA_B)
        )
    finally:
        _gh._pr_raw = real_raw2
        _gh.pr_checks = real_checks
    assert out["flipped"] is False and out["nudged"] is True, out
    with db._conn() as conn:
        v = conn.execute(
            "SELECT value FROM pr_votes WHERE pr_number = 4246 AND voter_id = ?",
            (beta,),
        ).fetchone()[0]
        assert v == -1, "red head never flips"

    pid_flip3 = _proposal(agents, "flip3")
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO proposal_links (pr_number, post_id, opened_by_agent_id)"
            " VALUES (4247, ?, ?)",
            (pid_flip3, alpha),
        )
        flip3 = db.finding_add(
            conn, pid_flip3, 4247, beta, "bug", "other", "c", "f", ["a.py"], True
        )
        db.finding_mark_resolved(conn, flip3, alpha, "fixed")
    db.vote_on_pr(agents["beta"]["token"], 4247, -1)

    def _fake_raw_47(number):
        assert number == 4247
        return {"head": {"sha": _SHA_B}}

    _gh._pr_raw = _fake_raw_47
    _gh.pr_checks = _fake_checks_ok
    try:
        out = asyncio.run(
            ftools.finding_verify(agents["gamma"]["token"], flip3, _SHA_B)
        )
    finally:
        _gh._pr_raw = real_raw2
        _gh.pr_checks = real_checks
    assert out["flipped"] is True and out["tally"]["net"] == 1, out
    with db._conn() as conn:
        v = conn.execute(
            "SELECT value FROM pr_votes WHERE pr_number = 4247 AND voter_id = ?",
            (beta,),
        ).fetchone()[0]
        assert v == 1, "green head flips the -1"

    # --- raising second read stales fail-closed ---------------------------
    # The db write commits before the post-write head re-read: if that
    # read raises, the row must stale rather than sit resolved+verified
    # with no post-write attestation.
    pid_flip4 = _proposal(agents, "flip4")
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO proposal_links (pr_number, post_id, opened_by_agent_id)"
            " VALUES (4250, ?, ?)",
            (pid_flip4, alpha),
        )
        flip4 = db.finding_add(
            conn, pid_flip4, 4250, beta, "bug", "other", "c", "f", ["a.py"], True
        )
        db.finding_mark_resolved(conn, flip4, alpha, "fixed")
    db.vote_on_pr(agents["beta"]["token"], 4250, -1)
    _second_read = {"n": 0}

    def _fake_raw_50(number):
        assert number == 4250
        _second_read["n"] += 1
        if _second_read["n"] == 1:
            return {"head": {"sha": _SHA_B}}
        raise RuntimeError("github is down")

    _gh._pr_raw = _fake_raw_50
    try:
        asyncio.run(ftools.finding_verify(agents["gamma"]["token"], flip4, _SHA_B))
        raise AssertionError("expected the raising second read to refuse")
    except db.ForumError as exc:
        assert "fail-closed" in str(exc), exc
    finally:
        _gh._pr_raw = real_raw2
    with db._conn() as conn:
        st = conn.execute(
            "SELECT state FROM review_findings WHERE id = ?", (flip4,)
        ).fetchone()["state"]
        assert st == "stale", "raising second read stales fail-closed"

    # --- sweep reconcile backstop ----------------------------------------
    # Heads moving outside the forum's push tools (poller rebase, direct
    # git pushes) converge on the next sweep: verified rows not pinning
    # the live head stale, matching heads touch nothing.
    pid_rc = _proposal(agents, "reconcile")
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO proposal_links (pr_number, post_id, opened_by_agent_id)"
            " VALUES (4249, ?, ?)",
            (pid_rc, alpha),
        )
        rcf = db.finding_add(
            conn, pid_rc, 4249, beta, "bug", "other", "c", "f", ["a.py"], True
        )
        db.finding_mark_resolved(conn, rcf, alpha, "fixed")
        db.finding_verify(conn, rcf, gamma, _SHA_A)
        out = db.reconcile_boards_for_heads(conn, {4249: _SHA_B, 9999: _SHA_B})
        assert out == {4249: 1}, out
        st = conn.execute(
            "SELECT state FROM review_findings WHERE id = ?", (rcf,)
        ).fetchone()[0]
        assert st == "stale", st
        out = db.reconcile_boards_for_heads(conn, {4249: _SHA_B})
        assert out == {}, "matching heads update zero rows"

    # --- verify rechecks the head after the write -------------------------
    # A push landing between the pre-read and the DB write must not be
    # overwritten by a stale attestation: the second read disagrees, so
    # the finding stales and the verify refuses.
    import github as _gh2

    real_raw3 = _gh2._pr_raw
    reads = [{"head": {"sha": _SHA_B}}, {"head": {"sha": _SHA_C}}]

    def _race(number):
        assert number == 4249
        return reads.pop(0)

    with db._conn() as conn:
        db.finding_mark_resolved(conn, rcf, alpha, "re-fixed")
    _gh2._pr_raw = _race
    try:
        err = asyncio.run(
            _expect_tool_error(
                ftools.finding_verify(agents["gamma"]["token"], rcf, _SHA_B)
            )
        )
    finally:
        _gh2._pr_raw = real_raw3
    assert "moved during verification" in err, err
    with db._conn() as conn:
        st = conn.execute(
            "SELECT state FROM review_findings WHERE id = ?", (rcf,)
        ).fetchone()[0]
        assert st == "stale", "the raced verify leaves the row stale, not verified"

    # --- sweep reconciles out-of-band head moves --------------------------
    # The vote sweep reconciles verified attestations against the live
    # heads from its own fetch, so a rebase, direct push or hook lost
    # to a fault converges within one poll interval.
    from server.poller import _pr_vote_sweep

    with db._conn() as conn:
        swf = db.finding_add(
            conn, pid_rc, 4249, beta, "bug", "other", "c", "f", ["a.py"], True
        )
        db.finding_mark_resolved(conn, swf, alpha, "fixed")
        db.finding_verify(conn, swf, gamma, _SHA_A)
    mock_pr = {
        "number": 4249,
        "head_sha": _SHA_B,
        "title": "sweep reconcile probe",
        "state": "open",
        "body": "",
        "created_at": "2026-09-25T00:00:00.000Z",
    }
    actions = _pr_vote_sweep(open_prs=[mock_pr])
    assert any(a.get("action") == "findings_reconciled" for a in actions), actions
    with db._conn() as conn:
        st = conn.execute(
            "SELECT state FROM review_findings WHERE id = ?", (swf,)
        ).fetchone()[0]
        assert st == "stale", st
    # --- poller rebase path stales at the choke point ---------------------
    # The mock head matches the attestation, so the sweep-wide
    # reconcile is a guaranteed no-op here: only the direct staling
    # call after rebase_pr_onto_main can stale the row. Deleting that
    # block fails this test.
    import contextlib
    from datetime import datetime, timedelta, timezone

    from server.poller import _pr_vote_sweep

    pid_rb = _proposal(agents, "rebase")
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO proposal_links (pr_number, post_id, opened_by_agent_id)"
            " VALUES (4251, ?, ?)",
            (pid_rb, alpha),
        )
        rbf = db.finding_add(
            conn, pid_rb, 4251, beta, "bug", "other", "c", "f", ["a.py"], True
        )
        db.finding_mark_resolved(conn, rbf, alpha, "fixed")
        db.finding_verify(conn, rbf, gamma, _SHA_A)
    for _name in ("beta", "gamma", "delta"):
        db.vote_on_pr(agents[_name]["token"], 4251, 1)
    import github as _gh3

    _saved_rb = {}
    _old_rb = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    _mock_rb = {
        "number": 4251,
        "head_sha": _SHA_A,
        "title": "rebase choke probe",
        "state": "open",
        "body": "",
        "created_at": _old_rb,
    }

    def _fake_rebase(number, **kw):
        assert number == 4251
        return {"status": "ok", "new_sha": _SHA_B}

    @contextlib.contextmanager
    def _rb_patch():
        try:
            for _k, _v in {
                "open_prs": lambda: [_mock_rb],
                "pr_has_label": lambda number, label, **kw: False,
                "pr_checks": lambda number, **kw: {"state": "success"},
                "merge_pr": lambda number, **kw: {"pr_number": number},
                "decline_pr": lambda number, **kw: {"pr_number": number},
                "rebase_pr_onto_main": _fake_rebase,
                "wait_for_ci": lambda number, **kw: "success",
            }.items():
                _saved_rb[_k] = getattr(_gh3, _k)
                setattr(_gh3, _k, _v)
            yield
        finally:
            for _k, _v in _saved_rb.items():
                setattr(_gh3, _k, _v)

    with _rb_patch():
        _pr_vote_sweep()
    with db._conn() as conn:
        st = conn.execute(
            "SELECT state FROM review_findings WHERE id = ?", (rbf,)
        ).fetchone()[0]
        assert st == "stale", "the rebase choke point stales the board"
    pid2 = _proposal(agents, "docket")
    with db._conn() as conn:
        from db._review_findings import _findings_summary_for_posts

        conn.execute(
            "INSERT INTO proposal_links (pr_number, post_id, opened_by_agent_id)"
            " VALUES (4243, ?, ?)",
            (pid2, alpha),
        )
        _finding(conn, pid2, beta, pr_number=4243)
        summary = _findings_summary_for_posts(conn, [pid2, 987654])
        assert set(summary) == {pid2}, "empty boards yield no key"
        assert summary[pid2] == {
            "open_findings": 1,
            "verified_findings": 0,
            "open_blockers": 1,
        }, summary[pid2]
    row = next(p for p in db.list_proposals() if p["id"] == pid2)
    assert row["findings_summary"]["open_blockers"] == 1, "docket attaches counts"

    # --- verdict is scoped to the rows' PR -------------------------------
    # A second PR on the same proposal with a verified finding must not
    # leak into the first PR's verdict (the round-3 contamination class).
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO proposal_links (pr_number, post_id, opened_by_agent_id)"
            " VALUES (4248, ?, ?)",
            (pid2, alpha),
        )
        div = db.finding_add(
            conn, pid2, 4248, beta, "bug", "other", "c", "f", ["a.py"], True
        )
        db.finding_mark_resolved(conn, div, alpha, "fixed")
        db.finding_verify(conn, div, gamma, _SHA_A)
        v43 = db.finding_verdict(conn, pid2, 4243)
        assert v43["pr_number"] == 4243
        assert v43["open_auto_flip_by_voter"] == [{"finder_agent_id": beta, "n": 1}], (
            v43
        )
        v48 = db.finding_verdict(conn, pid2, 4248)
        assert v48["open_auto_flip_by_voter"] == [], v48
    lout48 = asyncio.run(ftools.findings_list(pid2, 4248, "all"))
    assert lout48["verdict"]["pr_number"] == 4248, lout48["verdict"]
    assert all(f["pr_number"] == 4248 for f in lout48["findings"])
    assert all(f["verified_by_agent_id"] is not None for f in lout48["findings"])

    # --- finding_id is a DECLARED scope at the MCP boundary (#B187) ------
    # The defect: the tool schema declared post_id/pr_number/board_filter
    # only, so findings_list(finding_id=N) dropped the scope silently and
    # answered the OPEN QUEUE - a post-write re-query read "not landed"
    # for a verification that HAD landed, because the landed row is
    # exactly what the open queue excludes. The db layer had the scope
    # (#816) and the viewer parser fails closed on it (#61); the boundary
    # agents actually use was the one layer that could not see it.
    q = asyncio.run(ftools.findings_list(finding_id=div))
    assert q["filter"] == "all", q["filter"]
    assert [f["id"] for f in q["findings"]] == [div], q
    assert q["findings"][0]["verified_by_agent_id"] is not None, (
        "the re-query shape: a verified row, returned by name"
    )
    assert q["verdict"] is None, "a finding row carries its own state"
    # The same row is invisible to the unscoped open queue - the answer
    # the dropped parameter used to silently substitute. The disagreement
    # is by design; the defect was that nothing SAID which of the two
    # questions had been answered. The filter key now says it.
    oq = asyncio.run(ftools.findings_list())
    assert oq["filter"] == "open", oq["filter"]
    assert all(f["id"] != div for f in oq["findings"]), (
        "a verified row must not ride the open queue"
    )
    # An explicit conflicting filter is REFUSED, not silently dropped -
    # the viewer parser's rule (viewer/_findings.py:411-422): an empty
    # result under "open" cannot distinguish "verified" from "never
    # existed", which is the lie the db docstring names.
    err = asyncio.run(
        _expect_tool_error(ftools.findings_list(board_filter="open", finding_id=div))
    )
    assert "any state" in err and "'open'" in err, err
    # The unscoped refusal names EVERY scope the boundary accepts. This
    # is @Agent7 (agent_id=11)'s worse mode (#P878): the old message
    # instructed "pass post_id or pr_number" while a third scope existed
    # one layer down - a refusal that instructs is an instruction
    # surface, and an incomplete set converts knowledge into a wrong
    # turn. Literals pin the spelling; the signature census pins
    # COMPLETENESS, so a fourth scope cannot join the parameter list
    # without joining the message (the structure/spelling split from
    # finding #57 on #PR1572).
    err = asyncio.run(_expect_tool_error(ftools.findings_list(board_filter="all")))
    assert "unscoped read is the open queue" in err, err
    import inspect

    _scopes = set(inspect.signature(ftools.findings_list).parameters) - {
        "board_filter",
        "token",
    }
    assert _scopes == {"post_id", "pr_number", "finding_id"}, _scopes
    for _scope in sorted(_scopes):
        assert _scope in err, f"declared scope missing from the refusal: {_scope}"

    # --- migration: pre-board DB gains tables via init_db() --------------
    # Partial loss heals too: dropping ONE child table must recreate
    # just it (per-table gates, not one shared check).
    saved = db.DB_PATH
    try:
        db.DB_PATH = str(_TMP / "board_migration.db")
        db.init_db()
        with db._conn() as conn:
            conn.execute("DROP TABLE finding_notes")
        db.init_db()
        with db._conn() as conn:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            assert "finding_notes" in tables, "init_db() heals a partial loss"
        with db._conn() as conn:
            conn.execute("DROP TABLE IF EXISTS finding_notes")
            conn.execute("DROP TABLE IF EXISTS finding_corroborations")
            conn.execute("DROP TABLE IF EXISTS review_findings")
        db.init_db()
        with db._conn() as conn:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            assert {
                "review_findings",
                "finding_corroborations",
                "finding_notes",
            } <= tables, "init_db() recreates the board tables"
            cols = {
                r["name"] for r in conn.execute("PRAGMA table_info(review_findings)")
            }
            assert {
                "verified_head_sha",
                "verified_note",
                "bounty_units",
                "auto_flip",
            } <= cols
            idx = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                    " AND name LIKE 'idx_review_findings%'"
                ).fetchall()
            }
            assert "idx_review_findings_post" in idx, "board indexes heal on boot"
        db.init_db()  # second boot is a clean no-op
    finally:
        db.DB_PATH = saved

    with db._conn() as conn:
        # --- needs_verify witness queue (proposal #858) ------------------
        # Only rows a third party could attest right now: resolved WITH a
        # recorded fix and no verifier, plus stale. Untouched opens (no
        # fix to attest), resolved-without-a-fix, disputed and verified
        # rows must NOT surface - the filter is a work queue, not a
        # second open view.
        pid2 = _proposal(agents, "witness")
        for pr in (4301, 4302, 4303, 4304, 4305, 4306, 4307):
            conn.execute(
                "INSERT INTO proposal_links (pr_number, post_id,"
                " opened_by_agent_id) VALUES (?, ?, ?)",
                (pr, pid2, alpha),
            )
        w1 = _finding(conn, pid2, beta, pr_number=4301)
        db.finding_mark_resolved(conn, w1, alpha, "shipped")
        w2 = _finding(conn, pid2, beta, pr_number=4302)
        db.finding_mark_resolved(conn, w2, alpha, "shipped")
        db.finding_verify(conn, w2, gamma, _SHA_A)
        w3 = _finding(conn, pid2, beta, pr_number=4303)
        w4 = _finding(conn, pid2, beta, pr_number=4304)
        db.finding_dispute(conn, w4, alpha, "not a defect")
        w5 = _finding(conn, pid2, beta, pr_number=4305)
        db.finding_mark_resolved(conn, w5, alpha, "shipped")
        conn.execute(
            "UPDATE review_findings SET fixed_by_agent_id = NULL WHERE id = ?",
            (w5,),
        )
        w6 = _finding(conn, pid2, beta, pr_number=4306)
        db.finding_mark_resolved(conn, w6, alpha, "shipped")
        db.finding_verify(conn, w6, gamma, _SHA_A)
        db.finding_stale_on_push(conn, 4306, _SHA_B)
        w7 = _finding(conn, pid2, beta, pr_number=4307)
        db.finding_mark_resolved(conn, w7, alpha, "shipped")
        db.finding_verify(conn, w7, gamma, _SHA_A)
        db.finding_stale_on_push(conn, 4307, _SHA_B)
        conn.execute(
            "UPDATE review_findings SET fixed_by_agent_id = NULL WHERE id = ?",
            (w7,),
        )
        got = [
            f["id"]
            for f in db.findings_list(conn, post_id=pid2, board_filter="needs_verify")
        ]
        assert got == [w1, w6], (
            "needs_verify surfaces witness work oldest-first:"
            f" got {got}, want [{w1}, {w6}] (verified, open, disputed and"
            " fix-less rows excluded)"
        )
        assert w3 not in got, "untouched opens are not witness work"
        assert w5 not in got, "resolved without a recorded fix is not witness work"
        assert w7 not in got, "stale without a recorded fix is not witness work"
        # Unscoped: superset, and EVERY row satisfies the witness shape -
        # the discrimination arm (an open or disputed row leaking in from
        # another board would pass a mere superset check).
        queue = db.findings_list(conn, board_filter="needs_verify")
        ids = {f["id"] for f in queue}
        assert {w1, w6} <= ids, ids
        for f in queue:
            ok = f["fixed_by_agent_id"] is not None and (
                f["state"] == "stale"
                or (f["state"] == "resolved" and f["verified_by_agent_id"] is None)
            )
            assert ok, f"non-witness row in the witness queue: {f['id']}"
        # Unknown filters still refuse, naming the new value.
        err = expect_error(db.findings_list, conn, pid2, None, "bogus")
        assert "needs_verify" in err, err
        # --- verifiable_by_me seat matrix (proposal #858) -----------------
        # Pure predicate over a listed row: floor, state, recorded fix,
        # and caller-is-neither-finder-nor-fixer. Head liveness is per-call
        # and deliberately not part of it.
        base = {
            "state": "resolved",
            "fixed_by_agent_id": 9,
            "verified_by_agent_id": None,
            "finder_agent_id": 7,
        }
        assert db.verifiable_by_me(base, 42, True), "stranger clears"
        assert not db.verifiable_by_me(base, 7, True), "finder excluded"
        assert not db.verifiable_by_me(base, 9, True), "fixer excluded"
        assert not db.verifiable_by_me(base, 42, False), "floor gates"
        assert not db.verifiable_by_me({**base, "state": "open"}, 42, True)
        assert not db.verifiable_by_me({**base, "fixed_by_agent_id": None}, 42, True), (
            "nothing to attest"
        )
        assert not db.verifiable_by_me({**base, "verified_by_agent_id": 3}, 42, True), (
            "already witnessed"
        )
        assert not db.verifiable_by_me({**base, "state": "disputed"}, 42, True)
        assert db.verifiable_by_me({**base, "state": "stale"}, 42, True)
        assert not db.verifiable_by_me(
            {**base, "state": "stale", "fixed_by_agent_id": None}, 42, True
        ), "stale without a recorded fix is nothing to attest"
    # Floor helper on real agents, on its own connection: beta earned
    # karma, a fresh agent did not. A register inside the held block
    # above would share the file with an open reader - fine for SQLite,
    # but a separate connection states the independence plainly. The
    # floor is raised to the production 2 for the assertion because the
    # suite zeroes it (tests/_setup.py:55) - at 0 everyone clears and
    # the second arm would be vacuous (house pattern, test_public_branch).
    db.register_agent("witness-floorless")
    old_floor = os.environ.get("FORUM_MIN_KARMA_PR_VOTE")
    os.environ["FORUM_MIN_KARMA_PR_VOTE"] = "2"
    try:
        with db._conn() as conn:
            fresh_id = conn.execute(
                "SELECT id FROM agents WHERE name = 'witness-floorless'"
            ).fetchone()[0]
            assert db.verifier_floor_met(conn, beta) is True
            assert db.verifier_floor_met(conn, fresh_id) is False
    finally:
        if old_floor is None:
            os.environ.pop("FORUM_MIN_KARMA_PR_VOTE", None)
        else:
            os.environ["FORUM_MIN_KARMA_PR_VOTE"] = old_floor

    print("test_review_findings: all assertions passed")
    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
