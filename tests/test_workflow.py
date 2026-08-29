"""Test db._workflow: the create-pr run lifecycle.

Covers the #593 review hardening: TTL adaptivity + fallback (D2), the
365-day TTL cap (W4), run-status validation (D4), nudge resilience (D1),
collab-run preservation on PR close, sweep run_ids + chunking (D7/D8/W9),
restart (B2) and the run-ledger filters (W2/W3).
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_workflow_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)
os.environ["FORUM_WORKFLOW_ENFORCE"] = "1"
os.environ["FORUM_WORKFLOW_TTL_SECONDS"] = "3600"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db._workflow import (  # noqa: E402
    _workflow_file,
    _workflow_nudge,
    close_workflow_for_pr,
    close_workflow_for_proposal,
    list_workflow_runs,
    require_workflow_block,
    restart_workflow,
    start_workflow,
    sweep_expired_workflows,
)
from tests._setup import db, setup  # noqa: E402

_PATH = "workflows/create-pr.md"


def _now():
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _open_run(conn, pid: int):
    return conn.execute(
        "SELECT id FROM workflow_runs WHERE proposal_id = ? AND status = 'open'",
        (pid,),
    ).fetchone()


def _last_close_event(conn) -> dict:
    row = conn.execute(
        "SELECT detail FROM events WHERE kind = 'workflow_closed'"
        " ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return json.loads(row["detail"])


def main():
    agents, post_id = setup()
    alpha = agents["alpha"]
    beta = agents["beta"]
    gamma = agents["gamma"]

    # --- auto-start + idempotence + validation ---------------------------------
    prop = db.create_proposal(alpha["token"], "T1 run lifecycle", "t1 body")
    pid = prop["post_id"]
    with db._conn() as conn:
        run = _open_run(conn, pid)
        assert run is not None, "create_proposal auto-starts an open create-pr run"
        r1 = int(run["id"])
        row = conn.execute(
            "SELECT workflow_path, workflow_sha, status FROM workflow_runs WHERE id = ?",
            (r1,),
        ).fetchone()
        assert row["workflow_path"] == _PATH
        assert row["status"] == "open"
        assert row["workflow_sha"] and all(
            c in "0123456789abcdef" for c in row["workflow_sha"]
        ), "run records a content sha"
        # idempotent: a second start returns the same open run id
        r2 = start_workflow(conn, _PATH, pid, alpha["agent_id"])
        assert r2 == r1, "start is idempotent while a run is open"
        # D4: terminal statuses are validated, never silently no-op'd
        try:
            close_workflow_for_pr(conn, 42, "superseded")
            raise AssertionError("close_workflow_for_pr must reject bad status")
        except db.ForumError:
            pass
        try:
            close_workflow_for_proposal(conn, pid, "bogus")
            raise AssertionError("close_workflow_for_proposal must reject bad status")
        except db.ForumError:
            pass
    print("  auto-start / idempotence / status validation ok")

    # --- TTL adaptivity, 365-day cap (W4), bad-date fallback (D2) ---------------
    with db._conn() as conn:
        # future-dated proposal: adaptive floor stretches, cap clamps at 365d
        far = _iso(_now() + timedelta(days=400))
        conn.execute("UPDATE posts SET created_at = ? WHERE id = ?", (far, pid))
        conn.execute("UPDATE workflow_runs SET status = 'closed' WHERE id = ?", (r1,))
        r3 = start_workflow(conn, _PATH, pid, alpha["agent_id"])
        row = conn.execute(
            "SELECT expires_at FROM workflow_runs WHERE id = ?", (r3,)
        ).fetchone()
        cap = _now() + timedelta(days=365)
        expires = datetime.strptime(row["expires_at"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc
        )
        assert expires <= cap + timedelta(seconds=2), "run TTL is capped at 365d"
        assert expires >= cap - timedelta(seconds=2), "cap, not the 414d probe, wins"
        # bad created_at: probe parse fails -> plain now+TTL fallback (D2)
        conn.execute(
            "UPDATE posts SET created_at = 'not-a-timestamp' WHERE id = ?", (pid,)
        )
        conn.execute("UPDATE workflow_runs SET status = 'closed' WHERE id = ?", (r3,))
        r4 = start_workflow(conn, _PATH, pid, alpha["agent_id"])
        row = conn.execute(
            "SELECT expires_at FROM workflow_runs WHERE id = ?", (r4,)
        ).fetchone()
        ttl = timedelta(seconds=3600)
        expires = datetime.strptime(row["expires_at"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc
        )
        assert expires <= _now() + ttl + timedelta(seconds=2), (
            "bad-probe run falls back to plain now+TTL"
        )
        conn.execute("UPDATE workflow_runs SET status = 'closed' WHERE id = ?", (r4,))
        conn.execute(
            "UPDATE posts SET created_at = ? WHERE id = ?", (_iso(_now()), pid)
        )
        # restore a clean open run for later sections
        start_workflow(conn, _PATH, pid, alpha["agent_id"])
    print("  TTL adaptivity / cap / fallback ok")

    # --- sweep: run_ids + proposal_id in the close event (D7), chunking (D8) ---
    p2 = db.create_proposal(alpha["token"], "T2 sweep target", "t2 body")["post_id"]
    p3 = db.create_proposal(beta["token"], "T3 sweep target", "t3 body")["post_id"]
    with db._conn() as conn:
        r2_ = int(_open_run(conn, p2)["id"])
        r3_ = int(_open_run(conn, p3)["id"])
        # nothing expired yet
        assert sweep_expired_workflows(conn, [p2]) == 0
        past = _iso(_now() - timedelta(days=3))
        conn.execute(
            "UPDATE workflow_runs SET expires_at = ? WHERE id = ?", (past, r2_)
        )
        conn.execute(
            "UPDATE workflow_runs SET expires_at = ? WHERE id = ?", (past, r3_)
        )
        # single-proposal sweep closes only that proposal's run
        closed = sweep_expired_workflows(conn, [p2])
        assert closed == 1
        ev = _last_close_event(conn)
        assert ev["reason"] == "ttl_expired" and set(ev["run_ids"]) == {r2_}
        assert ev["proposal_id"] == p2, "single-proposal sweep names the proposal"
        assert _open_run(conn, p3) is not None
        # multi-proposal (whole-docket) sweep gathers the lone open expired run
        assert sweep_expired_workflows(conn) == 1
        ev = _last_close_event(conn)
        assert set(ev["run_ids"]) == {r3_} and "proposal_id" not in ev
        # chunking: 600 ids (two 500/100 chunks) with only the real ones hitting
        conn.execute(
            "UPDATE workflow_runs SET expires_at = ?, status = 'open'"
            " WHERE proposal_id IN (?, ?)",
            (past, p2, p3),
        )
        big = list(range(1, 1 + 500)) + list(range(500, 1 + 600))
        big = [i if i not in (p2, p3) else i for i in big]
        assert sweep_expired_workflows(conn, big) == 2
        ev = _last_close_event(conn)
        assert len(ev["run_ids"]) == 2
    print("  sweep run_ids / proposal_id / chunking ok")

    # --- collab keep-open on PR close, others close (P0-C) ---------------------
    pc = db.create_proposal(
        alpha["token"], "T4 collab workflow", "t4 body", collaborative=True
    )["post_id"]
    with db._conn() as conn:
        rc = int(_open_run(conn, pc)["id"])
        conn.execute(
            "INSERT INTO proposal_links (pr_number, post_id) VALUES (?, ?)",
            (9001, pc),
        )
        close_workflow_for_pr(conn, 9001, "merged")
        row = conn.execute(
            "SELECT status FROM workflow_runs WHERE id = ?", (rc,)
        ).fetchone()
        assert row["status"] == "open", "collab run survives a single PR merge"
        # non-collab control: open a fresh run then close it via its PR link
        conn.execute(
            "UPDATE workflow_runs SET status = 'open', expires_at = ?"
            " WHERE proposal_id = ?",
            (_iso(_now() + timedelta(hours=1)), p3),
        )
        rp2 = int(_open_run(conn, p3)["id"])
        conn.execute(
            "INSERT INTO proposal_links (pr_number, post_id) VALUES (?, ?)",
            (9002, p3),
        )
        close_workflow_for_pr(conn, 9002, "merged")
        row = conn.execute(
            "SELECT status FROM workflow_runs WHERE id = ?", (rp2,)
        ).fetchone()
        assert row["status"] == "merged", "non-collab run closes on PR merge"
    print("  collab keep-open ok")

    # --- restart (B2): author, delegate, admin, permission gate ---------------
    p5 = db.create_proposal(gamma["token"], "T5 restart", "t5 body")["post_id"]
    with db._conn() as conn:
        r5 = int(_open_run(conn, p5)["id"])
        # non-owner/outsider is refused
        try:
            restart_workflow(conn, p5, alpha["agent_id"])
            raise AssertionError("outsider restart must be refused")
        except db.ForumError:
            pass
        res = restart_workflow(conn, p5, gamma["agent_id"])
        assert res["post_id"] == p5 and res["workflow_path"] == _PATH
        assert res["restarted"] is True
        old = conn.execute(
            "SELECT status FROM workflow_runs WHERE id = ?", (r5,)
        ).fetchone()
        assert old["status"] == "closed"
        assert _open_run(conn, p5) is not None
    with db._conn() as conn:
        db.delegate_proposal(gamma["token"], p5, beta["name"])
    with db._conn() as conn:
        res = restart_workflow(conn, p5, beta["agent_id"])
        assert res["restarted"] is True
        # admin path (agent_id=None) restarts regardless of caller identity
        res = restart_workflow(conn, p5, None)
        assert res["restarted"] is True
        # no-open-run restart reports restarted False
        conn.execute(
            "UPDATE workflow_runs SET status = 'closed' WHERE id = ?",
            (res["run_id"],),
        )
        res = restart_workflow(conn, p5, beta["agent_id"])
        assert res["restarted"] is False
    print("  restart author / delegate / admin / gate ok")

    # --- require_workflow_block: lazy reopen (W1) and terminal close ----------
    p6 = db.create_proposal(alpha["token"], "T6 gate lazy reopen", "t6 body")["post_id"]
    with db._conn() as conn:
        r6 = int(_open_run(conn, p6)["id"])
        require_workflow_block(conn, p6, alpha["agent_id"])  # open run: passes silently
        conn.execute("UPDATE workflow_runs SET status = 'closed' WHERE id = ?", (r6,))
        require_workflow_block(conn, p6, alpha["agent_id"])  # retryable: reopens
        assert _open_run(conn, p6) is not None, (
            "non-terminal proposal reopens on demand"
        )
    # terminal proposal (superseded) blocks even when run-less
    p7 = db.create_proposal(alpha["token"], "T7 gate terminal", "t7 body")["post_id"]
    with db._conn() as conn:
        require_workflow_block(conn, p7, alpha["agent_id"])
        conn.execute(
            "UPDATE workflow_runs SET status = 'closed' WHERE id = ?",
            (int(_open_run(conn, p7)["id"]),),
        )
    sub = db.supersede_proposal(
        alpha["token"], p7, "T7 gate terminal v2", "t7 superseded body"
    )
    new_id = sub["post_id"]
    assert isinstance(new_id, int) and new_id != p7
    with db._conn() as conn:
        try:
            require_workflow_block(conn, p7, alpha["agent_id"])
            raise AssertionError(
                "a superseded proposal must stay PR-blocked without an open run"
            )
        except db.ForumError:
            pass
    print("  gate lazy-reopen / terminal block ok")

    # --- nudge (D1): quiet, populated, reopened flag, never raises ------------
    with db._conn() as conn:
        assert _workflow_nudge(conn, agents["fresh"]["agent_id"]) == {}
        nudge = _workflow_nudge(conn, alpha["agent_id"])
        assert "workflow_note" in nudge and nudge["workflow_runs"]
        assert any(
            r["proposal_id"] == p6 and r["workflow_action"] == "reopened"
            for r in nudge["workflow_runs"]
        ), "reopened runs are labelled, never presented as fresh"
        assert "[reopened]" in nudge["workflow_note"]
    with mock.patch(
        "db._workflow._open_workflow_runs_for",
        side_effect=RuntimeError("boom"),
    ):
        with db._conn() as conn:  # noqa: F541
            assert _workflow_nudge(conn, alpha["agent_id"]) == {}, (
                "a nudge defect must never break profile reads"
            )
    print("  nudge quiet / reopened / resilient ok")

    # --- ledger filters (W2/W3) ------------------------------------------------
    with db._conn() as conn:
        runs = list_workflow_runs(conn, proposal_id=p2)
        assert runs and all(r["proposal_id"] == p2 for r in runs)
        runs = list_workflow_runs(conn, status="closed")
        assert runs and all(r["status"] == "closed" for r in runs)
        gamma_runs = list_workflow_runs(conn, agent_id=gamma["agent_id"])
        assert gamma_runs, "gamma's own proposal shows under its filter"
        assert all(r["proposal_id"] == p5 for r in gamma_runs), (
            "agent filter returns only proposals the agent owns/starts/"
            "delegates - not unrelated runs"
        )
        assert not list_workflow_runs(conn, agent_id=agents["delta"]["agent_id"]), (
            "an uninvolved agent sees no runs"
        )
        runs = list_workflow_runs(conn, proposal_id=p2)
        assert runs[0]["workflow_path"] == _PATH
        stamp = [r["created_at"] for r in runs]
        assert stamp == sorted(stamp, reverse=True), "ledger is newest first"
    print("  ledger filters ok")

    # --- _workflow_file guard (D9) ---------------------------------------------
    p = _workflow_file(_PATH)
    assert p.name == "create-pr.md" and p.is_absolute()
    for bad in (
        "../../etc/passwd",
        "/etc/passwd",
        "workflows/x.txt",
        "workflows/../x.md",
    ):
        try:
            _workflow_file(bad)
            raise AssertionError(f"_workflow_file must reject {bad!r}")
        except db.ForumError:
            pass
    print("  _workflow_file traversal guard ok")

    print("ALL WORKFLOW TESTS PASSED")


if __name__ == "__main__":
    main()
