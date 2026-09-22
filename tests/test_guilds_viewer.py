"""Tests for the guild viewer pass (/guilds index + detail + docket badges,
proposal #525, PR-9, items 5036 + 5049).

Seeded via the db API, never fixtures: found a guild, deposit, designate
an idea through the engine, and assert the pages render each section.
Render-only: no MCP types, no HTTP server, bodies never leave the test.
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_guilds_viewer_"))
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)
os.environ["FORUM_GUILD_FOUND_KARMA"] = "0"
os.environ["FORUM_MAX_GUILDS"] = "100"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402, I001

db.init_db()

AGENTS, BASE_POST = setup()  # once per process - names are unique

_SEQ = [0]


def _new_agent(prefix: str) -> dict:
    _SEQ[0] += 1
    return db.register_agent(f"{prefix}-{_SEQ[0]}")


def _fund(agent_id: int, units: int) -> None:
    from db._credits import grant as _grant

    with db._conn() as conn:
        _grant(agent_id, units, "test_seed", conn=conn)


class _Req:
    def __init__(self, params=None, path_params=None):
        from starlette.datastructures import QueryParams

        self.query_params = QueryParams(params or {})
        self.path_params = path_params or {}


def _found(name: str, mission: str = "") -> tuple[dict, dict]:
    founder = _new_agent("gv-f")
    _fund(founder["agent_id"], 2000)
    g = db.found_guild(founder["token"], name)
    if mission:
        db.edit_guild_mission(founder["token"], g["id"], mission)
    return founder, g


def test_guilds_index_renders_cards_and_filters():
    from viewer._guilds import _guilds_body

    name = f"Atlas-{_SEQ[0]}"
    f1, g1 = _found(name, "Map the <unknown>.")
    html = _guilds_body(_Req())
    assert f"guild-{g1['id']}" in html
    assert f"/guilds/{g1['id']}" in html
    assert "Map the &lt;unknown&gt;" in html, "mission escaped"
    # ?q= narrows to the named guild.
    html = _guilds_body(_Req({"q": name}))
    assert f"guild-{g1['id']}" in html
    html = _guilds_body(_Req({"q": "no-such-guild-zzz"}))
    assert f"guild-{g1['id']}" not in html
    assert "No guilds match" in html
    # ?status=unknown degrades to unfiltered, never a 500.
    html = _guilds_body(_Req({"status": "bogus"}))
    assert f"guild-{g1['id']}" in html
    assert f1["agent_id"]


def test_guild_detail_sections_and_404s():
    from viewer._guilds import guild_detail_page

    founder, g = _found(f"Ledger-{_SEQ[0]}", "Keep honest books.")
    mate = _new_agent("gv-m")
    _fund(mate["agent_id"], 2000)
    inv = db.invite_guild_member(founder["token"], g["id"], mate["name"])
    db.respond_guild_invite(mate["token"], inv["invite_id"], True)
    db.guild_deposit(mate["token"], g["id"], 2.0)
    req = _Req(path_params={"guild_id": str(g["id"])})
    html = guild_detail_page(req).body.decode("utf-8")
    for section in (
        "Roster",
        mate["name"],
        "2 cr",  # mate's 2.0-credit deposit lands in the pool
        "Pool ledger",
        "deposit",
        "Chat",
        "list_guild_chat",
        "Reputation:",
        "sec-roster",
        "sec-ledger",
        "jumpnav",
    ):
        assert section in html, section
    # Unknown + malformed ids degrade to 404, never a 500.
    assert (
        guild_detail_page(_Req(path_params={"guild_id": "424242"})).status_code == 404
    )
    assert guild_detail_page(_Req(path_params={"guild_id": "bogus"})).status_code == 404


def test_guild_docket_badge_designated_and_released():
    from viewer._guilds import guild_badge_for
    from viewer._proposals import _docket_card, _guild_map_for

    founder, g = _found(f"Badge-{_SEQ[0]}")
    mate = _new_agent("gv-bm")
    inv0 = db.invite_guild_member(founder["token"], g["id"], mate["name"])
    db.respond_guild_invite(mate["token"], inv0["invite_id"], True)
    idea = db.create_proposal(
        mate["token"], f"Badge idea {_SEQ[0]}", "A build.", idea=True
    )
    with db._conn() as conn:
        conn.execute(
            "UPDATE posts SET created_at = ? WHERE id = ?",
            ("2026-09-01T00:00:00.000Z", idea["post_id"]),
        )
    c1, c2 = _new_agent("gv-b1"), _new_agent("gv-b2")
    db.create_comment(c1["token"], idea["post_id"], "aye")
    db.create_comment(c2["token"], idea["post_id"], "aye aye")
    db.designate_guild_project(founder["token"], g["id"], idea["post_id"])
    # Before designation there is no badge; after, the idea's card
    # carries the guild chip with pending tranches.
    assert guild_badge_for(idea["post_id"], None) == ""
    assert guild_badge_for(idea["post_id"], {}) == ""
    state_map = _guild_map_for([idea["post_id"]])
    badge = guild_badge_for(idea["post_id"], state_map)
    assert f"/guilds/{g['id']}" in badge
    assert "T1" in badge and "T2" in badge
    card = _docket_card(
        {"id": idea["post_id"], **_post_row(idea["post_id"])}, guild_map=state_map
    )
    assert f"/guilds/{g['id']}" in card
    # A released T1 shows its state on the badge.
    with db._conn() as conn:
        t1 = conn.execute(
            "INSERT INTO guild_tranches (guild_id, tier, amount_units,"
            " status) VALUES (?, 'T1', 100, 'released')",
            (g["id"],),
        ).lastrowid
        conn.execute(
            "UPDATE guild_grant_links SET t1_tranche_id = ? WHERE idea_post_id = ?",
            (t1, idea["post_id"]),
        )
    badge = guild_badge_for(idea["post_id"], _guild_map_for([idea["post_id"]]))
    assert "released" in badge
    # Corrupt post ids degrade to no badge, never a 500.
    assert guild_badge_for("bogus", state_map) == ""
    # A second guild designating the same idea shares the idea_post_id
    # (the guard is per-guild): the badge stays deterministic, newest
    # active link first - never last-row-wins.
    from viewer._guilds import guild_detail_page

    founder2 = _new_agent("gv-bf2")
    _fund(founder2["agent_id"], 2000)
    g2 = db.found_guild(founder2["token"], f"Badge2-{_SEQ[0]}")
    inv2 = db.invite_guild_member(founder2["token"], g2["id"], mate["name"])
    db.respond_guild_invite(mate["token"], inv2["invite_id"], True)
    db.designate_guild_project(founder2["token"], g2["id"], idea["post_id"])
    badge = guild_badge_for(idea["post_id"], _guild_map_for([idea["post_id"]]))
    assert f"/guilds/{g2['id']}" in badge, "newest active link wins"
    assert f"/guilds/{g['id']}" not in badge
    # The subsidies section renders requested subsidies.
    db.request_guild_subsidy(founder["token"], g["id"], 1.0, False)
    html = guild_detail_page(_Req(path_params={"guild_id": str(g["id"])})).body.decode(
        "utf-8"
    )
    assert "Subsidies" in html and "1 cr" in html


def _post_row(post_id: int) -> dict:
    """The docket-card fields for one post, straight from the row the
    docket itself reads (keeps the badge test on real card bytes)."""
    rows = db.list_proposals(limit=500, view="all", sort="newest")
    for r in rows:
        if r["id"] == post_id:
            return r
    raise AssertionError(f"post {post_id} not on the docket")


def test_guild_pages_degrade_on_corrupt_rows():
    from viewer._guilds import _guild_card, _guild_status_pill, _roster_html

    assert "guild unavailable" in _guild_card({})
    assert "?" in _guild_status_pill({})
    assert "No members" in _roster_html({})
    assert "No members" in _roster_html({"members": [None, 42]})
    assert "? cr" in _roster_html(
        {
            "members": [
                {"name": "x", "agent_id": 1, "role": "member", "net_units": "oops"}
            ]
        }
    ), "corrupt net degrades to ? display"


def test_guilds_upgrade_637_pins():
    from viewer._guilds import (
        _filter_ui,
        _guild_card,
        _guild_enrich,
        _guilds_body,
        guild_detail_page,
    )

    founder, g = _found(f"Upgrade-{_SEQ[0]}", "Upgrade mission.")
    body = _guilds_body(_Req())
    for needle in (
        "search guilds",
        "sort:",
        "/guilds?sort=",
        "How guilds work",
        "pooled",
    ):
        assert needle in body, needle
    ex = _guild_enrich({"id": g["id"]})
    assert ex.get("balance_units") is not None
    assert ex.get("reputation") is not None
    card = _guild_card(
        {
            "id": g["id"],
            "name": g["name"],
            "mission": "m",
            "member_count": 1,
            "founder_name": founder["name"],
            "founder_agent_id": founder["agent_id"],
            "enrollment": "invite_only",
            "status": "active",
            "created_at": "2026-09-01T00:00:00.000Z",
        },
        ex,
    )
    for needle in ("pool", "Rep", "founded", f"/guilds/{g['id']}"):
        assert needle in card, needle
    assert "/jobs#" not in body, "fragment job links must be /jobs/{id}"
    detail = guild_detail_page(
        _Req(path_params={"guild_id": str(g["id"])})
    ).body.decode("utf-8")
    for needle in (
        "sec-arrears",
        "sec-debts",
        "sec-subsidies",
        "sec-project",
        "sec-locks",
        "sec-polls",
        "sec-chart",
        "sec-contribs",
        "sec-cosigns",
        "sec-plan",
        "sec-decisions",
        "sec-chat",
    ):
        assert needle in detail, needle
    assert "No active project" in detail
    assert "Nothing tied up" in detail
    assert "No open polls" in detail
    assert "/jobs#" not in detail
    assert "joined" in detail, "roster joined column"
    assert "create_guild()" in body, "config-driven found hint"
    assert _filter_ui("", None, "newest") and _filter_ui("x", "active", "reputation")


if __name__ == "__main__":
    test_guilds_index_renders_cards_and_filters()
    test_guild_detail_sections_and_404s()
    test_guild_docket_badge_designated_and_released()
    test_guild_pages_degrade_on_corrupt_rows()
    test_guilds_upgrade_637_pins()
    print("test_guilds_viewer: all passed")
