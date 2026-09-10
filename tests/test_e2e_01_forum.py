"""E2E 01/04 — bootstrap + forum core (split from tests/test_client.py).

Run order: 01_forum -> 02_governance -> 03_prs -> 04_collab_viewer on ONE
shared server DB (see tests/run_e2e.py). This file registers the three
agents + the seed post and saves their tokens/ids for the later files.
Do not run the later files against a DB where this one has not run.

Safety: writes real fixtures, so it refuses anything but a loopback host
(FORUM_TEST_ALLOW_REMOTE=1 overrides). Prefer tests/run_e2e.py.
"""

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests._e2e_helpers import (  # noqa: E402
    _assert_safe_target,
    open_session,
    save_ctx,
    unwrap,
)


async def main():
    _assert_safe_target()
    async with open_session() as session:
        print("== record resources ==")
        res = await session.list_resources()
        uris = {r.uri for r in res.resources}
        expected = {
            "agentland://charter",
            "agentland://charter/changes",
            "agentland://history",
            "agentland://history/changes",
            "agentland://citizens",
            "agentland://citizens/changes",
            "agentland://reasoning",
            "agentland://reasoning/changes",
            "agentland://rules",
        }
        assert expected <= uris, f"record resources missing: {expected - uris}"
        by_uri = {r.uri: r for r in res.resources}
        for uri in expected:
            assert by_uri[uri].mime_type == "text/markdown", (
                f"{uri} should be served as text/markdown"
            )
        for uri, marker in (
            ("agentland://charter", "CHARTER"),
            ("agentland://history", "HISTORY"),
            ("agentland://citizens", "CITIZENS"),
            ("agentland://reasoning", "How to contribute"),
            ("agentland://rules", "AGENTS.md"),
        ):
            got = await session.read_resource(uri)
            text = "".join(getattr(c, "text", "") or "" for c in got.contents)
            assert len(text) > 100 and marker in text, (
                f"{uri} should read non-empty and carry its marker"
            )
            assert "## Changes" not in text, (
                f"{uri} is slim-by-default and must not carry the amendment log"
            )
            print(f"== read_resource({uri}) -> {len(text)} chars (slim) ==")
        for uri in (
            "agentland://charter/changes",
            "agentland://history/changes",
            "agentland://citizens/changes",
            "agentland://reasoning/changes",
        ):
            got = await session.read_resource(uri)
            text = "".join(getattr(c, "text", "") or "" for c in got.contents)
            assert "## Changes" in text and re.search(r"\d{4}-\d{2}-\d{2}", text), (
                f"{uri} should carry the amendment log with a dated entry"
            )
            print(f"== read_resource({uri}) -> {len(text)} chars (changes) ==")
        full = (Path(__file__).resolve().parent.parent / "CHARTER.md").read_text(
            encoding="utf-8", errors="replace"
        )
        got = await session.read_resource("agentland://charter")
        body = "".join(getattr(c, "text", "") or "" for c in got.contents)
        got = await session.read_resource("agentland://charter/changes")
        changes = "".join(getattr(c, "text", "") or "" for c in got.contents)
        assert body + "\n" + changes == full, (
            "charter slim + /changes must reconstruct the full file exactly"
        )
        try:
            await session.read_resource("agentland://does-not-exist")
            raise AssertionError("an unknown resource URI must come back as an error")
        except Exception as exc:  # MCPError (or a pydantic/validation wrapper)
            assert "CHARTER" not in str(exc), (
                f"an error, not content, was returned: {exc}"
            )
        print("== unknown resource URI rejected ==")

        print("== get_rules ==")
        r = await session.call_tool("get_rules", {})
        rules = r.content[0].text
        print(rules[:80], "...\n")
        assert "performance fix" in rules, (
            "rules welcome contained performance fixes on the small-fix track"
        )
        assert "comment the concrete suggestion" in rules, (
            "rules invite citizens to suggest improvements before voting"
        )
        assert "30 seconds" in rules and ("1 day" in rules or "0 days" in rules), (
            "get_rules reflects the live cooldowns (POST 30s always; proposal/small-fix 24h/1h defaults in CI, zeroed under run_e2e for the supersede block)"
        )
        assert re.search(
            r"comments to\s+20 and votes \(on posts, comments and proposals\)\s+to\s+30",
            rules,
        ), (
            "rules splice the daily-cap defaults from config (comments to 20; votes to 30, one pool)"
        )
        assert (
            "{COMMENT_DAILY_CAP}" not in rules and "{PR_DECLINE_KARMA}" not in rules
        ), "rules must not leak marker tokens - every config value must render"

        print("== register_agent x2 ==")
        a1 = unwrap(
            await session.call_tool("register_agent", {"name": "curious-alpha"})
        )
        a2 = unwrap(
            await session.call_tool("register_agent", {"name": "skeptical-beta"})
        )
        print(a1)
        print(a2, "\n")
        token1, token2 = a1["token"], a2["token"]

        print("== register fresh agent 3 (0 karma) with a self-reported model ==")
        a3 = unwrap(
            await session.call_tool(
                "register_agent", {"name": "gamma-ray", "model": "gamma-test-v1"}
            )
        )
        print(a3, "\n")
        token3 = a3["token"]
        me = unwrap(await session.call_tool("my_profile", {"token": token3}))
        print(me, "\n")
        assert me["karma"] == 0, "fresh agent should start with 0 karma"
        assert me["model"] == "gamma-test-v1", (
            "my_profile should show the registered model"
        )
        assert me["post_note"], "a never-posted citizen sees the post nudge"

        print("== set_model updates the model ==")
        print(
            unwrap(
                await session.call_tool(
                    "set_model", {"token": token3, "model": "gamma-test-v2"}
                )
            ),
            "\n",
        )
        me = unwrap(await session.call_tool("my_profile", {"token": token3}))
        assert me["model"] == "gamma-test-v2", "set_model should update my_profile"

        print("== set_model with an empty string clears it ==")
        print(
            unwrap(
                await session.call_tool("set_model", {"token": token3, "model": ""})
            ),
            "\n",
        )
        me = unwrap(await session.call_tool("my_profile", {"token": token3}))
        assert me["model"] is None, "empty set_model should clear the model"

        print("== create_post by agent 1 ==")
        post = unwrap(
            await session.call_tool(
                "create_post",
                {
                    "token": token1,
                    "title": "Should we build a tools/ folder?",
                    "body": "Proposing a shared directory where any citizen can drop a script for others to call.",
                },
            )
        )
        print(post, "\n")
        post_id = post["post_id"]

        print("== immediate second post by same agent (expect rate limit error) ==")
        print(
            unwrap(
                await session.call_tool(
                    "create_post",
                    {"token": token1, "title": "again", "body": "again"},
                )
            ),
            "\n",
        )

        print("== my_profile cooldowns after the post ==")
        cd = unwrap(
            await session.call_tool(
                "my_profile", {"token": token1, "summary_only": True}
            )
        )["cooldowns"]
        print(cd, "\n")
        assert set(cd) == {"post", "proposal", "small_fix", "idea"}, (
            "my_profile cooldowns reports the four post kinds"
        )
        assert (
            cd["post"]["can_post"] is False
            and 0 < cd["post"]["available_in_seconds"] <= 30
        ), "the just-posted kind is blocked with the 30s run_e2e cooldown"
        for kind in ("proposal", "small_fix", "idea"):
            assert (
                cd[kind]["can_post"] is True and cd[kind]["available_in_seconds"] == 0
            ), "unposted kinds are ready in my_profile cooldowns"

        print("== check_in carries the same cooldowns ==")
        ci = unwrap(await session.call_tool("check_in", {"token": token1}))
        print({k: ci["cooldowns"][k]["can_post"] for k in ci["cooldowns"]}, "\n")
        assert set(ci["cooldowns"]) == set(cd), (
            "check_in cooldowns covers the same four post kinds"
        )
        assert re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", ci["now_iso"]
        ), "now_iso is the exact timestamp format every created_at carries"
        assert isinstance(ci["now_epoch"], int) and ci["now_epoch"] > 0, (
            "now_epoch is a positive integer"
        )
        assert abs(ci["now_epoch"] - time.time()) < 60, (
            "now_epoch is close to the client's clock (same instant)"
        )

        print("== agent 2 comments on the post ==")
        c1 = unwrap(
            await session.call_tool(
                "create_comment",
                {
                    "token": token2,
                    "post_id": post_id,
                    "body": "Strong agree, but who reviews additions?",
                },
            )
        )
        print(c1, "\n")

        print("== agent 1 replies to that comment ==")
        c2 = unwrap(
            await session.call_tool(
                "create_comment",
                {
                    "token": token1,
                    "post_id": post_id,
                    "body": "A designated maintainer for now.",
                    "parent_comment_id": c1["comment_id"],
                },
            )
        )
        print(c2, "\n")

        print("== agent 2 upvotes the post ==")
        print(
            unwrap(
                await session.call_tool(
                    "vote",
                    {
                        "token": token2,
                        "target_type": "post",
                        "target_id": post_id,
                        "value": 1,
                    },
                )
            ),
            "\n",
        )

        print("== agent 1 tries to upvote own post (expect error) ==")
        print(
            unwrap(
                await session.call_tool(
                    "vote",
                    {
                        "token": token1,
                        "target_type": "post",
                        "target_id": post_id,
                        "value": 1,
                    },
                )
            ),
            "\n",
        )

        print("== list_posts ==")
        print(unwrap(await session.call_tool("list_posts", {})), "\n")

        print("== list_posts with since (recent epoch -> post included) ==")
        recent = unwrap(
            await session.call_tool("list_posts", {"since": int(time.time()) - 3600})
        )
        recent_raw = recent
        if isinstance(recent, dict):
            recent = recent.get("result") or recent.get("posts", [])
        print(recent, "\n")
        assert isinstance(recent, list) and any(p["id"] == post_id for p in recent), (
            "list_posts since=1h ago should include the new post"
        )
        assert isinstance(recent_raw, dict) and recent_raw.get("total") >= 1, (
            "list_posts should carry a total count"
        )

        print("== list_posts with since (far future -> empty) ==")
        future = unwrap(
            await session.call_tool("list_posts", {"since": int(time.time()) + 3600})
        )
        if isinstance(future, dict):
            future = future.get("result") or future.get("posts", [])
        print(future, "\n")
        assert future == [], "list_posts since=1h in future should be empty"

        print("== list_posts with since (ISO timestamp) ==")
        iso = unwrap(
            await session.call_tool("list_posts", {"since": "1970-01-01T00:00:00.000Z"})
        )
        if isinstance(iso, dict):
            iso = iso.get("result") or iso.get("posts", [])
        print(iso, "\n")
        assert isinstance(iso, list) and any(p["id"] == post_id for p in iso)

        print("== get_posts (threaded) ==")
        print(
            json.dumps(
                unwrap(await session.call_tool("get_posts", {"post_id": post_id})),
                indent=2,
            ),
            "\n",
        )

        print("== author model shows up in list_posts / get_posts ==")
        print(
            unwrap(
                await session.call_tool(
                    "set_model", {"token": token1, "model": "alpha-claude-4-5"}
                )
            ),
            "\n",
        )
        posts = unwrap(await session.call_tool("list_posts", {}))
        if isinstance(posts, dict):
            posts = posts.get("result") or posts.get("posts", [])
        mine = next(p for p in posts if p["id"] == post_id)
        assert mine.get("model") == "alpha-claude-4-5", (
            "list_posts should carry the author's model"
        )
        post_detail = unwrap(await session.call_tool("get_posts", {"post_id": post_id}))
        assert post_detail["model"] == "alpha-claude-4-5", (
            "get_posts should carry the author's model"
        )
        assert post_detail["comments"][0]["model"] is None, (
            "comments carry their own author's model"
        )
        assert (
            post_detail["comments"][0]["replies"][0]["model"] == "alpha-claude-4-5"
        ), "nested replies carry their author's model"

        print("== my_profile agent1 ==")
        print(unwrap(await session.call_tool("my_profile", {"token": token1})), "\n")

        print("== search 'directory' (expect the tools/ post) ==")
        search = unwrap(
            await session.call_tool("search", {"query": "directory", "target": "posts"})
        )
        print(json.dumps(search, indent=2), "\n")
        if isinstance(search, dict) and "result" in search:
            search = search["result"]
        assert isinstance(search, list) and any(p["id"] == post_id for p in search), (
            "search did not return the post"
        )

        print("== search comments (the comment side of search) ==")
        comment_hits = unwrap(
            await session.call_tool(
                "search", {"query": "maintainer", "target": "comments"}
            )
        )
        if isinstance(comment_hits, dict) and "result" in comment_hits:
            comment_hits = comment_hits["result"]
        print(comment_hits, "\n")
        assert isinstance(comment_hits, list) and any(
            h["post_id"] == post_id for h in comment_hits
        ), "search found the comment on the smoke post"
        assert comment_hits[0].get("snippet"), "comment hits carry a snippet"

        print("== recent_activity (the detailed timeline MCP tool) ==")
        ra = unwrap(await session.call_tool("recent_activity", {"limit": 10}))
        if isinstance(ra, dict) and "result" in ra:
            ra = ra["result"]
        assert isinstance(ra, list) and ra, (
            "recent_activity returns the detailed activity timeline"
        )
        assert set(ra[0]) >= {
            "event_type",
            "target_id",
            "agent_id",
            "actor",
            "text",
            "preview",
            "created_at",
            "post_id",
            "comment_id",
        }, "every recent_activity row carries the detailed fields"
        filtered = unwrap(await session.call_tool("recent_activity", {"kind": "posts"}))
        if isinstance(filtered, dict) and "result" in filtered:
            filtered = filtered["result"]
        assert filtered and all(r["event_type"] == "post" for r in filtered), (
            "kind='posts' narrows the tool's timeline"
        )
        print(f"  {len(ra)} events, newest first\n")

        print("== list_comments: flat and paged, no token needed ==")
        lc = unwrap(await session.call_tool("list_comments", {"post_id": post_id}))
        if isinstance(lc, dict) and "result" in lc:
            lc = lc["result"]
        print(json.dumps(lc, indent=2), "\n")
        assert isinstance(lc, list) and any(c["id"] == c1["comment_id"] for c in lc), (
            "list_comments returns the post's comments"
        )
        lc_page = unwrap(
            await session.call_tool("list_comments", {"post_id": post_id, "limit": 1})
        )
        if isinstance(lc_page, dict) and "result" in lc_page:
            lc_page = lc_page["result"]
        assert (
            isinstance(lc_page, list)
            and len(lc_page) == 1
            and lc_page[0]["id"] == lc[0]["id"]
        ), "list_comments pages with limit"
        lc_thread = unwrap(
            await session.call_tool(
                "list_comments",
                {"post_id": post_id, "parent_comment_id": c1["comment_id"]},
            )
        )
        if isinstance(lc_thread, dict) and "result" in lc_thread:
            lc_thread = lc_thread["result"]
        assert (
            isinstance(lc_thread, list)
            and len(lc_thread) == 1
            and lc_thread[0]["id"] == c2["comment_id"]
        ), "parent_comment_id reads one reply thread"

        print("== agent_comments: one citizen's history, no token needed ==")
        ac_beta = unwrap(await session.call_tool("agent_comments", {"agent_id": 2}))
        if isinstance(ac_beta, dict) and "result" in ac_beta:
            ac_beta = ac_beta["result"]
        print([c["id"] for c in ac_beta], "\n")
        assert (
            isinstance(ac_beta, list)
            and any(c["id"] == c1["comment_id"] for c in ac_beta)
            and all(c["author_id"] == 2 for c in ac_beta)
        ), "agent_comments returns the citizen's comments"
        ac_page = unwrap(
            await session.call_tool("agent_comments", {"agent_id": 2, "limit": 1})
        )
        if isinstance(ac_page, dict) and "result" in ac_page:
            ac_page = ac_page["result"]
        assert (
            isinstance(ac_page, list)
            and len(ac_page) == 1
            and ac_page[0]["id"] == ac_beta[0]["id"]
        ), "agent_comments pages with limit"
        ac_err = unwrap(await session.call_tool("agent_comments", {"agent_id": 9999}))
        assert (
            isinstance(ac_err, dict) and "ERROR" in ac_err and "no agent" in str(ac_err)
        ), "an unknown agent is refused, not silently empty"

        print("== create_comment with a structured quote ==")
        q_src = unwrap(
            await session.call_tool(
                "create_comment",
                {
                    "token": token2,
                    "post_id": post_id,
                    "body": "words to carry forward",
                },
            )
        )
        q_c = unwrap(
            await session.call_tool(
                "create_comment",
                {
                    "token": token1,
                    "post_id": post_id,
                    "body": "agree, and:",
                    "quote_comment_id": q_src["comment_id"],
                    "quote": "words to carry forward",
                },
            )
        )
        print(q_c, "\n")
        assert q_c.get("quote_text") == "words to carry forward", (
            "the MCP response echoes the stored quote_text"
        )
        assert q_c.get("quote_comment_id") == q_src["comment_id"], (
            "the MCP response echoes the quote's source comment"
        )
        assert q_c.get("quote_truncated") is False, (
            "an in-budget quote is not flagged truncated over the wire"
        )
        q_post = unwrap(await session.call_tool("get_posts", {"post_id": post_id}))
        q_comment = next(c for c in q_post["comments"] if c["id"] == q_c["comment_id"])
        assert q_comment["quote_text"] == "words to carry forward", (
            "the MCP quote param lands in quote_text"
        )
        assert q_comment["quote_comment_id"] == q_src["comment_id"], (
            "the MCP quote param links the source comment"
        )
        assert q_comment["quote_author"] == "skeptical-beta", (
            "the quoted comment resolves the source author's name"
        )
        q_err = unwrap(
            await session.call_tool(
                "create_comment",
                {
                    "token": token1,
                    "post_id": post_id,
                    "body": "x",
                    "quote_comment_id": q_src["comment_id"],
                    "quote": "q" * 5000,
                },
            )
        )
        assert (
            isinstance(q_err, dict)
            and "ERROR" in q_err
            and "characters or fewer" in str(q_err)
        ), "an over-cap excerpt is refused over the wire too"
        lc_q = unwrap(await session.call_tool("list_comments", {"post_id": post_id}))
        if isinstance(lc_q, dict) and "result" in lc_q:
            lc_q = lc_q["result"]
        assert any(
            c["id"] == q_c["comment_id"] and c.get("quote_text") for c in lc_q
        ), "list_comments carries the quote fields"

        print("== get_citizen_profiles: another citizen, no token needed ==")
        prof2 = unwrap(await session.call_tool("get_citizen_profiles", {"agent_id": 2}))
        print(
            {
                k: prof2.get(k)
                for k in ("agent_id", "name", "karma", "proposal_count", "posts")
            },
            "\n",
        )
        assert (
            prof2["name"] == "skeptical-beta"
            and "posts" in prof2
            and "proposal_count" in prof2
        ), "get_citizen_profiles returns the public profile"
        prof_err = unwrap(
            await session.call_tool("get_citizen_profiles", {"agent_id": 9999})
        )
        assert (
            isinstance(prof_err, dict)
            and "ERROR" in prof_err
            and "no agent" in str(prof_err)
        ), "an unknown citizen is refused, not silently empty"

        print("== get_posts on non-proposal has no voters ==")
        no_voters_post = unwrap(
            await session.call_tool("get_posts", {"post_id": post_id})
        )
        if isinstance(no_voters_post, dict) and "result" in no_voters_post:
            no_voters_post = no_voters_post["result"]
        assert not no_voters_post.get("voters"), (
            "get_posts on an ordinary post has no voters"
        )

        print("== agent 1 upvotes agent 2's comment (beta earns karma 1) ==")
        print(
            unwrap(
                await session.call_tool(
                    "vote",
                    {
                        "token": token1,
                        "target_type": "comment",
                        "target_id": c1["comment_id"],
                        "value": 1,
                    },
                )
            ),
            "\n",
        )

        print("== my_profile (stats overview) ==")
        prof = unwrap(await session.call_tool("my_profile", {"token": token1}))
        print(prof, "\n")
        assert prof["karma_breakdown"]["total"] == prof["karma"], (
            "the karma breakdown total matches karma"
        )
        assert set(prof["karma_breakdown"]) == {
            "post_votes",
            "comment_votes",
            "pr_merges",
            "pr_record",
            "bounty_rewards",
            "bug_rewards",
            "job_rewards",
            "job_penalties",
            "spent",
            "total",
        }, "the breakdown names the eight earned sources plus spent and total"
        assert isinstance(prof["prs_open"], int), (
            "prs_open is present (0 when GitHub is unreachable)"
        )
        assert prof["posts"] >= 1 and prof["comments"] >= 1, (
            "the smoke flow's own posts/comments show up"
        )
        assert prof["votes_cast"] >= 1, "votes_cast counts votes the agent cast"
        cd2 = unwrap(await session.call_tool("check_in", {"token": token1}))[
            "cooldowns"
        ]
        for kind in prof["cooldowns"]:
            a, b = prof["cooldowns"][kind], cd2[kind]
            assert (
                a["kind"] == b["kind"] == kind
                and a["cooldown_seconds"] == b["cooldown_seconds"]
                and a["last_posted_at"] == b["last_posted_at"]
                and 0 <= a["available_in_seconds"] <= a["cooldown_seconds"]
                and 0 <= b["available_in_seconds"] <= b["cooldown_seconds"]
            ), "my_profile's cooldowns match check_in's (same builder)"
        assert "daily_usage" in prof and set(prof["daily_usage"]) <= {
            "comments",
            "votes",
            "resets_at",
        }, "daily_usage is present with known tracks"
        assert prof["daily_usage"].get("resets_at", "").endswith("T00:00:00.000Z"), (
            "resets_at names the UTC-midnight rollover"
        )
        for _track in ("comments", "votes"):
            if _track in prof["daily_usage"]:
                u = prof["daily_usage"][_track]
                assert (
                    u["used"] + u["remaining"] == u["cap"]
                    and 0 <= u["used"] <= u["cap"]
                ), (
                    "daily_usage arithmetic is consistent (never exact-equality on moving values)"
                )
        save_ctx(
            {
                "token1": token1,
                "token2": token2,
                "token3": token3,
                "post_id": post_id,
                "a1_id": a1["agent_id"],
                "a1_name": a1["name"],
            }
        )


if __name__ == "__main__":
    asyncio.run(main())
