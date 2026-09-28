"""The panel principal end-to-end on the grant MONEY path (proposal #803).

`test_admin_identity.py` pins that `_admin_agent` RETURNS `{"id": None,
name: "admin"}` for a panel login matching no citizen. That is the
resolver's shape. Nothing pinned that a CONSUMER survives it - and the
consumer that matters is the one the operator actually reported: accept a
grant from /admin/guilds/1.

`db/_guilds_lending.py` `admin_decide_guild_grant` is the seam:

    return decide_guild_grant("", int(request_id), approve, admin=True,
                              as_agent=agent)

The empty token is never consulted because resolution prefers a non-None
`as_agent`, so `_require_active_agent` is bypassed entirely and `agent["id"]`
is None at every downstream write: `guild_grant_requests.decided_by`, the
`EVT_GUILD_GRANT_DECIDED` actor, and the `_notify` actor on the founder
fan-out. A NOT NULL insert or a notification actor that cannot take NULL
would ship green on the resolver pins alone.

OWN FILE and OWN truncated session DB on purpose. These move real treasury
credits into a guild pool, mutate `agents` rows, and stand up a guild per
test; `tests/test_guilds_grants.py` already shares one session DB with ~28
siblings and spends a file-wide grant budget, so the heavy composition
belongs beside the resolver pins rather than inside that shared file.

The four pins, and what each one rules OUT:

1. approve with a panel principal - the money leg. A bare
   `decided_by IS NULL` would also pass on a tree where the decision never
   happened (NULL is the row's pre-existing state), so it is paired with
   `status`, `decided_at` and the money actually moving.
2. decline with a panel principal - a SEPARATE write statement carrying its
   own `decided_by = ?`, and the one arm where the actor is written and no
   money moves. That isolates actor handling from settlement.
3. a CITIZEN login through the identical composition - the control. Without
   it, "NULL handled correctly" and "the column is always NULL now" are the
   same passing test, and a control satisfiable only by the wrong behaviour
   is not a control.
4. a foreign guild id - the operator-error guard, which must keep firing for
   a panel principal or the guard would only ever protect the citizen path.
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_admin_grant_actor_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)
os.environ["FORUM_GUILD_FOUND_KARMA"] = "0"
os.environ["FORUM_MAX_GUILDS"] = "100"
os.environ["FORUM_JOB_CREATOR_MIN_KARMA"] = "0"
os.environ["FORUM_INVOICE_MIN_KARMA"] = "0"
# Two approvals at 2cr each. The default window would cover it, but pinning
# it here keeps the file's budget independent of whatever the default
# becomes - and unlike the approval knobs, this one is a plain env read, so
# no deployment drift value is baked into an assertion.
os.environ["FORUM_GUILD_GRANT_BUDGET"] = "10.0"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402, I001

db.init_db()

AGENTS, BASE_POST = setup()

_SEQ = [0]

# A panel login that is deliberately NOT a registered citizen - the shape
# that produced the operator's "unknown admin." report, and the literal
# `_admin_user` falls back to when no ADMIN_PASSWORD is configured.
PANEL_LOGIN = "definitely-not-an-admin"


def _new_agent(prefix: str) -> dict:
    _SEQ[0] += 1
    return db.register_agent(f"{prefix}-{_SEQ[0]}")


def _fund(agent_id: int, units: int) -> None:
    import db._credits as _cr

    with db._conn() as _c:
        ok = _cr.grant(
            agent_id,
            units,
            "admin_grant_actor_seed",
            target_type="test",
            target_id=1,
            conn=_c,
        )
    assert ok, "treasury could not fund the test seed"


def _treasury() -> int:
    import db._credits as _cr

    with db._conn() as conn:
        return _cr.treasury_balance(conn)


def _supply() -> int:
    with db._conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(delta_units), 0) FROM credit_entries"
            " WHERE account IN ('agent', 'treasury', 'escrow', 'guild')"
        ).fetchone()
    return int(row[0] or 0)


def _pool(guild_id: int) -> int:
    with db._conn() as conn:
        return db.guild_balance(conn, guild_id)


def _found(name: str | None = None) -> tuple[dict, dict]:
    ag = _new_agent("aga-founder")
    _fund(ag["agent_id"], 600)
    return ag, db.found_guild(ag["token"], name or f"Aga-{_SEQ[0]}")


def _mate(founder: dict, guild: dict) -> dict:
    mate = _new_agent("aga-mate")
    _fund(mate["agent_id"], 300)
    inv = db.invite_guild_member(founder["token"], guild["id"], mate["name"])
    db.respond_guild_invite(mate["token"], inv["invite_id"], True)
    db.guild_deposit(mate["token"], guild["id"], 10.0)
    return mate


def _old_idea(author: dict, tag: str, commenters: list[dict]) -> int:
    """An idea that clears the designation crucible on its own merits.

    Deliberately NOT via the founder-skip knob: the crucible is the real
    gate, and depending on a knob's live value would bake a deployment fact
    into a test.
    """
    idea = db.create_proposal(
        author["token"], f"Aga idea {tag}", "A guild-scale build.", idea=True
    )
    pid = idea["post_id"]
    with db._conn() as conn:
        conn.execute(
            "UPDATE posts SET created_at = ? WHERE id = ?",
            ("2026-09-01T00:00:00.000Z", pid),
        )
    for c in commenters:
        db.create_comment(c["token"], pid, f"looks good ({c['name']})")
    return pid


def _ready_request(tag: str, amount: float = 2.0) -> tuple[dict, dict, dict]:
    """A real, UNDECIDED grant request with full scaffolding around it.

    Stops one step short of deciding on purpose: the caller drives the
    decision so it can choose WHICH principal makes it. A helper that always
    decided as the founder could not tell the panel and citizen arms apart.
    """
    founder, guild = _found()
    mate = _mate(founder, guild)
    db.guild_deposit(founder["token"], guild["id"], 25.0)
    c1, c2 = _new_agent("aga-c1"), _new_agent("aga-c2")
    idea = _old_idea(mate, tag, [c1, c2])
    db.designate_guild_project(founder["token"], guild["id"], idea)
    # The grant gate requires the collaborative proposal to carry a work
    # breakdown: `request_guild_grant` refuses with "that proposal carries no
    # to-do list yet". A pin that drops this raises at module scope, so every
    # pin below it is a dark def - a red file, never a silent pass, but
    # coverage of nothing.
    db.create_todo_list(mate["token"], idea, "plan", [{"text": "build"}])
    prop = db.promote_idea(
        mate["token"], idea, f"Build {idea}", "Full body here.", collaborative=True
    )
    req = db.request_guild_grant(
        founder["token"], guild["id"], prop["post_id"], amount, "build funds"
    )
    return founder, guild, req


def _req_row(request_id: int) -> dict:
    with db._conn() as conn:
        row = conn.execute(
            "SELECT * FROM guild_grant_requests WHERE id = ?", (request_id,)
        ).fetchone()
    assert row is not None, f"grant request {request_id} vanished"
    return dict(row)


def _last_guild_note(agent_id: int) -> dict | None:
    with db._conn() as conn:
        row = conn.execute(
            "SELECT * FROM notifications WHERE agent_id = ? AND kind = 'guild'"
            " ORDER BY id DESC LIMIT 1",
            (agent_id,),
        ).fetchone()
    return dict(row) if row is not None else None


def test_admin_grant_approve_survives_a_null_actor() -> None:
    """THE pin, on the money leg. Fail-before: this raised
    ForumError("unknown admin.") on the pre-#803 tree, so no panel grant
    could be accepted at all."""
    founder, guild, req = _ready_request("approve")
    gid = guild["id"]
    treasury_before, pool_before, supply_before = _treasury(), _pool(gid), _supply()

    out = db.admin_decide_guild_grant(PANEL_LOGIN, req["request_id"], True, gid)
    assert out["status"] == "paid", out

    row = _req_row(req["request_id"])
    # The row WAS written ...
    assert row["status"] == "paid", row
    assert row["decided_at"], row
    # ... and it carries a NULL actor. Paired with the three lines above on
    # purpose: `decided_by IS NULL` alone is also true of a tree where the
    # UPDATE never ran, because NULL is the row's pre-existing state.
    assert row["decided_by"] is None, row

    # The money moved, and it moved as a transfer. Treasury down, guild pool
    # claim up, supply unchanged - so a minted payment cannot satisfy this
    # and a lost payment cannot either.
    treasury_after, pool_after, supply_after = _treasury(), _pool(gid), _supply()
    assert treasury_after < treasury_before, (treasury_before, treasury_after)
    assert pool_after > pool_before, (pool_before, pool_after)
    assert supply_after == supply_before, (supply_before, supply_after)

    # The founder is still told. `_notify` drops a row only when
    # `agent_id == actor_agent_id`; the actor is None here, so the founder
    # must NOT be self-dropped, and `_actor_name(None, None)` must return
    # None without a `WHERE id = None` lookup. This is the leg a reviewer
    # asked to be read rather than inferred from the moderation.py pattern.
    note = _last_guild_note(founder["agent_id"])
    assert note is not None, "the panel approval notified nobody"
    assert note["actor_agent_id"] is None, note
    assert note["actor_name"] is None, note
    assert f"grant request #{req['request_id']}" in note["body"], note


def test_admin_grant_decline_survives_a_null_actor() -> None:
    """Decline is a SEPARATE write carrying its own `decided_by = ?`, so the
    NULL has to survive it too - and it is the one arm where the actor is
    written and NO money moves, which isolates actor handling from
    settlement. A sweep, not a restatement of the approve arm."""
    founder, guild, req = _ready_request("decline")
    gid = guild["id"]
    treasury_before, pool_before = _treasury(), _pool(gid)

    out = db.admin_decide_guild_grant(PANEL_LOGIN, req["request_id"], False, gid)
    assert out["status"] == "declined", out

    row = _req_row(req["request_id"])
    assert row["status"] == "declined", row
    assert row["decided_at"], row
    assert row["decided_by"] is None, row
    assert _treasury() == treasury_before, (treasury_before, _treasury())
    assert _pool(gid) == pool_before, (pool_before, _pool(gid))

    note = _last_guild_note(founder["agent_id"])
    assert note is not None, "the panel decline notified nobody"
    assert note["actor_agent_id"] is None, note
    assert "declined by admin" in note["body"], note


def test_admin_grant_with_a_citizen_login_stamps_that_citizen() -> None:
    """THE CONTROL, and without it the two arms above prove nothing.

    "NULL actor handled correctly" and "the actor column is now always NULL"
    are the same passing test. A registered admin drives the identical
    composition and MUST stamp their real id, so the NULL above is a fact
    about the panel principal rather than about the column. Fresh fixtures
    carry 0 karma; the admin path needs none.
    """
    who = _new_agent("aga-paneladmin")
    founder, guild, req = _ready_request("control")

    out = db.admin_decide_guild_grant(who["name"], req["request_id"], True, guild["id"])
    assert out["status"] == "paid", out

    row = _req_row(req["request_id"])
    assert row["decided_at"], row
    assert row["decided_by"] == who["agent_id"], row

    note = _last_guild_note(founder["agent_id"])
    assert note is not None, "the citizen decision notified nobody"
    assert note["actor_agent_id"] == who["agent_id"], note
    assert note["actor_name"] == who["name"], note


def test_admin_grant_refuses_a_foreign_guild_id() -> None:
    """The operator-error guard on the same seam, so the new principal did
    not quietly turn the panel into an any-request payer. The panel lists one
    guild's requests; a POST naming another guild's request id is refused
    before the engine is reached - and it must still fire for a PANEL
    principal, or the guard would only ever protect the citizen path."""
    founder, guild, req = _ready_request("guard")
    _other, other_guild = _found()

    try:
        db.admin_decide_guild_grant(
            PANEL_LOGIN, req["request_id"], True, other_guild["id"]
        )
        raise AssertionError("a foreign guild id was accepted")
    except AssertionError:
        raise
    except Exception as exc:
        assert "is not this guild's" in str(exc), exc

    row = _req_row(req["request_id"])
    assert row["status"] == "requested", row
    assert row["decided_at"] is None, row
    assert row["decided_by"] is None, row


def main() -> None:
    for fn in (
        test_admin_grant_approve_survives_a_null_actor,
        test_admin_grant_decline_survives_a_null_actor,
        test_admin_grant_with_a_citizen_login_stamps_that_citizen,
        test_admin_grant_refuses_a_foreign_guild_id,
    ):
        fn()
        print(f"  ok  {fn.__name__}")
    print("All admin grant actor tests passed.")


if __name__ == "__main__":
    main()
