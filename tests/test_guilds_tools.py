"""Guild tool surface (proposal #525, PR-8): thin MCP wrappers over the
PR-1–PR-7 engine plus the guild_id extensions. Pins the tool layer
only (auth gates, admin-flag refusal, executor attribution, linkage
storage, profiles enrichment) - economics stay pinned in the engine
suites. Validation parity: tools pass straight through, so db error
copy is asserted, not reworded.
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_guilds_tools_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)
os.environ["FORUM_GUILD_FOUND_KARMA"] = "0"
os.environ["FORUM_MAX_GUILDS"] = "100"
os.environ["FORUM_JOB_CREATOR_MIN_KARMA"] = "0"
os.environ["FORUM_INVOICE_MIN_KARMA"] = "0"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402, I001
from server.tools import guilds as gtools  # noqa: E402, I001
from server.tools import economy as etools  # noqa: E402, I001
from server.tools import forum as ftools  # noqa: E402, I001
from server.tools import discovery as dtools  # noqa: E402, I001

db.init_db()

AGENTS, BASE_POST = setup()  # once per process - names are unique

_SEQ = [0]


def _new_agent(prefix: str) -> dict:
    _SEQ[0] += 1
    return db.register_agent(f"{prefix}-{_SEQ[0]}")


def _fund(agent_id: int, units: int):
    import db._credits as _cr

    with db._conn() as _c:
        ok = _cr.grant(
            agent_id,
            units,
            "guild_tools_seed",
            target_type="test",
            target_id=1,
            conn=_c,
        )
    assert ok, "treasury could not fund the test seed"


def _pool(guild_id: int) -> int:
    with db._conn() as conn:
        return db.guild_balance(conn, guild_id)


def _guild() -> tuple[dict, dict, dict]:
    founder = _new_agent("gt-founder")
    _fund(founder["agent_id"], 1000)
    guild = gtools.create_guild(founder["token"], f"Tools-{_SEQ[0]}")
    mate = _new_agent("gt-mate")
    _fund(mate["agent_id"], 600)
    inv = gtools.invite_guild_member(founder["token"], guild["id"], mate["name"])
    gtools.respond_guild_invite(mate["token"], inv["invite_id"], True)
    return founder, guild, mate


def test_create_rename_mission_disband():
    founder = _new_agent("gt-rnm")
    _fund(founder["agent_id"], 1000)
    guild = gtools.create_guild(founder["token"], "Rename-Me")
    gid = guild["id"]
    assert gtools.get_guild(gid)["name"] == "Rename-Me"
    try:
        gtools.rename_guild(founder["token"], gid, "  ")
        raise AssertionError("empty rename accepted")
    except Exception as exc:
        assert "empty" in str(exc), exc
    renamed = gtools.rename_guild(founder["token"], gid, "Renamed")
    assert renamed["name"] == "Renamed", renamed
    assert gtools.get_guild(gid)["name"] == "Renamed"
    assert gtools.get_guild(gid)["mission"] == ""
    try:
        gtools.edit_guild_mission(founder["token"], gid, "x" * 201)
        raise AssertionError("long mission accepted")
    except Exception as exc:
        assert "200" in str(exc), exc
    mission = gtools.edit_guild_mission(founder["token"], gid, "Build things")
    assert mission["mission"] == "Build things", mission
    assert gtools.get_guild(gid)["mission"] == "Build things"
    mate = _new_agent("gt-rnm-m")
    try:
        gtools.rename_guild(mate["token"], gid, "Hijack")
        raise AssertionError("non-founder renamed")
    except Exception as exc:
        assert "founder" in str(exc), exc
    out = gtools.disband_guild(founder["token"], gid, "zero")
    assert out["mode"] == "zero", out


def test_membership_round_trip():
    founder, guild, mate = _guild()
    gid = guild["id"]
    assert (
        gtools.set_guild_enrollment(founder["token"], gid, "open")["enrollment"]
        == "open"
    )
    stranger = _new_agent("gt-stranger")
    req = gtools.request_guild_join(stranger["token"], gid, "let me in")
    assert req["request_id"] is not None, req
    gtools.respond_guild_join(founder["token"], req["request_id"], True)
    assert gtools.heartbeat_guild(stranger["token"], gid)["guild_id"] == gid
    left = gtools.leave_guild(stranger["token"], gid)
    assert "paid_units" in left, left
    try:
        gtools.rejoin_guild(stranger["token"], gid)
        raise AssertionError("cooldown-busted rejoin passed")
    except Exception as exc:
        assert "cooldown" in str(exc), exc
    with db._conn() as conn:
        conn.execute(
            "UPDATE guild_leave_log SET left_at = ?"
            " WHERE guild_id = ? AND agent_id = ?",
            (
                "2026-08-01T00:00:00.000Z",
                gid,
                stranger["agent_id"],
            ),
        )
    assert gtools.rejoin_guild(stranger["token"], gid)["guild_id"] == gid
    gtools.set_guild_enrollment(founder["token"], gid, "invite_only")


def test_money_wrappers_move_pool():
    founder, guild, mate = _guild()
    gid = guild["id"]
    gtools.guild_deposit(founder["token"], gid, 25.0)
    gtools.guild_deposit(mate["token"], gid, 10.0)
    assert _pool(gid) == 700, _pool(gid)
    out = gtools.guild_withdraw(founder["token"], gid, 5.0)
    assert out["fee_units"] == 2, out
    # Proposal #611: the wallet pays the net (the 2u fee stays pool-owned
    # via the retention pair) while the memo extinguishes the gross.
    assert _pool(gid) == 700 - 98, _pool(gid)
    with db._conn() as conn:
        assert db.guild_memo_balance(conn, gid) == 700 - 100, _pool(gid)


def test_invoice_wrapper_full_and_part():
    founder, guild, mate = _guild()
    gid = guild["id"]
    gtools.guild_deposit(founder["token"], gid, 25.0)
    inv = db.create_invoice(mate["token"], founder["name"], 2.0, "tools bill")
    db.accept_invoice(founder["token"], inv["invoice_id"])
    paid = gtools.guild_pay_invoice(founder["token"], inv["invoice_id"], 1.0)
    assert paid["remaining_units"] == 20, paid
    assert _pool(gid) == 500 - 20, _pool(gid)
    paid = gtools.guild_pay_invoice(founder["token"], inv["invoice_id"])
    assert paid["remaining_units"] == 0, paid
    assert _pool(gid) == 500 - 40, _pool(gid)


def test_stake_and_subsidy_wrappers():
    founder, guild, mate = _guild()
    gid = guild["id"]
    gtools.guild_deposit(founder["token"], gid, 25.0)
    sponsor = _new_agent("gt-sp")
    post = db.create_post(sponsor["token"], "Stake prop T", "Body text here.")
    for name in ("beta", "gamma", "delta", "epsilon", "zeta"):
        db.vote(AGENTS[name]["token"], "post", post["post_id"], 1)
    prop = db.create_proposal(sponsor["token"], "Stake Prop T", "Body")
    pid = prop["post_id"]
    for name in ("beta", "gamma", "delta"):
        db.vote_on_proposal(AGENTS[name]["token"], pid, 1)
    cos = gtools.request_guild_cosign(founder["token"], gid, "stake", 5.0)
    gtools.confirm_guild_cosign(founder["token"], cos["cosign_id"])
    staked = gtools.guild_stake(founder["token"], pid, 2.5, 2)
    assert staked["guild_id"] == gid, staked
    sub = gtools.request_guild_subsidy(founder["token"], gid, 1.0, False, "tools grant")
    assert sub["status"] == "paid" and sub["amount_units"] == 20, sub


def test_decide_subsidy_admin_gate():
    founder, guild, mate = _guild()
    gid = guild["id"]
    out = gtools.request_guild_subsidy(founder["token"], gid, 5.0, True, "big ask")
    assert out["status"] == "requested" and out["tier"] == "admin", out
    try:
        gtools.decide_guild_subsidy(founder["token"], out["subsidy_id"], True)
        raise AssertionError("non-admin decided")
    except Exception as exc:
        assert "Admin privileges" in str(exc), exc
    old = os.environ.get("ADMIN_USER")
    os.environ["ADMIN_USER"] = founder["name"]
    try:
        decided = gtools.decide_guild_subsidy(founder["token"], out["subsidy_id"], True)
    finally:
        if old is None:
            os.environ.pop("ADMIN_USER", None)
        else:
            os.environ["ADMIN_USER"] = old
    assert decided["status"] == "paid", decided


def test_designate_and_match_wrappers():
    founder, guild, mate = _guild()
    gid = guild["id"]
    gtools.guild_deposit(founder["token"], gid, 25.0)
    c1, c2 = _new_agent("gt-d1"), _new_agent("gt-d2")
    idea = db.create_proposal(
        mate["token"], f"Tools idea {_SEQ[0]}", "A build.", idea=True
    )
    with db._conn() as conn:
        conn.execute(
            "UPDATE posts SET created_at = ? WHERE id = ?",
            ("2026-09-01T00:00:00.000Z", idea["post_id"]),
        )
    db.create_comment(c1["token"], idea["post_id"], "yes")
    db.create_comment(c2["token"], idea["post_id"], "yes indeed")
    des = gtools.designate_guild_project(founder["token"], gid, idea["post_id"])
    assert des["guild_id"] == gid, des
    window = gtools.open_guild_match_window(
        founder["token"], gid, "lump", amount_credits=2.0
    )
    assert window["status"] == "paid" and window["amount_units"] == 40, window


def test_chat_and_polls_wrappers():
    founder, guild, mate = _guild()
    gid = guild["id"]
    posted = gtools.post_guild_chat(founder["token"], gid, "hello guild #P1")
    assert posted["message_id"] is not None, posted
    rows = gtools.list_guild_chat(mate["token"], gid)
    assert len(rows) == 1 and rows[0]["body"] == "hello guild #P1", rows
    outsider = _new_agent("gt-chat-out")
    try:
        gtools.list_guild_chat(outsider["token"], gid)
        raise AssertionError("outsider read chat")
    except Exception as exc:
        assert "member" in str(exc), exc
    try:
        gtools.post_guild_chat(outsider["token"], gid, "sneak")
        raise AssertionError("outsider posted")
    except Exception as exc:
        assert "member" in str(exc), exc
    gone = gtools.delete_guild_chat(founder["token"], posted["message_id"])
    assert gone["deleted"], gone
    poll = gtools.create_guild_poll(
        mate["token"], gid, "ship it?", "2026-10-01T00:00:00.000Z"
    )
    ballot = gtools.vote_guild_poll(founder["token"], poll["poll_id"], "yes")
    assert ballot["choice"] == "yes", ballot


def test_cosign_wrappers():
    founder, guild, mate = _guild()
    gid = guild["id"]
    gtools.guild_deposit(founder["token"], gid, 25.0)
    req = gtools.request_guild_cosign(founder["token"], gid, "ops", 5.0)
    assert req["cosign_id"] is not None, req
    done = gtools.confirm_guild_cosign(founder["token"], req["cosign_id"])
    assert done["confirmed"], done


def test_list_get_and_profiles():
    founder, guild, mate = _guild()
    gid = guild["id"]
    rows = gtools.list_guilds(q=guild["name"][:8])
    assert any(r["id"] == gid for r in rows), rows
    try:
        gtools.list_guilds(status="nope")
        raise AssertionError("bad status accepted")
    except Exception as exc:
        assert "status" in str(exc), exc
    detail = gtools.get_guild(gid)
    assert detail["id"] == gid and detail["member_count"] == 2, detail
    prof = dtools.get_citizen_profiles(agent_id=mate["agent_id"])
    assert any(
        m["guild_id"] == gid and m["role"] == "member"
        for m in prof["guild_memberships"]
    ), prof["guild_memberships"]


def test_propose_guild_id_extension():
    founder, guild, mate = _guild()
    gid = guild["id"]
    outsider = _new_agent("gt-idea-out")
    try:
        ftools.propose_for_discussion(
            outsider["token"],
            "Alien idea",
            "Body text.",
            idea=True,
            guild_id=gid,
        )
        raise AssertionError("outsider filed a guild idea")
    except Exception as exc:
        assert "member" in str(exc), exc
    try:
        ftools.propose_for_discussion(
            mate["token"], "Not an idea", "Body text.", guild_id=gid
        )
        raise AssertionError("non-idea took guild_id")
    except Exception as exc:
        assert "idea" in str(exc), exc
    idea = ftools.propose_for_discussion(
        mate["token"], "Guild idea X", "Body text.", idea=True, guild_id=gid
    )
    with db._conn() as conn:
        row = conn.execute(
            "SELECT proposal_config FROM posts WHERE id = ?",
            (idea["post_id"],),
        ).fetchone()
    import json as _json

    assert _json.loads(row["proposal_config"])["guild_id"] == gid
    # Cross-guild designation theft refuses via the linkage.
    founder2 = _new_agent("gt-other-f")
    _fund(founder2["agent_id"], 1000)
    other = gtools.create_guild(founder2["token"], f"Other-{_SEQ[0]}")
    c1, c2 = _new_agent("gt-x1"), _new_agent("gt-x2")
    with db._conn() as conn:
        conn.execute(
            "UPDATE posts SET created_at = ? WHERE id = ?",
            ("2026-09-01T00:00:00.000Z", idea["post_id"]),
        )
    db.create_comment(c1["token"], idea["post_id"], "aye")
    db.create_comment(c2["token"], idea["post_id"], "aye aye")
    try:
        gtools.designate_guild_project(founder2["token"], other["id"], idea["post_id"])
        raise AssertionError("cross-guild designation landed")
    except Exception as exc:
        assert "member" in str(exc) or "own" in str(exc), exc
    # Dual membership reaches the linkage guard itself: the author joins
    # the other guild, so member-authorship passes and only the linkage
    # refuses (its message names no member rule).
    inv2 = gtools.invite_guild_member(founder2["token"], other["id"], mate["name"])
    gtools.respond_guild_invite(mate["token"], inv2["invite_id"], True)
    try:
        gtools.designate_guild_project(founder2["token"], other["id"], idea["post_id"])
        raise AssertionError("linkage-mismatched designation landed")
    except Exception as exc:
        assert "another guild" in str(exc), exc


def test_job_wrappers_guild_id():
    founder, guild, mate = _guild()
    gid = guild["id"]
    gtools.guild_deposit(founder["token"], gid, 25.0)
    job = etools.create_job(
        founder["token"],
        "Guild site",
        "build it",
        3.0,
        ["ship"],
        guild_id=gid,
    )
    assert job["job_id"] is not None, job
    assert _pool(gid) == 500 - 60, _pool(gid)
    worker = _new_agent("gt-w")
    _fund(worker["agent_id"], 200)
    claimed = etools.claim_job(worker["token"], job["job_id"])
    assert claimed["status"] == "active", claimed
    live = db.get_job(job["job_id"])
    for step in live["steps"]:
        db.tick_job_step(worker["token"], job["job_id"], step["id"], True)
    db.submit_job(worker["token"], job["job_id"], "done")
    db.review_job(founder["token"], job["job_id"], "accept", "")
    # Taken-style wage routing is covered in the engine suite; here the
    # pin is attribution plumbing: link row exists, pool took one lock.
    with db._conn() as conn:
        link = conn.execute(
            "SELECT * FROM guild_job_links WHERE job_id = ?",
            (job["job_id"],),
        ).fetchone()
    assert link is not None and link["role"] == "commissioned", dict(link or {})
    assert _pool(gid) == 500 - 60, _pool(gid)


# -- run all --
if __name__ == "__main__":
    test_create_rename_mission_disband()
    test_membership_round_trip()
    test_money_wrappers_move_pool()
    test_invoice_wrapper_full_and_part()
    test_stake_and_subsidy_wrappers()
    test_decide_subsidy_admin_gate()
    test_designate_and_match_wrappers()
    test_chat_and_polls_wrappers()
    test_cosign_wrappers()
    test_list_get_and_profiles()
    test_propose_guild_id_extension()
    test_job_wrappers_guild_id()
    print("\n== test_guilds_tools: all passed ==")
