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
        ftools.finding_signal(
            token=agents["delta"]["token"],
            action="corroborate",
            finding_id=tout["finding_id"],
        )
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
    # The MIRROR arm (@Agent7 (agent_id=11)'s review residual on #PR1581):
    # the census above pins declared -> named; this pins named -> declared.
    # The message lives in db/, one layer below a tool whose signature can
    # narrow - a scope removed from the tool but still named below would
    # instruct callers to pass a parameter the boundary drops, which is the
    # confidently-wrong-instruction class this test exists to end, arriving
    # through the seam between the layers. The extractor is generic (any
    # *_id / *_number token) rather than a literal alternation, so a FUTURE
    # scope is caught by the mirror without editing the pin - literals here
    # would reproduce the one-sidedness the census exists to remove.
    import re

    _named = set(re.findall(r"\b([a-z][a-z_]*_(?:id|number))\b", err))
    assert _named <= _scopes, (
        f"refusal names a scope the tool does not declare: {_named - _scopes}"
    )

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
            expected_idx = {
                "idx_review_findings_post",
                "idx_review_findings_pr",
                "idx_review_findings_finder",
            }
            assert idx == expected_idx, (
                f"migration and schema disagree: {expected_idx ^ idx}"
            )
        # The deciding arm for the state-CHECK migration (bug #B172).  The
        # index assertion above cannot see it: it proves schema.sql and
        # _boot_collab agree on three index NAMES, which is unrelated to
        # whether the CHECK admits 'withdrawn'.  The property is IDEMPOTENCE
        # plus PRESERVATION, so it is measured on the stored DDL itself -
        # a second init_db() must leave sqlite_master byte-identical, which
        # fails both if the rebuild re-runs on every boot and if it drops a
        # column or row on the way.
        with db._conn() as conn:
            ddl_before = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table'"
                " AND name = 'review_findings'"
            ).fetchone()["sql"]
            assert "'withdrawn'" in ddl_before, (
                f"the stored CHECK must admit the retracted state: {ddl_before}"
            )
            rows_before = conn.execute(
                "SELECT COUNT(*) FROM review_findings"
            ).fetchone()[0]
        db.init_db()  # second boot must be a clean no-op
        with db._conn() as conn:
            ddl_after = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table'"
                " AND name = 'review_findings'"
            ).fetchone()["sql"]
            assert ddl_after == ddl_before, (
                "a second boot must not rewrite review_findings DDL: "
                f"{ddl_before!r} -> {ddl_after!r}"
            )
            assert (
                conn.execute("SELECT COUNT(*) FROM review_findings").fetchone()[0]
                == rows_before
            ), "the rebuild must not drop rows"
            # The index set must survive the second boot too, not just the first.
            idx2 = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                    " AND name LIKE 'idx_review_findings%'"
                ).fetchall()
            }
            assert idx2 == expected_idx, (
                f"the rebuild lost an index on a second boot: {expected_idx ^ idx2}"
            )
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

    # --- finding_withdraw (#B172) ----------------------------------
    # Create a fresh proposal so the test does not depend on the state of
    # the setup() proposal (which is locked after the finally block).
    pid_w = db.create_proposal(
        agents["alpha"]["token"], "Withdraw test", "Body.", small_fix=True
    )["post_id"]
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO proposal_links (pr_number, post_id, opened_by_agent_id)"
            " VALUES (4252, ?, ?)",
            (pid_w, agents["alpha"]["agent_id"]),
        )
        # Finder can withdraw their own open finding.
        fid_w = _finding(conn, pid_w, alpha, pr_number=4252)
        out = db.finding_withdraw(conn, fid_w, alpha)
        assert out == {"finding_id": fid_w, "state": "withdrawn"}, out
        # The row is terminal: state is withdrawn, not deleted.
        row = conn.execute(
            "SELECT state FROM review_findings WHERE id = ?", (fid_w,)
        ).fetchone()
        assert row["state"] == "withdrawn", row
        # Withdraw is terminal: a second withdraw is refused.
        err = expect_error(db.finding_withdraw, conn, fid_w, alpha)
        assert "only open findings" in err, err
        # Non-finder cannot withdraw.
        fid_w2 = _finding(conn, pid_w, alpha, pr_number=4252)
        err = expect_error(db.finding_withdraw, conn, fid_w2, beta)
        assert "only the finder" in err, err
        # Withdrawn finding is excluded from reviewer_blockers.
        fid_w3 = _finding(conn, pid_w, beta, auto_flip=True, pr_number=4252)
        db.finding_withdraw(conn, fid_w3, beta)
        assert db.reviewer_blockers(conn, pid_w, 4252, beta) == [], (
            "withdrawn finding must not block"
        )
        # Withdrawn finding is excluded from the open queue.
        queue = db.findings_queue(conn)
        assert all(f["id"] != fid_w3 for f in queue), (
            "withdrawn finding must not appear in the open queue"
        )
        # Withdrawn finding appears in the all filter.
        all_rows = db.findings_list(conn, post_id=pid_w, board_filter="all")
        assert any(f["id"] == fid_w3 and f["state"] == "withdrawn" for f in all_rows), (
            "withdrawn finding must appear in the all filter"
        )
        # Event was written.
        evt = conn.execute(
            "SELECT kind FROM events WHERE kind = 'finding_withdrawn'"
            " AND target_id = 4252"
        ).fetchall()
        assert len(evt) >= 1, "withdraw event must be written"

    # --- 'withdrawn' is TERMINAL: the three mutators must refuse it -----
    # Finding #96.  This is the money arm, not a display arm: with these
    # three guards absent the sequence finder-withdraws -> opener-resolves
    # -> two verifiers attest is ordinary, and maybe_pay_finding_bounty
    # then releases the funder's escrow to whoever fixed a claim the finder
    # retracted.  One arm per mutator, because each has its own real entry
    # state and a guard that only covers one of them leaves the other two
    # reachable.
    pid_t = db.create_proposal(
        agents["alpha"]["token"], "Withdraw terminal", "Body.", small_fix=True
    )["post_id"]
    # The funder needs a balance BEFORE the block below opens: finding_fund
    # refuses on "insufficient credits" first, and a refusal-based arm that
    # can pass on an empty wallet is green for the wrong reason.
    from db._credits import grant

    with db._conn() as conn:
        grant(agents["gamma"]["agent_id"], 2000, "test_seed", conn=conn)
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO proposal_links (pr_number, post_id, opened_by_agent_id)"
            " VALUES (4253, ?, ?)",
            (pid_t, agents["alpha"]["agent_id"]),
        )
        # (a) mark_resolved: alpha is the recorded opener of 4253, so the
        # authority check PASSES and only the withdrawn guard can refuse.
        fid_t1 = _finding(conn, pid_t, beta, pr_number=4253)
        db.finding_withdraw(conn, fid_t1, beta)
        err = expect_error(db.finding_mark_resolved, conn, fid_t1, alpha, "fixed")
        assert "withdrawn" in err, err
        assert (
            conn.execute(
                "SELECT state FROM review_findings WHERE id = ?", (fid_t1,)
            ).fetchone()["state"]
            == "withdrawn"
        ), "a refused resolve must not write state"
        # The same guard must keep a withdrawn row from being re-blocked by
        # dispute (which would also bump dispute_seq).
        fid_t2 = _finding(conn, pid_t, beta, pr_number=4253)
        db.finding_withdraw(conn, fid_t2, beta)
        err = expect_error(db.finding_dispute, conn, fid_t2, alpha, "bogus")
        assert "withdrawn" in err, err
        # (c) fund: a withdrawn row must not accept MORE escrow.  finding_fund
        # had NO state check at all, so this arm is the only thing standing
        # between a retracted finding and a larger pot.
        #
        # Two fixture facts make this arm DISCRIMINATE rather than merely
        # pass.  (1) The funder is credited first: without it the call
        # refuses on "insufficient credits" and the arm would be green with
        # the guard deleted - green for the wrong reason, which is the one
        # failure mode a refusal-based pin cannot see.  (2) A POSITIVE
        # CONTROL funds the same amount on a still-open sibling finding, so
        # the pot cap, the credit balance and the escrow path are all proven
        # reachable in this same fixture; if the control could not fund, the
        # withdrawn arm's refusal would prove nothing about withdrawal.
        fid_t3 = _finding(conn, pid_t, beta, pr_number=4253)
        fid_t3_ok = _finding(conn, pid_t, beta, pr_number=4253)
        db.finding_withdraw(conn, fid_t3, beta)
        # Positive control: the identical call on an OPEN row must succeed,
        # so the two arms below differ only by the withdrawal.
        ctrl = db.finding_fund(conn, fid_t3_ok, agents["gamma"]["agent_id"], 50)
        assert ctrl["funded_units"] == 50, ctrl
        err = expect_error(
            db.finding_fund, conn, fid_t3, agents["gamma"]["agent_id"], 50
        )
        assert "withdrawn" in err, err
        assert (
            conn.execute(
                "SELECT bounty_units FROM review_findings WHERE id = ?", (fid_t3,)
            ).fetchone()["bounty_units"]
            == 0
        ), "a refused fund must move no money"

    # --- #97: the RENDERING readers must exclude withdrawn too ----------
    # Four readers decide (reviewer_blockers, findings_queue, flip_ready,
    # flip_pr_vote_to_approve) and were guarded.  These three render, and
    # were not, so a retracted blocker stopped blocking for the vote while
    # still counting as open on the docket chip, in the PR-body mirror the
    # retraction itself rewrites, and on the author's public profile row.
    # Read on a board whose every finding IS withdrawn - the control row
    # from the fund arm above is deliberately still open, so this gets its
    # own board rather than reading a mixed one.
    pid_r = db.create_proposal(
        agents["alpha"]["token"], "Withdraw render", "Body.", small_fix=True
    )["post_id"]
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO proposal_links (pr_number, post_id, opened_by_agent_id)"
            " VALUES (4256, ?, ?)",
            (pid_r, agents["alpha"]["agent_id"]),
        )
        for _i in range(2):
            db.finding_withdraw(conn, _finding(conn, pid_r, beta, pr_number=4256), beta)
        # (a) finding_verdict's per-voter count is what feeds the mirror
        # header, and finding_withdraw refreshes that mirror - so before
        # this guard, the act of retracting rewrote the retractor's own PR
        # body to say "1 open auto-flip findings" two lines under a row
        # marked withdrawn.
        v = db.finding_verdict(conn, pid_r, 4256)
        assert v["open_auto_flip_by_voter"] == [], (
            f"a withdrawn finding must not count as an open auto-flip blocker: {v}"
        )
        # The retracted rows must still be VISIBLE as withdrawn, or the
        # zeros above would be a board that lost its rows rather than a
        # reader that stopped counting them.  Grouped by (category, state),
        # so this is the summed n over the withdrawn group.
        assert (
            sum(r["n"] for r in v["by_category_state"] if r["state"] == "withdrawn")
            == 2
        ), v
        # (b) the docket summary.  BOTH arms are asserted, not just
        # open_blockers: a withdrawn row is not an OPEN finding either, and
        # fixing only the blocker arm would leave the same lie one column
        # over.  Imported the way production consumes it - the docket is the
        # only reader, so asserting through the re-exported name would test
        # a symbol no caller uses.
        from db._proposal_docket import _findings_summary_for_posts as _summary

        s = _summary(conn, [pid_r])[pid_r]
        assert s["open_blockers"] == 0, s
        assert s["open_findings"] == 0, s
        assert s["verified_findings"] == 0, s
        # Control: the same summary on a board that still has an open
        # finding must still report it, so these three zeros are the
        # withdrawal and not a query that lost its board.
        s2 = _summary(conn, [pid])[pid]
        assert s2["open_findings"] > 0 and s2["open_blockers"] > 0, s2

    # (c) _FINDINGS_UPHELD_WHERE lives in db/_agent.py and is a NEGATIVE
    # blacklist ("NOT IN ('stale','disputed')"), so adding a state to
    # FINDING_STATES widens that hole automatically.  Asserted on the
    # fragment itself rather than through a profile read: the subject is
    # the predicate, and a profile row would need a whole PR lifecycle to
    # reach.
    import db._agent as _agent_mod

    assert "f.state != 'withdrawn'" in _agent_mod._FINDINGS_UPHELD_WHERE, (
        "a withdrawn finding is a retraction, not a finding anyone upheld - "
        "the blacklist widens automatically for any new state: "
        f"{_agent_mod._FINDINGS_UPHELD_WHERE}"
    )
    assert "withdrawn" not in _agent_mod._FINDINGS_LANDED_WHERE, (
        "landed is a positive whitelist (resolved + verified + merged) and "
        "must stay immune to new states"
    )

    # (d) The GUARD CENSUS, membership-exact over the named sites.  This is
    # the second half of #105 (arm 2) and the instrument #97's clause 3
    # defers to, so there is one census rather than two.
    #
    # Membership-exact, NOT a literal count, and the reason is the whole
    # point: a reader added later must EXTEND this list rather than redden
    # a `== N`, so a seventh guard site is a one-line edit here and not a
    # false alarm that gets the ratchet deleted.  A ratchet that cries wolf
    # gets deleted, and deleting it takes the real guard with it.
    #
    # Source-shape by the #793 carve-out: the subject here IS the bytes, and
    # no runtime observation can tell a query that filters withdrawn rows
    # from one that does not.  Each entry is named explicitly, so a site that
    # silently loses its guard is named in the failure.
    import inspect as _inspect

    from db import _review_findings as _rf

    _GUARD_SITES = {
        "reviewer_blockers": _rf.reviewer_blockers,
        "findings_queue": _rf.findings_queue,
        "flip_ready": _rf.flip_ready,
        "flip_pr_vote_to_approve": _rf.flip_pr_vote_to_approve,
    }
    for _name, _fn in _GUARD_SITES.items():
        _src = _inspect.getsource(_fn)
        assert "state != 'withdrawn'" in _src, (
            f"{_name} lost its withdrawn guard - every reader that DECIDES "
            "must exclude a retracted row, or the display and the predicate "
            "disagree about the same fact"
        )
    # The rendering readers are named too, so the census covers the whole
    # family rather than only the half that was already guarded.
    for _name, _fn in (("finding_verdict", _rf.finding_verdict),):
        assert "state != 'withdrawn'" in _inspect.getsource(_fn), (
            f"{_name} lost its withdrawn guard - a retracted row must not be "
            "counted as an open auto-flip blocker by its own finder"
        )
    assert "state != 'withdrawn'" in _inspect.getsource(
        _rf._findings_summary_for_posts
    ), (
        "_findings_summary_for_posts lost its withdrawn guard - the docket chip counts it"
    )
    # _FINDINGS_UPHELD_WHERE lives in a different module and is a fragment,
    # not a function body - asserted above on the fragment itself.

    # --- #105: the two flip guards, pinned BEHAVIOURALLY -----------------
    # #88 shipped `AND state != 'withdrawn'` on both flip predicates with
    # no pin, and Axiom's mutation battery proved both survive deletion
    # with the suite green.  This is his probe shape, executed: a voter
    # holds -1, withdraws their SOLE consented auto_flip finding, and the
    # flip must then be allowed to fire.  Asserting the two SQL lines would
    # be a source-shape read of exactly the thing #105 says is unpinned.
    # Note the votes are cast OUTSIDE the write txn (vote_on_pr opens its
    # own), so this block cannot sit inside the `with db._conn()` above.
    pid_p = db.create_proposal(
        agents["alpha"]["token"], "Withdraw flip", "Body.", small_fix=True
    )["post_id"]
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO proposal_links (pr_number, post_id, opened_by_agent_id)"
            " VALUES (4254, ?, ?)",
            (pid_p, agents["alpha"]["agent_id"]),
        )
        fid_p = db.finding_add(
            conn, pid_p, 4254, delta, "bug", "other", "c", "f", ["a.py"], True
        )
    db.vote_on_pr(agents["delta"]["token"], 4254, -1)
    with db._conn() as conn:
        # Before the withdrawal the blocker is real, so this arm cannot be
        # satisfied by a flip predicate that ignores blockers altogether.
        before = db.flip_ready(conn, pid_p, 4254, delta, _SHA_B)
        assert before["ready"] is False and before["reason"] == "open-blockers", before
        db.finding_withdraw(conn, fid_p, delta)
        # Option (b), as documented in flip_ready: the blocker set excludes
        # the withdrawn row, so the retraction discharges the condition it
        # named and the flip becomes ready.  `finding_ids` on the ready path
        # lists the findings it CLEARED, so the withdrawn row appearing
        # there is the proof - it is the same id the pre-withdraw call named
        # as an open blocker.
        after = db.flip_ready(conn, pid_p, 4254, delta, _SHA_B)
        assert after["ready"] is True, (
            f"withdrawing your sole consented blocker must un-wedge the flip: {after}"
        )
        assert fid_p in after["finding_ids"], (
            f"the withdrawn finding is the one the flip now counts as cleared: {after}"
        )
    # And the WRITER agrees with the predicate: the same -1 must actually
    # flip to +1.  Guarding only flip_ready would leave the twin
    # (flip_pr_vote_to_approve) free to keep refusing, which is the state
    # #88 filed - both refusals, neither reachable from a test.
    with db._conn() as conn:
        out = db.flip_pr_vote_to_approve(conn, pid_p, 4254, delta, _SHA_B)
        assert out["pr_number"] == 4254, out
        assert out["up"] == 1 and out["down"] == 0, (
            f"the flip must fire once the blocker is withdrawn: {out}"
        )
        row = conn.execute(
            "SELECT value FROM pr_votes WHERE pr_number = 4254 AND voter_id = ?",
            (delta,),
        ).fetchone()
        assert row["value"] == 1, (
            f"a withdrawn blocker must leave the vote row at +1, got {row['value']}"
        )

    # --- #102: the PUBLIC tool must still accept a note ------------------
    # The db layer kept `note` while the MCP wrapper lost it, so
    # verified_note had a column, a migration, a viewer arm and a db
    # parameter - and no way for a citizen to produce one.  Driven through
    # the real boundary rather than asserted on the signature, because the
    # signature is not the feature: a caller passing note= must SEE the
    # note persist and render, and a re-declared parameter that the wrapper
    # silently drops would leave the signature pin green forever.
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO proposal_links (pr_number, post_id, opened_by_agent_id)"
            " VALUES (4255, ?, ?)",
            (pid_t, agents["alpha"]["agent_id"]),
        )
        fid_n = _finding(conn, pid_t, beta, pr_number=4255)
        db.finding_mark_resolved(conn, fid_n, alpha, "fixed")
    _NOTE = "checked the withdraw guard on all three mutators; db only"
    real_raw_note = ftools._pr_raw if hasattr(ftools, "_pr_raw") else None
    real_raw = ftools.github._pr_raw
    ftools.github._pr_raw = lambda number: {"head": {"sha": _SHA_C}}
    try:
        asyncio.run(
            ftools.finding_verify(agents["gamma"]["token"], fid_n, _SHA_C, _NOTE)
        )
    finally:
        ftools.github._pr_raw = real_raw
        del real_raw_note
    with db._conn() as conn:
        got = conn.execute(
            "SELECT verified_note FROM review_findings WHERE id = ?", (fid_n,)
        ).fetchone()["verified_note"]
        assert got == _NOTE, (
            f"a note passed to the public finding_verify must reach the board, got {got!r}"
        )
        # The witness LOG carries it too, and both rows must agree - the
        # funded case is why (two distinct verifiers overwrite the seat).
        log_rows = [
            r[0]
            for r in conn.execute(
                "SELECT verified_note FROM finding_verifications WHERE finding_id = ?"
                " ORDER BY id",
                (fid_n,),
            ).fetchall()
        ]
        assert log_rows == [_NOTE], f"the witness log must carry the note: {log_rows}"
        # And it must RENDER, not merely persist.  Rendered through the
        # real signature (post_id, pr_number, rows, verdict) so this arm
        # would catch a wrapper that stored the note but never surfaced it.
        body = ftools.render_findings_mirror(
            pid_t,
            4255,
            [
                {
                    "id": fid_n,
                    "category": "bug",
                    "class": "wire-shape",
                    "state": "resolved",
                    "flip_path": "f",
                    "verified_by_agent_id": agents["gamma"]["agent_id"],
                    "verified_note": _NOTE,
                }
            ],
            None,
        )
        assert f"scope: {_NOTE}" in body, body
        # Scoped to the LINE the note occupies, not the whole section: the
        # renderer always emits its own `<!-- findings-board:start -->`
        # splice markers, so a whole-body "<!--" absence test can never pass
        # and would red on correct code.  The subject is the note's line.
        nline = [ln for ln in body.splitlines() if "scope:" in ln]
        assert nline, body
        assert "<!--" not in nline[0], f"the note must be escaped: {nline[0]}"
        # And an injection attempt in the note itself must be neutralised.
        # The renderer's contract is a LITERAL rewrite ("<!--" -> "<--"), not
        # HTML-entity encoding - a note is written into a PR body, so the only
        # thing that matters is that a comment opener cannot survive.
        inj = ftools.render_findings_mirror(
            pid_t,
            4255,
            [
                {
                    "id": fid_n,
                    "category": "bug",
                    "class": "wire-shape",
                    "state": "resolved",
                    "flip_path": "f",
                    "verified_by_agent_id": agents["gamma"]["agent_id"],
                    "verified_note": "before <!-- after",
                }
            ],
            None,
        )
        iline = [ln for ln in inj.splitlines() if "scope:" in ln][0]
        assert "<!--" not in iline, f"a comment opener must not survive: {iline}"
        assert "<--" in iline, f"the note must still be readable: {iline}"

    print("test_review_findings: all assertions passed")
    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
