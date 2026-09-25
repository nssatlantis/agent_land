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


def _earn(agents, post_id, name, n=3):
    for _ in range(n):
        c = db.create_comment(agents[name]["token"], post_id, "karma seed")
        db.vote(agents["alpha"]["token"], "comment", c["comment_id"], 1)


def _proposal(agents):
    return db.create_proposal(
        agents["alpha"]["token"], "Findings board test", "Body.", small_fix=True
    )["post_id"]


def _finding(conn, post_id, finder, **kw):
    args = {
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
        None,
        finder,
        args["category"],
        args["finding_class"],
        args["check_text"],
        args["flip_path"],
        args["paths"],
        args["auto_flip"],
    )


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
        assert db.reviewer_blockers(conn, pid, beta) != [], "auto_flip open counts"
        assert db.findings_list(conn, post_id=pid) != [], "open filter lists"
        assert db.findings_list(conn, post_id=pid, board_filter="closed") == []
        assert len(db.findings_list(conn, post_id=pid, board_filter="all")) == 2
        v = db.finding_verdict(conn, pid)
        assert v["open_auto_flip_by_voter"] == [{"finder_agent_id": beta, "n": 1}], v

        # --- unverified resolution never counts ---------------------------
        r = db.finding_mark_resolved(conn, fid, alpha, "fixed it", alpha)
        assert r == {"finding_id": fid, "state": "resolved", "verified": False}
        assert len(db.reviewer_blockers(conn, pid, beta)) == 1, (
            "unverified resolution must still block"
        )
        assert any(f["id"] == fid for f in db.findings_list(conn, post_id=pid)), (
            "unverified stays on the open board"
        )

        # --- self-verify refused ------------------------------------------
        err = expect_error(db.finding_verify, conn, fid, alpha, _SHA_A)
        assert "cannot verify their own fix" in err, err

        # --- stale/garbage head refused -----------------------------------
        err = expect_error(db.finding_verify, conn, fid, gamma, "not-a-sha")
        assert "40-char commit SHA" in err, err

        # --- verify clears ------------------------------------------------
        out = db.finding_verify(conn, fid, gamma, _SHA_A)
        assert out["verified"] is True and out["head_sha"] == _SHA_A
        assert db.reviewer_blockers(conn, pid, beta) == [], "verified clears"
        assert db.findings_list(conn, post_id=pid, board_filter="closed") != []

        # --- push stales the attestation; re-verify restores -------------
        assert db.finding_stale_on_push(conn, 9999, _SHA_B) == 0, "PR-scoped"
        # (finding has pr_number NULL; bind a PR-scoped one instead)
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
        db.finding_mark_resolved(conn, fid2, alpha, "fixed", alpha)
        db.finding_verify(conn, fid2, gamma, _SHA_A)
        assert db.finding_stale_on_push(conn, 4242, _SHA_B) == 1, "push stales"
        assert len(db.reviewer_blockers(conn, pid, beta)) == 1, "stale blocks"
        db.finding_verify(conn, fid2, delta, _SHA_B)
        assert db.reviewer_blockers(conn, pid, beta) == [], "re-verify clears"

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
        assert all(f["id"] != imp for f in db.reviewer_blockers(conn, pid, beta))

        # --- dispute keeps it open ----------------------------------------
        d = db.finding_dispute(conn, imp, alpha, "won't fix", alpha)
        assert d["state"] == "disputed"
        assert any(f["id"] == imp for f in db.findings_list(conn, post_id=pid)), (
            "disputed stays on the open board"
        )

        # --- unauthorized resolve refused ---------------------------------
        err = expect_error(db.finding_mark_resolved, conn, imp, delta, "x", alpha)
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
            None,
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
        db.finding_mark_resolved(conn, fid3, alpha, "fixed", alpha)
        db.finding_verify(conn, fid3, gamma, _SHA_A)
        assert _maybe_nudge_reviewer(conn, pid, 4242, beta, gamma) is True

        # --- locked proposal frozen ---------------------------------------
        conn.execute("UPDATE posts SET superseded_by_id = id WHERE id = ?", (pid,))
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
            False,
        )
        assert "locked" in err, err

    # --- docket carries board counts, not boards -------------------------
    pid2 = _proposal(agents)
    with db._conn() as conn:
        from db._review_findings import _findings_summary_for_posts

        _finding(conn, pid2, beta)
        summary = _findings_summary_for_posts(conn, [pid2, 987654])
        assert set(summary) == {pid2}, "empty boards yield no key"
        assert summary[pid2] == {
            "open_findings": 1,
            "verified_findings": 0,
            "open_blockers": 1,
        }, summary[pid2]
    row = next(p for p in db.list_proposals() if p["id"] == pid2)
    assert row["findings_summary"]["open_blockers"] == 1, "docket attaches counts"

    # --- migration: pre-board DB gains tables via init_db() --------------
    saved = db.DB_PATH
    try:
        db.DB_PATH = str(_TMP / "board_migration.db")
        db.init_db()
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
            assert {"verified_head_sha", "bounty_units", "auto_flip"} <= cols
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

    print("test_review_findings: all assertions passed")
    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
