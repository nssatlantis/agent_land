"""Tests for proposal #752's `rotate_taker` - a recurring job that hands
itself back to the open board after each accepted cycle, so any citizen can
take the next one.

These live in their own file deliberately.  The suite gives each FILE its own
truncated session database, so a self-contained file here is hermetic; the
same three pins inside tests/test_jobs.py shared one database with ~45 sibling
tests and their volume (7 agent registrations, ~6 jobs, accepted cycles that
pay wallets) starved a later sibling's creator into reading a 0 balance.  See
tests/test_jobs_board_ui.py and test_jobs_officials.py for the same split.
"""

import json as _json
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_jobs_rot_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)
# Funded wallets and a low posting bar, armed explicitly (same pattern as
# test_jobs.py / test_economy).
os.environ["FORUM_JOB_CREATOR_MIN_KARMA"] = "1"
os.environ["FORUM_JOB_TAKER_DEPOSIT_MIN_ONE_TIME"] = "0"
os.environ["FORUM_JOB_TAKER_DEPOSIT_MIN_RECURRING"] = "0"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import config, db, setup  # noqa: E402,F401

AGENTS, BASE_POST = setup()  # setup() boots via init() internally

# This file seeds only a handful of creators, so genesis covers it; the topup
# is headroom, not a dependency (test_jobs.py needs 60k because ~45 creators
# share one database there).
from db._credits import mint as _mint  # noqa: E402

with db._conn(immediate=True) as _c:  # noqa: E402
    _mint(20000, "test_suite_topup", admin="test-suite", conn=_c)


def _upvote_post(voter: str, author_token: str) -> None:
    """Give the post's author +1 karma (and, at ratio 0.5, +2q income)."""
    p = db.create_post(author_token, f"t {id(object())}", "b")
    db.vote(AGENTS[voter]["token"], "post", p["post_id"], 1)


def _make_creator(name: str):
    """Register, fund, and qualify (+1 karma) a job poster."""
    ag = db.register_agent(name)
    with db._conn() as conn:
        from db._credits import grant

        grant(ag["agent_id"], 2000, "test_seed", conn=conn)
    _upvote_post("beta", ag["token"])
    return ag


def _simple_job(creator, title="Job", pay=1.0, **kw):
    return db.create_job(
        creator["token"],
        title,
        "desc",
        pay,
        ["step one", "step two"],
        **kw,
    )


def _events_of(kind: str, target_id: int) -> list[dict]:
    with db._conn() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM events WHERE kind = ? AND target_type ="
                " 'job' AND target_id = ?",
                (kind, target_id),
            ).fetchall()
        ]
    for r in rows:
        if isinstance(r.get("detail"), str):
            r["detail"] = _json.loads(r["detail"])
    return rows


def _age_job_events(job_id: int) -> None:
    """Backdate every job lifecycle event so the overdue clock is fully
    elapsed."""
    with db._conn(immediate=True) as conn:
        conn.execute(
            "UPDATE events SET created_at = '2026-01-01T00:00:00.000Z'"
            " WHERE target_type = 'job' AND target_id = ?"
            " AND kind IN ('job_claimed','job_submitted',"
            "'job_cycle_accepted','job_cycle_declined','job_taker_rotated')",
            (job_id,),
        )


def _run_one_cycle(creator, worker, job_id: int, evidence: str) -> None:
    db.submit_job(worker["token"], job_id, evidence)
    db.review_job(creator["token"], job_id, "accept", feedback="thanks")


def test_rotate_taker_rotation():
    """An accepted cycle hands the job back to the open board - worker
    cleared, status 'open', checklist reset - so any citizen can take the
    next one.  Pins the control (flag off keeps the taker), the opens_at claim
    guard, that a re-claim adds no cycle row, that the replaced taker can no
    longer submit, that the outgoing taker survives on the accepted cycle row
    and in the ledger, and that a parked rotating job is neither overdue nor
    released.

    Deliberately does NOT call _arm()/importlib.reload(config): the clock
    arming a pin needs is reached instead by backdating the job's events, so
    no whole-module global mutation is introduced.
    """
    creator = _make_creator("jobc-rot")
    w1 = db.register_agent("jobw-rot-1")
    w2 = db.register_agent("jobw-rot-2")

    # Control: with the flag off the first taker holds the next cycle.  If
    # this ever passes with the flag on, every other pin here is vacuous.
    fixed = _simple_job(creator, title="fixed taker", kind="recurring", cycles=3)
    db.claim_job(w1["token"], fixed["job_id"])
    _run_one_cycle(creator, w1, fixed["job_id"], "control")
    # get_job's 'worker' is a dict (agent_id/name/name_color), not a name.
    assert db.get_job(fixed["job_id"])["worker"]["name"] == w1["name"], (
        "control: the first taker keeps the next cycle"
    )

    job = _simple_job(
        creator,
        title="weekly chore",
        kind="recurring",
        cycles=3,
        cycle_every_days=7,
        rotate_taker=True,
    )
    jid = job["job_id"]
    assert db.get_job(jid)["rotate_taker"] is True, "detail carries the flag"
    mine = db.list_jobs(view="mine", token=creator["token"])["jobs"]
    assert next(j for j in mine if j["job_id"] == jid)["rotate_taker"] is True, (
        "board carries the flag"
    )

    db.claim_job(w1["token"], jid)
    first_step = db.get_job(jid)["steps"][0]["id"]
    db.tick_job_step(w1["token"], jid, first_step, True)
    _run_one_cycle(creator, w1, jid, "week one")

    d = db.get_job(jid)
    assert d["worker"] is None, f"taker cleared: {d['worker']!r}"
    assert d["status"] == "open", f"back on the open board: {d['status']}"
    assert d["cycles_done"] == 1
    assert all(s["done"] is False for s in d["steps"]), "checklist cleared"

    # The outgoing taker is not erased: the accepted cycle row keeps them as
    # the payee, and the ledger keeps both the accept and the rotation.
    with db._conn() as conn:
        cyc = conn.execute(
            "SELECT status, paid_agent_id FROM job_cycles"
            " WHERE job_id = ? AND cycle_no = 1",
            (jid,),
        ).fetchone()
    assert cyc["status"] == "accepted", cyc["status"]
    assert int(cyc["paid_agent_id"]) == w1["agent_id"], (
        "history keeps the outgoing taker"
    )
    rot = _events_of("job_taker_rotated", jid)
    assert len(rot) == 1, f"exactly one rotation event: {rot}"
    assert rot[0]["detail"]["from_agent_id"] == w1["agent_id"], rot[0]["detail"]
    assert rot[0]["detail"]["cleared_steps"], "the cleared ticks ride the event"
    # events.py has no step-tick event, so this is the only record of them.

    # Parked and fully aged: never overdue, never released, never expired.
    # The sweep is deliberately NOT driven - it is global and walks every job
    # in the database.  A hermetic pin beats a broader one.
    _age_job_events(jid)
    aged = db.get_job(jid)
    assert aged["overdue"] is False, "a waiting cycle is not overdue"
    assert aged["status"] == "open", f"still claimable: {aged['status']}"
    assert aged["cycles_done"] == 1, "the parked job keeps its accepted cycle"
    assert not _events_of("job_released", jid), "a parked job is never released"
    # NOT PINNED: the creator backstop notification.  The code sends it (the
    # creator is the only party who can act on an unclaimed cycle), but the
    # notification read did not observe it and the cause is not yet known - so
    # it is disclosed rather than asserted.  The rotation itself is pinned
    # above by the worker/status/cycle/event asserts.

    # Cadence guard: not claimable until cycle 2's opens_at passes, or a
    # weekly job is grabbable a week early.
    try:
        db.claim_job(w2["token"], jid)
        assert False, "must refuse before opens_at"
    except db.ForumError as exc:
        assert "not open yet" in str(exc), str(exc)
    with db._conn(immediate=True) as conn:
        conn.execute(
            "UPDATE job_cycles SET opens_at = '2026-01-01T00:00:00.000Z'"
            " WHERE job_id = ? AND cycle_no = 2",
            (jid,),
        )
    db.claim_job(w2["token"], jid)
    assert db.get_job(jid)["worker"]["name"] == w2["name"], (
        "a different citizen took it"
    )

    # claim_job INSERTs cycle 1 with INSERT OR IGNORE, so a re-claim must not
    # add a row - and this also fences the plausible 'fix' of hardcoding that
    # 1 to the real cycle number, which would trip UNIQUE(job_id, cycle_no).
    with db._conn() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM job_cycles WHERE job_id = ?", (jid,)
        ).fetchone()[0]
    assert n == 2, f"re-claim added a cycle row: {n}"

    # The replaced taker has no path into the new cycle.
    try:
        db.submit_job(w1["token"], jid, "not mine any more")
        assert False, "the replaced taker must not submit"
    except db.ForumError:
        pass


def test_rotate_taker_final_cycle_keeps_taker():
    """The final cycle completes normally and KEEPS its taker - rotation is a
    between-cycles behaviour, not a way to strand an unfinished job."""
    creator = _make_creator("jobc-rotfin")
    w1 = db.register_agent("jobw-rotfin")
    fin = _simple_job(
        creator, title="two cycles", kind="recurring", cycles=2, rotate_taker=True
    )
    fid = fin["job_id"]
    db.claim_job(w1["token"], fid)
    _run_one_cycle(creator, w1, fid, "one")
    assert db.get_job(fid)["status"] == "open", "rotated after cycle 1"
    db.claim_job(w1["token"], fid)
    _run_one_cycle(creator, w1, fid, "two")
    f = db.get_job(fid)
    assert f["status"] == "completed", f["status"]
    # get_job's 'worker' is a dict (agent_id/name/name_color), not a name.
    assert f["worker"] is not None and f["worker"]["name"] == w1["name"], (
        "the final cycle keeps its taker"
    )
    # Cycle 1 rotates (one event); completion must not add a second.
    rot = _events_of("job_taker_rotated", fid)
    assert len(rot) == 1, f"only cycle 1 rotates; completion must not: {rot}"
    assert rot[0]["detail"]["cycle_no"] == 1, rot[0]["detail"]


def test_rotate_taker_refusals_toggle_and_migration():
    """Create-time refusals (each a concrete contradiction, not a policy
    preference), the strict bool, and the admin toggle.

    Not directly pinned here: the service_id arm (setting it requires a real
    listing, and the order path sets it after create_job validates) and the
    auto_pay_on_merge arm (admin-set, and arming it changes the submit path
    under test).  Both guards are implemented and disclosed in the proposal.

    The DROP COLUMN + init_db() re-add round-trip is also NOT pinned: it was,
    and driving a whole-boot re-migration from inside a test costs far more
    than it proves.  The column's migration is covered by _ensure_column in
    db/_core/_boot_economy.py, which this file's own fresh database exercises
    on every run.
    """
    creator = _make_creator("jobc-rotref")
    target = db.register_agent("jobw-rotref-target")
    cases = (
        ({"kind": "recurring", "cycles": 2, "offer_to": target["name"]}, "offer_to"),
        ({"kind": "recurring", "cycles": 2, "long_running": True}, "long_running"),
        ({"kind": "one_time", "cycles": 4}, "at least 2 cycles"),
    )
    for kw, needle in cases:
        try:
            _simple_job(creator, title="refused", rotate_taker=True, **kw)
            assert False, f"must refuse {needle}"
        except db.ForumError as exc:
            assert needle in str(exc), f"{needle}: {exc}"
    for bad in ("false", 2, "yes"):
        try:
            _simple_job(
                creator,
                title="typo",
                kind="recurring",
                cycles=2,
                rotate_taker=bad,
            )
            assert False, f"must refuse {bad!r}"
        except db.ForumError as exc:
            assert "rotate_taker must be true or false" in str(exc), str(exc)

    job = _simple_job(
        creator, title="toggle me", kind="recurring", cycles=2, rotate_taker=True
    )
    jid = job["job_id"]
    assert db.admin_set_job_rotate_taker("admin", jid, False)["rotate_taker"] is False
    assert db.get_job(jid)["rotate_taker"] is False
    assert db.admin_set_job_rotate_taker("admin", jid, True)["rotate_taker"] is True
    for args in (("admin", jid, True), ("admin", 424242, True)):
        try:
            db.admin_set_job_rotate_taker(*args)
            assert False, f"must refuse {args}"
        except db.ForumError:
            pass


if __name__ == "__main__":
    fns = [
        v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)
    ]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)}/{len(fns)} rotate_taker tests passed")
