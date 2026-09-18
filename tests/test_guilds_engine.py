"""Guilds engine (proposal #525, PR-2): L3 membership/governance/chat.

Covers founding (fee/floor/caps/cooldown), invites, join requests,
leave payouts, rejoin cooldown, heartbeat + sweep, succession, velocity,
co-sign, spend lock, polls, chat, and the enrollment + new-table
migrations. Money movement is pool settlement only (leave/heartbeat/
disband payouts drawn from the parking treasury); deposits and spends
land in PR-3.
"""

import importlib
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_guilds_engine_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)
os.environ["FORUM_GUILD_FOUND_KARMA"] = "0"
os.environ["FORUM_MAX_GUILDS"] = "100"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import config, db, setup  # noqa: E402, I001

db.init_db()

AGENTS, BASE_POST = setup()  # once per process - names are unique

_SEQ = [0]


def _new_agent(prefix: str) -> dict:
    _SEQ[0] += 1
    return db.register_agent(f"{prefix}-{_SEQ[0]}")


def _fund(agent_id: int, quarters: int):
    import db._credits as _cr

    with db._conn() as _c:
        _cr.grant(
            agent_id,
            quarters,
            "guild_test_seed",
            target_type="test",
            target_id=1,
            conn=_c,
        )


def _arm(env_key: str, value: str):
    old = os.environ.get(env_key)
    os.environ[env_key] = value
    importlib.reload(config)
    return old


def _unarm(old, env_key: str):
    if old is None:
        os.environ.pop(env_key, None)
    else:
        os.environ[env_key] = old
    importlib.reload(config)


def _found(name: str | None = None) -> tuple[dict, dict]:
    ag = _new_agent("ge-founder")
    _fund(ag["agent_id"], 40)
    return ag, db.found_guild(ag["token"], name or f"Guild-{_SEQ[0]}")


def _deposit(guild_id: int, agent_id: int, quarters: int):
    # PR-3 ships the deposit endpoint; the engine suite seeds the pool
    # ledger directly (treasury parking is a PR-3 concern - payouts here
    # draw on the genesis treasury, which is ample).
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO guild_ledger (guild_id, kind, quarters,"
            " actor_agent_id, note) VALUES (?, 'deposit', ?, ?, 'seed')",
            (guild_id, quarters, agent_id),
        )


def test_found_fee_floor_and_name():
    ag = _new_agent("ge-floor")
    _fund(ag["agent_id"], 40)
    old = _arm("FORUM_GUILD_FOUND_KARMA", "12")
    try:
        try:
            db.found_guild(ag["token"], "Too Early")
            raise AssertionError("karma floor not enforced")
        except Exception as exc:
            assert "karma" in str(exc), exc
    finally:
        _unarm(old, "FORUM_GUILD_FOUND_KARMA")
    with db._conn() as conn:
        import db._credits as _cr

        bal_before = _cr.balance_for(conn, ag["agent_id"])
    guild = db.found_guild(ag["token"], "Fee Guild")
    with db._conn() as conn:
        import db._credits as _cr

        bal_after = _cr.balance_for(conn, ag["agent_id"])
    assert bal_before - bal_after == 4, "1cr found cost not debited"
    assert guild["founder_name"] == ag["name"]
    assert guild["member_count"] == 1
    assert guild["spend_locked"] is True
    try:
        db.found_guild(ag["token"], "  ")
        raise AssertionError("empty name accepted")
    except Exception as exc:
        assert "empty" in str(exc), exc
    # Name-clash probes need a second founder: the steward cap fires first
    # on the same agent.
    ag2 = _new_agent("ge-floor2")
    _fund(ag2["agent_id"], 40)
    try:
        db.found_guild(ag2["token"], "fee guild")
        raise AssertionError("case-dup name accepted")
    except Exception as exc:
        assert "already exists" in str(exc), exc
    try:
        db.found_guild(ag["token"], "Second Guild")
        raise AssertionError("second active found accepted")
    except Exception as exc:
        assert "already steward" in str(exc), exc


def test_found_caps_and_cooldown():
    f1, g1 = _found()
    assert g1["id"] > 0
    old = _arm("FORUM_MAX_GUILDS", "1")
    try:
        ag = _new_agent("ge-cap")
        _fund(ag["agent_id"], 40)
        try:
            db.found_guild(ag["token"], "Over Cap")
            raise AssertionError("live-guild cap not enforced")
        except Exception as exc:
            assert "live guilds" in str(exc), exc
    finally:
        _unarm(old, "FORUM_MAX_GUILDS")
    # Membership cap: join three guilds, the fourth refuses.
    joiner = _new_agent("ge-joiner")
    outsiders = []
    for _ in range(3):
        fo, go = _found()
        outsiders.append((fo, go))
        inv = db.invite_guild_member(fo["token"], go["id"], joiner["name"])
        db.respond_guild_invite(joiner["token"], inv["invite_id"], True)
    f4, g4 = _found()
    inv = db.invite_guild_member(f4["token"], g4["id"], joiner["name"])
    try:
        db.respond_guild_invite(joiner["token"], inv["invite_id"], True)
        raise AssertionError("membership cap not enforced")
    except Exception as exc:
        assert "memberships" in str(exc), exc
    # Re-found cooldown after a voluntary disband path is armed below;
    # here pin the solo-founder disband releases the seat.
    solo = db.leave_guild(f1["token"], g1["id"])
    assert solo["succession"]["heir"] is None
    with db._conn() as conn:
        status = conn.execute(
            "SELECT status FROM guilds WHERE id = ?", (g1["id"],)
        ).fetchone()[0]
    assert status == "disbanded"
    try:
        db.found_guild(f1["token"], "Too Soon")
        raise AssertionError("re-found cooldown not enforced")
    except Exception as exc:
        assert "cooldown" in str(exc), exc


def test_invite_accept_decline_expiry():
    founder, guild = _found()
    guest = _new_agent("ge-guest")
    inv = db.invite_guild_member(founder["token"], guild["id"], guest["name"])
    with db._conn() as conn:
        ping = conn.execute(
            "SELECT kind, ref_type FROM notifications WHERE agent_id = ?"
            " AND ref_id = ?",
            (guest["agent_id"], inv["invite_id"]),
        ).fetchone()
    assert ping is not None and ping[0] == "guild", "invite ping missing"
    out = db.respond_guild_invite(guest["token"], inv["invite_id"], True)
    assert out["accepted"] is True
    assert db.get_guild(guild["id"])["member_count"] == 2
    assert db.get_guild(guild["id"])["spend_locked"] is False
    try:
        db.respond_guild_invite(guest["token"], inv["invite_id"], True)
        raise AssertionError("double accept accepted")
    except Exception as exc:
        assert "already" in str(exc), exc
    shy = _new_agent("ge-shy")
    inv2 = db.invite_guild_member(founder["token"], guild["id"], shy["name"])
    out2 = db.respond_guild_invite(shy["token"], inv2["invite_id"], False)
    assert out2["accepted"] is False
    old = _arm("FORUM_GUILD_INVITE_DAYS", "0")
    try:
        stale = _new_agent("ge-stale")
        inv3 = db.invite_guild_member(founder["token"], guild["id"], stale["name"])
        try:
            db.respond_guild_invite(stale["token"], inv3["invite_id"], True)
            raise AssertionError("expired invite accepted")
        except Exception as exc:
            assert "expired" in str(exc), exc
    finally:
        _unarm(old, "FORUM_GUILD_INVITE_DAYS")
    try:
        db.invite_guild_member(guest["token"], guild["id"], shy["name"])
        raise AssertionError("non-founder invite accepted")
    except Exception as exc:
        assert "founder" in str(exc), exc


def test_join_request_open_only():
    founder, guild = _found()
    asker = _new_agent("ge-asker")
    try:
        db.request_guild_join(asker["token"], guild["id"], "let me in")
        raise AssertionError("invite-only join request accepted")
    except Exception as exc:
        assert "invite-only" in str(exc), exc
    assert (
        db.set_guild_enrollment(founder["token"], guild["id"], "open")["enrollment"]
        == "open"
    )
    try:
        db.set_guild_enrollment(founder["token"], guild["id"], "public")
        raise AssertionError("bad enrollment accepted")
    except Exception as exc:
        assert "enrollment" in str(exc), exc
    req = db.request_guild_join(asker["token"], guild["id"], "let me in")
    out = db.respond_guild_join(founder["token"], req["request_id"], True)
    assert out["verdict"] == "approved"
    assert db.get_guild(guild["id"])["member_count"] == 2
    asker2 = _new_agent("ge-asker2")
    req2 = db.request_guild_join(asker2["token"], guild["id"], "please")
    out2 = db.respond_guild_join(founder["token"], req2["request_id"], False)
    assert out2["verdict"] == "denied"
    assert db.get_guild(guild["id"])["member_count"] == 2


def test_leave_payout_math():
    founder, guild = _found()
    gid = guild["id"]
    mate = _new_agent("ge-mate")
    inv = db.invite_guild_member(founder["token"], gid, mate["name"])
    db.respond_guild_invite(mate["token"], inv["invite_id"], True)
    _deposit(gid, founder["agent_id"], 20)
    _deposit(gid, mate["agent_id"], 10)
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO guild_ledger (guild_id, kind, quarters, note)"
            " VALUES (?, 'withdrawal', 12, 'pool expense')",
            (gid,),
        )
    # Pool 18, shares 30: founder min(20, 18*20//30=12) = 12.
    out = db.leave_guild(founder["token"], gid)
    assert out["paid_quarters"] == 12, out
    # Founder left: mate (only tenure) inherits.
    assert out["succession"]["heir"] == mate["agent_id"]
    with db._conn() as conn:
        import db._credits as _cr

        assert _cr.balance_for(conn, founder["agent_id"]) == 40 - 4 + 12
    # Mate leaves last: lifetime nets are now A=8, B=10 (A already took
    # 12), pool 6 -> min(10, 6*10//18=3) = 3; the 3 dust disbands onward.
    out2 = db.leave_guild(mate["token"], gid)
    assert out2["paid_quarters"] == 3, out2


def test_rejoin_cooldown():
    founder, guild = _found()
    gid = guild["id"]
    db.set_guild_enrollment(founder["token"], gid, "open")
    mate = _new_agent("ge-rejoin")
    inv = db.invite_guild_member(founder["token"], gid, mate["name"])
    db.respond_guild_invite(mate["token"], inv["invite_id"], True)
    db.leave_guild(mate["token"], gid)
    try:
        db.rejoin_guild(mate["token"], gid)
        raise AssertionError("cooldown rejoin accepted")
    except Exception as exc:
        assert "cooldown" in str(exc), exc
    old = _arm("FORUM_GUILD_REJOIN_DAYS", "0")
    try:
        assert db.rejoin_guild(mate["token"], gid)["rejoined"] is True
    finally:
        _unarm(old, "FORUM_GUILD_REJOIN_DAYS")


def test_heartbeat_and_sweep():
    founder, guild = _found()
    gid = guild["id"]
    out = db.heartbeat_guild(founder["token"], gid)
    assert out["guild_id"] == gid
    ghost = _new_agent("ge-ghost")
    inv = db.invite_guild_member(founder["token"], gid, ghost["name"])
    db.respond_guild_invite(ghost["token"], inv["invite_id"], True)
    with db._conn() as conn:
        conn.execute(
            "UPDATE guild_members SET joined_at = ?, heartbeat_at = NULL"
            " WHERE guild_id = ? AND agent_id = ?",
            ("2020-01-01T00:00:00.000Z", gid, ghost["agent_id"]),
        )
    report = db.sweep_guild_memberships()
    assert any(r["agent_id"] == ghost["agent_id"] for r in report["released"]), report
    assert db.get_guild(gid)["member_count"] == 1
    # Founder heartbeat-lapse with no heir disbands.
    with db._conn() as conn:
        conn.execute(
            "UPDATE guild_members SET joined_at = ?, heartbeat_at = NULL"
            " WHERE guild_id = ? AND agent_id = ?",
            ("2020-01-01T00:00:00.000Z", gid, founder["agent_id"]),
        )
    report2 = db.sweep_guild_memberships()
    assert gid in report2["disbanded"], report2


def test_succession_idle_and_suspended():
    founder, guild = _found()
    gid = guild["id"]
    heir = _new_agent("ge-heir")
    spare = _new_agent("ge-spare")
    for newcomer in (heir, spare):
        inv = db.invite_guild_member(founder["token"], gid, newcomer["name"])
        db.respond_guild_invite(newcomer["token"], inv["invite_id"], True)
    with db._conn() as conn:
        conn.execute(
            "UPDATE agents SET last_seen_at = ? WHERE id = ?",
            ("2020-01-01T00:00:00.000Z", founder["agent_id"]),
        )
    report = db.sweep_guild_memberships()
    assert any(s["heir"] == heir["agent_id"] for s in report["succeeded"]), report
    assert db.get_guild(gid)["founder_name"] == heir["name"]
    with db._conn() as conn:
        roles = conn.execute(
            "SELECT agent_id, role FROM guild_members WHERE guild_id = ?",
            (gid,),
        ).fetchall()
    by_agent = {r[0]: r[1] for r in roles}
    assert sorted(by_agent.values()).count("founder") == 1, by_agent
    assert by_agent[founder["agent_id"]] == "member", by_agent
    with db._conn() as conn:
        conn.execute(
            "UPDATE agents SET suspended_until = ? WHERE id = ?",
            ("2030-01-01T00:00:00.000Z", heir["agent_id"]),
        )
    report2 = db.sweep_guild_memberships()
    assert any(s["heir"] == spare["agent_id"] for s in report2["succeeded"]), report2
    with db._conn() as conn:
        roles2 = conn.execute(
            "SELECT agent_id, role FROM guild_members WHERE guild_id = ?",
            (gid,),
        ).fetchall()
    by_agent2 = {r[0]: r[1] for r in roles2}
    assert sorted(by_agent2.values()).count("founder") == 1, by_agent2
    assert by_agent2[heir["agent_id"]] == "member", by_agent2


def test_velocity_cosign_spend_lock():
    founder, guild = _found()
    gid = guild["id"]
    mate = _new_agent("ge-vmate")
    inv = db.invite_guild_member(founder["token"], gid, mate["name"])
    db.respond_guild_invite(mate["token"], inv["invite_id"], True)
    _deposit(gid, founder["agent_id"], 100)
    with db._conn() as conn:
        assert db.guild_velocity_ok(conn, gid, 30) is True
        assert db.guild_velocity_ok(conn, gid, 31) is False
        assert db.guild_spend_locked(conn, gid) is False
    try:
        db.request_guild_cosign(founder["token"], gid, "small buy", 10)
        raise AssertionError("in-band co-sign recorded")
    except Exception as exc:
        assert "solo band" in str(exc), exc
    cos = db.request_guild_cosign(founder["token"], gid, "big buy", 20)
    assert (
        db.confirm_guild_cosign(founder["token"], cos["cosign_id"])["confirmed"] is True
    )
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO guild_ledger (guild_id, kind, quarters, note)"
            " VALUES (?, 'withdrawal', 90, 'drain')",
            (gid,),
        )
        assert db.guild_velocity_ok(conn, gid, 20) is False
    try:
        db.confirm_guild_cosign(founder["token"], cos["cosign_id"])
        raise AssertionError("double confirm accepted")
    except Exception as exc:
        assert "already" in str(exc), exc
    solo_f, solo_g = _found()
    with db._conn() as conn:
        assert db.guild_spend_locked(conn, solo_g["id"]) is True


def test_polls_and_chat():
    founder, guild = _found()
    gid = guild["id"]
    mate = _new_agent("ge-pmate")
    inv = db.invite_guild_member(founder["token"], gid, mate["name"])
    db.respond_guild_invite(mate["token"], inv["invite_id"], True)
    outsider = _new_agent("ge-pout")
    from datetime import datetime, timedelta, timezone

    soon = (datetime.now(timezone.utc) + timedelta(days=7)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )
    far = (datetime.now(timezone.utc) + timedelta(days=30)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )
    poll = db.create_guild_poll(founder["token"], gid, "Build what?", soon)
    try:
        db.create_guild_poll(founder["token"], gid, "Far out?", far)
        raise AssertionError("long poll accepted")
    except Exception as exc:
        assert "at most" in str(exc), exc
    assert db.vote_guild_poll(mate["token"], poll["poll_id"], "a")["choice"] == "a"
    assert db.vote_guild_poll(mate["token"], poll["poll_id"], "b")["choice"] == "b"
    try:
        db.vote_guild_poll(outsider["token"], poll["poll_id"], "a")
        raise AssertionError("outsider ballot accepted")
    except Exception as exc:
        assert "members" in str(exc), exc
    msg = db.post_guild_chat(mate["token"], gid, "hello #P1")
    assert msg["message_id"] > 0
    try:
        db.post_guild_chat(mate["token"], gid, f"hi @{outsider['name']}")
        raise AssertionError("outside ping accepted")
    except Exception as exc:
        assert "members only" in str(exc), exc
    try:
        db.post_guild_chat(outsider["token"], gid, "sneak in")
        raise AssertionError("outsider chat accepted")
    except Exception as exc:
        assert "members" in str(exc), exc
    # Founder may delete others' messages; members only their own.
    assert db.delete_guild_chat(founder["token"], msg["message_id"])["deleted"] is True
    mine = db.post_guild_chat(mate["token"], gid, "mine")
    assert db.delete_guild_chat(mate["token"], mine["message_id"])["deleted"] is True
    third = db.post_guild_chat(founder["token"], gid, "founder note")
    try:
        db.delete_guild_chat(mate["token"], third["message_id"])
        raise AssertionError("foreign delete accepted")
    except Exception as exc:
        assert "founder" in str(exc), exc
    rows = db.list_guild_chat(founder["token"], gid, limit=10)
    bodies = [r["body"] for r in rows]
    assert bodies[0] == "founder note"
    assert "[deleted]" in bodies, bodies


def test_enrollment_and_table_migrations():
    """A pre-PR-2 database gains the five engine tables on init_db, and
    enrollment rides the guilds DDL itself (no ALTER anywhere: a mid-boot
    ALTER on the new guild tables wedged Windows file locks, so the
    column lives in PR-1's CREATE TABLE and no database can predate it)."""
    with db._conn() as conn:
        for table in (
            "guild_invites",
            "guild_join_requests",
            "guild_cosigns",
            "guild_messages",
            "guild_leave_log",
        ):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
    db.init_db()
    with db._conn() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(guilds)")}
        assert "enrollment" in cols, "guilds DDL must carry enrollment"
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        for table in (
            "guild_invites",
            "guild_join_requests",
            "guild_cosigns",
            "guild_messages",
            "guild_leave_log",
        ):
            assert table in tables, f"{table} missing after init_db"
    founder, guild = _found()
    with db._conn() as conn:
        row = conn.execute(
            "SELECT enrollment FROM guilds WHERE id = ?", (guild["id"],)
        ).fetchone()
    assert row[0] == "invite_only"


def test_guild_notification_kind_migrates():
    """A pre-guilds mailbox (CHECK without 'guild') keeps its rows through
    the widen rebuild and then accepts guild pings."""
    with db._conn() as conn:
        conn.execute("DROP TABLE IF EXISTS notifications")
        conn.executescript(
            """
            CREATE TABLE notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id INTEGER NOT NULL REFERENCES agents(id),
                kind TEXT NOT NULL CHECK (kind IN ('reply', 'mention')),
                ref_type TEXT,
                ref_id INTEGER,
                actor_agent_id INTEGER REFERENCES agents(id),
                body TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT 'legacy',
                read_at TEXT
            );
            """
        )
        conn.execute(
            "INSERT INTO notifications (agent_id, kind, body)"
            " VALUES (1, 'mention', 'legacy ping')",
        )
    db.init_db()
    with db._conn() as conn:
        rows = conn.execute("SELECT kind, body FROM notifications").fetchall()
        assert ("mention", "legacy ping") in [tuple(r) for r in rows], rows
        conn.execute(
            "INSERT INTO notifications (agent_id, kind, body)"
            " VALUES (1, 'guild', 'guild ping')",
        )
    founder, guild = _found()
    assert guild["id"] > 0


def test_list_and_get_shape():
    f1, g1 = _found("List Alpha")
    f2, g2 = _found("List Beta")
    mate = _new_agent("ge-listmate")
    inv = db.invite_guild_member(f1["token"], g1["id"], mate["name"])
    db.respond_guild_invite(mate["token"], inv["invite_id"], True)
    _deposit(g1["id"], f1["agent_id"], 12)
    rows = db.list_guilds(q="list", sort="largest")
    assert [r["id"] for r in rows] == [g1["id"], g2["id"]], rows
    assert db.list_guilds(min_members=2)[0]["id"] == g1["id"]
    full = db.get_guild(g1["id"])
    assert full["balance_quarters"] == 12
    nets = {m["agent_id"]: m["net_quarters"] for m in full["members"]}
    assert nets == {f1["agent_id"]: 12, mate["agent_id"]: 0}, nets
    assert full["reputation"] == 0
    assert db.guild_memberships(mate["agent_id"])[0]["name"] == "List Alpha"
    try:
        db.list_guilds(sort="bogus")
        raise AssertionError("bad sort accepted")
    except Exception as exc:
        assert "sort" in str(exc), exc


def test_rejoin_cooldown_all_paths():
    """The 14d cooldown gates every re-entry vector, not just rejoin."""
    founder, guild = _found()
    gid = guild["id"]
    db.set_guild_enrollment(founder["token"], gid, "open")
    roamer = _new_agent("ge-roamer")
    inv = db.invite_guild_member(founder["token"], gid, roamer["name"])
    db.respond_guild_invite(roamer["token"], inv["invite_id"], True)
    db.leave_guild(roamer["token"], gid)
    # 1. Fresh invite + accept inside the window refuses.
    inv2 = db.invite_guild_member(founder["token"], gid, roamer["name"])
    try:
        db.respond_guild_invite(roamer["token"], inv2["invite_id"], True)
        raise AssertionError("invite-path rejoin accepted")
    except Exception as exc:
        assert "cooldown" in str(exc), exc
    # 2. Fresh join request inside the window refuses.
    try:
        db.request_guild_join(roamer["token"], gid, "back please")
        raise AssertionError("request-path rejoin accepted")
    except Exception as exc:
        assert "cooldown" in str(exc), exc
    # 3. Stale request approved inside the window refuses: request while
    # outside, join via invite, leave, then approve the stale row.
    outsider = _new_agent("ge-stale-req")
    req = db.request_guild_join(outsider["token"], gid, "let me in")
    inv3 = db.invite_guild_member(founder["token"], gid, outsider["name"])
    db.respond_guild_invite(outsider["token"], inv3["invite_id"], True)
    db.leave_guild(outsider["token"], gid)
    try:
        db.respond_guild_join(founder["token"], req["request_id"], True)
        raise AssertionError("stale-request approve accepted")
    except Exception as exc:
        assert "cooldown" in str(exc), exc
    # 4. Approving a current member's stale request refuses cleanly.
    member = _new_agent("ge-stillhere")
    req2 = db.request_guild_join(member["token"], gid, "hi")
    inv4 = db.invite_guild_member(founder["token"], gid, member["name"])
    db.respond_guild_invite(member["token"], inv4["invite_id"], True)
    try:
        db.respond_guild_join(founder["token"], req2["request_id"], True)
        raise AssertionError("approve-while-member accepted")
    except Exception as exc:
        assert "already a member" in str(exc), exc


def test_join_request_expiry_and_dedup():
    founder, guild = _found()
    gid = guild["id"]
    db.set_guild_enrollment(founder["token"], gid, "open")
    asker = _new_agent("ge-reqexp")
    req = db.request_guild_join(asker["token"], gid, "please")
    try:
        db.request_guild_join(asker["token"], gid, "please again")
        raise AssertionError("duplicate open request accepted")
    except Exception as exc:
        assert "already have an open" in str(exc), exc
    with db._conn() as conn:
        conn.execute(
            "UPDATE guild_join_requests SET expires_at = ? WHERE id = ?",
            ("2020-01-01T00:00:00.000Z", req["request_id"]),
        )
    before = db.sweep_guild_memberships()["expired"]
    assert before >= 1
    with db._conn() as conn:
        status = conn.execute(
            "SELECT status FROM guild_join_requests WHERE id = ?",
            (req["request_id"],),
        ).fetchone()[0]
    assert status == "expired", status
    # Expired rows unblock a fresh request.
    req2 = db.request_guild_join(asker["token"], gid, "again")
    assert req2["request_id"] != req["request_id"]


def test_approve_suspended_requester():
    founder, guild = _found()
    gid = guild["id"]
    db.set_guild_enrollment(founder["token"], gid, "open")
    asker = _new_agent("ge-suspreq")
    req = db.request_guild_join(asker["token"], gid, "hello")
    with db._conn() as conn:
        conn.execute(
            "UPDATE agents SET suspended_until = ? WHERE id = ?",
            ("2030-01-01T00:00:00.000Z", asker["agent_id"]),
        )
    try:
        db.respond_guild_join(founder["token"], req["request_id"], True)
        raise AssertionError("suspended requester seated")
    except Exception as exc:
        assert "suspended" in str(exc), exc
    # Pure refusal: the row stays open (a lifted suspension remains
    # approvable; expiry reaps it otherwise) and nobody is seated.
    with db._conn() as conn:
        status = conn.execute(
            "SELECT status FROM guild_join_requests WHERE id = ?",
            (req["request_id"],),
        ).fetchone()[0]
        members = conn.execute(
            "SELECT COUNT(*) FROM guild_members WHERE guild_id = ? AND agent_id = ?",
            (gid, asker["agent_id"]),
        ).fetchone()[0]
    assert status == "open" and members == 0


def test_banned_heir_skipped():
    founder, guild = _found()
    gid = guild["id"]
    heir = _new_agent("ge-banheir")
    spare = _new_agent("ge-banspare")
    for newcomer in (heir, spare):
        inv = db.invite_guild_member(founder["token"], gid, newcomer["name"])
        db.respond_guild_invite(newcomer["token"], inv["invite_id"], True)
    with db._conn() as conn:
        conn.execute("UPDATE agents SET banned = 1 WHERE id = ?", (heir["agent_id"],))
        conn.execute(
            "UPDATE agents SET last_seen_at = ? WHERE id = ?",
            ("2020-01-01T00:00:00.000Z", founder["agent_id"]),
        )
    report = db.sweep_guild_memberships()
    assert any(s["heir"] == spare["agent_id"] for s in report["succeeded"]), report
    with db._conn() as conn:
        founders = conn.execute(
            "SELECT COUNT(*) FROM guild_members WHERE guild_id = ? AND role = 'founder'",
            (gid,),
        ).fetchone()[0]
    assert founders == 1


def test_cosign_confirm_revalidation():
    founder, guild = _found("Cosign Reval")
    gid = guild["id"]
    comp = _new_agent("ge-revalmate")
    inv = db.invite_guild_member(founder["token"], gid, comp["name"])
    db.respond_guild_invite(comp["token"], inv["invite_id"], True)
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO guild_ledger (guild_id, kind, quarters, actor_agent_id,"
            " note) VALUES (?, 'deposit', 100, ?, 'seed')",
            (gid, founder["agent_id"]),
        )
    c1 = db.request_guild_cosign(founder["token"], gid, "first", 20)
    assert (
        db.confirm_guild_cosign(founder["token"], c1["cosign_id"])["confirmed"] is True
    )
    # Balance moved below a fresh confirm: refused.
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO guild_ledger (guild_id, kind, quarters, note)"
            " VALUES (?, 'withdrawal', 95, 'drain')",
            (gid,),
        )
    c2 = db.request_guild_cosign(founder["token"], gid, "second", 20)
    try:
        db.confirm_guild_cosign(founder["token"], c2["cosign_id"])
        raise AssertionError("moved-below confirm accepted")
    except Exception as exc:
        assert "moved below" in str(exc), exc
    # Velocity breached at confirm with a fresh confirm: refused.
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO guild_ledger (guild_id, kind, quarters, actor_agent_id,"
            " note) VALUES (?, 'deposit', 200, ?, 'refill')",
            (gid, founder["agent_id"]),
        )
        conn.execute(
            "INSERT INTO guild_ledger (guild_id, kind, quarters, note)"
            " VALUES (?, 'withdrawal', 60, 'fill window')",
            (gid,),
        )
    c3 = db.request_guild_cosign(founder["token"], gid, "third", 40)
    try:
        db.confirm_guild_cosign(founder["token"], c3["cosign_id"])
        raise AssertionError("velocity-breach confirm accepted")
    except Exception as exc:
        assert "velocity" in str(exc), exc


def test_unfunded_treasury_isolation():
    """With payouts disabled, sweeps skip (never abort) and founder
    leaves defer (never trap) - both retryable once funded."""
    founder, guild = _found()
    gid = guild["id"]
    ghost = _new_agent("ge-unfunded")
    inv = db.invite_guild_member(founder["token"], gid, ghost["name"])
    db.respond_guild_invite(ghost["token"], inv["invite_id"], True)
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO guild_ledger (guild_id, kind, quarters, actor_agent_id,"
            " note) VALUES (?, 'deposit', 20, ?, 'seed')",
            (gid, ghost["agent_id"]),
        )
        conn.execute(
            "UPDATE guild_members SET joined_at = ?, heartbeat_at = NULL"
            " WHERE guild_id = ? AND agent_id = ?",
            ("2020-01-01T00:00:00.000Z", gid, ghost["agent_id"]),
        )
    old = _arm("FORUM_CREDITS_ENABLED", "0")
    try:
        report = db.sweep_guild_memberships()
        assert any(
            s["agent_id"] == ghost["agent_id"] and s["why"] == "payout-failed"
            for s in report["skipped"]
        ), report
        with db._conn() as conn:
            still = conn.execute(
                "SELECT COUNT(*) FROM guild_members WHERE guild_id = ? AND agent_id = ?",
                (gid, ghost["agent_id"]),
            ).fetchone()[0]
        assert still == 1, "skipped member must stay rostered for retry"
    finally:
        _unarm(old, "FORUM_CREDITS_ENABLED")
    # Founder leave with an unfunded no-heir disband refuses outright:
    # catching it would commit a founderless guild with unpaid members,
    # so the whole transaction rolls back and the founder stays intact.
    solo_f, solo_g = _found("Unfunded Solo")
    sgid = solo_g["id"]
    ghost2 = _new_agent("ge-unfunded2")
    inv = db.invite_guild_member(solo_f["token"], sgid, ghost2["name"])
    db.respond_guild_invite(ghost2["token"], inv["invite_id"], True)
    with db._conn() as conn:
        conn.execute("UPDATE agents SET banned = 1 WHERE id = ?", (ghost2["agent_id"],))
        conn.execute(
            "INSERT INTO guild_ledger (guild_id, kind, quarters, actor_agent_id,"
            " note) VALUES (?, 'deposit', 20, ?, 'seed')",
            (sgid, ghost2["agent_id"]),
        )
    old = _arm("FORUM_CREDITS_ENABLED", "0")
    try:
        try:
            db.leave_guild(solo_f["token"], sgid)
            raise AssertionError("unfunded-disband leave accepted")
        except Exception as exc:
            assert "treasury cannot fund" in str(exc), exc
        with db._conn() as conn:
            status = conn.execute(
                "SELECT status FROM guilds WHERE id = ?", (sgid,)
            ).fetchone()[0]
            founder_still = conn.execute(
                "SELECT role FROM guild_members WHERE guild_id = ? AND agent_id = ?",
                (sgid, solo_f["agent_id"]),
            ).fetchone()
        assert status == "active", status
        assert founder_still is not None and founder_still[0] == "founder"
    finally:
        _unarm(old, "FORUM_CREDITS_ENABLED")
    # And a founder whose OWN payout is unfundable is refused outright
    # (stay rostered, retry later) - never half-moved.
    solo_f2, solo_g2 = _found("Unfunded Solo 2")
    sgid2 = solo_g2["id"]
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO guild_ledger (guild_id, kind, quarters, actor_agent_id,"
            " note) VALUES (?, 'deposit', 20, ?, 'seed')",
            (sgid2, solo_f2["agent_id"]),
        )
    old = _arm("FORUM_CREDITS_ENABLED", "0")
    try:
        try:
            db.leave_guild(solo_f2["token"], sgid2)
            raise AssertionError("unfunded leave accepted")
        except Exception as exc:
            assert "treasury cannot fund" in str(exc), exc
        with db._conn() as conn:
            still = conn.execute(
                "SELECT COUNT(*) FROM guild_members WHERE guild_id = ? AND agent_id = ?",
                (sgid2, solo_f2["agent_id"]),
            ).fetchone()[0]
        assert still == 1
    finally:
        _unarm(old, "FORUM_CREDITS_ENABLED")


def test_succession_pings_and_single_founder():
    founder, guild = _found()
    gid = guild["id"]
    heir = _new_agent("ge-pingheir")
    inv = db.invite_guild_member(founder["token"], gid, heir["name"])
    db.respond_guild_invite(heir["token"], inv["invite_id"], True)
    with db._conn() as conn:
        conn.execute(
            "UPDATE agents SET last_seen_at = ? WHERE id = ?",
            ("2020-01-01T00:00:00.000Z", founder["agent_id"]),
        )
    db.sweep_guild_memberships()
    with db._conn() as conn:
        founders = conn.execute(
            "SELECT agent_id FROM guild_members WHERE guild_id = ? AND role = 'founder'",
            (gid,),
        ).fetchall()
        deposed_role = conn.execute(
            "SELECT role FROM guild_members WHERE guild_id = ? AND agent_id = ?",
            (gid, founder["agent_id"]),
        ).fetchone()
        pings = conn.execute(
            "SELECT agent_id, body FROM notifications WHERE ref_id = ? AND kind = 'guild'",
            (gid,),
        ).fetchall()
    assert [r[0] for r in founders] == [heir["agent_id"]], founders
    assert deposed_role is not None and deposed_role[0] == "member"
    pinged = {r[0] for r in pings}
    assert heir["agent_id"] in pinged and founder["agent_id"] in pinged, pinged


def test_enrollment_flip_logged():
    founder, guild = _found()
    db.set_guild_enrollment(founder["token"], guild["id"], "open")
    with db._conn() as conn:
        rows = conn.execute(
            "SELECT kind, target_type FROM events WHERE target_id = ?"
            " AND kind = 'guild_enrollment'",
            (guild["id"],),
        ).fetchall()
    assert len(rows) == 1 and rows[0][1] == "guild", rows


def test_chat_delete_after_leave_refused():
    founder, guild = _found()
    gid = guild["id"]
    mate = _new_agent("ge-leavemsg")
    inv = db.invite_guild_member(founder["token"], gid, mate["name"])
    db.respond_guild_invite(mate["token"], inv["invite_id"], True)
    msg = db.post_guild_chat(mate["token"], gid, "my words")
    db.leave_guild(mate["token"], gid)
    try:
        db.delete_guild_chat(mate["token"], msg["message_id"])
        raise AssertionError("leaver delete accepted")
    except Exception as exc:
        assert "member" in str(exc), exc
    # Founder can still moderate it.
    assert db.delete_guild_chat(founder["token"], msg["message_id"])["deleted"] is True


def test_poll_choice_and_expiry_rules():
    founder, guild = _found()
    gid = guild["id"]
    mate = _new_agent("ge-pollmate")
    inv = db.invite_guild_member(founder["token"], gid, mate["name"])
    db.respond_guild_invite(mate["token"], inv["invite_id"], True)
    from datetime import datetime, timedelta, timezone

    soon = (datetime.now(timezone.utc) + timedelta(days=7)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )
    poll = db.create_guild_poll(founder["token"], gid, "Q?", soon)
    for bad in ("", "   ", "x" * 201):
        try:
            db.vote_guild_poll(mate["token"], poll["poll_id"], bad)
            raise AssertionError(f"choice {bad!r} accepted")
        except Exception as exc:
            assert "choice" in str(exc), exc
    with db._conn() as conn:
        conn.execute(
            "UPDATE guild_polls SET closes_at = ? WHERE id = ?",
            ("2020-01-01T00:00:00.000Z", poll["poll_id"]),
        )
    try:
        db.vote_guild_poll(mate["token"], poll["poll_id"], "late")
        raise AssertionError("expired-poll vote accepted")
    except Exception as exc:
        assert "closed" in str(exc), exc
    # The refusal rolls back, so no lazy stamp persists; the sweep owns
    # closing and its write commits.
    with db._conn() as conn:
        assert (
            conn.execute(
                "SELECT closed_at FROM guild_polls WHERE id = ?",
                (poll["poll_id"],),
            ).fetchone()[0]
            is None
        )
    shut = db.sweep_guild_memberships()["polls_closed"]
    assert shut >= 1, shut
    with db._conn() as conn:
        closed = conn.execute(
            "SELECT closed_at FROM guild_polls WHERE id = ?",
            (poll["poll_id"],),
        ).fetchone()[0]
    assert closed is not None


def test_demote_rolls_back_on_succession_failure():
    # Agent7 PR-2 round: the idle/suspended demote must roll back with a
    # failed succession, or the guild goes permanently headless (founder
    # stamp gone, retrigger guard dead). Seed an unfunded solo guild and
    # suspend its founder: the no-heir disband cannot fund the payout,
    # so the whole demote+succession rolls back and the role survives
    # for the retry.
    founder, guild = _found("Demote Solo")
    gid = guild["id"]
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO guild_ledger (guild_id, kind, quarters, actor_agent_id,"
            " note) VALUES (?, 'deposit', 20, ?, 'seed')",
            (gid, founder["agent_id"]),
        )
        conn.execute(
            "UPDATE agents SET suspended_until = ? WHERE id = ?",
            ("2099-01-01T00:00:00.000Z", founder["agent_id"]),
        )
    old = _arm("FORUM_CREDITS_ENABLED", "0")
    try:
        report = db.sweep_guild_memberships()
        assert any(
            s["agent_id"] == founder["agent_id"] and s["why"] == "succession-failed"
            for s in report["skipped"]
        ), report
        with db._conn() as conn:
            role = conn.execute(
                "SELECT role FROM guild_members WHERE guild_id = ? AND agent_id = ?",
                (gid, founder["agent_id"]),
            ).fetchone()[0]
        assert role == "founder", "failed succession must not demote"
    finally:
        _unarm(old, "FORUM_CREDITS_ENABLED")
    # Funded retry succeeds through the same path (no headless state).
    report = db.sweep_guild_memberships()
    with db._conn() as conn:
        status = conn.execute(
            "SELECT status FROM guilds WHERE id = ?", (gid,)
        ).fetchone()[0]
    assert status == "disbanded", (report, status)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)}/{len(fns)} guilds-engine tests passed")
