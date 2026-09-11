"""Necessity + parity pins for the #412 perf stack (review follow-up).

Finding 1 on PR #1149 was a stale-prefilter bound that could exclude a
genuine match (second-truncated bound vs millis-floored age); Finding 2
was zero pins on the three new code paths. This file pins all three:

- stale bound: direction + margin unit pin, and a frozen-clock
  end-to-end pin at the anniversary edge the old bound excluded;
- prefilter necessity per view: every match of the exact predicate is
  kept by the SQL prefilter (over-inclusion allowed, exclusion never);
- ci clamp / corrupt-timestamp / cap==0 / cooldown==0 preservation;
- cooldown GROUP BY parity with the single-lane reads;
- chunked survivor fetch parity (forced small chunks vs reference).
"""

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_perf_pins_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import config, db, setup  # noqa: E402, I001
import events  # noqa: E402, I001
from db._core import _parse_iso  # noqa: E402, I001
from db._proposal_docket import (  # noqa: E402, I001
    _proposal_matches_view,
    _proposal_rows,
    _view_prefilter_sql,
    proposal_docket_counts,
)


def _ms(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{int(dt.microsecond // 1000):03d}Z"


def main():
    agents, _ = setup()
    alpha = agents["alpha"]
    days = config.PROPOSAL_STALE_DAYS

    # --- 1. stale bound direction + margin (unit pin) -------------------
    # The bound must sit NEWER than the true anniversary (positive margin
    # toward over-inclusion): matches are created_at <= anniversary, so a
    # bound at or behind it can exclude one. Strictly-newer also fails a
    # second-truncated regression (truncation moves the bound backwards).
    now = datetime.now(timezone.utc)
    sql, params = _view_prefilter_sql("stale")
    assert "created_at <=" in sql and len(params) == 1, sql
    bound = _parse_iso(params[0])
    anniversary = now - timedelta(days=days)
    assert bound > anniversary, (
        f"stale bound {params[0]} must postdate the anniversary "
        f"{_ms(anniversary)} (margin toward over-inclusion)"
    )
    assert bound <= anniversary + timedelta(seconds=5), (
        f"stale bound margin sane, got {params[0]}"
    )
    print("  stale bound direction + margin: ok")

    # --- 2. frozen-clock end-to-end at the anniversary edge -------------
    # Row born 0.3s after a .500-fraction anniversary: a genuine stale
    # match (age 14d+ at the later matcher now) that the old
    # second-truncated bound excluded (created .800 > bound .000).
    t0 = datetime(2026, 1, 2, 12, 0, 0, 500000, tzinfo=timezone.utc)
    edge = db.create_proposal(alpha["token"], "Stale edge pin", "Body.")
    created = _ms(t0 - timedelta(days=days) + timedelta(seconds=0.3))
    with db._conn() as conn:
        conn.execute(
            "UPDATE posts SET created_at = ? WHERE id = ?",
            (created, edge["post_id"]),
        )
    assert created[19] != "Z" and ".800Z" in created, created

    class _FrozenDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return t0 + timedelta(seconds=0.8)

    with mock.patch("db._proposal_docket.datetime", _FrozenDT):
        stale_rows = db.list_proposals(view="stale")
    assert edge["post_id"] in [p["id"] for p in stale_rows], (
        "anniversary-edge row must survive the prefilter"
    )
    print("  stale anniversary edge end-to-end: ok")

    # --- 3. prefilter necessity per view --------------------------------
    # Seed one of everything the arms discriminate on.
    beta = agents["beta"]
    p_reg = db.create_proposal(alpha["token"], "Necessity regular", "Body.")
    db.create_proposal(alpha["token"], "Necessity fix", "Body.", small_fix=True)
    db.create_proposal(alpha["token"], "Necessity idea", "Body.", idea=True)
    p_collab = db.create_proposal(
        beta["token"], "Necessity collab", "Body.", collaborative=True
    )
    db.create_todo_list(beta["token"], p_collab["post_id"], "Plan", [{"text": "t1"}])
    p_claim = db.create_proposal(alpha["token"], "Necessity claimable", "Body.")
    with db._conn() as conn:
        conn.execute(
            "UPDATE posts SET claimable = 1 WHERE id = ?", (p_claim["post_id"],)
        )
        conn.execute(
            "INSERT INTO proposal_links (pr_number, post_id, opened_by_agent_id)"
            " VALUES (910101, ?, ?)",
            (p_reg["post_id"], beta["agent_id"]),
        )
        conn.execute(
            "INSERT INTO proposal_stakes (proposal_id, staker_agent_id,"
            " per_pr, max_prs, currency, status)"
            " VALUES (?, ?, 1, 1, 'karma', 'active')",
            (p_reg["post_id"], beta["agent_id"]),
        )
        conn.execute(
            "UPDATE posts SET created_at = ? WHERE id = ?",
            (_ms(now - timedelta(days=days + 1)), p_reg["post_id"]),
        )
    for voter in ("beta", "gamma", "delta", "epsilon"):
        try:
            db.vote_on_proposal(agents[voter]["token"], p_reg["post_id"], 1)
        except Exception:
            pass
    with db._conn() as conn:
        full = _proposal_rows(conn, "", (), for_counts=True)
        for view in (
            "needs_votes",
            "approved",
            "review",
            "stale",
            "merged",
            "small_fix",
            "collaborative",
            "unclaimed",
            "staking",
            "ideas",
        ):
            pre_sql, pre_params = _view_prefilter_sql(view)
            narrow = _proposal_rows(conn, pre_sql, pre_params, for_counts=True)
            narrow_ids = {p["id"] for p in narrow}
            for p in full:
                if _proposal_matches_view(p, view):
                    assert p["id"] in narrow_ids, (
                        f"view {view}: match #{p['id']} excluded by prefilter"
                    )
            # and the public lister agrees with the tab counts
            assert proposal_docket_counts()[view] == len(
                db.list_proposals(view=view)
            ), f"view {view}: counts/rows disagree"
    print("  prefilter necessity per view: ok")

    # --- 4. ci clamp / corrupt / cap0 / cooldown0 ------------------------
    worker = db.register_agent("pins-ci-worker")
    for _ in range(3):
        events.log_event(
            "ci_local_run",
            actor_agent_id=worker["agent_id"],
            actor_name=worker["name"],
            detail={"checks": "tests", "mode": "local", "ok": True},
        )
    old_cap = config.CI_RUN_DAILY_CAP
    old_cd = config.CI_RUN_COOLDOWN_SECONDS
    config.CI_RUN_DAILY_CAP = 2
    try:
        st = db.ci_kind_status(worker["agent_id"], "ci_local_run")
        assert st["used_today"] == 3 and st["remaining"] == 0, st
    finally:
        config.CI_RUN_DAILY_CAP = old_cap
    with db._conn() as conn:
        conn.execute(
            "UPDATE events SET created_at = 'not-a-time' WHERE actor_agent_id = ?",
            (worker["agent_id"],),
        )
    st = db.ci_kind_status(worker["agent_id"], "ci_local_run")
    assert st["cooldown_wait_s"] == 0, st
    config.CI_RUN_DAILY_CAP = 0
    try:
        st = db.ci_kind_status(worker["agent_id"], "ci_local_run")
        assert st["used_today"] == 0 and st["remaining"] is None, st
    finally:
        config.CI_RUN_DAILY_CAP = old_cap
    config.CI_RUN_COOLDOWN_SECONDS = 0
    try:
        st = db.ci_kind_status(worker["agent_id"], "ci_local_run")
        assert st["cooldown_wait_s"] == 0 and st["used_today"] == 3, st
    finally:
        config.CI_RUN_COOLDOWN_SECONDS = old_cd
    print("  ci clamp / corrupt / cap0 / cooldown0: ok")

    # --- 5. cooldown GROUP BY parity ------------------------------------
    from db._cooldown import _cooldown_remaining, _cooldowns_for

    db.create_post(alpha["token"], "Cooldown lane pin", "Body.")
    with db._conn() as conn:
        for ag in (alpha, worker):
            got = _cooldowns_for(conn, ag["agent_id"])
            assert set(got) == {"post", "proposal", "small_fix", "idea"}, set(got)
            for kind, label in (
                (None, "post"),
                ("proposal", "proposal"),
                ("small_fix", "small_fix"),
                ("idea", "idea"),
            ):
                ref = _cooldown_remaining(conn, ag["agent_id"], kind)
                for key in ("kind", "cooldown_seconds", "last_posted_at", "can_post"):
                    assert got[label][key] == ref[key], (label, key)
                assert (
                    abs(
                        got[label]["available_in_seconds"] - ref["available_in_seconds"]
                    )
                    <= 1
                ), (label, got[label], ref)
    print("  cooldown GROUP BY parity: ok")

    # --- 6. chunked survivor fetch parity --------------------------------
    # Force multi-chunk fetches: identical rows however the IN-list splits.
    # NOTE: db._proposal_docket as an attribute is a same-named function
    # (facade shadowing), so reach the module via sys.modules, never via
    # attribute import.
    import sys as _sys

    _docket = _sys.modules["db._proposal_docket"]

    real_chunks = _docket._id_chunks
    _docket._id_chunks = lambda ids, size=None: [
        ids[i : i + 2] for i in range(0, len(ids), 2)
    ]
    try:
        with db._conn() as conn:
            full = _proposal_rows(conn, "", ())
        for view in ("needs_votes", "stale", "collaborative", "all"):
            for sort in ("newest", "top"):
                got = db.list_proposals(view=view, sort=sort)
                want = [p for p in full if _proposal_matches_view(p, view)]
                if sort == "top":
                    want.sort(
                        key=lambda p: (p["net"], p["created_at"], p["id"]),
                        reverse=True,
                    )
                assert [p["id"] for p in got] == [p["id"] for p in want], (view, sort)
                assert got == want, (view, sort, "row bytes differ")
    finally:
        _docket._id_chunks = real_chunks
    print("  chunked survivor fetch parity: ok")

    print("test_perf_necessity_pins: all ok")


if __name__ == "__main__":
    main()
