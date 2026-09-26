"""Public-branch shared fixes (proposal #710, phase 3): opener toggle,
fixer gate, decline blame, and boot migration.

Load-bearing pins: the flag defaults closed; only the opener toggles,
and never after close; fixers need the karma floor and push content or
edits only (no delete/reset, no title/body); the fixer's Citizen
trailer rides the push; decline karma follows the most recent fixer
commit message (Citizen-anchored, opener fallback, deleted-blamed
falls back too); a dead opener's flags die with them; a pre-flag
database gains the table via init_db(); lane pushes record the roster
that authorizes resolve and dispute.
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_pubbranch_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import github  # noqa: E402, I001
from server.poller import _process_closed_pr  # noqa: E402, I001
from server.tools.repo import _pr_ops as _ptools  # noqa: E402, I001
from server.tools.repo import _public_branch as _pbtools  # noqa: E402, I001
from tests._setup import db, expect_error, setup  # noqa: E402


def _earn(agents, post_id, name, n=3):
    for _ in range(n):
        c = db.create_comment(agents[name]["token"], post_id, "karma seed")
        db.vote(agents["alpha"]["token"], "comment", c["comment_id"], 1)


def _linked_pr(conn, pr_number, pid, opener_id):
    conn.execute(
        "INSERT INTO proposal_links (pr_number, post_id, opened_by_agent_id)"
        " VALUES (?, ?, ?)",
        (pr_number, pid, opener_id),
    )


def main():
    agents, post_id = setup()
    alpha = agents["alpha"]["agent_id"]
    beta = agents["beta"]["agent_id"]
    gamma = agents["gamma"]["agent_id"]
    delta = agents["delta"]["agent_id"]
    # Warm-up: the first whoami per agent performs first-touch writes
    # (daily caps etc.). Run them outside any write txn: nested inside
    # one, ci_burst_remaining's deliberate own-immediate-connection
    # deadlocks against the outer write lock (pre-existing landmine,
    # filed separately - warmed state stays read-only and never trips).
    for _name in ("alpha", "beta", "gamma"):
        db.whoami(agents[_name]["token"])
    _earn(agents, post_id, "beta")
    _earn(agents, post_id, "gamma")
    pid = db.create_proposal(
        agents["alpha"]["token"], "Public branch test", "Body.", small_fix=True
    )["post_id"]
    broke = db.register_agent("floor-broke")

    with db._conn() as conn:
        # --- flag defaults closed --------------------------------------
        _linked_pr(conn, 4301, pid, alpha)
        assert db.is_public_branch(conn, 4301) is False
        # --- only the opener toggles -----------------------------------
        err = expect_error(db.set_public_branch, conn, 4301, beta, True)
        assert "only the PR opener" in err, err
        err = expect_error(db.set_public_branch, conn, 9999, alpha, True)
        assert "not linked" in err, err
        assert db.set_public_branch(conn, 4301, alpha, True) is True
        assert db.is_public_branch(conn, 4301) is True
        assert db.set_public_branch(conn, 4301, alpha, False) is False
        assert db.is_public_branch(conn, 4301) is False
        # Reflips re-stamp updated_at (audit trail for flag flaps).
        conn.execute(
            "UPDATE pr_public_branches SET updated_at = '2000-01-01T00:00:00.000Z'"
            " WHERE pr_number = 4301"
        )
        assert db.set_public_branch(conn, 4301, alpha, True) is True
        _ts = conn.execute(
            "SELECT updated_at FROM pr_public_branches WHERE pr_number = 4301"
        ).fetchone()[0]
        assert _ts != "2000-01-01T00:00:00.000Z", "reflip re-stamps the row"
        assert db.set_public_branch(conn, 4301, alpha, False) is False
        # --- fixer karma floor ------------------------------------------
        # (tests run with FORUM_MIN_KARMA_PR_VOTE=0; raise it like
        # test_pr_vote does to arm the floor, then restore.)
        import os as _os

        _old_floor = _os.environ.get("FORUM_MIN_KARMA_PR_VOTE")
        _os.environ["FORUM_MIN_KARMA_PR_VOTE"] = "2"
        try:
            err = expect_error(db.check_fixer_eligible, conn, broke["agent_id"])
            assert "at least" in err, err
            db.check_fixer_eligible(conn, beta)
        finally:
            if _old_floor is None:
                _os.environ.pop("FORUM_MIN_KARMA_PR_VOTE", None)
            else:
                _os.environ["FORUM_MIN_KARMA_PR_VOTE"] = _old_floor

    # --- decline blame follows the fixer --------------------------------
    # Blame reads commit MESSAGES: the Citizen trailer lives at the
    # message end, while the bare git author name never carries one.
    _opener_msg = f"opener work\n\nCitizen: alpha (agent_id={alpha})"
    _fixer_msg = f"shared fix\n\nCitizen: beta (agent_id={beta})"
    assert db.decline_blame_agent(alpha, []) == alpha, "no commits: opener pays"
    assert db.decline_blame_agent(alpha, ["x", "no trailer here"]) == alpha, (
        "unparseable messages are skipped"
    )
    assert db.decline_blame_agent(alpha, [_opener_msg, _fixer_msg]) == beta, (
        "most recent fixer pays"
    )
    assert db.decline_blame_agent(alpha, [_fixer_msg, _opener_msg]) == beta, (
        "later opener commits do not absolve the fixer"
    )
    assert db.decline_blame_agent(alpha, [_opener_msg]) == alpha, (
        "opener-only branch: opener pays"
    )
    assert (
        db.decline_blame_agent(alpha, [f"framed (agent_id={beta}) title"]) == alpha
    ), "a bare parenthesized id without the Citizen anchor never matches"
    assert (
        db.decline_blame_agent(alpha, ["Citizen: alpha (agent_id=999999)"]) == 999999
    ), "parse first, validate later - existence is the poller's job (pinned below)"

    # --- tool toggle rides the same ledger, open PRs only ---------------
    _real_aget_tool = github.aget_pr

    async def _fake_aget_open(number):
        assert number == 4301
        return {"number": 4301, "state": "open"}

    async def _fake_aget_closed(number):
        return {"number": number, "state": "closed"}

    async def _fake_aget_dead(number):
        raise RuntimeError("github is down")

    github.aget_pr = _fake_aget_open
    try:
        out = asyncio.run(
            _pbtools.set_public_branch(agents["alpha"]["token"], 4301, True)
        )
        assert out == {"pr_number": 4301, "public_branch": True}, out
        err = asyncio.run(
            _expect_tool_error(
                _pbtools.set_public_branch(agents["beta"]["token"], 4301, False)
            )
        )
        assert "only the PR opener" in err, err
    finally:
        github.aget_pr = _real_aget_tool
    # Closed PRs refuse the toggle: flipping post-close could move
    # decline karma after the fact.
    github.aget_pr = _fake_aget_closed
    try:
        err = asyncio.run(
            _expect_tool_error(
                _pbtools.set_public_branch(agents["alpha"]["token"], 4301, True)
            )
        )
        assert "cannot change after close" in err, err
    finally:
        github.aget_pr = _real_aget_tool
    # A dead GitHub read refuses rather than risking a post-close flip.
    github.aget_pr = _fake_aget_dead
    try:
        err = asyncio.run(
            _expect_tool_error(
                _pbtools.set_public_branch(agents["alpha"]["token"], 4301, True)
            )
        )
        assert "cannot read PR" in err, err
    finally:
        github.aget_pr = _real_aget_tool

    # --- fixer gate on repo_update_pr (mocked GitHub) ---------------------
    real_aget = github.aget_pr
    real_aupdate = github.aupdate_pr
    pushed = []

    async def _fake_aget(number):
        assert number == 4301
        return {
            "number": 4301,
            "state": "open",
            "title": "t",
            "body": f"Proposal: #{pid}\n\nCitizen: alpha (agent_id={alpha})",
            "head": {"ref": "branch", "sha": "a" * 40},
            "base": {"ref": "main", "sha": "b" * 40},
        }

    async def _fake_aupdate(number, changes, **kw):
        pushed.append((number, changes, kw.get("citizen")))
        return {"pr_number": number, "pushed": True}

    github.aget_pr = _fake_aget
    github.aupdate_pr = _fake_aupdate
    try:
        # Flag off: the classic refusal survives.
        with db._conn() as conn:
            db.set_public_branch(conn, 4301, alpha, False)
        err = asyncio.run(
            _expect_tool_error(
                _ptools.repo_update_pr(
                    agents["beta"]["token"],
                    4301,
                    files=[{"path": "a.txt", "content": "hi"}],
                )
            )
        )
        assert "belongs to" in err, err
        assert pushed == [], "refused updates push nothing"
        # Flag on, floor met: the fixer push goes through.
        with db._conn() as conn:
            db.set_public_branch(conn, 4301, alpha, True)
        asyncio.run(
            _ptools.repo_update_pr(
                agents["beta"]["token"],
                4301,
                files=[{"path": "a.txt", "content": "hi"}],
            )
        )
        assert pushed != [] and pushed[0][0] == 4301, pushed
        assert pushed[0][2] == f"beta (agent_id={beta})", (
            "the fix commit carries the fixer's trailer, never the opener's"
        )
        # Flag on, floor-broke fixer: refused at the tool path.
        import os as _os2

        _old_floor2 = _os2.environ.get("FORUM_MIN_KARMA_PR_VOTE")
        _os2.environ["FORUM_MIN_KARMA_PR_VOTE"] = "2"
        _n_pushed = len(pushed)
        try:
            err = asyncio.run(
                _expect_tool_error(
                    _ptools.repo_update_pr(
                        broke["token"],
                        4301,
                        files=[{"path": "b.txt", "content": "hi"}],
                    )
                )
            )
        finally:
            if _old_floor2 is None:
                _os2.environ.pop("FORUM_MIN_KARMA_PR_VOTE", None)
            else:
                _os2.environ["FORUM_MIN_KARMA_PR_VOTE"] = _old_floor2
        assert "at least" in err, err
        assert len(pushed) == _n_pushed, "refused updates push nothing"
        # Flag on, title/body by a fixer: refused.
        err = asyncio.run(
            _expect_tool_error(
                _ptools.repo_update_pr(agents["beta"]["token"], 4301, title="hijack")
            )
        )
        assert "files only" in err, err
        # Flag on, delete/reset by a fixer: refused - gutting the branch
        # stays with the opener.
        err = asyncio.run(
            _expect_tool_error(
                _ptools.repo_update_pr(
                    agents["beta"]["token"],
                    4301,
                    files=[{"path": "a.txt", "delete": True}],
                )
            )
        )
        assert "add or patch files only" in err, err
        err = asyncio.run(
            _expect_tool_error(
                _ptools.repo_update_pr(
                    agents["beta"]["token"],
                    4301,
                    files=[{"path": "a.txt", "reset": True}],
                )
            )
        )
        assert "add or patch files only" in err, err

        # Flag on, closed PR: the fixer lane refuses before any push.
        async def _fake_aget_closed_full(number):
            assert number == 4301
            return {
                "number": 4301,
                "state": "closed",
                "title": "t",
                "body": f"Proposal: #{pid}\n\nCitizen: alpha (agent_id={alpha})",
                "head": {"ref": "branch", "sha": "a" * 40},
                "base": {"ref": "main", "sha": "b" * 40},
            }

        _n_pushed = len(pushed)
        github.aget_pr = _fake_aget_closed_full
        try:
            err = asyncio.run(
                _expect_tool_error(
                    _ptools.repo_update_pr(
                        agents["beta"]["token"],
                        4301,
                        files=[{"path": "a.txt", "content": "hi"}],
                    )
                )
            )
        finally:
            github.aget_pr = _fake_aget
        assert "not open" in err, err
        assert len(pushed) == _n_pushed, "refused updates push nothing"
    finally:
        github.aget_pr = real_aget
        github.aupdate_pr = real_aupdate

    # --- declined public branch bills the fixer, not the opener ----------
    with db._conn() as conn:
        _linked_pr(conn, 4302, pid, alpha)
        db.set_public_branch(conn, 4302, alpha, True)
    real_commits = github.pr_commits
    github.pr_commits = lambda number: {
        "commits": [
            {
                "sha": "a" * 40,
                "message": f"opener work\n\nCitizen: alpha (agent_id={alpha})",
                "author_name": "alpha",
            },
            {
                "sha": "b" * 40,
                "message": f"shared fix\n\nCitizen: beta (agent_id={beta})",
                "author_name": "beta",
            },
        ]
    }
    try:
        _process_closed_pr(
            {
                "number": 4302,
                "declined": True,
                "closed_at": "2026-09-25T00:00:00.000Z",
                "citizen": {"name": "alpha", "agent_id": alpha},
            }
        )
    finally:
        github.pr_commits = real_commits
    with db._conn() as conn:
        rec = conn.execute(
            "SELECT agent_id, status FROM pr_record WHERE pr_number = 4302"
        ).fetchone()
        assert rec is not None and rec["status"] == "declined", rec
        assert rec["agent_id"] == beta, "fixer pays the decline karma"

    # --- a blamed fixer since deleted falls back to the opener -----------
    # Recording a ghost would bill nobody and leave no history row.
    from tests._setup import moderation

    doomed = db.register_agent("doomed-fixer")
    doomed_id = doomed["agent_id"]
    with db._conn() as conn:
        _linked_pr(conn, 4303, pid, alpha)
        db.set_public_branch(conn, 4303, alpha, True)
    moderation.delete_agent(doomed_id, "root", destroy_content=True)
    github.pr_commits = lambda number: {
        "commits": [
            {
                "sha": "c" * 40,
                "message": f"opener work\n\nCitizen: alpha (agent_id={alpha})",
                "author_name": "alpha",
            },
            {
                "sha": "d" * 40,
                "message": f"shared fix\n\nCitizen: doomed (agent_id={doomed_id})",
                "author_name": "doomed",
            },
        ]
    }
    try:
        _process_closed_pr(
            {
                "number": 4303,
                "declined": True,
                "closed_at": "2026-09-25T00:00:00.000Z",
                "citizen": {"name": "alpha", "agent_id": alpha},
            }
        )
    finally:
        github.pr_commits = real_commits
    with db._conn() as conn:
        rec = conn.execute(
            "SELECT agent_id, status FROM pr_record WHERE pr_number = 4303"
        ).fetchone()
        assert rec is not None and rec["status"] == "declined", rec
        assert rec["agent_id"] == alpha, "deleted blamed falls back to the opener"

    # --- closed-then-declined upgrade names the fixer --------------------
    with db._conn() as conn:
        _linked_pr(conn, 4305, pid, alpha)
        db.set_public_branch(conn, 4305, alpha, True)
        conn.execute(
            "INSERT INTO pr_record (pr_number, agent_id, status, karma, closed_at)"
            " VALUES (?, ?, 'closed', 0, ?)",
            (4305, alpha, "2026-09-25T00:00:00.000Z"),
        )
        assert (
            db.record_pr_decline(
                4305,
                beta,
                "2026-09-25T00:00:00.000Z",
                conn=conn,
                blamed_fixer=True,
            )
            is True
        )
        rec = conn.execute(
            "SELECT agent_id, status FROM pr_record WHERE pr_number = 4305"
        ).fetchone()
        assert rec["agent_id"] == beta and rec["status"] == "declined", rec
    # --- decline on a closed (non-public) branch bills the opener --------
    with db._conn() as conn:
        _linked_pr(conn, 4306, pid, alpha)
    real_commits2 = github.pr_commits
    github.pr_commits = lambda number: {
        "commits": [
            {
                "sha": "e" * 40,
                "message": f"opener work\n\nCitizen: alpha (agent_id={alpha})",
                "author_name": "alpha",
            },
            {
                "sha": "f" * 40,
                "message": f"shared fix\n\nCitizen: beta (agent_id={beta})",
                "author_name": "beta",
            },
        ]
    }
    try:
        _process_closed_pr(
            {
                "number": 4306,
                "declined": True,
                "closed_at": "2026-09-25T00:00:00.000Z",
                "citizen": {"name": "alpha", "agent_id": alpha},
            }
        )
    finally:
        github.pr_commits = real_commits2
    with db._conn() as conn:
        rec = conn.execute(
            "SELECT agent_id, status FROM pr_record WHERE pr_number = 4306"
        ).fetchone()
        assert rec is not None and rec["status"] == "declined", rec
        assert rec["agent_id"] == alpha, "flag off: opener pays"
    # --- prefetch failure falls back to the opener -----------------------
    with db._conn() as conn:
        _linked_pr(conn, 4307, pid, alpha)
        db.set_public_branch(conn, 4307, alpha, True)

    def _boom_commits(number):
        raise RuntimeError("github is down")

    github.pr_commits = _boom_commits
    try:
        _process_closed_pr(
            {
                "number": 4307,
                "declined": True,
                "closed_at": "2026-09-25T00:00:00.000Z",
                "citizen": {"name": "alpha", "agent_id": alpha},
            }
        )
    finally:
        github.pr_commits = real_commits
    with db._conn() as conn:
        rec = conn.execute(
            "SELECT agent_id, status FROM pr_record WHERE pr_number = 4307"
        ).fetchone()
        assert rec is not None and rec["status"] == "declined", rec
        assert rec["agent_id"] == alpha, "failed fetch: opener pays"

    # --- fixer roster authorizes resolve/dispute ------------------------
    with db._conn() as conn:
        _linked_pr(conn, 4310, pid, alpha)
        db.set_public_branch(conn, 4310, alpha, True)
        assert db.pr_fixer_ids(conn, 4310) == []
        db.record_pr_fixer(conn, 4310, beta)
        db.record_pr_fixer(conn, 4310, beta)
        assert db.pr_fixer_ids(conn, 4310) == [beta]
        rfid = db.finding_add(
            conn, pid, 4310, gamma, "bug", "other", "c", "f", ["a.py"], False
        )
        out = db.finding_mark_resolved(conn, rfid, beta, "fixed", (beta,))
        assert out["state"] == "resolved" and out["verified"] is False
        err = expect_error(db.finding_mark_resolved, conn, rfid, gamma, "x")
        assert "authorized fixer" in err, err
        # flag-off keeps the roster: contributions are history.
        db.set_public_branch(conn, 4310, alpha, False)
        assert db.pr_fixer_ids(conn, 4310) == [beta]
    # The tools wire the roster: a member resolves, a stranger refused.
    from server.tools.repo import _findings as _ftools

    with db._conn() as conn:
        rfid2 = db.finding_add(
            conn, pid, 4310, gamma, "bug", "other", "c2", "f2", ["b.py"], False
        )
    out = asyncio.run(
        _ftools.finding_mark_resolved(agents["beta"]["token"], rfid2, "via roster")
    )
    assert out == {"finding_id": rfid2, "state": "resolved", "verified": False}
    err = asyncio.run(
        _expect_tool_error(
            _ftools.finding_mark_resolved(agents["gamma"]["token"], rfid2, "x")
        )
    )
    assert "authorized fixer" in err, err
    # --- lane pushes record the roster ----------------------------------
    _real_aget2 = github.aget_pr
    _real_aupdate2 = github.aupdate_pr
    pushed2 = []

    async def _fake_aget2(number):
        assert number == 4311
        return {
            "number": 4311,
            "state": "open",
            "title": "t",
            "body": f"Proposal: #{pid}\n\nCitizen: alpha (agent_id={alpha})",
            "head": {"ref": "branch", "sha": "a" * 40},
            "base": {"ref": "main", "sha": "b" * 40},
        }

    async def _fake_aupdate2(number, changes, **kw):
        pushed2.append(number)
        return {"pr_number": number, "pushed": True}

    with db._conn() as conn:
        _linked_pr(conn, 4311, pid, alpha)
        db.set_public_branch(conn, 4311, alpha, True)
        conn.execute(
            "INSERT INTO workspace_claims (proposal_id, agent_id, name, status)"
            " VALUES (?, ?, ?, 'active')",
            (pid, delta, "watch"),
        )
    github.aget_pr = _fake_aget2
    github.aupdate_pr = _fake_aupdate2
    try:
        asyncio.run(
            _ptools.repo_update_pr(
                agents["gamma"]["token"],
                4311,
                files=[{"path": "c.txt", "content": "hi"}],
            )
        )
    finally:
        github.aget_pr = _real_aget2
        github.aupdate_pr = _real_aupdate2
    assert pushed2 == [4311]
    with db._conn() as conn:
        assert gamma in db.pr_fixer_ids(conn, 4311)
    # The lane push pings the opener and the claim holder, never self.
    with db._conn() as conn:
        for _who, _expect in ((alpha, True), (delta, True), (gamma, False)):
            _n = conn.execute(
                "SELECT COUNT(*) FROM notifications WHERE agent_id = ?"
                " AND kind = 'pr' AND ref_id = ? AND body LIKE '%shared fix%'",
                (_who, 4311),
            ).fetchone()[0]
            assert (_n >= 1) == _expect, (_who, _n)
    # --- victim roster rows die with their author ------------------------
    doomed3 = db.register_agent("doomed-roster")
    with db._conn() as conn:
        db.record_pr_fixer(conn, 4311, doomed3["agent_id"])
        assert doomed3["agent_id"] in db.pr_fixer_ids(conn, 4311)
    moderation.delete_agent(doomed3["agent_id"], "root", destroy_content=True)
    with db._conn() as conn:
        assert doomed3["agent_id"] not in db.pr_fixer_ids(conn, 4311)

    # --- a dead opener's flags die with them ------------------------------
    doomed2 = db.register_agent("doomed-opener")
    with db._conn() as conn:
        _linked_pr(conn, 4304, pid, doomed2["agent_id"])
        db.set_public_branch(conn, 4304, doomed2["agent_id"], True)
        assert db.is_public_branch(conn, 4304) is True
    moderation.delete_agent(doomed2["agent_id"], "root", destroy_content=True)
    with db._conn() as conn:
        assert db.is_public_branch(conn, 4304) is False, "orphan flag is swept"
        assert db.is_public_branch(conn, 4302) is True, "survivor flags stay"

    # --- migration: pre-flag DB gains the table via init_db() ------------
    saved = db.DB_PATH
    try:
        db.DB_PATH = str(_TMP / "flag_migration.db")
        db.init_db()
        with db._conn() as conn:
            conn.execute("DROP TABLE pr_public_branches")
            conn.execute("DROP TABLE pr_fixers")
        db.init_db()
        with db._conn() as conn:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            assert "pr_public_branches" in tables, "init_db() recreates the table"
            assert "pr_fixers" in tables, "init_db() recreates the roster"
        db.init_db()  # second boot is a clean no-op
    finally:
        db.DB_PATH = saved

    print("test_public_branch: all assertions passed")
    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)


async def _expect_tool_error(coro):
    """Await a tool call that must refuse; return the error text."""
    try:
        await coro
    except Exception as exc:
        return str(exc)
    raise AssertionError("expected tool error")


if __name__ == "__main__":
    main()
