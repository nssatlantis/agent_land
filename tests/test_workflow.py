"""Test db._workflow: the create-pr run lifecycle.

Covers the #593 review hardening: TTL adaptivity + fallback (D2), the
365-day TTL cap (W4), run-status validation (D4), nudge resilience (D1),
the per-PR lifecycle (workflows part 2: bind, per-PR close, CI-green
completion to 'completed'), collab-run preservation on PR close, sweep
run_ids + chunking (D7/D8/W9), restart (B2) and the run-ledger filters
(W2/W3), plus the A1 boot-backfill guard (a proposal that ever ran is
never re-seeded) and the A2 ghost-run reconcile (a folded run with no
linked PR closes to 'closed' with reason no_pr_linked). PR B: the guided
steps surface - parser (7 keys in order), snapshot/seed/backfill,
permissioned + audited manual ticks, managed-key refusals, the steps gate
with its dry-run bypass, open/verify auto-ticks, and COUNT(*) vs the
LIMIT-50 listing. Per-agent run ownership (the fork set ON here): the gate
and repo_workflow_status scope to the caller, claim/delegate/claim-time
create the caller's own run, and bind never crosses agents.
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
os.environ["FORUM_WORKFLOW_STEPS_ENFORCE"] = "1"
os.environ["FORUM_WORKFLOW_PER_AGENT"] = "1"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db._workflow import (  # noqa: E402
    _open_agent_run,
    _parse_workflow_steps,
    _workflow_file,
    _workflow_nudge,
    bind_open_run,
    close_workflow_for_pr,
    close_workflow_for_proposal,
    complete_workflow_for_pr,
    count_workflow_runs,
    list_bound_open_runs,
    list_workflow_runs,
    reconcile_open_runs,
    require_workflow_block,
    restart_workflow,
    seed_steps_for_open_runs,
    stale_open_run_count,
    start_workflow,
    sweep_expired_workflows,
    tick_workflow_step,
    workflow_steps_for_run,
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


def _tick_manual_steps(conn, pid: int, agent_id: int) -> None:
    """Tick every unticked manual (non-managed) step of a proposal's open
    create-pr run - the gate (FORUM_WORKFLOW_STEPS_ENFORCE=1) blocks on them,
    so legacy blocks that just want 'open run exists' must clear them first."""
    rows = conn.execute(
        "SELECT id, agent_id FROM workflow_runs"
        " WHERE proposal_id = ? AND status = 'open'",
        (pid,),
    ).fetchall()
    if not rows:
        return
    own = [r for r in rows if r["agent_id"] == agent_id]
    pick = own[0] if own else rows[0]
    for step in workflow_steps_for_run(conn, int(pick["id"])):
        if step["done"] or step["step_key"] in ("open", "verify"):
            continue
        tick_workflow_step(conn, int(pick["id"]), step["step_key"], agent_id)


def _open_runs_for(conn, pid: int):
    return conn.execute(
        "SELECT id, agent_id FROM workflow_runs"
        " WHERE proposal_id = ? AND status = 'open' ORDER BY id",
        (pid,),
    ).fetchall()


def test_per_agent_ownership(agents):
    """Per-agent run ownership (the fork): claiming a to-do item/list, taking a
    delegation, or claiming a proposal each create the CALLER's OWN open
    create-pr run; the gate resolves the caller's own run; binding a PR never
    crosses agents."""
    db.init_db()  # the previous block drops workflow_runs to test degradation
    a, b, g, d = (
        agents["alpha"],
        agents["beta"],
        agents["gamma"],
        agents["delta"],
    )
    # --- claim_todo_item wires a caller-owned run --------------------------
    p1 = db.create_proposal(
        a["token"], "T-per item claim", "t-per body", collaborative=True
    )["post_id"]
    lst = db.create_todo_list(a["token"], p1, "L1", [{"text": "i1"}])
    db.join_proposal(b["token"], p1)
    with db._conn() as conn:
        assert len(_open_runs_for(conn, p1)) == 1, "only the author's run initially"
    db.claim_todo_item(b["token"], p1, lst["items"][0]["id"])
    with db._conn() as conn:
        b_run = _open_agent_run(conn, p1, b["agent_id"])
        a_run = _open_agent_run(conn, p1, a["agent_id"])
        assert b_run is not None, "claiming an item started the caller's run"
        assert a_run is not None
        assert b_run != a_run, "the item claim owns a run distinct from the author's"
        # gate scopes to the caller: block beta until BETA's steps tick
        try:
            require_workflow_block(conn, p1, b["agent_id"])
            raise AssertionError("beta's unticked own run must block the gate")
        except db.ForumError as exc:
            assert "waiting on completed steps" in str(exc), exc
        _tick_manual_steps(conn, p1, b["agent_id"])
        require_workflow_block(conn, p1, b["agent_id"])  # clears on beta's run
        a_open_step = next(
            s for s in workflow_steps_for_run(conn, a_run) if s["step_key"] == "open"
        )
        assert a_open_step["done"] == 0, (
            "beta clearing the gate never ticked the author's run"
        )
    # --- claim_todo_list (whole-list claim mode) wires a caller-owned run ---
    p2 = db.create_proposal(
        a["token"], "T-per list claim", "t-per body2", collaborative=True
    )["post_id"]
    lst2 = db.create_todo_list(a["token"], p2, "L1", [{"text": "i1"}, {"text": "i2"}])
    db.set_todo_claim_mode(a["token"], p2, "list")
    db.join_proposal(g["token"], p2)
    with db._conn() as conn:
        assert _open_agent_run(conn, p2, g["agent_id"]) is None
    db.claim_todo_list(g["token"], p2, lst2["id"])
    with db._conn() as conn:
        g_run = _open_agent_run(conn, p2, g["agent_id"])
        assert g_run is not None and g_run != _open_agent_run(
            conn, p2, a["agent_id"]
        ), "list claim owns its own run"
    # --- delegate_proposal wires a caller-owned run ------------------------
    p3 = db.create_proposal(a["token"], "T-per deleg", "t-per body3")["post_id"]
    with db._conn() as conn:
        a3 = _open_agent_run(conn, p3, a["agent_id"])
        assert a3 is not None
    db.delegate_proposal(a["token"], p3, g["name"])
    with db._conn() as conn:
        g3 = _open_agent_run(conn, p3, g["agent_id"])
        assert g3 is not None, "taking a delegation started the delegate's run"
        assert g3 != a3, "delegate's run is distinct from the author's"
    # --- claim_proposal wires a caller-owned run ---------------------------
    p4 = db.create_proposal(
        a["token"], "T-per claim prop", "t-per body4", claimable=True
    )["post_id"]
    with db._conn() as conn:
        a4 = _open_agent_run(conn, p4, a["agent_id"])
        assert a4 is not None
    db.claim_proposal(d["token"], p4)
    with db._conn() as conn:
        d4 = _open_agent_run(conn, p4, d["agent_id"])
        assert d4 is not None, "claiming the proposal started the claimer's run"
        assert d4 != a4, "claimer's run is distinct from the author's"
    # --- bind never crosses agents -----------------------------------------
    p5b = db.create_proposal(
        a["token"], "T-per bind", "t-per body5", collaborative=True
    )["post_id"]
    lst5 = db.create_todo_list(a["token"], p5b, "L1", [{"text": "i1"}])
    db.join_proposal(b["token"], p5b)
    db.claim_todo_item(b["token"], p5b, lst5["items"][0]["id"])
    with db._conn() as conn:
        a5 = _open_agent_run(conn, p5b, a["agent_id"])
        b5 = _open_agent_run(conn, p5b, b["agent_id"])
        assert a5 is not None and b5 is not None
        bound = bind_open_run(conn, p5b, 50001, b["agent_id"])
        assert bound == b5, "a PR opened by beta stamps beta's own run"
        assert bound != a5, "beta's PR never stamps the author's run"
    print("  per-agent ownership: claim/list/deleg/claim-prop runs + gate scope ok")


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

    # --- per-PR lifecycle: every PR owns its run, closes on ITS OWN outcome ---
    # (part 2). The collaborative skip is gone: a collab proposal holds one
    # open run per bound PR - bind_open_run stamps the auto-start unbound run
    # with the first PR and opens a fresh bound run for each later PR - and a
    # PR outcome closes exactly the run(s) carrying that pr_number.
    pc = db.create_proposal(
        alpha["token"], "T4 collab workflow", "t4 body", collaborative=True
    )["post_id"]
    with db._conn() as conn:
        rc_unbound = int(_open_run(conn, pc)["id"])
        # first PR onto a collab proposal binds (stamps) the unbound run
        run4a = bind_open_run(conn, pc, 9001, alpha["agent_id"])
        assert run4a == rc_unbound, "first PR stamps the auto-started unbound run"
        assert _open_run(conn, pc)["id"] == rc_unbound, "the stamp keeps the run"
        # a second PR on the same proposal gets its OWN bound run
        run4b = bind_open_run(conn, pc, 9002, beta["agent_id"])
        assert run4b != run4a, "collab holds one open run PER PR, not one shared"
        assert bind_open_run(conn, pc, 9002, beta["agent_id"]) == run4b, (
            "re-binding a PR reuses its open run"
        )
        # the CI-green sweep scan set reflects exactly the bound open runs
        bound = list_bound_open_runs(conn)
        assert sorted(int(r["pr_number"]) for r in bound) == [9001, 9002], bound
        assert [int(r["id"]) for r in list_bound_open_runs(conn, {9001})] == [run4a], (
            "pr-number narrowing keeps only that PR's open run"
        )
        # per-agent restart is scoped to the CALLER's own open runs: alpha
        # holds run4a and beta holds run4b, so a blanket restart of alpha's
        # runs refuses only once alpha itself holds several. Give alpha a
        # second own run (a fresh bound run for a second alpha PR), then
        # retire it so the two-PR merge/complete lifecycle below sees 9001+9002.
        run_alpha2 = bind_open_run(conn, pc, 9005, alpha["agent_id"])
        assert run_alpha2 not in (run4a, run4b), "9005 owns a fresh alpha run"
        try:
            restart_workflow(conn, pc, alpha["agent_id"])
            raise AssertionError("restart with multiple OWN runs must be refused")
        except db.ForumError:
            pass
        conn.execute(
            "UPDATE workflow_runs SET status = 'closed', decided_at = ? WHERE id = ?",
            (_iso(_now()), run_alpha2),
        )
        row = conn.execute(
            "SELECT status FROM workflow_runs WHERE id = ?", (run4b,)
        ).fetchone()
        assert row["status"] == "open", "alpha's restart never touched beta's run"
        # each PR's merge closes exactly ITS run, leaving the sibling open
        close_workflow_for_pr(conn, 9001, "merged")
        row = conn.execute(
            "SELECT status FROM workflow_runs WHERE id = ?", (run4a,)
        ).fetchone()
        assert row["status"] == "merged", "bound run closes on ITS PR merge"
        row = conn.execute(
            "SELECT status FROM workflow_runs WHERE id = ?", (run4b,)
        ).fetchone()
        assert row["status"] == "open", "the sibling PR's run survives"
        # churn guard (belt): a DECIDED PR is never re-minted - the outcome
        # poller re-fetches recently-closed PRs every cycle, so re-binding a
        # PR whose run already closed as 'merged' must return None and leave
        # exactly one run behind, not start a fresh open one
        n_before = conn.execute(
            "SELECT COUNT(*) AS n FROM workflow_runs WHERE pr_number = 9001", ()
        ).fetchone()["n"]
        assert bind_open_run(conn, pc, 9001, alpha["agent_id"]) is None, (
            "re-binding an already-decided PR returns None"
        )
        n_after = conn.execute(
            "SELECT COUNT(*) AS n FROM workflow_runs WHERE pr_number = 9001", ()
        ).fetchone()["n"]
        assert n_before == n_after == 1, (
            f"decided-PR re-bind must not mint a fresh run ({n_before} -> {n_after})"
        )
        # CI-green completion (part 2): records 'completed', notifies the
        # starter (kind 'workflow') and is idempotent
        assert complete_workflow_for_pr(conn, 9002, "ci_green") == 1
        row = conn.execute(
            "SELECT status, decided_at FROM workflow_runs WHERE id = ?", (run4b,)
        ).fetchone()
        assert row["status"] == "completed" and row["decided_at"]
        notif = conn.execute(
            "SELECT kind, ref_type, ref_id, body FROM notifications"
            " WHERE kind = 'workflow' AND agent_id = ? ORDER BY id DESC LIMIT 1",
            (beta["agent_id"],),
        ).fetchone()
        assert notif is not None and "CI-green" in notif["body"], notif
        assert list_bound_open_runs(conn) == [], "no open bound runs remain"
        assert complete_workflow_for_pr(conn, 9002) == 0, "completion is idempotent"
        # non-collab control: a plain proposal's run closes on its PR outcome
        conn.execute(
            "UPDATE workflow_runs SET status = 'open', expires_at = ?"
            " WHERE proposal_id = ?",
            (_iso(_now() + timedelta(hours=1)), p3),
        )
        rp2 = int(_open_run(conn, p3)["id"])
        bind_open_run(conn, p3, 9003, beta["agent_id"])
        close_workflow_for_pr(conn, 9003, "merged")
        row = conn.execute(
            "SELECT status FROM workflow_runs WHERE id = ?", (rp2,)
        ).fetchone()
        assert row["status"] == "merged", "non-collab run closes on PR merge"
    print("  per-PR bind / close / complete / restart-guard ok")

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
        ev = conn.execute(
            "SELECT target_type, target_id, detail FROM events"
            " WHERE kind = 'workflow_closed' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        evd = json.loads(ev["detail"])
        assert ev["target_type"] == "workflow_run" and ev["target_id"] == r5, (
            "restart close event targets the closed run, not the post"
        )
        assert evd.get("run_id") == r5, "restart detail names the closed run"
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
        _tick_manual_steps(conn, p6, alpha["agent_id"])
        require_workflow_block(conn, p6, alpha["agent_id"])  # open run: passes silently
        conn.execute("UPDATE workflow_runs SET status = 'closed' WHERE id = ?", (r6,))
        require_workflow_block(conn, p6, alpha["agent_id"])  # retryable: reopens
        assert _open_run(conn, p6) is not None, (
            "non-terminal proposal reopens on demand"
        )
    # terminal proposal (superseded) blocks even when run-less
    p7 = db.create_proposal(alpha["token"], "T7 gate terminal", "t7 body")["post_id"]
    with db._conn() as conn:
        _tick_manual_steps(conn, p7, alpha["agent_id"])
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
        assert any(
            r["expires_in_seconds"] is not None for r in nudge["workflow_runs"]
        ), "runs with a TTL surface expires_in_seconds"
        assert any(
            "expires in" in nudge["workflow_note"]
            and r["expires_in_seconds"] is not None
            for r in nudge["workflow_runs"]
        ), "the note names a human-readable expiry for TTL-bound runs"
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

    # --- reconcile_open_runs: close stale open runs on decided proposals ------
    # A declined/closed proposal is retryable, so its live status is never
    # 'merged' - the OLD backfill ("skip merged") re-opened a run for it on
    # every boot, and nothing closed it (close_workflow_for_pr only fires on
    # poller-processed outcomes). reconcile heals exactly that residue.
    # The blocks above (TTL adaptivity, gate lazy-reopen, nudge) deliberately
    # fold and re-open runs on still-'open' proposals with no PR ever linked.
    # Under A2 those are ghost runs (an open copy behind a folded run and no
    # proposal_links row) - reconcile closes them too, so clear the residue
    # they left so the exact counts below measure only what this block creates.
    with db._conn() as conn:
        reconcile_open_runs(conn)
    p8 = db.create_proposal(gamma["token"], "T8 reconcile live", "t8 body")["post_id"]
    p9 = db.create_proposal(gamma["token"], "T9 reconcile declined", "t9 body")[
        "post_id"
    ]
    with db._conn() as conn:
        r9 = int(_open_run(conn, p9)["id"])
        db.record_proposal_outcome(81234, p9, "declined", db._now_iso(), conn=conn)
        assert db._proposal_status_for(conn, p9) == "declined"
        # live proposal untouched: reconcile ignores 'open'-status proposals
        assert stale_open_run_count(conn) == 1, stale_open_run_count(conn)
        assert reconcile_open_runs(conn) == 1
        row = conn.execute(
            "SELECT status, decided_at FROM workflow_runs WHERE id = ?", (r9,)
        ).fetchone()
        assert row["status"] == "declined" and row["decided_at"], (
            "stale run closes to the proposal's exact decided status"
        )
        assert _open_run(conn, p8) is not None, "live run survives reconciliation"
        ev = conn.execute(
            "SELECT target_type, target_id, detail FROM events"
            " WHERE kind = 'workflow_closed' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        evd = json.loads(ev["detail"])
        assert ev["target_type"] == "post" and ev["target_id"] == p9
        assert evd["reason"] == "proposal_decided" and evd["run_ids"] == [r9]
        assert evd["status"] == "declined" and evd["proposal_id"] == p9
        # idempotent: a second pass closes nothing
        assert reconcile_open_runs(conn) == 0 and stale_open_run_count(conn) == 0
        # reopen the run manually (the wedge this sweep exists to clear) and
        # prove the sweep closes it again
        conn.execute("UPDATE workflow_runs SET status = 'open' WHERE id = ?", (r9,))
        conn.execute("UPDATE workflow_runs SET decided_at = NULL WHERE id = ?", (r9,))
        assert stale_open_run_count(conn) == 1
        assert reconcile_open_runs(conn) == 1
    print("  reconcile live/declined/idempotent ok")

    # superseded proposals close to 'closed' (locked by a newer version)
    p10 = db.create_proposal(gamma["token"], "T10 reconcile superseded", "t10 body")[
        "post_id"
    ]
    with db._conn() as conn:
        r10 = int(_open_run(conn, p10)["id"])
    db.supersede_proposal(
        gamma["token"], p10, "T10 reconcile superseded v2", "t10 superseded body"
    )
    with db._conn() as conn:
        assert db._proposal_status_for(conn, p10) == "open", (
            "superseded-only proposal reports 'open' from PR status"
        )
        # re-open the run that supersede closed (simulating the pre-feature
        # residue) - reconcile must close it via the superseded gate
        conn.execute("UPDATE workflow_runs SET status = 'open' WHERE id = ?", (r10,))
        conn.execute("UPDATE workflow_runs SET decided_at = NULL WHERE id = ?", (r10,))
        assert stale_open_run_count(conn) == 1
        assert reconcile_open_runs(conn) == 1
        row = conn.execute(
            "SELECT status, decided_at FROM workflow_runs WHERE id = ?", (r10,)
        ).fetchone()
        assert row["status"] == "closed" and row["decided_at"]
    print("  reconcile superseded -> closed ok")

    # collaborative-open proposals read 'open' (db/_proposal_status) - a live
    # collab run must survive reconciliation untouched
    p13 = db.create_proposal(
        gamma["token"],
        "T13 reconcile collab-open",
        "t13 body",
        collaborative=True,
        max_collaborators=2,
    )["post_id"]
    with db._conn() as conn:
        r13 = int(_open_run(conn, p13)["id"])
        assert db._proposal_status_for(conn, p13) == "open"
        assert stale_open_run_count(conn) == 0, stale_open_run_count(conn)
        assert reconcile_open_runs(conn) == 0
        assert int(_open_run(conn, p13)["id"]) == r13, (
            "a collaborative-open proposal's run is not stale"
        )
    print("  reconcile collaborative-open untouched ok")

    # CHARTER VI.5: a declined proposal retried in flight (fresh PR link, no
    # outcome yet) reads 'open' again - reconcile must leave its run alone so
    # the retry's checklist isn't stolen mid-flight
    p14 = db.create_proposal(gamma["token"], "T14 reconcile retried", "t14 body")[
        "post_id"
    ]
    with db._conn() as conn:
        r14 = int(_open_run(conn, p14)["id"])
        db.record_proposal_outcome(81236, p14, "declined", db._now_iso(), conn=conn)
        assert db._proposal_status_for(conn, p14) == "declined"
    db.link_pr_to_proposal(81237, p14, gamma["agent_id"])  # the retry PR
    with db._conn() as conn:
        assert db._proposal_status_for(conn, p14) == "open", (
            "a fresh PR link flips the declined proposal back to open"
        )
        assert stale_open_run_count(conn) == 0, stale_open_run_count(conn)
        assert reconcile_open_runs(conn) == 0
        assert int(_open_run(conn, p14)["id"]) == r14, (
            "a retried-in-flight run survives reconciliation"
        )
    print("  reconcile declined->retried skipped ok")

    # pre-index residue: two open runs on one proposal (possible only on a DB
    # that predates idx_workflow_runs_open_unbound). Dropping the partial
    # unique index frees the unbound 'open' slot, then two open rows can
    # coexist; reconcile closes BOTH in one pass with a single count:2 event,
    # and the index is re-created (init_db would anyway, but leaving the DB
    # guarded for later blocks).
    p15 = db.create_proposal(gamma["token"], "T15 reconcile multi-open", "t15 body")[
        "post_id"
    ]
    with db._conn() as conn:
        r15a = int(_open_run(conn, p15)["id"])
        conn.execute("DROP INDEX IF EXISTS idx_workflow_runs_open_unbound")
        cur = conn.execute(
            "INSERT INTO workflow_runs"
            " (workflow_path, workflow_sha, status, proposal_id, agent_id,"
            "  created_at, expires_at)"
            " VALUES (?, ?, 'open', ?, ?, ?, ?)",
            (
                _PATH,
                "pre-index-hash",
                p15,
                gamma["agent_id"],
                db._now_iso(),
                db._now_iso(),
            ),
        )
        r15b = int(cur.lastrowid)
        db.record_proposal_outcome(81238, p15, "declined", db._now_iso(), conn=conn)
        closed = reconcile_open_runs(conn)
        assert closed == 2, closed
        rows = conn.execute(
            "SELECT status FROM workflow_runs WHERE id IN (?, ?)", (r15a, r15b)
        ).fetchall()
        assert {r["status"] for r in rows} == {"declined"}, (
            "every residue run closes to the proposal's exact decided status"
        )
        ev = conn.execute(
            "SELECT detail FROM events"
            " WHERE kind = 'workflow_closed' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        evd = json.loads(ev["detail"])
        assert evd["count"] == 2 and sorted(evd["run_ids"]) == sorted([r15a, r15b]), evd
        assert stale_open_run_count(conn) == 0, stale_open_run_count(conn)
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_workflow_runs_open_unbound"
            " ON workflow_runs(workflow_path, proposal_id)"
            " WHERE status = 'open' AND pr_number IS NULL"
        )
    print("  reconcile multi-open residue -> closed 2 ok")

    # --- boot sweep: init_db backfill + reconcile (cross-boot stability) ------
    # The backfill's skip predicate is now "!= open", not "== merged", so a
    # decided proposal is never re-opened on boot; reconcile then closes any
    # run that the old gate leaked. Re-running init_db proves the pair.
    p11 = db.create_proposal(beta["token"], "T11 boot sweep live", "t11 body")[
        "post_id"
    ]
    p12 = db.create_proposal(beta["token"], "T12 boot sweep declined", "t12 body")[
        "post_id"
    ]
    with db._conn() as conn:
        r12 = int(_open_run(conn, p12)["id"])
        db.record_proposal_outcome(81235, p12, "declined", db._now_iso(), conn=conn)
    db.init_db()  # second boot: schema + backfill + reconcile
    with db._conn() as conn:
        row = conn.execute(
            "SELECT status FROM workflow_runs WHERE id = ?", (r12,)
        ).fetchone()
        assert row["status"] == "declined", (
            "boot reconciliation closes the leaked run on init_db"
        )
        assert _open_run(conn, p12) is None, (
            "backfill never re-opens a run for a decided proposal"
        )
        assert _open_run(conn, p11) is not None, (
            "live proposal keeps (or regains) its run across boots"
        )
    print("  boot sweep backfill + reconcile cross-boot ok")

    # --- ghost-run residue: A1 backfill guard + A2 reconcile ------------------
    # A still-'open' proposal that folded its create-pr run with no PR ever
    # linked is what the OLD boot backfill ("skip only merged") leaked: every
    # boot re-opened the run, and it then idled until the TTL sweep folded it
    # again - a perpetual ghost. A1 now back-seeds only proposals that NEVER
    # had a create-pr run (any status), so this state never regenerates; A2's
    # reconcile closes any copy a pre-fix boot leaked.
    pg1 = db.create_proposal(gamma["token"], "T16 ghost live no-link", "t16 body")[
        "post_id"
    ]
    pg2 = db.create_proposal(beta["token"], "T17 runless live boot-seeded", "t17 body")[
        "post_id"
    ]
    with db._conn() as conn:
        rg1 = int(_open_run(conn, pg1)["id"])
        # fold the run exactly as a TTL expiry would - no PR was ever linked
        conn.execute(
            "UPDATE workflow_runs SET status = 'closed', decided_at = ? WHERE id = ?",
            (db._now_iso(), rg1),
        )
        # pg2 has never had a create-pr run (drop the auto-started one) - the
        # same state as a proposal written before the feature landed
        conn.execute("DELETE FROM workflow_runs WHERE proposal_id = ?", (pg2,))
    db.init_db()  # a fresh boot: schema + backfill + reconcile
    with db._conn() as conn:
        # A1: the backfill seeds only run-less proposals - pg1's folded run
        # (any status) stops it from being a candidate again.
        assert _open_run(conn, pg1) is None, (
            "a proposal that folded a run without a link is never re-seeded"
        )
        assert _open_run(conn, pg2) is not None, (
            "a run-less live proposal is still boot-seeded"
        )
        # A2: simulate the OLD gate's leak (a fresh open copy of a folded run,
        # the tell left in place - exactly what a pre-fix boot produced) and
        # prove reconcile closes it as a ghost, not as a decided proposal.
        conn.execute(
            "INSERT INTO workflow_runs"
            " (workflow_path, workflow_sha, status, proposal_id, agent_id,"
            "  created_at, expires_at)"
            " VALUES (?, ?, 'open', ?, ?, ?, ?)",
            (
                _PATH,
                "ghost-copy-hash",
                pg1,
                gamma["agent_id"],
                db._now_iso(),
                db._now_iso(),
            ),
        )
        rg1c = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        assert stale_open_run_count(conn) == 1, stale_open_run_count(conn)
        assert reconcile_open_runs(conn) == 1
        row = conn.execute(
            "SELECT status, decided_at FROM workflow_runs WHERE id = ?", (rg1c,)
        ).fetchone()
        assert row["status"] == "closed" and row["decided_at"] is not None, (
            "the ghost run reconciles to 'closed'"
        )
        ev = _last_close_event(conn)
        assert ev["reason"] == "no_pr_linked", ev
        assert ev["status"] == "closed" and ev["proposal_id"] == pg1, ev
        assert ev["run_ids"] == [rg1c], ev
        assert _open_run(conn, pg2) is not None, (
            "the freshly-seeded live run survives reconciliation"
        )
    print("  ghost-run residue: A1 guard + A2 reconcile ok")

    # --- guided steps (workflows part 2, PR B) ------------------------------
    # Every open create-pr run snapshots the workflow's `## Steps` checklist
    # (ordered `**key**` tokens) into workflow_run_steps. The parser yields
    # create-pr's 7 keys in order; a fresh proposal's run carries them; manual
    # ticks are starter/author/delegate-only, idempotent and audited
    # (done_by); the managed 'open'/'verify' keys refuse hand ticks; the
    # steps gate (WORKFLOW_STEPS_ENFORCE=1) blocks until every step before
    # 'open' is ticked but dry_run bypasses it; bind auto-ticks 'open',
    # CI-green/merge auto-tick 'verify'; count_workflow_runs counts what the
    # LIMIT-50 listing truncates; seed_steps_for_open_runs backfills
    # pre-feature open runs.
    parsed = _parse_workflow_steps(_PATH)
    expected_keys = [
        "update-local",
        "validate-manifest",
        "not-gutted",
        "lint",
        "test",
        "open",
        "verify",
    ]
    assert [p["key"] for p in parsed] == expected_keys, [p["key"] for p in parsed]
    assert all(p["text"].startswith(f"{i}. ") for i, p in enumerate(parsed, start=1)), (
        "steps snapshot the whole numbered line"
    )
    print("  steps: create-pr parser (7 keys in order) ok")

    ps = db.create_proposal(beta["token"], "T18 steps gate", "t18 body")["post_id"]
    with db._conn() as conn:
        r18 = int(_open_run(conn, ps)["id"])
        steps = workflow_steps_for_run(conn, r18)
        assert [s["step_key"] for s in steps] == expected_keys, (
            "a fresh run carries the full checklist in order"
        )
        assert [s["position"] for s in steps] == list(range(1, 8))
        assert all(s["text"] for s in steps), "steps carry snapshotted text"
        assert all(not s["done"] for s in steps), "fresh steps start unticked"
        assert any(s["done_by_name"] is None for s in steps), (
            "unticked steps have no owner yet"
        )
        # manual tick by the proposal author is audited and idempotent
        ticked = tick_workflow_step(conn, r18, "update-local", beta["agent_id"])
        assert ticked["done"] == 1 and ticked["done_by_name"] == beta["name"], ticked
        again = tick_workflow_step(conn, r18, "update-local", beta["agent_id"])
        assert again["done"] == 1 and again["done_at"] == ticked["done_at"], (
            "re-ticking a done step is a no-op (same stamp)"
        )
    print("  steps: author tick audited + idempotent ok")

    ps2 = db.create_proposal(beta["token"], "T19 steps permission", "t19 body")[
        "post_id"
    ]
    with db._conn() as conn:
        # swap the starter to gamma (restart-equivalent) so starter != author
        r2_ = int(_open_run(conn, ps2)["id"])
        conn.execute("UPDATE workflow_runs SET status = 'closed' WHERE id = ?", (r2_,))
        r19 = start_workflow(conn, _PATH, ps2, gamma["agent_id"])
        tick_workflow_step(conn, r19, "lint", gamma["agent_id"])  # starter may tick
        tick_workflow_step(conn, r19, "lint", beta["agent_id"])  # author may tick
        try:
            tick_workflow_step(conn, r19, "lint", agents["delta"]["agent_id"])
            raise AssertionError("an outsider must not tick a run's steps")
        except db.ForumError:
            pass
        for managed in ("open", "verify"):
            try:
                tick_workflow_step(conn, r19, managed, beta["agent_id"])
                raise AssertionError(
                    f"hand tick of managed {managed!r} must be refused"
                )
            except db.ForumError as exc:
                assert "auto-managed" in str(exc), exc
    print("  steps: starter/author/delegate-only + managed refusal ok")

    ps3 = db.create_proposal(beta["token"], "T20 steps gate", "t20 body")["post_id"]
    with db._conn() as conn:
        r3_ = int(_open_run(conn, ps3)["id"])
        # gate: unticked manual steps before 'open' block with a remedy list
        try:
            require_workflow_block(conn, ps3, beta["agent_id"])
            raise AssertionError("the steps gate must block while 2-5 are unticked")
        except db.ForumError as exc:
            msg = str(exc)
            assert "waiting on completed steps before 'open'" in msg, msg
            assert "validate-manifest" in msg and "not-gutted" in msg, msg
            assert "repo_workflow_step" in msg, msg
        # dry_run passes through untested - validate-manifest rehearses dry-run
        require_workflow_block(conn, ps3, beta["agent_id"], dry_run=True)
        # tick 1-5 and the gate clears
        for key in ("update-local", "validate-manifest", "not-gutted", "lint", "test"):
            tick_workflow_step(conn, r3_, key, beta["agent_id"])
        require_workflow_block(conn, ps3, beta["agent_id"])
        # binding a PR auto-ticks managed 'open'
        r18b = bind_open_run(conn, ps3, 84002, beta["agent_id"])
        assert r18b == r3_, "binding stamps the unbound open run"
        open_step = next(
            s for s in workflow_steps_for_run(conn, r3_) if s["step_key"] == "open"
        )
        assert open_step["done"] == 1, "PR-link auto-ticks the managed 'open' step"
        # merge auto-ticks managed 'verify' (system tick, no actor)
        close_workflow_for_pr(conn, 84002, "merged")
        verify_step = next(
            s for s in workflow_steps_for_run(conn, r3_) if s["step_key"] == "verify"
        )
        assert verify_step["done"] == 1 and verify_step["done_by"] is None, (
            "merge auto-ticks 'verify' as a system tick"
        )
        # per-agent ownership: a PR opened by beta binds BETA's own run, never
        # gamma's differently-owned run (gamma is ps2's starter). So binding
        # 84001 by beta creates a fresh beta run; gamma's untouched r19 is not
        # stamped, and CI-green on 84001 credits beta's own run.
        r19b = bind_open_run(conn, ps2, 84001, beta["agent_id"])
        assert r19b != r19, "per-agent: beta's PR binds beta's own run, not gamma's"
        r19b_open = next(
            s for s in workflow_steps_for_run(conn, r19b) if s["step_key"] == "open"
        )
        assert r19b_open["done"] == 1, "PR-link auto-ticks the opener's BETA run"
        r19_open = next(
            s for s in workflow_steps_for_run(conn, r19) if s["step_key"] == "open"
        )
        assert r19_open["done"] == 0, "beta's PR never stamps gamma's run"
        complete_workflow_for_pr(conn, 84001, "ci_green")
        r19b_verify = next(
            s for s in workflow_steps_for_run(conn, r19b) if s["step_key"] == "verify"
        )
        assert r19b_verify["done"] == 1, "CI-green auto-ticks 'verify'"
        assert r19b_verify["done_by_name"] == beta["name"], (
            "verify credit goes to the run's starter (the opener's OWN run)"
        )
    print("  steps: gate blocks/dry-run bypass + open/verify auto-ticks ok")

    # the ledger's summary count is the unbounded COUNT(*), never len(list):
    # list_workflow_runs caps at 50 rows, so a busy ledger must not
    # undercount the admin summary line.
    with db._conn() as conn:
        # the ledger carries a steps_summary on steps-bearing runs
        assert any(r["steps_summary"] is not None for r in list_workflow_runs(conn)), (
            "ledger rows carry a steps_summary where the run has steps"
        )
        total_before = count_workflow_runs(conn)
        closed_before = count_workflow_runs(conn, status="closed")
        for i in range(72001, 72056):
            conn.execute(
                "INSERT INTO workflow_runs"
                " (workflow_path, workflow_sha, status, proposal_id, pr_number,"
                "  agent_id, created_at, decided_at)"
                " VALUES (?, ?, 'closed', ?, ?, ?, ?, ?)",
                (
                    _PATH,
                    "bulk-hash",
                    ps3,
                    i,
                    beta["agent_id"],
                    db._now_iso(),
                    db._now_iso(),
                ),
            )
        assert count_workflow_runs(conn) == total_before + 55, count_workflow_runs(conn)
        assert count_workflow_runs(conn, status="closed") == closed_before + 55
        closed_rows = list_workflow_runs(conn, status="closed")
        assert len(closed_rows) == 50, "the ledger listing stays capped at 50 rows"
    print("  steps: count_workflow_runs (COUNT(*)) beats the LIMIT-50 listing ok")

    # pre-feature backfill: an open run with no steps is seeded by the boot
    # hook (seed_steps_for_open_runs) and stays idempotent on the next pass.
    ps4 = db.create_proposal(beta["token"], "T21 steps backfill", "t21 body")["post_id"]
    with db._conn() as conn:
        r4_ = int(_open_run(conn, ps4)["id"])
        conn.execute("DELETE FROM workflow_run_steps WHERE run_id = ?", (r4_,))
        assert workflow_steps_for_run(conn, r4_) == [], "the run is pre-feature"
        assert seed_steps_for_open_runs(conn) >= 1
        assert [s["step_key"] for s in workflow_steps_for_run(conn, r4_)] == (
            expected_keys
        ), "the lazily-ignored backfill seeds the full checklist"
        assert seed_steps_for_open_runs(conn) == 0, "a second pass seeds nothing new"
    print("  steps: seed_steps_for_open_runs backfill + idempotence ok")

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

    # --- link_pr_to_proposal stamps degrade silently (no workflow_runs) ------
    # The P0-1 stamp must never fail the proposal-link itself: a schema that
    # predates workflow_runs (or a test booting a minimal schema) must still
    # record the link. Drop the table and prove the call survives.
    with db._conn() as conn:
        conn.execute("DROP TABLE IF EXISTS workflow_runs")
    db.link_pr_to_proposal(70999, post_id, alpha["agent_id"])
    with db._conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM proposal_links WHERE pr_number = 70999", ()
        ).fetchone()
        assert row is not None, "the PR link records even when workflow_runs is absent"
    print("  link_pr_to_proposal degrades silently without workflow_runs ok")

    # run last: it leaves own runs behind, which would perturb the earlier
    # global run-ledger/sweep assertions if it ran up front.
    test_per_agent_ownership(agents)
    print("ALL WORKFLOW TESTS PASSED")


if __name__ == "__main__":
    main()
