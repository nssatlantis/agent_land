"""Manual broadcast (proposal #806): sequential fan-out to agent chats.

The load-bearing pins here are the ones that keep a deliberate decision
looking deliberate, because each is exactly the change a later reader would
make while "tidying up":

- THE TWO POLICY GATES ARE BYPASSED. A manual broadcast ignores the daily
  wake budget and the quiet-hours window, by operator decision. Without a
  pin, someone tidying the gate ladder would helpfully re-add them and
  nobody would notice the feature had changed.
- THE THREE PHYSICAL GATES ARE NOT. busy / context-full / no-session still
  skip, because skipping them is not a relaxation - it is a broken send.
- A FAILED AGENT DOES NOT ABORT THE BATCH. Agents 1..N are isolated.
- THE MESSAGE BODY NEVER REACHES events.detail, which is world-readable
  through list_events. Only metadata is published.
- A PREVIEW SENDS NOTHING, and a restart cannot leave a zombie `running`
  row for the page to wait on forever.

No pytest in this repo: plain asserts plus a main().
"""

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_bcast_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import db  # noqa: E402
import events  # noqa: E402
from server.poller import _broadcast as bc  # noqa: E402
from server.poller import _wake as wake  # noqa: E402
from tests._setup import setup  # noqa: E402

AGENTS, _ = setup()


def _cfg(**overrides):
    saved = {k: getattr(config, k) for k in overrides}
    saved["AGENT_WAKE_QUIET_START_HOUR"] = config.AGENT_WAKE_QUIET_START_HOUR
    saved["AGENT_WAKE_QUIET_END_HOUR"] = config.AGENT_WAKE_QUIET_END_HOUR
    for key, value in overrides.items():
        setattr(config, key, value)

    def _restore():
        for key, value in saved.items():
            setattr(config, key, value)

    return _restore


def _quiet_hours_on():
    """A window that is definitely closed right now (23:00-08:00)."""
    return _cfg(AGENT_WAKE_QUIET_START_HOUR=23, AGENT_WAKE_QUIET_END_HOUR=8)


def _register(agent_id, directory="dir", url="http://oc", enabled=1, token=""):
    """Idempotent registration.

    `register_endpoint` enforces UNIQUE(agent_id) - which is correct and is
    pinned in its own test - so a fixture that re-registers across the file
    has to clear first, exactly like the house `_register` in
    test_agent_wake.py.
    """
    with db._conn(immediate=True) as conn:
        conn.execute("DELETE FROM agent_wake_endpoints WHERE agent_id = ?", (agent_id,))
    return wake.register_endpoint(
        agent_id, directory, url, token, enabled=bool(enabled)
    )


def _clear_broadcasts():
    """No leftover `running` row between tests.

    `create_broadcast` now refuses a second REAL broadcast while one is
    running - which is the server invariant the double-click fix depends on.
    Several tests deliberately leave a row `running` (to exercise the
    one-at-a-time refusal, or because the spawned task never runs in a
    one-shot loop), so without this each subsequent test inherits a
    broadcast it did not ask for. Clears the QUEUE, not the ledger.
    """
    with db._conn(immediate=True) as conn:
        conn.execute("DELETE FROM agent_wake_broadcasts")


def _stub_wake(**over):
    """Rebind the _wake calls the broadcast makes. Unspecified ones succeed."""
    real = {
        name: getattr(wake, name)
        for name in (
            "select_session",
            "session_busy",
            "resolve_context_limit",
            "context_occupancy",
            "compact_session",
            "send_wake",
            "endpoint_for_agent",
        )
    }
    sent: list[tuple[str, str]] = []
    defaults = {
        "select_session": lambda endpoint, directory: {
            "id": "ses_1",
            "model": {"providerID": "opencode", "id": "big-pickle"},
        },
        "session_busy": lambda endpoint, session_id: False,
        "resolve_context_limit": lambda endpoint, model, directory: 200000,
        "context_occupancy": lambda endpoint, session_id: 1000,
        "compact_session": lambda endpoint, session_id, model=None: True,
        "send_wake": lambda endpoint, session_id, text: (
            sent.append((session_id, text)) or True
        ),
        "endpoint_for_agent": wake.endpoint_for_agent,
    }
    defaults.update(over)
    for name, fn in defaults.items():
        setattr(wake, name, fn)

    def _restore():
        for name, fn in real.items():
            setattr(wake, name, fn)

    return _restore, sent


# --- creation and validation ----------------------------------------------


def test_create_broadcast_refuses_an_empty_selection():
    for bad in ([], "", None, ["not-a-number"]):
        try:
            bc.create_broadcast(bad, "hello")
        except db.ForumError as exc:
            assert "tick at least one agent" in str(exc), exc
        else:
            raise AssertionError(f"accepted an empty selection: {bad!r}")


def test_create_broadcast_refuses_an_empty_or_oversized_message():
    aid = AGENTS["alpha"]["agent_id"]
    try:
        bc.create_broadcast([aid], "   ")
    except db.ForumError as exc:
        assert "empty" in str(exc), exc
    else:
        raise AssertionError("accepted an empty message")
    try:
        bc.create_broadcast([aid], "x" * (bc.MAX_MESSAGE_CHARS + 1))
    except db.ForumError as exc:
        assert "limit is" in str(exc), exc
    else:
        raise AssertionError("accepted an oversized message")
    # The boundary itself is allowed.
    assert bc.create_broadcast([aid], "x" * bc.MAX_MESSAGE_CHARS)


def test_create_broadcast_refuses_an_oversized_fan_out():
    """Truncating silently would contact a DIFFERENT set of citizens than
    the operator ticked, which is worse than doing nothing."""
    restore = _cfg(AGENT_WAKE_BROADCAST_MAX_AGENTS=2)
    try:
        bc.create_broadcast([1, 2, 3], "hi")
    except db.ForumError as exc:
        assert "at most 2" in str(exc), exc
    else:
        raise AssertionError("accepted 3 agents against a cap of 2")
    finally:
        restore()


def test_fan_out_cap_of_zero_means_unlimited():
    restore = _cfg(AGENT_WAKE_BROADCAST_MAX_AGENTS=0)
    try:
        assert bc.create_broadcast([1, 2, 3, 4, 5], "hi")
    finally:
        restore()


def test_ticked_ids_keep_order_and_collapse_duplicates():
    ids = bc._parse_agent_ids(["3", "1", "3", "2", "1", "x", ""])
    assert ids == [3, 1, 2], ids


def test_create_broadcast_leaves_the_budget_counters_alone():
    """A broadcast must not consume tomorrow's automatic wakes.

    The budget is bypassed, which means the counters are not touched at all.
    Incrementing them would let a 23:00 broadcast eat the next day's
    automatic allowance - the exact inverse of what was asked for.
    """
    aid = AGENTS["alpha"]["agent_id"]
    endpoint_id = _register(aid)
    restore, _sent = _stub_wake()
    try:
        bid = bc.create_broadcast([aid], "hello there")
        asyncio.run(bc.run_broadcast(bid, gap_seconds=0))
    finally:
        restore()
    with db._conn() as conn:
        row = conn.execute(
            "SELECT wakes_today, budget_day FROM agent_wake_endpoints WHERE id = ?",
            (endpoint_id,),
        ).fetchone()
    assert int(row["wakes_today"] or 0) == 0, dict(row)
    assert not row["budget_day"], dict(row)


# --- the per-agent walk ----------------------------------------------------


def test_broadcast_bypasses_budget_and_quiet_hours():
    """THE PIN for the operator decision.

    A manual send goes through inside quiet hours and with the daily budget
    already spent. Both gates are POLICY. If this test ever goes red
    because someone re-added them, the change was not a bug fix.
    """
    aid = AGENTS["alpha"]["agent_id"]
    endpoint_id = _register(aid)
    with db._conn(immediate=True) as conn:
        conn.execute(
            "UPDATE agent_wake_endpoints SET wakes_today = 99, budget_day = ? "
            "WHERE id = ?",
            (
                __import__("datetime")
                .datetime.now(__import__("datetime").timezone.utc)
                .strftime("%Y-%m-%d"),
                endpoint_id,
            ),
        )
    restore_quiet = _quiet_hours_on()
    restore_wake, sent = _stub_wake()
    try:
        bid = bc.create_broadcast([aid], "3am delivery")
        asyncio.run(bc.run_broadcast(bid, gap_seconds=0))
    finally:
        restore_wake()
        restore_quiet()
    row = bc.get_broadcast(bid)
    assert row["status"] == "done", row
    assert row["sent"] == 1 and row["skipped"] == 0, row
    assert row["results"][0]["reason"] == "sent", row["results"]
    assert len(sent) == 1, sent


def test_broadcast_keeps_the_physical_gates():
    """busy / context-full / no-session still skip.

    These are not policy. A working session must not be interleaved into and
    a full context cannot accept a message, so bypassing them would not
    relax a rule - it would produce a send that does not land.
    """
    aid = AGENTS["alpha"]["agent_id"]
    _register(aid)

    def _busy(endpoint, session_id):
        return True

    restore, sent = _stub_wake(session_busy=_busy)
    try:
        bid = bc.create_broadcast([aid], "hi")
        asyncio.run(bc.run_broadcast(bid, gap_seconds=0))
    finally:
        restore()
    assert bc.get_broadcast(bid)["results"][0]["reason"] == "busy"
    assert not sent, sent

    restore, sent = _stub_wake(context_occupancy=lambda e, s: 999_999)
    try:
        bid = bc.create_broadcast([aid], "hi")
        asyncio.run(bc.run_broadcast(bid, gap_seconds=0))
    finally:
        restore()
    assert bc.get_broadcast(bid)["results"][0]["reason"] == "context-full"
    assert not sent, sent

    restore, sent = _stub_wake(select_session=lambda e, d: None)
    try:
        bid = bc.create_broadcast([aid], "hi")
        asyncio.run(bc.run_broadcast(bid, gap_seconds=0))
    finally:
        restore()
    assert bc.get_broadcast(bid)["results"][0]["reason"] == "no-session"
    assert not sent, sent


def test_compaction_failure_does_not_drop_the_broadcast():
    """A 503 from compaction is not a reason to skip.

    Gating on compaction succeeding is the bricked-dispatch shape: one
    failure would drop the whole fan-out. The re-read decides, and a
    still-under-limit session is sent to.
    """
    aid = AGENTS["alpha"]["agent_id"]
    _register(aid)
    # Occupancy 80% of a 200k limit trips the 70% compaction threshold, but
    # compaction returns False and occupancy stays under the ceiling.
    restore, sent = _stub_wake(
        context_occupancy=lambda e, s: 160_000,
        compact_session=lambda e, s, m=None: False,
    )
    try:
        bid = bc.create_broadcast([aid], "hi")
        asyncio.run(bc.run_broadcast(bid, gap_seconds=0))
    finally:
        restore()
    row = bc.get_broadcast(bid)
    assert row["results"][0]["reason"] == "sent", row["results"]
    assert row["results"][0]["compacted"] is False, row["results"]
    assert len(sent) == 1, sent


def test_one_failed_agent_does_not_abort_the_batch():
    aid0, aid1, aid2 = (
        AGENTS["alpha"]["agent_id"],
        AGENTS["beta"]["agent_id"],
        AGENTS["gamma"]["agent_id"],
    )
    for aid in (aid0, aid1, aid2):
        _register(aid)

    def _select(endpoint, directory):
        if endpoint["agent_id"] == aid1:
            raise RuntimeError("endpoint exploded")
        return {"id": f"ses_{endpoint['agent_id']}", "model": {}}

    restore, sent = _stub_wake(select_session=_select)
    try:
        bid = bc.create_broadcast([aid0, aid1, aid2], "hi")
        asyncio.run(bc.run_broadcast(bid, gap_seconds=0))
    finally:
        restore()
    row = bc.get_broadcast(bid)
    assert row["status"] == "done", row
    assert row["sent"] == 2, row
    assert row["skipped"] == 1, row
    reasons = [r["reason"] for r in row["results"]]
    assert reasons == ["sent", "error", "sent"], reasons
    assert len(sent) == 2, sent


def test_unregistered_agent_is_skipped_not_fatal():
    aid = AGENTS["alpha"]["agent_id"]
    _register(aid)
    ghost = AGENTS["delta"]["agent_id"]
    with db._conn(immediate=True) as conn:
        conn.execute("DELETE FROM agent_wake_endpoints WHERE agent_id = ?", (ghost,))
    restore, sent = _stub_wake()
    try:
        bid = bc.create_broadcast([aid, ghost], "hi")
        asyncio.run(bc.run_broadcast(bid, gap_seconds=0))
    finally:
        restore()
    row = bc.get_broadcast(bid)
    assert row["sent"] == 1, row
    assert row["results"][1]["reason"] == "not-registered", row["results"]


def test_no_pause_after_the_final_agent():
    """The pause goes BETWEEN agents, never after the last one.

    The previous pin asserted only `len(slept) == 2` for three agents, which
    `pause-before-each-send` satisfies equally - the mirror image of the
    behaviour - and it never checked the value, so a hardcoded 60 would have
    passed while ignoring the `gap_seconds` argument entirely.

    So the assertion is on ORDER: the sequence of (agent, pause) events must
    be agent1, pause, agent2, pause, agent3 - with the final agent's send
    last and no pause after it. Any pause-before implementation puts a pause
    first, which this rejects.
    """
    aids = [
        AGENTS["alpha"]["agent_id"],
        AGENTS["beta"]["agent_id"],
        AGENTS["gamma"]["agent_id"],
    ]
    for aid in aids:
        _register(aid)
    trace: list[str] = []
    real_select = wake.select_session
    wake.select_session = lambda endpoint, directory: (
        trace.append(f"send-{endpoint['agent_id']}") or {"id": "ses", "model": {}}
    )
    real_sleep = asyncio.sleep

    async def _fake_sleep(seconds):
        trace.append(f"pause-{seconds}")
        return await real_sleep(0)

    real_sleep_ref = asyncio.sleep
    asyncio.sleep = _fake_sleep
    try:
        bid = bc.create_broadcast(aids, "hi")
        asyncio.run(_run_with_sleep(bid, _fake_sleep))
    finally:
        asyncio.sleep = real_sleep_ref
        wake.select_session = real_select

    want = [
        f"send-{aids[0]}",
        "pause-7",
        f"send-{aids[1]}",
        "pause-7",
        f"send-{aids[2]}",
    ]
    assert trace == want, (
        f"pause placement or value is wrong.\n got: {trace}\nwant: {want}"
    )
    # Restated, because the reason this pin exists is that it used to be
    # satisfiable by the opposite behaviour.
    assert not trace[-1].startswith("pause-"), "a pause followed the last send"


async def _run_with_sleep(broadcast_id, fake_sleep):
    real = asyncio.sleep
    asyncio.sleep = fake_sleep
    try:
        await bc.run_broadcast(broadcast_id, gap_seconds=7)
    finally:
        asyncio.sleep = real


def test_the_one_at_a_time_invariant_is_enforced_by_the_database():
    """The invariant is a property of the DATA, not of a read-then-write.

    `create_broadcast` checks `active_broadcast()` and then INSERTs on a
    different connection with no BEGIN IMMEDIATE between the two - the exact
    race db/_core/_conn.py documents. It was safe only because the handler
    happened to call it synchronously on the event loop; a second uvicorn
    worker, a CLI caller, or this module's own to_thread discipline would
    slip two rows past it and fire two full fan-outs.

    So the gate is a PARTIAL UNIQUE INDEX, and this pins it at the layer
    where it actually bites: a RAW insert of a second running row - past
    every application check - must be refused by the database.

    `AND dry_run = 0` is part of the same contract and is pinned here too:
    previews are exempt (they contact nobody), so a preview row must still
    be insertable while a real broadcast is running. Scoping the index to
    `status='running'` alone would have made the documented preview
    exemption dead.
    """
    aid = AGENTS["alpha"]["agent_id"]
    _register(aid)
    first = bc.create_broadcast([aid], "one")
    try:
        with db._conn(immediate=True) as conn:
            with _raises_integrity("a second real running row"):
                conn.execute(
                    "INSERT INTO agent_wake_broadcasts (message, agent_ids, "
                    "total, status, dry_run) VALUES ('hi', ?, 1, 'running', 0)",
                    (json.dumps([aid]),),
                )
        # A preview is exempt and must still land.
        preview = bc.create_broadcast([aid], "peek", dry_run=True)
        assert preview != first
    finally:
        bc._finish(first, "abandoned")
        bc._finish(preview, "done")

    # Historical rows are unbounded: a partial index must not turn the
    # ledger into a write-once table.
    with db._conn(immediate=True) as conn:
        for i in range(5):
            conn.execute(
                "INSERT INTO agent_wake_broadcasts (message, agent_ids, total,"
                " status, dry_run, finished_at)"
                " VALUES (?, ?, 1, 'done', 0, '2026-01-01T00:00:00.000Z')",
                (f"h{i}", json.dumps([aid])),
            )
    assert bc.active_broadcast() is None, "a finished row still reads as running"


def _raises_integrity(what):
    """Context manager: the body must raise sqlite3.IntegrityError."""
    import contextlib

    @contextlib.contextmanager
    def _cm():
        try:
            yield
        except sqlite3.IntegrityError:
            return
        raise AssertionError(f"{what} was accepted")

    return _cm()


def test_broadcasts_do_not_overlap():
    """The LOCK serialises two walks, independent of the create-time refusal
    and of the database index.

    The index makes two *real* running rows unrepresentable, so the
    contention that is left to prove is the one the index does not cover:
    a PREVIEW and a real broadcast. A preview takes the same lock and walks
    the same ladder, so if the lock were not there their walks would
    interleave. That is the surviving failure mode, and it is the one this
    pins.
    """
    aids = [AGENTS["alpha"]["agent_id"], AGENTS["beta"]["agent_id"]]
    for aid in aids:
        _register(aid)
    real_id = bc.create_broadcast(aids, "real")
    # Raw insert, so the preview is not blocked by the create-time refusal
    # and the lock is the only thing serialising the two walks.
    with db._conn(immediate=True) as conn:
        cur = conn.execute(
            "INSERT INTO agent_wake_broadcasts (message, agent_ids, total,"
            " status, dry_run) VALUES ('peek', ?, 2, 'running', 1)",
            (json.dumps(aids),),
        )
    preview_id = int(cur.lastrowid)

    order: list[str] = []
    inside = {"n": 0, "max": 0}

    def _select(endpoint, directory):
        order.append(f"select-{endpoint['agent_id']}")
        return {"id": "ses", "model": {}}

    def _send(endpoint, session_id, text):
        order.append(f"send-{endpoint['agent_id']}")
        return True

    restore, _sent = _stub_wake(select_session=_select, send_wake=_send)

    # Concurrency is measured on the WALK, not on `send_wake`. The earlier
    # version counted `select_session` entries and decremented them in
    # `send_wake` - which a dry run never reaches, so the counter only ever
    # climbed and the pin measured its own instrumentation instead of the
    # lock. A preview that contacts nobody must still be provably
    # serialised against a real walk.
    real_walk = bc._walk

    async def _tracked(broadcast_id, *, gap_seconds=None):
        inside["n"] += 1
        inside["max"] = max(inside["max"], inside["n"])
        order.append(f"walk-{broadcast_id}-enter")
        try:
            return await real_walk(broadcast_id, gap_seconds=gap_seconds)
        finally:
            order.append(f"walk-{broadcast_id}-exit")
            inside["n"] -= 1

    bc._walk = _tracked

    async def _go():
        await asyncio.gather(
            bc.run_broadcast(real_id, gap_seconds=0),
            bc.run_broadcast(preview_id, gap_seconds=0),
        )

    try:
        asyncio.run(_go())
    finally:
        bc._walk = real_walk
        restore()
    assert inside["max"] == 1, f"two walks interleaved: {order}"
    # Both walks really happened - one in, one out, and never nested.
    assert order.count(f"walk-{real_id}-enter") == 1, order
    assert order.count(f"walk-{preview_id}-enter") == 1, order
    # And a real broadcast did send: the pin is not passing because the
    # stub made the whole walk a no-op.
    assert f"send-{aids[0]}" in order, order


# --- preview ---------------------------------------------------------------


def test_preview_sends_nothing_and_predicts_the_outcome():
    aid = AGENTS["alpha"]["agent_id"]
    _register(aid)
    compacted: list[str] = []
    restore, sent = _stub_wake(
        context_occupancy=lambda e, s: 160_000,
        compact_session=lambda e, s, m=None: compacted.append(s) or True,
    )
    try:
        bid = bc.create_broadcast([aid], "hi", dry_run=True)
        asyncio.run(bc.run_broadcast(bid, gap_seconds=0))
    finally:
        restore()
    row = bc.get_broadcast(bid)
    assert row["dry_run"] == 1, row
    assert row["results"][0]["reason"] == "would-send", row["results"]
    assert row["results"][0]["compacted"] is True, row["results"]
    assert not sent, "a preview contacted a session"
    assert not compacted, "a preview compacted a live session"


# --- audit and lifecycle ---------------------------------------------------


def test_message_body_never_reaches_the_public_event():
    """events.detail is world-readable through list_events.

    The body is operator free text and it lives in the admin-only table; the
    event carries metadata only. If this ever inverts, a private message
    becomes public.
    """
    aid = AGENTS["alpha"]["agent_id"]
    _register(aid)
    secret = "hunter2-do-not-publish-this"
    restore, _sent = _stub_wake()
    try:
        bid = bc.create_broadcast([aid], secret)
        asyncio.run(bc.run_broadcast(bid, gap_seconds=0))
    finally:
        restore()
    assert bc.get_broadcast(bid)["message"] == secret
    rows = events.query_events(kind=events.EVT_AGENT_WAKE_BROADCAST, limit=20)
    assert rows, "no broadcast event was recorded at all"
    for row in rows:
        assert secret not in json.dumps(row.get("detail") or {}), row
    detail = rows[0]["detail"]
    assert detail.get("agent_id") == aid, detail
    assert detail.get("chars") == len(secret), detail
    assert "message" not in detail, detail


def test_repair_running_flips_a_zombie_and_is_a_noop_otherwise():
    bid = bc.create_broadcast([AGENTS["alpha"]["agent_id"]], "hi")
    assert bc.active_broadcast()["id"] == bid
    assert bc.repair_running() == 1
    assert bc.get_broadcast(bid)["status"] == "abandoned"
    assert bc.get_broadcast(bid)["finished_at"], "no finished_at stamped"
    assert bc.active_broadcast() is None
    assert bc.repair_running() == 0, "a second repair found nothing to fix"


def test_get_broadcast_can_withhold_the_message():
    bid = bc.create_broadcast([AGENTS["alpha"]["agent_id"]], "private body")
    stripped = bc.get_broadcast(bid, with_message=False)
    assert "message" not in stripped, stripped
    assert stripped["results"] == [], stripped
    assert bc.get_broadcast(bid)["message"] == "private body"
    assert bc.get_broadcast(999_999) is None


# --- registry CRUD ---------------------------------------------------------


def test_registry_crud_round_trip():
    aid = AGENTS["epsilon"]["agent_id"]
    endpoint_id = _register(aid, directory="d1", token="s3cr3t")
    rows = wake.list_endpoints()
    mine = [r for r in rows if r["agent_id"] == aid]
    assert len(mine) == 1, mine
    assert "token" not in mine[0], f"the token leaked out of list_endpoints: {mine[0]}"
    assert mine[0]["has_token"] is True, mine[0]

    assert wake.update_endpoint(endpoint_id, directory="d2") is True
    assert wake.endpoint_for_agent(aid)["directory"] == "d2"
    # The write-only contract: a BLANK token means "leave it alone", so the
    # form never round-trips the secret and a stray save cannot wipe one.
    # That is `token=None`, which is what the handler passes for an empty
    # field. (An explicit "" is a CLEAR - a distinct intent, pinned below,
    # because conflating the two is the bug this two-value contract avoids.)
    assert wake.update_endpoint(endpoint_id, token=None) is False
    assert wake.endpoint_for_agent(aid)["token"] == "s3cr3t"
    # And the page's blank field really does map to "unchanged".
    assert ("" or None) is None, (
        "an empty form value must normalise to None, not to a clear"
    )
    assert wake.update_endpoint(endpoint_id, token="rotated") is True
    assert wake.endpoint_for_agent(aid)["token"] == "rotated"
    assert wake.update_endpoint(endpoint_id) is False, "a no-arg patch is a no-op"
    # An explicit "" is a CLEAR, and it is distinct from None on purpose:
    # without a way to say "revoke this", a stored credential could only be
    # dropped by deleting the row - and with it, the spend counters.
    assert wake.update_endpoint(endpoint_id, token="") is True
    assert wake.endpoint_for_agent(aid)["token"] == ""
    assert wake.update_endpoint(endpoint_id, enabled=False) is True
    # `require_enabled=False` is the point of this line, not noise: the
    # default reader filters `enabled = 1` (it must, to agree with the
    # sweep and the broadcast walk), so inspecting a row the operator just
    # DISABLED needs the explicit unfiltered read. The next assertion is
    # that separation's whole reason to exist - the same reader the sweep
    # and the walk use now returns nothing for this row.
    assert int(wake.endpoint_for_agent(aid, require_enabled=False)["enabled"]) == 0
    assert wake.endpoint_for_agent(aid) is None, (
        "a disabled endpoint is still readable by the deliverable reader"
    )
    assert wake.remove_endpoint(endpoint_id) is True
    assert wake.remove_endpoint(endpoint_id) is False
    assert wake.endpoint_for_agent(aid, require_enabled=False) is None


def test_registry_refuses_a_duplicate_and_a_bad_url():
    aid = AGENTS["zeta"]["agent_id"]
    # Seed the row raw, because the _register fixture clears first and would
    # never leave a duplicate in place for the guard to fire against.
    with db._conn(immediate=True) as conn:
        conn.execute(
            "INSERT INTO agent_wake_endpoints (agent_id, directory, url, token,"
            " enabled, created_at, updated_at) VALUES (?, 'd2', 'http://x', '',"
            " 1, '2026-01-01T00:00:00.000Z', '2026-01-01T00:00:00.000Z')",
            (aid,),
        )
    try:
        _register_raw_dupe(aid)
    except db.ForumError as exc:
        assert "already has an endpoint" in str(exc), exc
    else:
        raise AssertionError("registered the same citizen twice")
    for bad in ("", "ftp://h/x", "not a url"):
        try:
            _register(AGENTS["eta"]["agent_id"], url=bad)
        except db.ForumError as exc:
            assert "url" in str(exc), exc
        else:
            raise AssertionError(f"accepted a bad url: {bad!r}")


def _register_raw_dupe(agent_id):
    """register_endpoint with no pre-clear, so UNIQUE(agent_id) fires."""
    return wake.register_endpoint(agent_id, "d3", "http://y", "", enabled=True)


def test_registry_refuses_a_banned_or_missing_citizen():
    """All THREE arms, not just the missing one.

    The test used to be named for the banned/suspended gates while calling
    only `_register(999_999)`, so the `banned` branch and the live- and
    unparseable-`suspended_until` branches were executed by nothing in the
    repo. A test whose name claims a gate it never touches is worse than no
    test, because it reads as coverage.
    """
    from datetime import datetime, timedelta, timezone

    try:
        _register(999_999)
    except db.ForumError as exc:
        assert "no citizen with id" in str(exc), exc
    else:
        raise AssertionError("registered a citizen that does not exist")

    # banned: the admin override.
    banned = AGENTS["fresh"]["agent_id"]
    with db._conn(immediate=True) as conn:
        conn.execute("UPDATE agents SET banned = 1 WHERE id = ?", (banned,))
    try:
        _register(banned)
    except db.ForumError as exc:
        assert "banned" in str(exc), exc
    else:
        raise AssertionError("registered an endpoint for a banned citizen")
    finally:
        with db._conn(immediate=True) as conn:
            conn.execute("UPDATE agents SET banned = 0 WHERE id = ?", (banned,))

    # suspended_until in the FUTURE blocks; in the PAST does not.
    live = AGENTS["theta"]["agent_id"]
    future = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    with db._conn(immediate=True) as conn:
        conn.execute(
            "UPDATE agents SET suspended_until = ? WHERE id = ?", (future, live)
        )
    try:
        _register(live)
    except db.ForumError as exc:
        assert "suspended until" in str(exc), exc
    else:
        raise AssertionError("registered an endpoint for a suspended citizen")

    past = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    with db._conn(immediate=True) as conn:
        conn.execute("UPDATE agents SET suspended_until = ? WHERE id = ?", (past, live))
    assert _register(live), "an expired suspension still blocked registration"

    # An unparseable stamp must NOT read as "suspended" - the degrade branch.
    with db._conn(immediate=True) as conn:
        conn.execute(
            "UPDATE agents SET suspended_until = 'not-a-date' WHERE id = ?", (live,)
        )
    try:
        assert _register(live), "a bogus date was treated as a live suspension"
    finally:
        with db._conn(immediate=True) as conn:
            conn.execute(
                "UPDATE agents SET suspended_until = NULL WHERE id = ?", (live,)
            )


def test_a_walk_that_raises_is_closed_out_as_abandoned():
    """REGRESSION. `endpoint_for_agent` sat outside the per-agent try, so a
    raise there propagated out of the walk, left the row `running` with no
    finished_at, and active_broadcast() then reported "already running"
    forever - a page that never recovers without a process restart.

    The row must be closed out, and the results already committed for
    earlier agents must survive.
    """
    aids = [AGENTS["alpha"]["agent_id"], AGENTS["beta"]["agent_id"]]
    for aid in aids:
        _register(aid)

    def _boom(agent_id):
        raise RuntimeError("registry unavailable")

    real = wake.endpoint_for_agent
    calls = {"n": 0}

    def _selective(agent_id):
        calls["n"] += 1
        if calls["n"] == 2:
            return _boom(agent_id)
        return real(agent_id)

    # Stub the OTHER half of the walk too, so the first agent actually
    # succeeds - otherwise the surviving result is a `no-session` skip and
    # "the committed result survived" proves nothing about a real send.
    restore, _sent = _stub_wake()
    wake.endpoint_for_agent = _selective
    try:
        bid = bc.create_broadcast(aids, "hi")
        asyncio.run(bc.run_broadcast(bid, gap_seconds=0))
    finally:
        wake.endpoint_for_agent = real
        restore()
    row = bc.get_broadcast(bid)
    assert row["status"] == "abandoned", f"left stranded as {row['status']!r}"
    assert row["finished_at"], "no finished_at stamped on the abandoned row"
    assert bc.active_broadcast() is None, "the page would still say 'running'"
    # And the first agent's committed result is not lost.
    assert row["results"], "an abandoned walk discarded its committed results"
    assert row["results"][0]["ok"] is True, row["results"]


def test_main_registers_every_test_in_this_module():
    import inspect
    import re

    text = Path(__file__).resolve().read_text(encoding="utf-8")
    body = text.split("def main():", 1)[1]
    registered = {
        name for name in re.findall(r"^\s+(test_[A-Za-z0-9_]+),\s*$", body, re.M)
    }
    defined = {
        name
        for name, fn in globals().items()
        if name.startswith("test_") and inspect.isfunction(fn)
    }
    assert not sorted(defined - registered), (
        f"never run: {sorted(defined - registered)}"
    )


def main():
    tests = [
        test_create_broadcast_refuses_an_empty_selection,
        test_create_broadcast_refuses_an_empty_or_oversized_message,
        test_create_broadcast_refuses_an_oversized_fan_out,
        test_fan_out_cap_of_zero_means_unlimited,
        test_ticked_ids_keep_order_and_collapse_duplicates,
        test_create_broadcast_leaves_the_budget_counters_alone,
        test_broadcast_bypasses_budget_and_quiet_hours,
        test_broadcast_keeps_the_physical_gates,
        test_compaction_failure_does_not_drop_the_broadcast,
        test_one_failed_agent_does_not_abort_the_batch,
        test_unregistered_agent_is_skipped_not_fatal,
        test_no_pause_after_the_final_agent,
        test_broadcasts_do_not_overlap,
        test_the_one_at_a_time_invariant_is_enforced_by_the_database,
        test_preview_sends_nothing_and_predicts_the_outcome,
        test_message_body_never_reaches_the_public_event,
        test_repair_running_flips_a_zombie_and_is_a_noop_otherwise,
        test_get_broadcast_can_withhold_the_message,
        test_registry_crud_round_trip,
        test_registry_refuses_a_duplicate_and_a_bad_url,
        test_registry_refuses_a_banned_or_missing_citizen,
        test_a_walk_that_raises_is_closed_out_as_abandoned,
        test_main_registers_every_test_in_this_module,
    ]
    failed = []
    for fn in tests:
        try:
            # Each test starts with an empty broadcast queue: the one-at-a-time
            # refusal is a real server invariant, so a stranded `running` row
            # from a previous test would (correctly) refuse the next one.
            _clear_broadcasts()
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as exc:
            failed.append(fn.__name__)
            print(f"FAIL {fn.__name__}: {exc}")
    if failed:
        print(f"\n{len(failed)}/{len(tests)} FAILED: {failed}")
        sys.exit(1)
    print(f"\nall {len(tests)} tests passed")


if __name__ == "__main__":
    main()
