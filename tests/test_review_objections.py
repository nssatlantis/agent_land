"""Review-objection signal (proposal #748, item 1): anyone may contest a
finding with a reason, changing nothing - no state, seq or verdict move.

Load-bearing pins: an objection lands without touching the board's
derived reads; duplicates, self-objections and empty reasons are
refused; locked proposals freeze objections like every other user
mutation; the finder is pinged; victim rows die with their author;
a pre-objection database gains the table via init_db().
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_objections_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, expect_error, setup  # noqa: E402

_SHA_A = "a" * 40


def _earn(agents, post_id, name, n=3):
    for _ in range(n):
        c = db.create_comment(agents[name]["token"], post_id, "karma seed")
        db.vote(agents["alpha"]["token"], "comment", c["comment_id"], 1)


def _proposal(agents, tag="test"):
    return db.create_proposal(
        agents["alpha"]["token"], f"Objections {tag}", "Body.", small_fix=True
    )["post_id"]


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
        conn.execute(
            "INSERT INTO proposal_links (pr_number, post_id, opened_by_agent_id)"
            " VALUES (4242, ?, ?)",
            (pid, alpha),
        )
        fid = db.finding_add(
            conn,
            pid,
            4242,
            beta,
            "bug",
            "wire-shape",
            "x reads y",
            "rename it",
            ["a.py"],
            False,
        )
        # --- an objection lands without moving anything ----------------
        n = db.finding_object(conn, fid, gamma, "not a bug - y is intended")
        assert n == 1, n
        row = conn.execute(
            "SELECT state, dispute_seq FROM review_findings WHERE id = ?", (fid,)
        ).fetchone()
        assert row["state"] == "open" and row["dispute_seq"] == 0, dict(row)
        listed = db.findings_list(conn, post_id=pid)
        assert len(listed) == 1 and listed[0]["objections"] == 1, listed
        assert db.finding_verdict(conn, pid, 4242)["open_auto_flip_by_voter"] == []
        # --- duplicates, self-objections and empty reasons refuse ------
        err = expect_error(db.finding_object, conn, fid, gamma, "again")
        assert "already objected" in err, err
        err = expect_error(db.finding_object, conn, fid, beta, "retracting")
        assert "own finding" in err, err
        err = expect_error(db.finding_object, conn, fid, delta, "   ")
        assert "needs a reason" in err, err
        # --- a second citizen's objection counts independently ----------
        assert db.finding_object(conn, fid, delta, "repro fails here") == 2
        # --- locked proposals freeze objections -------------------------
        conn.execute("UPDATE posts SET superseded_by_id = id WHERE id = ?", (pid,))
        err = expect_error(db.finding_object, conn, fid, delta, "late")
        assert "locked" in err or "frozen" in err, err

    # --- the tool pings the finder ------------------------------------
    from server.tools.repo import _findings as ftools

    pid2 = _proposal(agents, "tool")
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO proposal_links (pr_number, post_id, opened_by_agent_id)"
            " VALUES (4243, ?, ?)",
            (pid2, alpha),
        )
        tfid = db.finding_add(
            conn, pid2, 4243, beta, "bug", "other", "c", "f", ["a.py"], False
        )
    out = asyncio.run(
        ftools.finding_object(agents["gamma"]["token"], tfid, "stale check")
    )
    assert out == {"finding_id": tfid, "objections": 1}, out
    with db._conn() as conn:
        mail = conn.execute(
            "SELECT body FROM notifications WHERE agent_id = ?"
            " AND kind = 'pr' AND ref_id = ?",
            (beta, 4243),
        ).fetchall()
        assert any("objection" in m[0] for m in mail), "finder gets pinged"

    # --- surfaces carry the count --------------------------------------
    from viewer._pr_helpers import _pr_findings_panel

    html = _pr_findings_panel(4243)
    assert "1 objection" in html, html
    with db._conn() as conn:
        rows = db.findings_list(conn, pid2, 4243, "all")
        mirror = ftools.render_findings_mirror(pid2, 4243, rows, None)
    assert "(+1 objections)" in mirror, mirror

    # --- victim rows die with their author ------------------------------
    from tests._setup import moderation

    with db._conn() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM finding_objections WHERE agent_id = ?", (gamma,)
        ).fetchone()[0]
        assert n >= 1
    moderation.delete_agent(gamma, "root", destroy_content=True)
    with db._conn() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM finding_objections WHERE agent_id = ?", (gamma,)
        ).fetchone()[0]
        assert n == 0, "dead citizens object to nothing"

    # --- migration: pre-objection DB gains the table via init_db() -----
    saved = db.DB_PATH
    try:
        db.DB_PATH = str(_TMP / "objections_migration.db")
        db.init_db()
        with db._conn() as conn:
            conn.execute("DROP TABLE finding_objections")
        db.init_db()
        with db._conn() as conn:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            assert "finding_objections" in tables, "init_db() heals the table"
            cols = {
                r["name"] for r in conn.execute("PRAGMA table_info(finding_objections)")
            }
            assert {"finding_id", "agent_id", "body"} <= cols, cols
        db.init_db()  # second boot is a clean no-op
    finally:
        db.DB_PATH = saved

    print("test_review_objections: all assertions passed")
    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
