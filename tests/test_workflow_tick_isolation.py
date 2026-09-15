"""Sibling isolation for workflow ticks + scoped CI auto-tick (#B27)."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_tick_iso_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)
os.environ["FORUM_WORKFLOW_ENFORCE"] = "1"
os.environ["FORUM_WORKFLOW_TTL_SECONDS"] = "3600"
os.environ["FORUM_WORKFLOW_STEPS_ENFORCE"] = "1"
os.environ["FORUM_WORKFLOW_PER_AGENT"] = "1"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db._workflow import (  # noqa: E402
    auto_tick_ci_steps,
    tick_workflow_step,
    workflow_steps_for_run,
)
from server.ci_runner._runs import _NO_TICK_CHECKS  # noqa: E402
from tests._setup import db, setup  # noqa: E402

setup()

TRIPLE = ("not-gutted", "lint", "test")

_n = [0]


def _fresh(prefix):
    _n[0] += 1
    return db.register_agent(f"{prefix}-{_n[0]}")


def _open_run_id(conn, pid, agent_id):
    row = conn.execute(
        "SELECT id FROM workflow_runs WHERE proposal_id = ?"
        " AND status = 'open' AND agent_id = ?",
        (pid, agent_id),
    ).fetchone()
    assert row is not None, f"no open run for proposal {pid}"
    return int(row["id"])


def _done_map(conn, run_id):
    return {
        s["step_key"]: bool(s["done"]) for s in workflow_steps_for_run(conn, run_id)
    }


def test_manual_tick_sibling_isolation():
    # Pins the already-correct manual path (never the #B27 fan-out site).
    ag = _fresh("tiso-man")
    p1 = db.create_proposal(ag["token"], "Tick iso A", "body A")["post_id"]
    p2 = db.create_proposal(ag["token"], "Tick iso B", "body B")["post_id"]
    with db._conn() as conn:
        r1 = _open_run_id(conn, p1, ag["agent_id"])
        r2 = _open_run_id(conn, p2, ag["agent_id"])
        for k in TRIPLE:
            tick_workflow_step(conn, r1, k, ag["agent_id"])
        d1 = _done_map(conn, r1)
        d2 = _done_map(conn, r2)
        for k in TRIPLE:
            assert d1[k] is True, (k, d1)
            assert d2[k] is False, (k, d2)


def test_local_singleton_ticks():
    ag = _fresh("tiso-solo")
    p = db.create_proposal(ag["token"], "Tick solo", "body")["post_id"]
    with db._conn() as conn:
        r = _open_run_id(conn, p, ag["agent_id"])
        out = auto_tick_ci_steps(
            conn,
            agent_id=ag["agent_id"],
            local_mode=True,
            branch_mode=False,
            ci_started_iso=db._now_iso(),
            tick_stamp=db._now_iso(),
        )
        assert sorted(o["step_key"] for o in out) == sorted(TRIPLE), out
        d = _done_map(conn, r)
        for k in TRIPLE:
            assert d[k] is True, (k, d)
        rows = conn.execute(
            "SELECT DISTINCT done_at FROM workflow_run_steps"
            " WHERE run_id = ? AND step_key IN"
            " ('not-gutted','lint','test')",
            (r,),
        ).fetchall()
        assert len(rows) == 1, rows


def test_local_ambiguous_ticks_nothing():
    ag = _fresh("tiso-amb")
    p1 = db.create_proposal(ag["token"], "Tick amb A", "body")["post_id"]
    p2 = db.create_proposal(ag["token"], "Tick amb B", "body")["post_id"]
    with db._conn() as conn:
        r1 = _open_run_id(conn, p1, ag["agent_id"])
        r2 = _open_run_id(conn, p2, ag["agent_id"])
        out = auto_tick_ci_steps(
            conn,
            agent_id=ag["agent_id"],
            local_mode=True,
            branch_mode=False,
            ci_started_iso=db._now_iso(),
            tick_stamp=db._now_iso(),
        )
        assert out == [], out
        for r in (r1, r2):
            d = _done_map(conn, r)
            for k in TRIPLE:
                assert d[k] is False, (r, k, d)


def test_branch_ticks_bound_only():
    ag = _fresh("tiso-br")
    pb = db.create_proposal(ag["token"], "Tick br bound", "body")["post_id"]
    pf = db.create_proposal(ag["token"], "Tick br free", "body")["post_id"]
    db.link_pr_to_proposal(95001, pb, ag["agent_id"])
    with db._conn() as conn:
        rb = _open_run_id(conn, pb, ag["agent_id"])
        rf = _open_run_id(conn, pf, ag["agent_id"])
        out = auto_tick_ci_steps(
            conn,
            agent_id=ag["agent_id"],
            pr_number=95001,
            local_mode=False,
            branch_mode=True,
            ci_started_iso=db._now_iso(),
            tick_stamp=db._now_iso(),
        )
        assert {o["run_id"] for o in out} == {rb}, out
        dd = _done_map(conn, rb)
        df = _done_map(conn, rf)
        for k in TRIPLE:
            assert dd[k] is True, (k, dd)
            assert df[k] is False, (k, df)


def test_bench_native_system_never_tick():
    ag = _fresh("tiso-never")
    p = db.create_proposal(ag["token"], "Tick never", "body")["post_id"]
    with db._conn() as conn:
        r = _open_run_id(conn, p, ag["agent_id"])
        o1 = auto_tick_ci_steps(
            conn,
            agent_id=ag["agent_id"],
            local_mode=True,
            is_bench=True,
            ci_started_iso=db._now_iso(),
            tick_stamp=db._now_iso(),
        )
        assert o1 == [], o1
        o2 = auto_tick_ci_steps(
            conn,
            agent_id=ag["agent_id"],
            is_native=True,
            ci_started_iso=db._now_iso(),
            tick_stamp=db._now_iso(),
        )
        assert o2 == [], o2
        o3 = auto_tick_ci_steps(
            conn,
            agent_id=ag["agent_id"],
            local_mode=True,
            is_system=True,
            ci_started_iso=db._now_iso(),
            tick_stamp=db._now_iso(),
        )
        assert o3 == [], o3
        d = _done_map(conn, r)
        for k in TRIPLE:
            assert d[k] is False, (k, d)


def test_temporal_and_permission_guards():
    ag = _fresh("tiso-guard")
    p = db.create_proposal(ag["token"], "Tick guard", "body")["post_id"]
    other = _fresh("tiso-guard-other")
    with db._conn() as conn:
        r = _open_run_id(conn, p, ag["agent_id"])
        out = auto_tick_ci_steps(
            conn,
            agent_id=ag["agent_id"],
            local_mode=True,
            branch_mode=False,
            ci_started_iso="2000-01-01T00:00:00.000Z",
            tick_stamp=db._now_iso(),
        )
        assert out == [], out
        db.link_pr_to_proposal(95002, p, ag["agent_id"])
        out2 = auto_tick_ci_steps(
            conn,
            agent_id=other["agent_id"],
            pr_number=95002,
            local_mode=False,
            branch_mode=True,
            ci_started_iso=db._now_iso(),
            tick_stamp=db._now_iso(),
        )
        assert out2 == [], out2
        d = _done_map(conn, r)
        for k in TRIPLE:
            assert d[k] is False, (k, d)


def test_no_tick_harness_set_and_branch_none():
    assert "benchmarks" in _NO_TICK_CHECKS, _NO_TICK_CHECKS
    assert "db_benchmark" in _NO_TICK_CHECKS
    assert "db_bench" in _NO_TICK_CHECKS
    assert "tests" not in _NO_TICK_CHECKS
    assert "static" not in _NO_TICK_CHECKS
    ag = _fresh("tiso-nonedef")
    p = db.create_proposal(ag["token"], "Tick none def", "body")["post_id"]
    with db._conn() as conn:
        r = _open_run_id(conn, p, ag["agent_id"])
        out = auto_tick_ci_steps(
            conn,
            agent_id=ag["agent_id"],
            pr_number=None,
            local_mode=False,
            branch_mode=True,
            ci_started_iso=db._now_iso(),
            tick_stamp=db._now_iso(),
        )
        assert out == [], out
        d = _done_map(conn, r)
        for k in TRIPLE:
            assert d[k] is False, (k, d)


def test_auto_tick_only_keys_scopes_static():
    # Static-only harness greens (checks="static") may tick `lint` alone:
    # the caller passes only_keys=("lint",); the default ticks all three.
    ag = _fresh("tiso-onlykeys")
    p = db.create_proposal(ag["token"], "Tick only keys", "body")["post_id"]
    with db._conn() as conn:
        r = _open_run_id(conn, p, ag["agent_id"])
        out = auto_tick_ci_steps(
            conn,
            agent_id=ag["agent_id"],
            local_mode=True,
            branch_mode=False,
            ci_started_iso=db._now_iso(),
            tick_stamp=db._now_iso(),
            only_keys=("lint",),
        )
        assert [o["step_key"] for o in out] == ["lint"], out
        d = _done_map(conn, r)
        assert [d[k] for k in TRIPLE] == [False, True, False], d


if __name__ == "__main__":
    fns = [
        v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)
    ]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)}/{len(fns)} tick-isolation tests passed")
