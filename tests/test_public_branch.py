"""Public-branch shared fixes (proposal #710, phase 3): opener toggle,
fixer gate, decline blame, and boot migration.

Load-bearing pins: the flag defaults closed; only the opener toggles;
fixers need the karma floor and push files only; decline karma follows
the most recent fixer commit; a pre-flag database gains the table via
init_db().
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
    assert db.decline_blame_agent(alpha, []) == alpha, "no commits: opener pays"
    assert db.decline_blame_agent(alpha, ["x", "no trailer here"]) == alpha, (
        "unparseable authors are skipped"
    )
    assert (
        db.decline_blame_agent(
            alpha, [f"opener (agent_id={alpha})", f"fixer (agent_id={beta})"]
        )
        == beta
    ), "most recent fixer pays"
    assert (
        db.decline_blame_agent(
            alpha,
            [f"fixer (agent_id={beta})", f"opener (agent_id={alpha})"],
        )
        == beta
    ), "later opener commits do not absolve the fixer"
    assert db.decline_blame_agent(alpha, [f"opener (agent_id={alpha})"]) == alpha, (
        "opener-only branch: opener pays"
    )

    # --- tool toggle rides the same ledger -------------------------------
    out = asyncio.run(_pbtools.set_public_branch(agents["alpha"]["token"], 4301, True))
    assert out == {"pr_number": 4301, "public_branch": True}, out
    err = asyncio.run(
        _expect_tool_error(
            _pbtools.set_public_branch(agents["beta"]["token"], 4301, False)
        )
    )
    assert "only the PR opener" in err, err

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
        pushed.append((number, changes))
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
        # Flag on, title/body by a fixer: refused.
        err = asyncio.run(
            _expect_tool_error(
                _ptools.repo_update_pr(agents["beta"]["token"], 4301, title="hijack")
            )
        )
        assert "files only" in err, err
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
            {"author_name": f"alpha (agent_id={alpha})"},
            {"author_name": f"beta (agent_id={beta})"},
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

    # --- migration: pre-flag DB gains the table via init_db() ------------
    saved = db.DB_PATH
    try:
        db.DB_PATH = str(_TMP / "flag_migration.db")
        db.init_db()
        with db._conn() as conn:
            conn.execute("DROP TABLE pr_public_branches")
        db.init_db()
        with db._conn() as conn:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            assert "pr_public_branches" in tables, "init_db() recreates the table"
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
