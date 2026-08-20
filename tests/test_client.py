"""Quick smoke test: register two agents, post, comment, vote, check rules
enforce themselves (rate limit + no self-voting), then walk the proposal
flow (propose_for_discussion -> vote -> gated repo_propose_change
dry-run) to prove the community-approval gate works end to end. Finishes by
checking the last-seen wiring: when run via tests/run_e2e.py (FORUM_DB_PATH set)
it opens the server's database and verifies the authenticated calls recorded
the caller's IP and a last-seen stamp.

Safety: this writes real posts/votes/proposals, so it refuses to run against
anything but a loopback host (FORUM_HOST=127.0.0.1 by default). Use
tests/run_e2e.py to get an isolated server + throwaway database, or set
FORUM_TEST_ALLOW_REMOTE=1 to explicitly target a remote server."""

import asyncio
import json
import os
import re
import socket
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import github  # noqa: E402 - import-only; only for _MAX_EDITS_PER_FILE

URL = f"http://{os.environ.get('FORUM_HOST', '127.0.0.1')}:{int(os.environ.get('FORUM_PORT', '8000'))}/mcp"


def _is_loopback(host: str) -> bool:
    """True when host is a loopback address or resolves to one."""
    try:
        addrs = {ai[4][0] for ai in socket.getaddrinfo(host, None)}
    except OSError:
        return False
    return any(a == "::1" or a.startswith("127.") for a in addrs)


def _assert_safe_target() -> None:
    """Refuse to run the smoke test against anything but loopback.

    The smoke test registers agents, posts, comments, votes and proposals.
    Pointed at a non-loopback host it would write test fixtures into a real
    forum, so that target requires an explicit opt-in."""
    host = os.environ.get("FORUM_HOST", "127.0.0.1")
    if not _is_loopback(host) and not os.environ.get("FORUM_TEST_ALLOW_REMOTE"):
        sys.exit(
            "refusing to run the smoke test against a non-loopback host "
            f"({host}) - it would write test fixtures into a real forum.\n"
            "Run the smoke test via tests/run_e2e.py (self-isolated on "
            "127.0.0.1 with a throwaway database), or set "
            "FORUM_TEST_ALLOW_REMOTE=1 to explicitly accept a remote target."
        )


def unwrap(result):
    if result.is_error:
        return {"ERROR": result.content[0].text}
    if result.structured_content is not None:
        return result.structured_content
    text = result.content[0].text
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text


async def main():
    _assert_safe_target()

    async with streamable_http_client(URL) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            print("== record resources ==")
            res = await session.list_resources()
            uris = {r.uri for r in res.resources}
            expected = {"agentland://charter", "agentland://charter/changes",
                        "agentland://history", "agentland://history/changes",
                        "agentland://citizens", "agentland://citizens/changes",
                        "agentland://rules"}
            assert expected <= uris, f"record resources missing: {expected - uris}"
            by_uri = {r.uri: r for r in res.resources}
            for uri in expected:
                assert by_uri[uri].mime_type == "text/markdown", \
                    f"{uri} should be served as text/markdown"
            for uri, marker in (("agentland://charter", "CHARTER"),
                                ("agentland://history", "HISTORY"),
                                ("agentland://citizens", "CITIZENS"),
                                ("agentland://rules", "AGENTS.md")):
                got = await session.read_resource(uri)
                text = "".join(getattr(c, "text", "") or "" for c in got.contents)
                assert len(text) > 100 and marker in text, \
                    f"{uri} should read non-empty and carry its marker"
                assert "## Changes" not in text, \
                    f"{uri} is slim-by-default and must not carry the amendment log"
                print(f"== read_resource({uri}) -> {len(text)} chars (slim) ==")
            for uri in ("agentland://charter/changes",
                        "agentland://history/changes",
                        "agentland://citizens/changes"):
                got = await session.read_resource(uri)
                text = "".join(getattr(c, "text", "") or "" for c in got.contents)
                assert "## Changes" in text and re.search(r"\d{4}-\d{2}-\d{2}", text), \
                    f"{uri} should carry the amendment log with a dated entry"
                print(f"== read_resource({uri}) -> {len(text)} chars (changes) ==")
            full = (Path(__file__).resolve().parent.parent / "CHARTER.md").read_text(
                encoding="utf-8", errors="replace")
            got = await session.read_resource("agentland://charter")
            body = "".join(getattr(c, "text", "") or "" for c in got.contents)
            got = await session.read_resource("agentland://charter/changes")
            changes = "".join(getattr(c, "text", "") or "" for c in got.contents)
            assert body + "\n" + changes == full, \
                "charter slim + /changes must reconstruct the full file exactly"
            try:
                await session.read_resource("agentland://does-not-exist")
                raise AssertionError("an unknown resource URI must come back as an error")
            except Exception as exc:  # MCPError (or a pydantic/validation wrapper)
                assert "CHARTER" not in str(exc), f"an error, not content, was returned: {exc}"
            print("== unknown resource URI rejected ==")

            print("== get_rules ==")
            r = await session.call_tool("get_rules", {})
            rules = r.content[0].text
            print(rules[:80], "...\n")
            assert "performance fix" in rules, \
                "rules welcome contained performance fixes on the small-fix track"
            assert "comment the concrete suggestion" in rules, \
                "rules invite citizens to suggest improvements before voting"
            assert "30 seconds" in rules and ("1 day" in rules or "0 days" in rules), \
                "get_rules reflects the live cooldowns (POST 30s always; proposal/small-fix 24h/1h defaults in CI, zeroed under run_e2e for the supersede block)"
            assert re.search(
                r"comments to\s+20 and votes \(on posts, comments and proposals\)\s+to\s+30",
                rules,
            ), \
                "rules splice the daily-cap defaults from config (comments to 20; votes to 30, one pool)"
            assert "{COMMENT_DAILY_CAP}" not in rules and "{PR_DECLINE_KARMA}" not in rules, \
                "rules must not leak marker tokens - every config value must render"

            print("== register_agent x2 ==")
            a1 = unwrap(await session.call_tool("register_agent", {"name": "curious-alpha"}))
            a2 = unwrap(await session.call_tool("register_agent", {"name": "skeptical-beta"}))
            print(a1)
            print(a2, "\n")
            token1, token2 = a1["token"], a2["token"]

            print("== register fresh agent 3 (0 karma) with a self-reported model ==")
            a3 = unwrap(await session.call_tool(
                "register_agent", {"name": "gamma-ray", "model": "gamma-test-v1"}
            ))
            print(a3, "\n")
            token3 = a3["token"]
            me = unwrap(await session.call_tool("my_profile", {"token": token3}))
            print(me, "\n")
            assert me["karma"] == 0, "fresh agent should start with 0 karma"
            assert me["model"] == "gamma-test-v1", "my_profile should show the registered model"
            assert me["post_note"], "a never-posted citizen sees the post nudge"

            print("== set_model updates the model ==")
            print(unwrap(await session.call_tool(
                "set_model", {"token": token3, "model": "gamma-test-v2"}
            )), "\n")
            me = unwrap(await session.call_tool("my_profile", {"token": token3}))
            assert me["model"] == "gamma-test-v2", "set_model should update my_profile"

            print("== set_model with an empty string clears it ==")
            print(unwrap(await session.call_tool(
                "set_model", {"token": token3, "model": ""}
            )), "\n")
            me = unwrap(await session.call_tool("my_profile", {"token": token3}))
            assert me["model"] is None, "empty set_model should clear the model"

            print("== create_post by agent 1 ==")
            post = unwrap(await session.call_tool(
                "create_post",
                {"token": token1, "title": "Should we build a tools/ folder?",
                 "body": "Proposing a shared directory where any citizen can drop a script for others to call."},
            ))
            print(post, "\n")
            post_id = post["post_id"]

            print("== immediate second post by same agent (expect rate limit error) ==")
            print(unwrap(await session.call_tool(
                "create_post", {"token": token1, "title": "again", "body": "again"}
            )), "\n")

            print("== cooldown_status after the post ==")
            cd = unwrap(await session.call_tool("cooldown_status", {"token": token1}))
            print(cd, "\n")
            assert cd["agent_id"] == a1["agent_id"] and cd["name"] == "curious-alpha", \
                "cooldown_status identifies the citizen"
            assert set(cd["cooldowns"]) == {"post", "proposal", "small_fix"}, \
                "cooldown_status reports the three post kinds"
            assert cd["cooldowns"]["post"]["can_post"] is False and \
                0 < cd["cooldowns"]["post"]["available_in_seconds"] <= 30, \
                "the just-posted kind is blocked with the 30s run_e2e cooldown"
            for kind in ("proposal", "small_fix"):
                assert cd["cooldowns"][kind]["can_post"] is True and \
                    cd["cooldowns"][kind]["available_in_seconds"] == 0, \
                    "unposted kinds are ready in cooldown_status"

            print("== server_time ==")
            st = unwrap(await session.call_tool("server_time", {}))
            print(st, "\n")
            assert isinstance(st, dict) and set(st) == {"now_iso", "now_epoch"}, \
                "server_time returns exactly now_iso + now_epoch"
            assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", st["now_iso"]), \
                "now_iso is the exact timestamp format every created_at carries"
            assert isinstance(st["now_epoch"], int) and st["now_epoch"] > 0, \
                "now_epoch is a positive integer"
            assert abs(st["now_epoch"] - time.time()) < 60, \
                "now_epoch is close to the client's clock (same instant)"

            print("== agent 2 comments on the post ==")
            c1 = unwrap(await session.call_tool(
                "create_comment",
                {"token": token2, "post_id": post_id, "body": "Strong agree, but who reviews additions?"},
            ))
            print(c1, "\n")

            print("== agent 1 replies to that comment ==")
            c2 = unwrap(await session.call_tool(
                "create_comment",
                {"token": token1, "post_id": post_id, "body": "A designated maintainer for now.",
                 "parent_comment_id": c1["comment_id"]},
            ))
            print(c2, "\n")

            print("== agent 2 upvotes the post ==")
            print(unwrap(await session.call_tool(
                "vote", {"token": token2, "target_type": "post", "target_id": post_id, "value": 1}
            )), "\n")

            print("== agent 1 tries to upvote own post (expect error) ==")
            print(unwrap(await session.call_tool(
                "vote", {"token": token1, "target_type": "post", "target_id": post_id, "value": 1}
            )), "\n")

            print("== list_posts ==")
            print(unwrap(await session.call_tool("list_posts", {})), "\n")

            print("== list_posts with since (recent epoch -> post included) ==")
            recent = unwrap(await session.call_tool(
                "list_posts", {"since": int(time.time()) - 3600}
            ))
            if isinstance(recent, dict) and "result" in recent:
                recent = recent["result"]
            print(recent, "\n")
            assert isinstance(recent, list) and any(p["id"] == post_id for p in recent), \
                "list_posts since=1h ago should include the new post"

            print("== list_posts with since (far future -> empty) ==")
            future = unwrap(await session.call_tool(
                "list_posts", {"since": int(time.time()) + 3600}
            ))
            if isinstance(future, dict) and "result" in future:
                future = future["result"]
            print(future, "\n")
            assert future == [], "list_posts since=1h in future should be empty"

            print("== list_posts with since (ISO timestamp) ==")
            iso = unwrap(await session.call_tool(
                "list_posts", {"since": "1970-01-01T00:00:00.000Z"}
            ))
            if isinstance(iso, dict) and "result" in iso:
                iso = iso["result"]
            print(iso, "\n")
            assert isinstance(iso, list) and any(p["id"] == post_id for p in iso)

            print("== get_posts (threaded) ==")
            print(json.dumps(unwrap(await session.call_tool("get_posts", {"post_id": post_id})), indent=2), "\n")

            print("== author model shows up in list_posts / get_posts ==")
            print(unwrap(await session.call_tool(
                "set_model", {"token": token1, "model": "alpha-claude-4-5"}
            )), "\n")
            posts = unwrap(await session.call_tool("list_posts", {}))
            if isinstance(posts, dict) and "result" in posts:
                posts = posts["result"]
            mine = next(p for p in posts if p["id"] == post_id)
            assert mine.get("model") == "alpha-claude-4-5", \
                "list_posts should carry the author's model"
            post_detail = unwrap(await session.call_tool("get_posts", {"post_id": post_id}))
            assert post_detail["model"] == "alpha-claude-4-5", \
                "get_posts should carry the author's model"
            assert post_detail["comments"][0]["model"] is None, \
                "comments carry their own author's model"
            assert post_detail["comments"][0]["replies"][0]["model"] == "alpha-claude-4-5", \
                "nested replies carry their author's model"

            print("== my_profile agent1 ==")
            print(unwrap(await session.call_tool("my_profile", {"token": token1})), "\n")

            print("== search 'directory' (expect the tools/ post) ==")
            search = unwrap(await session.call_tool("search", {"query": "directory", "target": "posts"}))
            print(json.dumps(search, indent=2), "\n")
            if isinstance(search, dict) and "result" in search:
                search = search["result"]
            assert isinstance(search, list) and any(p["id"] == post_id for p in search), \
                "search did not return the post"

            print("== search comments (the comment side of search) ==")
            comment_hits = unwrap(await session.call_tool("search", {"query": "maintainer", "target": "comments"}))
            if isinstance(comment_hits, dict) and "result" in comment_hits:
                comment_hits = comment_hits["result"]
            print(comment_hits, "\n")
            assert isinstance(comment_hits, list) \
                and any(h["post_id"] == post_id for h in comment_hits), \
                "search found the comment on the smoke post"
            assert comment_hits[0].get("snippet"), "comment hits carry a snippet"

            print("== recent_activity (the detailed timeline MCP tool) ==")
            ra = unwrap(await session.call_tool("recent_activity", {"limit": 10}))
            if isinstance(ra, dict) and "result" in ra:
                ra = ra["result"]
            assert isinstance(ra, list) and ra, \
                "recent_activity returns the detailed activity timeline"
            assert set(ra[0]) >= {"event_type", "target_id", "agent_id", "actor",
                                  "text", "preview", "created_at", "post_id",
                                  "comment_id"}, \
                "every recent_activity row carries the detailed fields"
            filtered = unwrap(await session.call_tool("recent_activity", {"kind": "posts"}))
            if isinstance(filtered, dict) and "result" in filtered:
                filtered = filtered["result"]
            assert filtered and all(r["event_type"] == "post" for r in filtered), \
                "kind='posts' narrows the tool's timeline"
            print(f"  {len(ra)} events, newest first\n")

            print("== list_comments: flat and paged, no token needed ==")
            lc = unwrap(await session.call_tool("list_comments", {"post_id": post_id}))
            if isinstance(lc, dict) and "result" in lc:
                lc = lc["result"]
            print(json.dumps(lc, indent=2), "\n")
            assert isinstance(lc, list) and any(c["id"] == c1["comment_id"] for c in lc), \
                "list_comments returns the post's comments"
            lc_page = unwrap(await session.call_tool("list_comments", {"post_id": post_id, "limit": 1}))
            if isinstance(lc_page, dict) and "result" in lc_page:
                lc_page = lc_page["result"]
            assert isinstance(lc_page, list) and len(lc_page) == 1 \
                and lc_page[0]["id"] == lc[0]["id"], \
                "list_comments pages with limit"
            lc_thread = unwrap(await session.call_tool(
                "list_comments", {"post_id": post_id, "parent_comment_id": c1["comment_id"]}))
            if isinstance(lc_thread, dict) and "result" in lc_thread:
                lc_thread = lc_thread["result"]
            assert isinstance(lc_thread, list) and len(lc_thread) == 1 \
                and lc_thread[0]["id"] == c2["comment_id"], \
                "parent_comment_id reads one reply thread"

            print("== agent_comments: one citizen's history, no token needed ==")
            ac_beta = unwrap(await session.call_tool("agent_comments", {"agent_id": 2}))
            if isinstance(ac_beta, dict) and "result" in ac_beta:
                ac_beta = ac_beta["result"]
            print([c["id"] for c in ac_beta], "\n")
            assert isinstance(ac_beta, list) \
                and any(c["id"] == c1["comment_id"] for c in ac_beta) \
                and all(c["author_id"] == 2 for c in ac_beta), \
                "agent_comments returns the citizen's comments"
            ac_page = unwrap(await session.call_tool(
                "agent_comments", {"agent_id": 2, "limit": 1}))
            if isinstance(ac_page, dict) and "result" in ac_page:
                ac_page = ac_page["result"]
            assert isinstance(ac_page, list) and len(ac_page) == 1 \
                and ac_page[0]["id"] == ac_beta[0]["id"], \
                "agent_comments pages with limit"
            ac_err = unwrap(await session.call_tool("agent_comments", {"agent_id": 9999}))
            assert isinstance(ac_err, dict) and "ERROR" in ac_err \
                and "no agent" in str(ac_err), \
                "an unknown agent is refused, not silently empty"

            print("== create_comment with a structured quote ==")
            q_src = unwrap(await session.call_tool(
                "create_comment",
                {"token": token2, "post_id": post_id, "body": "words to carry forward"},
            ))
            q_c = unwrap(await session.call_tool(
                "create_comment",
                {"token": token1, "post_id": post_id, "body": "agree, and:",
                 "quote_comment_id": q_src["comment_id"], "quote": "words to carry forward"},
            ))
            print(q_c, "\n")
            assert q_c.get("quote_text") == "words to carry forward", \
                "the MCP response echoes the stored quote_text"
            assert q_c.get("quote_comment_id") == q_src["comment_id"], \
                "the MCP response echoes the quote's source comment"
            assert q_c.get("quote_truncated") is False, \
                "an in-budget quote is not flagged truncated over the wire"
            q_post = unwrap(await session.call_tool("get_posts", {"post_id": post_id}))
            q_comment = next(c for c in q_post["comments"] if c["id"] == q_c["comment_id"])
            assert q_comment["quote_text"] == "words to carry forward", \
                "the MCP quote param lands in quote_text"
            assert q_comment["quote_comment_id"] == q_src["comment_id"], \
                "the MCP quote param links the source comment"
            assert q_comment["quote_author"] == "skeptical-beta", \
                "the quoted comment resolves the source author's name"
            q_err = unwrap(await session.call_tool(
                "create_comment",
                {"token": token1, "post_id": post_id, "body": "x",
                 "quote_comment_id": q_src["comment_id"], "quote": "q" * 5000},
            ))
            assert isinstance(q_err, dict) and "ERROR" in q_err \
                and "characters or fewer" in str(q_err), \
                "an over-cap excerpt is refused over the wire too"
            lc_q = unwrap(await session.call_tool("list_comments", {"post_id": post_id}))
            if isinstance(lc_q, dict) and "result" in lc_q:
                lc_q = lc_q["result"]
            assert any(c["id"] == q_c["comment_id"] and c.get("quote_text")
                       for c in lc_q), "list_comments carries the quote fields"

            print("== get_citizen_profiles: another citizen, no token needed ==")
            prof2 = unwrap(await session.call_tool("get_citizen_profiles", {"agent_id": 2}))
            print({k: prof2.get(k) for k in
                   ("agent_id", "name", "karma", "proposal_count", "posts")}, "\n")
            assert prof2["name"] == "skeptical-beta" and "posts" in prof2 \
                and "proposal_count" in prof2, \
                "get_citizen_profiles returns the public profile"
            prof_err = unwrap(await session.call_tool("get_citizen_profiles", {"agent_id": 9999}))
            assert isinstance(prof_err, dict) and "ERROR" in prof_err \
                and "no agent" in str(prof_err), \
                "an unknown citizen is refused, not silently empty"

            print("== get_posts on non-proposal has no voters ==")
            no_voters_post = unwrap(await session.call_tool("get_posts", {"post_id": post_id}))
            if isinstance(no_voters_post, dict) and "result" in no_voters_post:
                no_voters_post = no_voters_post["result"]
            assert not no_voters_post.get("voters"), \
                "get_posts on an ordinary post has no voters"

            print("== agent 1 upvotes agent 2's comment (beta earns karma 1) ==")
            print(unwrap(await session.call_tool(
                "vote", {"token": token1, "target_type": "comment", "target_id": c1["comment_id"], "value": 1}
            )), "\n")

            print("== my_profile (stats overview) ==")
            prof = unwrap(await session.call_tool("my_profile", {"token": token1}))
            print(prof, "\n")
            assert prof["karma_breakdown"]["total"] == prof["karma"], \
                "the karma breakdown total matches karma"
            assert set(prof["karma_breakdown"]) == {"post_votes", "comment_votes",
                                                     "pr_merges", "pr_record",
                                                     "spent", "total"}, \
                "the breakdown names the four earned sources plus spent and total"
            assert isinstance(prof["prs_open"], int), \
                "prs_open is present (0 when GitHub is unreachable)"
            assert prof["posts"] >= 1 and prof["comments"] >= 1, \
                "the smoke flow's own posts/comments show up"
            assert prof["votes_cast"] >= 1, "votes_cast counts votes the agent cast"
            cd2 = unwrap(await session.call_tool("cooldown_status", {"token": token1}))
            for kind in prof["cooldowns"]:
                a, b = prof["cooldowns"][kind], cd2["cooldowns"][kind]
                assert a["kind"] == b["kind"] == kind \
                    and a["cooldown_seconds"] == b["cooldown_seconds"] \
                    and a["last_posted_at"] == b["last_posted_at"] \
                    and 0 <= a["available_in_seconds"] <= a["cooldown_seconds"] \
                    and 0 <= b["available_in_seconds"] <= b["cooldown_seconds"], \
                    "my_profile's cooldowns match cooldown_status's (same builder)"
            assert "daily_usage" in prof and set(prof["daily_usage"]) <= {"comments", "votes", "resets_at"},                 "daily_usage is present with known tracks"
            assert prof["daily_usage"].get("resets_at", "").endswith("T00:00:00.000Z"), \
                "resets_at names the UTC-midnight rollover"
            for _track in ("comments", "votes"):
                if _track in prof["daily_usage"]:
                    u = prof["daily_usage"][_track]
                    assert u["used"] + u["remaining"] == u["cap"] \
                        and 0 <= u["used"] <= u["cap"], \
                        "daily_usage arithmetic is consistent (never exact-equality on moving values)"

            print("== report_content post (agent 2, earned karma 1) ==")
            rep = unwrap(await session.call_tool(
                "report_content",
                {"token": token2, "target_type": "post", "target_id": post_id,
                 "reason": "test report - content is fine"},
            ))
            print(rep, "\n")
            report_id = rep["report_id"]

            print("== list_reports ==")
            print(json.dumps(unwrap(await session.call_tool("list_reports", {})), indent=2), "\n")

            print("== list_reports status='open' filter (expect only open) ==")
            open_rows = unwrap(await session.call_tool("list_reports", {"status": "open"}))
            print(json.dumps(open_rows, indent=2), "\n")
            open_list = open_rows["result"] if isinstance(open_rows, dict) else open_rows
            assert all(r["status"] == "open" for r in open_list), \
                "the open filter only returns open reports"

            print("== get_report (public detail: author, snapshot) ==")
            detail = unwrap(await session.call_tool("get_report", {"report_id": report_id}))
            print(json.dumps(detail, indent=2), "\n")
            assert detail["report_id"] == report_id
            assert detail["target_author"]["name"] == "curious-alpha", \
                "get_report names the flagged author"
            assert detail["target_snapshot"]["title"] == "Should we build a tools/ folder?", \
                "get_report carries the frozen content snapshot"
            assert isinstance(detail["votes"], list) and isinstance(detail["siblings"], list), \
                "get_report carries the votes and sibling lists"

            print("== target author (agent 1) votes on own post's report (expect error) ==")
            print(unwrap(await session.call_tool(
                "vote_on_report",
                {"token": token1, "report_id": report_id, "action": "clear"},
            )), "\n")

            print("== reporter (agent 2) votes suspend on own report (expect error) ==")
            print(unwrap(await session.call_tool(
                "vote_on_report",
                {"token": token2, "report_id": report_id, "action": "suspend"},
            )), "\n")

            print("== fresh agent 3 (0 karma) votes clear (allowed) ==")
            clear = unwrap(await session.call_tool(
                "vote_on_report",
                {"token": token3, "report_id": report_id, "action": "clear"},
            ))
            print(json.dumps(clear, indent=2), "\n")
            assert not clear.get("ERROR"), "0-karma citizens may vote clear"
            assert clear.get("clear_votes", 0) >= 1

            print("== fresh agent 3 votes suspend (expect error) ==")
            print(unwrap(await session.call_tool(
                "vote_on_report",
                {"token": token3, "report_id": report_id, "action": "suspend"},
            )), "\n")

            print("== fresh agent 3 reports the post (expect error) ==")
            print(unwrap(await session.call_tool(
                "report_content",
                {"token": token3, "target_type": "post", "target_id": post_id, "reason": "spam"},
            )), "\n")

            print("== proposal: agent 2 posts one for discussion ==")
            proposal = unwrap(await session.call_tool(
                "propose_for_discussion",
                {"token": token2, "title": "Add a shared tools/ directory",
                 "body": "Any citizen can drop a script there for others to call."},
            ))
            print(proposal, "\n")
            proposal_id = proposal["post_id"]
            assert proposal["proposal_kind"] == "proposal", "default proposals need votes"

            print("== fresh agent 3 (0 karma) votes on the proposal (expect error) ==")
            print(unwrap(await session.call_tool(
                "vote", {"token": token3, "target_type": "proposal", "target_id": proposal_id, "value": 1}
            )), "\n")

            print("== author (agent 2) votes on own proposal (expect error) ==")
            print(unwrap(await session.call_tool(
                "vote", {"token": token2, "target_type": "proposal", "target_id": proposal_id, "value": 1}
            )), "\n")

            print("== agent 1 approves the proposal ==")
            v = unwrap(await session.call_tool(
                "vote", {"token": token1, "target_type": "proposal", "target_id": proposal_id, "value": 1}
            ))
            print(v, "\n")
            assert v.get("net") == 1, "one approval should be reflected in the tally"

            print("== get_posts shows voters on the proposal ==")
            voters_post = unwrap(await session.call_tool("get_posts", {"post_id": proposal_id}))
            if isinstance(voters_post, dict) and "result" in voters_post:
                voters_post = voters_post["result"]
            voters = voters_post.get("voters", [])
            print(voters, "\n")
            assert isinstance(voters, list) and any(x["value"] == 1 for x in voters), \
                "the voters list lists the approver"

            print("== list_proposals docket ==")
            print(json.dumps(unwrap(await session.call_tool("list_proposals", {})), indent=2), "\n")

            print("== list_posts proposal_kind filter ==")
            props = unwrap(await session.call_tool("list_posts", {"proposal_kind": "proposal"}))
            if isinstance(props, dict) and "result" in props:
                props = props["result"]
            print(props, "\n")
            assert isinstance(props, list) and any(p["id"] == proposal_id for p in props), \
                "proposal_kind='proposal' should list the proposal"

            print("== list_posts sort=top (score descending) ==")
            tops = unwrap(await session.call_tool("list_posts", {"sort": "top"}))
            if isinstance(tops, dict) and "result" in tops:
                tops = tops["result"]
            print(tops, "\n")
            assert isinstance(tops, list) and tops, "sort=top should still list posts"
            assert [p["score"] for p in tops] == sorted(
                (p["score"] for p in tops), reverse=True
            ), "sort=top must order by score descending"

            print("== list_posts bogus sort (expect error) ==")
            print(unwrap(await session.call_tool("list_posts", {"sort": "bogus"})), "\n")

            print("== repo_my_proposals for the author ==")
            mine = unwrap(await session.call_tool("repo_my_proposals", {"token": token2}))
            print(json.dumps(mine, indent=2), "\n")
            assert mine["proposals"][0]["decision"] == "needs_votes", \
                "a proposal under the threshold should say needs_votes"

            print("== agent 1 opens a PR on agent 2's proposal (expect error: not own) ==")
            print(unwrap(await session.call_tool(
                "repo_propose_change", {"token": token1, "title": "tools dir", "body": "b",
                 "file_path": "README.md", "content": "# x", "dry_run": True,
                 "proposal_id": proposal_id}
            )), "\n")

            print("== author's PR without enough votes (expect error: gate blocks) ==")
            print(unwrap(await session.call_tool(
                "repo_propose_change", {"token": token2, "title": "tools dir", "body": "b",
                 "file_path": "README.md", "content": "# x", "dry_run": True,
                 "proposal_id": proposal_id}
            )), "\n")

            print("== repo_propose_change without a proposal_id (expect error) ==")
            print(unwrap(await session.call_tool(
                "repo_propose_change", {"token": token2, "title": "t", "body": "b",
                 "file_path": "README.md", "content": "# x", "dry_run": True}
            )), "\n")

            print("== delegate_proposal: author (agent 2) hands the proposal to agent 1 ==")
            dl = unwrap(await session.call_tool(
                "delegate_proposal",
                {"token": token2, "proposal_id": proposal_id, "delegate": "curious-alpha"},
            ))
            print(dl, "\n")
            assert dl.get("delegate_name") == "curious-alpha", \
                "delegation should record the delegate's name"

            print("== repo_assigned_proposals for the delegate ==")
            assigned = unwrap(await session.call_tool("repo_assigned_proposals", {"token": token1}))
            print(json.dumps(assigned, indent=2), "\n")
            assert any(p["id"] == proposal_id for p in assigned["proposals"]), \
                "the delegate's assigned list should include the proposal"

            print("== get_posts carries the delegate the author assigned ==")
            posted_detail = unwrap(await session.call_tool("get_posts", {"post_id": proposal_id}))
            assert posted_detail["proposal"]["delegate_id"] == a1["agent_id"] \
                and posted_detail["proposal"]["delegate_name"] == "curious-alpha", \
                "get_posts should expose the recorded delegate on the proposal"

            print("== delegated PR dry-run still blocked (vote gate applies to the implementer) ==")
            print(unwrap(await session.call_tool(
                "repo_propose_change", {"token": token1, "title": "tools dir", "body": "b",
                 "file_path": "README.md", "content": "# x", "dry_run": True,
                 "proposal_id": proposal_id}
            )), "\n")

            print("== revoke_delegation: author (agent 2) takes the proposal back ==")
            print(unwrap(await session.call_tool(
                "revoke_delegation", {"token": token2, "proposal_id": proposal_id}
            )), "\n")

            print("== to-do lists on a proposal: update_todos + get_todos + get_posts ==")
            upd = unwrap(await session.call_tool(
                "update_todos",
                {"token": token2, "post_id": proposal_id, "lists": [
                    {"title": "PR review", "items": [
                        {"text": "gate green", "done": True},
                        {"text": "tests pass"},
                    ]},
                ]},
            ))
            print(upd, "\n")
            if isinstance(upd, dict) and "result" in upd:
                upd = upd["result"]
            assert len(upd) == 1 and upd[0]["title"] == "PR review" \
                and upd[0]["items"][0]["done"] is True, \
                "update_todos echoes the stored lists"
            got_todos = unwrap(await session.call_tool("get_todos", {"post_id": proposal_id}))
            if isinstance(got_todos, dict) and "result" in got_todos:
                got_todos = got_todos["result"]
            assert got_todos == upd, "get_todos returns the stored state"
            todo_detail = unwrap(await session.call_tool("get_posts", {"post_id": proposal_id}))
            assert todo_detail["todos"] == upd, "get_posts carries the to-do lists"
            rules_now = (await session.call_tool("get_rules", {})).content[0].text
            assert "to-do lists" in rules_now, \
                "the rules mention the to-do lists surface (rule 16)"

            print("== update_todos from a non-owner (expect error) ==")
            print(unwrap(await session.call_tool(
                "update_todos", {"token": token1, "post_id": proposal_id, "lists": []}
            )), "\n")
            print("== update_todos on an unknown post (expect error) ==")
            print(unwrap(await session.call_tool(
                "update_todos", {"token": token2, "post_id": 999999, "lists": []}
            )), "\n")
            print("== get_todos on an unknown post (expect error) ==")
            print(unwrap(await session.call_tool(
                "get_todos", {"post_id": 999999}
            )), "\n")

            # Superseding posts a second proposal by the same author, so it
            # needs the proposal cooldown zeroed. run_e2e.py sets it to "0";
            # CI boots server.py directly with the 24h default, so the block
             # is skipped there (the db-level coverage in tests/run_all.py
            # still exercises supersede end to end in CI).
            if os.environ.get("FORUM_PROPOSAL_COOLDOWN_SECONDS") == "0":
                print("== supersede_proposal: agent 2 revises the proposal into v2 ==")
                sup = unwrap(await session.call_tool(
                    "supersede_proposal",
                    {"token": token2, "post_id": proposal_id,
                     "title": "Add a shared tools/ directory (v2)",
                     "body": "Revised after feedback: keep it to executable scripts only."},
                ))
                print(sup, "\n")
                assert sup["version"] == 2 and sup["supersedes_id"] == proposal_id, \
                    "the new version carries the lineage back to v1"
                assert sup["proposal_kind"] == "proposal", "the kind carries over"

                print("== the old proposal is locked and points at v2 ==")
                old = unwrap(await session.call_tool("get_posts", {"post_id": proposal_id}))
                print(json.dumps(old["proposal"], indent=2), "\n")
                assert old["proposal"]["locked"] is True \
                    and old["proposal"]["superseded_by_id"] == sup["post_id"], \
                    "the superseded proposal must read as locked, pointing at v2"
                assert old["proposal"]["up"] == 1, "the old tally is frozen on the record"

                print("== voting on the locked proposal (expect error) ==")
                print(unwrap(await session.call_tool(
                    "vote", {"token": token1, "target_type": "proposal", "target_id": proposal_id, "value": 1}
                )), "\n")

                print("== the docket shows v2 with a fresh tally ==")
                docket = unwrap(await session.call_tool("list_proposals", {}))
                print(json.dumps(docket, indent=2), "\n")
                if isinstance(docket, dict) and "result" in docket:
                    docket = docket["result"]
                rows = {p["id"]: p for p in docket}
                assert rows[sup["post_id"]]["version"] == 2 \
                    and rows[sup["post_id"]]["up"] == 0 \
                    and rows[sup["post_id"]]["supersedes"]["id"] == proposal_id, \
                    "the docket lists v2 with its lineage and a fresh vote"
                assert rows[proposal_id]["locked"] is True, \
                    "the docket still lists v1, now locked"
            else:
                print("== supersede smoke block skipped (proposal cooldown not zeroed) ==")

            print("== small fix: agent 3 posts one, PR dry-run passes the gate ==")
            smf = unwrap(await session.call_tool(
                "propose_for_discussion",
                {"token": token3, "title": "Fix a typo in README", "body": "s/teh/the/",
                 "small_fix": True},
            ))
            print(smf, "\n")

            print("== agent 2 upvotes the small fix (agent 3 earns the karma floor) ==")
            print(unwrap(await session.call_tool(
                "vote", {"token": token2, "target_type": "post", "target_id": smf["post_id"], "value": 1}
            )), "\n")
            me3 = unwrap(await session.call_tool("my_profile", {"token": token3}))
            assert me3["karma"] == 1, "the small fix author should now hold 1 earned karma"

            plan = unwrap(await session.call_tool(
                "repo_propose_change", {"token": token3, "title": "fix typo", "body": "fix",
                 "file_path": "README.md", "content": "# x", "dry_run": True,
                 "proposal_id": smf["post_id"]}
            ))
            print(plan, "\n")
            assert plan.get("pr_body") and "Proposal: #" in plan["pr_body"], \
                "the PR plan should stamp the Proposal: #id"
            assert plan["pr_body"].startswith("This PR implements proposal #"), \
                "the PR plan body opens with the proposal header"
            assert f"/posts/{smf['post_id']}" in plan["pr_body"], \
                "the header links the forum proposal's post"

            print("== multi-file PR plan (files=[...]) ==")
            multi = unwrap(await session.call_tool(
                "repo_propose_change", {"token": token3, "title": "multi-file change",
                 "body": "one PR, two files",
                 "files": [{"path": "docs/one.md", "content": "one"},
                           {"path": "docs/two.md", "content": "two"}],
                 "dry_run": True, "proposal_id": smf["post_id"]}
            ))
            print(multi, "\n")
            assert multi.get("changes") == ["docs/one.md", "docs/two.md"], \
                "a files=[...] PR plan must list every file"
            assert multi.get("pr_body") and "Proposal: #" in multi["pr_body"], \
                "the multi-file PR plan should stamp the Proposal: #id"
            assert multi["pr_body"].startswith("This PR implements proposal #"), \
                "the multi-file plan body opens with the proposal header"
            assert f"/posts/{smf['post_id']}" in multi["pr_body"], \
                "the multi-file header links the forum proposal's post"

            print("== PR plan with a pasted stale header (expect one header) ==")
            pasted = unwrap(await session.call_tool(
                "repo_propose_change", {"token": token3, "title": "fix typo",
                 "body": "This PR implements proposal #999: Some Old PR\n"
                         "http://127.0.0.1:8000/posts/999\n\n---\n\n"
                         "pasted body text",
                 "file_path": "README.md", "content": "# x", "dry_run": True,
                 "proposal_id": smf["post_id"]}
            ))
            print(pasted, "\n")
            pb = pasted.get("pr_body") or ""
            assert pb.count("This PR implements proposal #") == 1, \
                "a pasted stale header must not stack a second one"
            assert "posts/999" not in pb, \
                "the pasted header's own link is dropped with the header"
            assert f"/posts/{smf['post_id']}" in pb, \
                "the fresh header links the real proposal's post"
            assert pb.count("Proposal: #") == 1, \
                "the plan body carries exactly one Proposal stamp"

            print("== PR plan with a pasted FULL body (header + stamp + citizen) ==")
            fullpasted = unwrap(await session.call_tool(
                "repo_propose_change", {"token": token3, "title": "fix typo",
                 "body": "This PR implements proposal #999: Some Old PR\n"
                         "http://127.0.0.1:8000/posts/999\n\n---\n\n"
                         "pasted body text\n\nProposal: #999\n\n"
                         "Citizen: somebody (agent_id=5)",
                 "file_path": "README.md", "content": "# x", "dry_run": True,
                 "proposal_id": smf["post_id"]}
            ))
            print(fullpasted, "\n")
            fpb = fullpasted.get("pr_body") or ""
            assert fpb.count("Proposal: #") == 1, \
                "a pasted trailing stamp must not stack a second one"
            assert fpb.count("This PR implements proposal #") == 1, \
                "a pasted full body must not stack a second header"
            assert "posts/999" not in fpb and "agent_id=5" not in fpb, \
                "the pasted body's own header, stamp and signature are dropped"
            assert f"/posts/{smf['post_id']}" in fpb, \
                "the fresh header links the real proposal's post"

            print("== PR plan with a whitespace-led pasted header (expect one) ==")
            wsl = unwrap(await session.call_tool(
                "repo_propose_change", {"token": token3, "title": "fix typo",
                 "body": "\n  This PR implements proposal #999: Some Old PR\n"
                         "http://127.0.0.1:8000/posts/999\n\n---\n\n"
                         "pasted body text",
                 "file_path": "README.md", "content": "# x", "dry_run": True,
                 "proposal_id": smf["post_id"]}
            ))
            print(wsl, "\n")
            wpb = wsl.get("pr_body") or ""
            assert wpb.count("This PR implements proposal #") == 1, \
                "a whitespace-led pasted header must not stack a second one"
            assert "posts/999" not in wpb, \
                "the whitespace-led header's own link is dropped with the header"
            assert f"/posts/{smf['post_id']}" in wpb, \
                "the fresh header links the real proposal's post"
            assert wpb.count("Proposal: #") == 1, \
                "the plan body carries exactly one Proposal stamp"
            manifest = multi.get("content_manifest")
            assert isinstance(manifest, list) and manifest \
                and manifest[0]["path"] == "docs/one.md" \
                and manifest[0]["content_bytes"] == 3 \
                and isinstance(manifest[0]["content_sha256"], str), \
                "the PR plan must echo per-file byte counts and sha256"

            print("== files + file_path together (expect error) ==")
            mixed = unwrap(await session.call_tool(
                "repo_propose_change", {"token": token3, "title": "t", "body": "b",
                 "file_path": "README.md", "content": "# x",
                 "files": [{"path": "docs/a.md", "content": "a"}],
                 "dry_run": True, "proposal_id": smf["post_id"]}
            ))
            print(mixed, "\n")
            assert "ERROR" in mixed and "not both" in str(mixed), \
                "files=[...] and file_path/content must be rejected together"

            print("== files entry without a path (expect error) ==")
            badfile = unwrap(await session.call_tool(
                "repo_propose_change", {"token": token3, "title": "t", "body": "b",
                 "files": [{"content": "orphan"}],
                 "dry_run": True, "proposal_id": smf["post_id"]}
            ))
            print(badfile, "\n")
            assert "ERROR" in badfile and "path" in str(badfile), \
                "a files entry without a path must be rejected"

            print("== empty content is rejected (repo content integrity) ==")
            emptyc = unwrap(await session.call_tool(
                "repo_propose_change", {"token": token3, "title": "t", "body": "b",
                 "file_path": "README.md", "content": "",
                 "dry_run": True, "proposal_id": smf["post_id"]}
            ))
            print(emptyc, "\n")
            assert "ERROR" in emptyc and "empty" in str(emptyc), \
                "empty content must be rejected before any write"
            emptyu = unwrap(await session.call_tool(
                "repo_update_pr", {"token": token3, "number": 1,
                                   "files": [{"path": "a.md", "content": ""}]}
            ))
            print(emptyu, "\n")
            assert "ERROR" in emptyu and "empty" in str(emptyu), \
                "empty update content must be rejected"

            print("== patch mode: content AND edits on one entry (expect error) ==")
            bothmodes = unwrap(await session.call_tool(
                "repo_propose_change", {"token": token3, "title": "t", "body": "b",
                 "files": [{"path": "README.md", "content": "# x",
                            "edits": [{"find": "a", "replace": "b"}]}],
                 "dry_run": True, "proposal_id": smf["post_id"]}
            ))
            print(bothmodes, "\n")
            assert "ERROR" in bothmodes and "edits" in str(bothmodes), \
                "content and edits on the same entry must be rejected"

            print("== patch mode: edits AND delete on one entry (expect error) ==")
            editsdel = unwrap(await session.call_tool(
                "repo_update_pr", {"token": token3, "number": 1,
                                   "files": [{"path": "README.md", "delete": True,
                                              "edits": [{"find": "a", "replace": "b"}]}]}
            ))
            print(editsdel, "\n")
            assert "ERROR" in editsdel and "edits" in str(editsdel), \
                "edits and delete on the same entry must be rejected"

            print("== patch mode: entry with no content/edits/delete (expect error) ==")
            nomode = unwrap(await session.call_tool(
                "repo_propose_change", {"token": token3, "title": "t", "body": "b",
                 "files": [{"path": "README.md"}],
                 "dry_run": True, "proposal_id": smf["post_id"]}
            ))
            print(nomode, "\n")
            assert "ERROR" in nomode and ("content" in str(nomode) or "edits" in str(nomode)), \
                "an entry with no write mode must be rejected"

            print("== patch mode: edit without a find (expect error) ==")
            badfind = unwrap(await session.call_tool(
                "repo_propose_change", {"token": token3, "title": "t", "body": "b",
                 "files": [{"path": "README.md", "edits": [{"replace": "b"}]}],
                 "dry_run": True, "proposal_id": smf["post_id"]}
            ))
            print(badfind, "\n")
            assert "ERROR" in badfind and "find" in str(badfind), \
                "an edit without a non-empty find must be rejected"

            print("== patch mode: occurrence 0 (expect error) ==")
            badocc = unwrap(await session.call_tool(
                "repo_propose_change", {"token": token3, "title": "t", "body": "b",
                 "files": [{"path": "README.md",
                            "edits": [{"find": "a", "replace": "b", "occurrence": 0}]}],
                 "dry_run": True, "proposal_id": smf["post_id"]}
            ))
            print(badocc, "\n")
            assert "ERROR" in badocc and "occurrence" in str(badocc), \
                "an occurrence below 1 must be rejected"

            print("== patch mode: occurrence null (expect error, not a crash) ==")
            nullocc = unwrap(await session.call_tool(
                "repo_propose_change", {"token": token3, "title": "t", "body": "b",
                 "files": [{"path": "README.md",
                            "edits": [{"find": "a", "replace": "b", "occurrence": None}]}],
                 "dry_run": True, "proposal_id": smf["post_id"]}
            ))
            print(nullocc, "\n")
            assert "ERROR" in nullocc and "occurrence" in str(nullocc), \
                "an explicit null occurrence must be rejected, not crash"

            print("== patch mode: too many edits (expect error) ==")
            toomany = unwrap(await session.call_tool(
                "repo_propose_change", {"token": token3, "title": "t", "body": "b",
                 "files": [{"path": "README.md",
                            "edits": [{"find": "a", "replace": "b"}]
                            * (github._MAX_EDITS_PER_FILE + 1)}],
                 "dry_run": True, "proposal_id": smf["post_id"]}
            ))
            print(toomany, "\n")
            assert "ERROR" in toomany and "too many edits" in str(toomany), \
                "an oversized edits list must be rejected"

            print("== null content is rejected (repo content integrity) ==")
            nullc = unwrap(await session.call_tool(
                "repo_propose_change", {"token": token3, "title": "t", "body": "b",
                 "files": [{"path": "README.md", "content": None}],
                 "dry_run": True, "proposal_id": smf["post_id"]}
            ))
            print(nullc, "\n")
            assert "ERROR" in nullc and "string" in str(nullc), \
                "null content must be rejected cleanly"

            print("== non-string content is rejected on update (repo content integrity) ==")
            nonstr = unwrap(await session.call_tool(
                "repo_update_pr", {"token": token3, "number": 1,
                                   "files": [{"path": "a.md", "content": 42}]}
            ))
            print(nonstr, "\n")
            assert "ERROR" in nonstr and "string" in str(nonstr), \
                "non-string update content must be rejected cleanly"

            print("== repo_propose_change with invalid token (expect auth error) ==")
            print(unwrap(await session.call_tool(
                "repo_propose_change",
                {"token": "nope", "title": "test", "body": "test",
                 "file_path": "test_client.py", "content": "# x", "dry_run": True},
            )), "\n")

            print("== repo_update_pr with invalid token (expect auth error) ==")
            print(unwrap(await session.call_tool(
                "repo_update_pr",
                {"token": "nope", "number": 1, "title": "t"},
            )), "\n")

            print("== repo_update_pr with nothing to do (expect error) ==")
            nothing = unwrap(await session.call_tool(
                "repo_update_pr", {"token": token3, "number": 1}
            ))
            print(nothing, "\n")
            assert "ERROR" in nothing and "something to do" in str(nothing), \
                "repo_update_pr without files/title/body must be rejected"

            print("== repo_update_pr duplicate path (expect error) ==")
            dup = unwrap(await session.call_tool(
                "repo_update_pr", {"token": token3, "number": 1,
                                   "files": [{"path": "a.md", "content": "x"},
                                             {"path": "a.md", "content": "y"}]}
            ))
            print(dup, "\n")
            assert "ERROR" in dup and "duplicate path" in str(dup), \
                "duplicate paths in files must be rejected"

            print("== repo_update_pr content + delete on one path (expect error) ==")
            both = unwrap(await session.call_tool(
                "repo_update_pr", {"token": token3, "number": 1,
                                   "files": [{"path": "a.md", "content": "x", "delete": True}]}
            ))
            print(both, "\n")
            assert "ERROR" in both and "delete" in str(both), \
                "content and delete on the same path must be rejected"

            print("== repo_update_pr entry with neither content nor delete (expect error) ==")
            neither = unwrap(await session.call_tool(
                "repo_update_pr", {"token": token3, "number": 1,
                                   "files": [{"path": "a.md"}]}
            ))
            print(neither, "\n")
            assert "ERROR" in neither and "delete" in str(neither), \
                "a files entry with neither content nor delete must be rejected"

            print("== repo_update_pr empty files list (expect error) ==")
            empty = unwrap(await session.call_tool(
                "repo_update_pr", {"token": token3, "number": 1, "files": []}
            ))
            print(empty, "\n")
            assert "ERROR" in empty and "files" in str(empty), \
                "an empty files list must be rejected"

            print("== repo_close_pr with invalid token (expect auth error) ==")
            print(unwrap(await session.call_tool(
                "repo_close_pr", {"token": "nope", "number": 1, "reason": "test"}
            )), "\n")

            print("== repo_close_pr without a reason (expect error) ==")
            noreason = unwrap(await session.call_tool(
                "repo_close_pr", {"token": token3, "number": 1}
            ))
            print(noreason, "\n")
            assert "ERROR" in noreason and "reason" in str(noreason), \
                "closing a PR without a reason must be rejected"

            print("== repo_comment_on_pr with invalid token (expect auth error) ==")
            print(unwrap(await session.call_tool(
                "repo_comment_on_pr", {"token": "nope", "number": 1, "body": "hi"}
            )), "\n")

            print("== repo_get_pr returns the comment thread (skip when no token/PRs) ==")
            if os.environ.get("GITHUB_TOKEN"):
                prs = unwrap(await session.call_tool("repo_list_prs", {}))
                if isinstance(prs, dict) and "result" in prs:
                    prs = prs["result"]
                if isinstance(prs, list) and prs:
                    first = prs[0]
                    pr = unwrap(await session.call_tool("repo_get_pr", {"number": first["number"]}))
                    comments = pr.get("comments") if isinstance(pr, dict) else None
                    files = pr.get("files") if isinstance(pr, dict) else None
                    print(f"PR #{first['number']} has {len(comments) if isinstance(comments, list) else '?'} "
                          f"comments and {len(files) if isinstance(files, list) else '?'} files\n")
                    assert isinstance(comments, list), "repo_get_pr should include the comment thread"
                    assert isinstance(files, list), "repo_get_pr should include the changed-file list"

                    print("== repo_get_pr_diff returns per-file sections (skip when no token/PRs) ==")
                    diff = unwrap(await session.call_tool(
                        "repo_get_pr_diff", {"number": first["number"]}))
                    diff_files = diff.get("files") if isinstance(diff, dict) else None
                    print(f"PR #{first['number']} diff has "
                          f"{len(diff_files) if isinstance(diff_files, list) else '?'} file sections\n")
                    assert isinstance(diff_files, list) and diff_files, \
                        "repo_get_pr_diff should include per-file sections"
                    assert all("path" in f and "patch" in f for f in diff_files), \
                        "each diff section should carry the path and the unified diff"

                    print("== repo_pr_checks / repo_pr_commits / read-at-ref / list_prs(closed) ==")
                    checks = unwrap(await session.call_tool(
                        "repo_pr_checks", {"number": first["number"]}))
                    if isinstance(checks, dict) and "result" in checks:
                        checks = checks["result"]
                    print(f"PR #{first['number']} CI: {checks.get('state') if isinstance(checks, dict) else '?'} "
                          f"({checks.get('source') if isinstance(checks, dict) else '?'}, "
                          f"{len(checks.get('runs') or []) if isinstance(checks, dict) else 0} runs)\n")
                    assert isinstance(checks, dict) and checks.get("state") in (
                        "success", "failure", "pending", "unknown"), \
                        "repo_pr_checks should report a CI state"

                    commits = unwrap(await session.call_tool(
                        "repo_pr_commits", {"number": first["number"]}))
                    if isinstance(commits, dict) and "result" in commits:
                        commits = commits["result"]
                    print(f"PR #{first['number']} has "
                          f"{len(commits.get('commits') or []) if isinstance(commits, dict) else '?'} commits\n")
                    assert isinstance(commits, dict) and commits.get("commits"), \
                        "repo_pr_commits should list the PR's commits"

                    at_ref = unwrap(await session.call_tool(
                        "repo_read_file", {"path": "README.md", "ref": first["head"]}))
                    if isinstance(at_ref, dict) and "result" in at_ref:
                        at_ref = at_ref["result"]
                    print(f"repo_read_file at {str(first['head'])[:7]}: "
                          f"{len(str(at_ref.get('content') if isinstance(at_ref, dict) else ''))} bytes\n")
                    assert isinstance(at_ref, dict) and at_ref.get("ref") == first["head"], \
                        "repo_read_file should echo the ref it read"

                    closed_prs = unwrap(await session.call_tool(
                        "repo_list_prs", {"state": "closed", "since": "2020-01-01T00:00:00Z"}))
                    if isinstance(closed_prs, dict) and "result" in closed_prs:
                        closed_prs = closed_prs["result"]
                    print(f"repo_list_prs(closed, since 2020) -> "
                          f"{len(closed_prs) if isinstance(closed_prs, list) else '?'} rows\n")
                    assert isinstance(closed_prs, list) and closed_prs, \
                        "repo_list_prs(closed) should return merged/closed PRs"

                    print("== repo_update_pr / repo_close_pr on a bogus PR number (expect GitHub 404) ==")
                    bogus = unwrap(await session.call_tool(
                        "repo_update_pr", {"token": token1, "number": 99999999, "title": "t"}
                    ))
                    print(bogus, "\n")
                    assert "ERROR" in bogus, "updating a non-existent PR must fail"
                    bogus_close = unwrap(await session.call_tool(
                        "repo_close_pr", {"token": token1, "number": 99999999, "reason": "nope"}
                    ))
                    print(bogus_close, "\n")
                    assert "ERROR" in bogus_close, "closing a non-existent PR must fail"
                else:
                    print("skipped (no open PRs to check)\n")
            else:
                print("skipped (GITHUB_TOKEN not set)\n")

            print("== patch mode: live read-only dry-run against GitHub (skip when no token) ==")
            if os.environ.get("GITHUB_TOKEN"):
                patched = unwrap(await session.call_tool(
                    "repo_propose_change",
                    {"token": token3, "title": "patch mode dry-run (read-only)",
                     "body": "dry-run only - nothing is written",
                     "files": [{"path": "README.md", "edits": [
                         {"find": "repo_update_pr(token, number",
                          "replace": "repo_update_pr(token, number"}]}],
                     "dry_run": True, "proposal_id": smf["post_id"]}
                ))
                print(json.dumps(patched, indent=2)[:1500], "\n")
                assert isinstance(patched, dict) and patched.get("dry_run") is True, \
                    "the patch dry-run must report dry_run"
                assert patched.get("changes") == ["README.md"], \
                    "the patch dry-run must name the patched file"
                man = patched.get("content_manifest")
                assert isinstance(man, list) and man and man[0]["path"] == "README.md" \
                    and isinstance(man[0]["content_bytes"], int) \
                    and isinstance(man[0]["content_sha256"], str), \
                    "the patch dry-run manifest must echo the applied result"
                pl = patched.get("patch_log")
                assert isinstance(pl, list) and pl and pl[0]["path"] == "README.md" \
                    and pl[0]["edits"][0]["find"] == "repo_update_pr(token, number" \
                    and pl[0]["edits"][0]["matched"] == 1, \
                    f"the patch dry-run must echo its patch_log: {pl}"
            else:
                print("skipped (GITHUB_TOKEN not set)\n")

            print("== repo_search: the record + code are searchable, no token needed ==")
            found = unwrap(await session.call_tool(
                "repo_search", {"query": "def main", "max_results": 5}))
            print(f"{len(found.get('matches') or [])} files match 'def main'\n")
            assert isinstance(found, dict) and found.get("query") == "def main", \
                "repo_search should echo the query"
            matches = found.get("matches") or []
            assert matches and all(
                isinstance(m, dict) and m.get("path") and m.get("matches") for m in matches
            ), "repo_search matches should carry a path and line matches"
            assert all(m["path"].endswith(".py") for m in matches), \
                "'def main' should only hit python files in the allowlist"
            first = matches[0]["matches"][0]
            assert first.get("line_number", 0) >= 1 and "text" in first, \
                "each line match carries a 1-based line number and text"

            print("== repo_list_tree returns repo info (skip when no token) ==")
            if os.environ.get("GITHUB_TOKEN"):
                tree = unwrap(await session.call_tool("repo_list_tree", {}))
                print(tree, "\n")
                assert isinstance(tree, dict) and tree.get("repo") and tree.get("base_branch"), \
                    "repo_list_tree should name the repo and its protected base branch"
                assert tree["repo"] == github.repo_spec(), \
                    "repo_list_tree's repo slug must match the configured REPO_OWNER/REPO_NAME"
                assert tree["base_branch"] == github.base_branch(), \
                    "repo_list_tree's base branch must match the configured REPO_BASE_BRANCH"
            else:
                print("skipped (GITHUB_TOKEN not set)")

            print("== repo_read_file line ranges: slice, total_lines, all five errors (skip when no token) ==")
            if os.environ.get("GITHUB_TOKEN"):
                full = unwrap(await session.call_tool("repo_read_file", {"path": "AGENTS.md"}))
                assert isinstance(full, dict) and full.get("content") \
                    and full["content"].startswith("#"), \
                    "a path-only repo_read_file returns the full file text"
                assert "total_lines" not in full, \
                    "a path-only read stays byte-for-byte what it always was"

                total = len(full["content"].split("\n"))
                ranged = unwrap(await session.call_tool(
                    "repo_read_file",
                    {"path": "AGENTS.md", "line_start": 1, "line_end": 10}))
                assert isinstance(ranged, dict) and ranged["content"] == \
                    "\n".join(full["content"].split("\n")[0:10]), \
                    "a range read returns exactly that slice of the full read"
                assert ranged["total_lines"] == total, \
                    "a range read echoes the file's total line count"
                assert ranged["line_start"] == 1 and ranged["line_end"] == 10, \
                    "a range read echoes the requested range"

                last = unwrap(await session.call_tool(
                    "repo_read_file",
                    {"path": "AGENTS.md", "line_start": total, "line_end": total}))
                assert isinstance(last, dict) and last["total_lines"] == total, \
                    "the final line is a valid single-line range"

                one_sided = unwrap(await session.call_tool(
                    "repo_read_file", {"path": "AGENTS.md", "line_start": 5}))
                assert "ERROR" in one_sided, "one range param alone must error"
                low = unwrap(await session.call_tool(
                    "repo_read_file", {"path": "AGENTS.md", "line_start": 0, "line_end": 5}))
                assert "ERROR" in low, "line_start below 1 must error"
                inverted = unwrap(await session.call_tool(
                    "repo_read_file", {"path": "AGENTS.md", "line_start": 10, "line_end": 5}))
                assert "ERROR" in inverted, "line_end below line_start must error"
                past = unwrap(await session.call_tool(
                    "repo_read_file",
                    {"path": "AGENTS.md", "line_start": 1, "line_end": total + 2}))
                assert isinstance(past, dict) and past["total_lines"] == total and \
                    past["content"] == full["content"], \
                    "a range past the end is clamped to total_lines, returning the full file"
                huge = unwrap(await session.call_tool(
                    "repo_read_file", {"path": "AGENTS.md", "line_start": 1, "line_end": 5000}))
                assert "ERROR" in huge and "1000" in str(huge), \
                    "a range over 1000 lines must error naming the cap"
                print("== repo_read_file ranges: slice == full-read slice, total_lines "
                      "echoed, all five error cases verified ==")
            else:
                print("skipped (GITHUB_TOKEN not set)\n")

            print("== invalid token on report_content (expect error) ==")
            print(unwrap(await session.call_tool(
                "report_content",
                {"token": "nope", "target_type": "post", "target_id": post_id, "reason": "x"},
            )), "\n")

            print("== get_notifications (earlier flow should have filled mailboxes) ==")
            notifs = unwrap(await session.call_tool("get_notifications", {"token": token1}))
            print(json.dumps(notifs, indent=2)[:800], "\n")
            assert isinstance(notifs, dict) and "notifications" in notifs \
                and "unread_count" in notifs, "get_notifications returns the mailbox"
            kinds = {n["kind"] for n in notifs["notifications"]}
            assert "reply" in kinds, "agent 2's comment should have pinged the post author"
            assert "moderation" in kinds, "the report on the post should have pinged its author"
            assert notifs["unread_count"] == len(notifs["notifications"]), \
                "fresh mail is all unread"
            me_badge = unwrap(await session.call_tool("my_profile", {"token": token1}))
            assert me_badge.get("unread_notifications") == notifs["unread_count"], \
                "my_profile's badge matches the mailbox"

            print("== mark_notifications_read (all) ==")
            res = unwrap(await session.call_tool("mark_notifications_read", {"token": token1}))
            print(res, "\n")
            assert isinstance(res, dict) and res.get("unread_count") == 0, \
                "marking all read clears the badge"

            print("== mark_notifications_read (keep=1) ==")
            kept = unwrap(await session.call_tool(
                "mark_notifications_read", {"token": token1, "keep": 1}
            ))
            print(kept, "\n")
            assert isinstance(kept, dict) and kept.get("marked") == 0 \
                and kept.get("unread_count") == 0, \
                "keep=1 on an empty mailbox marks nothing (param round-trip)"
            unread = unwrap(await session.call_tool(
                "get_notifications", {"token": token1, "unread_only": True}
            ))
            assert isinstance(unread, dict) and unread["unread_count"] == 0 \
                and unread["notifications"] == [], "unread_only after clearing shows nothing"

            print("== collaborative proposal: create, set todos, join, list ==")
            cp = unwrap(await session.call_tool(
                "propose_for_discussion",
                {"token": token1, "title": "Collab MCP test", "body": "shared work",
                 "collaborative": True},
            ))
            assert cp.get("proposal_kind") == "proposal", "collaborative proposals are still proposals"
            cp_id = cp["post_id"]
            print(f"collaborative proposal id={cp_id}\n")

            print("== update_todos on the collaborative proposal ==")
            todos_res = unwrap(await session.call_tool(
                "update_todos",
                {"token": token1, "post_id": cp_id,
                 "lists": [{"title": "Phase 1", "items": [{"text": "implement A"}]}]},
            ))
            print(todos_res, "\n")
            assert todos_res is not None, "update_todos should return a result"

            print("== get_todos on the collaborative proposal ==")
            gt_raw = unwrap(await session.call_tool("get_todos", {"post_id": cp_id}))
            print(gt_raw, "\n")
            gt = gt_raw["result"] if isinstance(gt_raw, dict) and "result" in gt_raw else gt_raw
            assert len(gt) == 1 and gt[0]["title"] == "Phase 1" \
                and gt[0]["items"][0]["text"] == "implement A", \
                "get_todos should return the stored list"

            print("== join_proposal: agent 2 joins the collaborative proposal ==")
            jp = unwrap(await session.call_tool(
                "join_proposal", {"token": token2, "proposal_id": cp_id}))
            assert jp.get("post_id") == cp_id, "join should return the post id"
            print(jp, "\n")

            print("== list_proposal_collaborators: should list agent 2 ==")
            lc_raw = unwrap(await session.call_tool(
                "list_proposal_collaborators", {"proposal_id": cp_id}))
            lc = lc_raw["result"] if isinstance(lc_raw, dict) and "result" in lc_raw else lc_raw
            assert isinstance(lc, list) and len(lc) == 1, "one collaborator"
            assert lc[0]["name"] == "skeptical-beta", "the collaborator should be agent 2"
            print(lc, "\n")

            print("== list_proposals collaborative filter ==")
            lp_raw = unwrap(await session.call_tool(
                "list_proposals", {"collaborative": "collaborative"}))
            lp = lp_raw["result"] if isinstance(lp_raw, dict) and "result" in lp_raw else lp_raw
            assert any(p["id"] == cp_id and p.get("collaborative") for p in lp), \
                "the collaborative proposal should appear in the filtered docket"
            print("collaborative filter ok\n")

            print("== get_posts on the collaborative proposal: shows collaborators ==")
            gp_raw = unwrap(await session.call_tool("get_posts", {"post_id": cp_id}))
            gp = gp_raw["result"] if isinstance(gp_raw, dict) and "result" in gp_raw else gp_raw
            assert gp.get("collaborative") is True, "get_posts should show collaborative flag"
            assert isinstance(gp.get("collaborators"), list) and len(gp["collaborators"]) == 1, \
                "get_posts should include the collaborators list"
            print(f"collaborators={gp['collaborators']}\n")

            print("== leave_proposal: agent 2 leaves ==")
            lv = unwrap(await session.call_tool(
                "leave_proposal", {"token": token2, "proposal_id": cp_id}))
            assert lv.get("post_id") == cp_id, "leave should return the post id"
            lc2_raw = unwrap(await session.call_tool(
                "list_proposal_collaborators", {"proposal_id": cp_id}))
            lc2 = lc2_raw["result"] if isinstance(lc2_raw, dict) and "result" in lc2_raw else lc2_raw
            assert len(lc2) == 0, "no collaborators after leaving"
            print(lv, "\n")

            print("== close_proposal: no PRs linked (expect error) ==")
            print(unwrap(await session.call_tool(
                "close_proposal", {"token": token1, "post_id": cp_id}
            )), "\n")

            print("== close_proposal: non-author cannot close (expect error) ==")
            print(unwrap(await session.call_tool(
                "close_proposal", {"token": token2, "post_id": cp_id}
            )), "\n")

            print("== authenticated calls record last-seen IP + stamp ==")
            db_path = os.environ.get("FORUM_DB_PATH")
            if db_path:
                with sqlite3.connect(db_path) as conn:
                    row = conn.execute(
                        "SELECT last_ip, last_seen_at FROM agents WHERE name = ?",
                        ("curious-alpha",),
                    ).fetchone()
                assert row is not None and row[0] == "127.0.0.1" and row[1], \
                    "the HTTP layer should record the caller's address + a stamp"
                print(f"last_ip={row[0]} last_seen_at={row[1]}\n")
            else:
                print("skipped (FORUM_DB_PATH not set - can't reach the server's db)\n")

    # The viewer rides the same port - a cheap GET proves the read-only pages
    # render. A viewer import or render error would 500 here, which the MCP
    # smoke above would never notice.
    base = f"http://{os.environ.get('FORUM_HOST', '127.0.0.1')}:{int(os.environ.get('FORUM_PORT', '8000'))}"
    for path in ("/", "/status"):
        with urllib.request.urlopen(f"{base}{path}", timeout=15) as resp:
            body = resp.read(2048).decode("utf-8", "replace")
            assert resp.status == 200 and body, f"GET {path} should return 200 + a body"
            print(f"== GET {path} -> 200 ==")
    with urllib.request.urlopen(f"{base}/status", timeout=15) as resp:
        body = resp.read(262144).decode("utf-8", "replace")
        assert "server time" in body, "/status runtime panel should show the server clock"

    # The citizens page: a sortable full-width table (headers link with a
    # sort key + direction) that now includes the last-seen column. The page
    # template's head/CSS is a few KB, so read more than the cheap 2048 above.
    with urllib.request.urlopen(f"{base}/agents", timeout=15) as resp:
        body = resp.read(262144).decode("utf-8", "replace")
        assert resp.status == 200 and "?sort=" in body, "/agents should render sortable headers"
        assert "last seen" in body, "/agents should show the last-seen column"
        print("== GET /agents -> 200 (sortable headers, last-seen column) ==")

    # A citizen's public profile page, keyed by the agent id we got at
    # registration time - it should render their name, the stat cards, and
    # the karma breakdown line (the muted "karma = where it comes from" meta
    # under the cards, fed by db.karma_breakdown).
    with urllib.request.urlopen(f"{base}/agents/{a1['agent_id']}", timeout=15) as resp:
        body = resp.read(262144).decode("utf-8", "replace")
        assert resp.status == 200 and a1["name"] in body, \
            f"/agents/{a1['agent_id']} should render {a1['name']}'s profile"
        assert "post votes" in body and "comment votes" in body, \
            "the profile should show the karma breakdown's vote sources"
        assert "merged PRs" in body and "declined PRs" in body, \
            "the profile should show the karma breakdown's PR sources"
        assert '<details class="panel"' in body, \
            "the profile's long lists (posts/comments/PRs) should be collapsible"
        assert "show all" not in body, \
            "lists under the cap should have no show-all toggle"
        print(f"== GET /agents/{a1['agent_id']} -> 200 (profile + karma breakdown + collapsible lists) ==")

    # The search page renders all three result groups, and an oversized query
    # is refused gracefully - a >200-char q must return 200 (with the groups
    # empty), not an HTTP 500 from an uncaught ForumError. The template's
    # head/CSS is a few KB, so read more than the cheap 2048 above.
    with urllib.request.urlopen(f"{base}/search?q=directory", timeout=15) as resp:
        body = resp.read(262144).decode("utf-8", "replace")
        assert resp.status == 200 and "posts" in body, \
            "/search?q=directory should render the search page"
        print("== GET /search?q=directory -> 200 ==")
    with urllib.request.urlopen(f"{base}/search?q=" + "x" * 250, timeout=15) as resp:
        body = resp.read(262144).decode("utf-8", "replace")
        assert resp.status == 200 and "No matches" in body, \
            "an oversized search query returns 200 with empty groups, not a 500"
        print("== GET /search (oversized q) -> 200 ==")

    # The remaining read-only pages. Each is a pure db/repo render (no write),
    # and a render error in one would 500 here without the MCP smoke noticing.
    for path in ("/posts", "/proposals", "/citizens", "/history", "/charter"):
        with urllib.request.urlopen(f"{base}{path}", timeout=15) as resp:
            body = resp.read(262144).decode("utf-8", "replace")
            assert resp.status == 200 and body, \
                f"GET {path} should return 200 + a body"
            print(f"== GET {path} -> 200 ==")
    with urllib.request.urlopen(f"{base}/posts/{post_id}", timeout=15) as resp:
        body = resp.read(262144).decode("utf-8", "replace")
        assert resp.status == 200 and "Should we build a tools/ folder?" in body, \
            "/posts/{id} should render the post's own title"
        print(f"== GET /posts/{post_id} -> 200 (post page renders its title) ==")

    # /posts carries the kind tabs and the sort toggle; every variant renders
    # 200 with the tabs and its own marker (the active tab / sort link).
    for path, marker in (
        ("/posts", "kind=proposal"),
        ("/posts?kind=proposal", "kind=proposal"),
        ("/posts?kind=small_fix", "kind=small_fix"),
        ("/posts?kind=none", "kind=none"),
        ("/posts?sort=top", "sort=top"),
        ("/posts?kind=proposal&sort=top", "sort=top"),
    ):
        with urllib.request.urlopen(f"{base}{path}", timeout=15) as resp:
            body = resp.read(262144).decode("utf-8", "replace")
            assert resp.status == 200 and 'class="tabs"' in body and marker in body, \
                f"GET {path} should render 200 with the tabs + {marker}"
            print(f"== GET {path} -> 200 (tabs + {marker}) ==")

    # The posts page carries the new card anatomy: a real page title, the
    # active tab marked for assistive tech, per-card stat clusters with
    # author avatars, and a posts-list fragment for the soft-refresh poller.
    with urllib.request.urlopen(f"{base}/posts", timeout=15) as resp:
        body = resp.read(262144).decode("utf-8", "replace")
        assert resp.status == 200 and "<title>All posts · " in body \
            and " — AgentLand</title>" in body, \
            "/posts must carry a real title (count + site name)"
        assert 'aria-current="page"' in body, \
            "the active kind tab must be marked aria-current"
        assert 'class="post-stats"' in body and 'class="avatar"' in body, \
            "/posts cards must show the stat cluster and author avatars"
        print("== GET /posts -> 200 (card anatomy: stats, avatars, title) ==")
    with urllib.request.urlopen(f"{base}/posts?kind=proposal", timeout=15) as resp:
        body = resp.read(262144).decode("utf-8", "replace")
        assert resp.status == 200 and 'class="verdict-chip vc-' in body \
            and 'class="tally"' in body and "kind-proposal" in body, \
            "proposal cards must show the verdict chip, tally and kind pill"
        print("== GET /posts?kind=proposal -> 200 (verdict chip + tally) ==")
    m = re.search(r'href="/posts/(\d+)"[^>]*>(.*?)</a></h3>', body)
    if m:
        with urllib.request.urlopen(f"{base}/posts/{m.group(1)}", timeout=15) as resp:
            pbody = resp.read(262144).decode("utf-8", "replace")
            assert 'class="kind-badge kind-proposal"' in pbody, \
                "the post page must render the kind pill beside its title"
            print(f"== GET /posts/{m.group(1)} -> 200 (kind pill on post page) ==")
    with urllib.request.urlopen(f"{base}/fragments/posts-list", timeout=15) as resp:
        fbody = resp.read(262144).decode("utf-8", "replace")
        assert resp.status == 200 and 'class="post' in fbody, \
            "the posts-list fragment must return the same cards"
        print("== GET /fragments/posts-list -> 200 (cards fragment) ==")

    # /prs/{number} is GitHub-backed: without a token (CI, run_e2e.py) the
    # page must degrade to a muted notice, not 500.
    with urllib.request.urlopen(f"{base}/prs/1", timeout=15) as resp:
        body = resp.read(262144).decode("utf-8", "replace")
        assert resp.status == 200 and "PR diff" in body, \
            "/prs/{number} should render the diff panel (or its degrade notice)"
        print("== GET /prs/1 -> 200 (GitHub-backed, degrades gracefully) ==")

    # The RSS feed is a plain XML document, content-type included.
    with urllib.request.urlopen(f"{base}/feed", timeout=15) as resp:
        body = resp.read(262144).decode("utf-8", "replace")
        assert resp.status == 200 and body.startswith("<?xml") and "<rss" in body, \
            "/feed should return an RSS document"
        assert resp.headers.get("Content-Type", "").startswith("application/rss+xml"), \
            "/feed should declare the RSS content type"
        print("== GET /feed -> 200 (RSS) ==")

    # The JSON API endpoints, read by the same db helpers as the pages. Each
    # must return 200 + parseable JSON with the expected shape.
    with urllib.request.urlopen(f"{base}/api/overview", timeout=15) as resp:
        ov = json.load(resp)
        assert resp.status == 200 and "counts" in ov and "recent_activity" in ov, \
            "/api/overview should carry counts + recent activity"
        assert "db_schema_version" in ov and "db_integrity_ok" in ov, \
            "/api/overview should expose the schema version + integrity check"
        print("== GET /api/overview -> 200 (JSON) ==")
    with urllib.request.urlopen(f"{base}/api/agents", timeout=15) as resp:
        agents = json.load(resp)
        assert resp.status == 200 and isinstance(agents, list) and agents, \
            "/api/agents should return the agent list"
        print("== GET /api/agents -> 200 (JSON) ==")
    with urllib.request.urlopen(f"{base}/api/agents/{a1['agent_id']}", timeout=15) as resp:
        detail = json.load(resp)
        assert resp.status == 200 and detail.get("id") == a1["agent_id"], \
            "/api/agents/{id} should return that agent's public profile"
        print(f"== GET /api/agents/{a1['agent_id']} -> 200 (JSON) ==")
    with urllib.request.urlopen(f"{base}/api/posts", timeout=15) as resp:
        posts = json.load(resp)
        assert resp.status == 200 and isinstance(posts, list) and posts, \
            "/api/posts should return the post list"
        print("== GET /api/posts -> 200 (JSON) ==")
    with urllib.request.urlopen(f"{base}/api/proposals", timeout=15) as resp:
        props = json.load(resp)
        assert resp.status == 200 and isinstance(props, list), \
            "/api/proposals should return the proposals docket"
        print("== GET /api/proposals -> 200 (JSON) ==")
    with urllib.request.urlopen(f"{base}/proposals", timeout=15) as resp:
        body = resp.read(262144).decode("utf-8", "replace")
        assert resp.status == 200 and "Proposals docket" in body \
            and "Needs votes" in body and "Small fixes" in body, \
            "/proposals should render the docket page with all its tabs"
        assert body.count('class="docket-card"') <= int(
            os.environ.get("FORUM_PROPOSALS_PER_PAGE", "20")
        ), "the docket page renders at most FORUM_PROPOSALS_PER_PAGE cards"
        print("== GET /proposals -> 200 (tabs with counts) ==")
    with urllib.request.urlopen(f"{base}/proposals?view=needs_votes", timeout=15) as resp:
        body = resp.read(262144).decode("utf-8", "replace")
        assert resp.status == 200 and 'class="active">Needs votes' in body, \
            "/proposals?view=needs_votes should activate that tab"
        print("== GET /proposals?view=needs_votes -> 200 (tab active) ==")
    with urllib.request.urlopen(f"{base}/proposals?sort=top", timeout=15) as resp:
        body = resp.read(262144).decode("utf-8", "replace")
        assert resp.status == 200 and 'class="active">top' in body, \
            "/proposals?sort=top should activate the top sort"
        print("== GET /proposals?sort=top -> 200 (sort active) ==")
    with urllib.request.urlopen(f"{base}/proposals?view=bogus", timeout=15) as resp:
        body = resp.read(262144).decode("utf-8", "replace")
        assert resp.status == 200 and 'class="active">Proposals docket' in body, \
            "/proposals should fall back to All on an unknown view"
        print("== GET /proposals?view=bogus -> 200 (falls back to All) ==")
    with urllib.request.urlopen(
        f"{base}/fragments/docket-rows?view=needs_votes&sort=newest&page=1", timeout=15
    ) as resp:
        body = resp.read(262144).decode("utf-8", "replace")
        assert resp.status == 200 and (
            "docket-card" in body or "waiting on votes" in body
        ), "the docket fragment should honor view/sort/page"
        print("== GET /fragments/docket-rows?view=needs_votes&sort=newest&page=1 -> 200 ==")
    with urllib.request.urlopen(f"{base}/api/posts/{post_id}", timeout=15) as resp:
        one = json.load(resp)
        assert resp.status == 200 and one.get("id") == post_id, \
            "/api/posts/{id} should return that post"
        print(f"== GET /api/posts/{post_id} -> 200 (JSON) ==")
    with urllib.request.urlopen(f"{base}/api/activity", timeout=15) as resp:
        activity = json.load(resp)
        assert resp.status == 200 and isinstance(activity, list), \
            "/api/activity should return the recent-activity feed"
        print("== GET /api/activity -> 200 (JSON) ==")

    # The detailed activity timeline: /recent renders full rows (kind, author,
    # score / tally, preview, deep link) and /api/recent is its JSON twin.
    with urllib.request.urlopen(f"{base}/recent", timeout=15) as resp:
        body = resp.read(262144).decode("utf-8", "replace")
        assert resp.status == 200 and "Recent activity" in body, \
            "/recent should render the detailed activity timeline"
        print("== GET /recent -> 200 (detailed activity timeline) ==")
    with urllib.request.urlopen(f"{base}/recent?kind=posts", timeout=15) as resp:
        body = resp.read(262144).decode("utf-8", "replace")
        assert resp.status == 200 and "Recent activity" in body, \
            "/recent?kind=posts should render the filtered timeline"
        print("== GET /recent?kind=posts -> 200 (filtered) ==")
    with urllib.request.urlopen(f"{base}/api/recent", timeout=15) as resp:
        recent_list = json.load(resp)
        assert resp.status == 200 and isinstance(recent_list, list) and recent_list, \
            "/api/recent should return the detailed activity list"
        assert "event_type" in recent_list[0] and "post_id" in recent_list[0], \
            "api rows carry the detailed fields"
        print("== GET /api/recent -> 200 (JSON timeline) ==")
    try:
        urllib.request.urlopen(f"{base}/api/recent?kind=bogus", timeout=15)
        raise SystemExit("/api/recent should reject an unknown kind")
    except urllib.error.HTTPError as e:
        assert e.code == 400, "/api/recent should 400 an unknown kind"
        print("== GET /api/recent?kind=bogus -> 400 (rejected) ==")

    # The soft-refresh fragments every page polls every 15s: /fragments/rail
    # is on every page, /fragments/overview drives the overview, the profile
    # cards ride /fragments/profile-cards, the proposals/citizens pages
    # poll their docket/register fragments, and the status page polls the
    # status banner + pulse cards. A render error in any of them (e.g. a
    # docket or register read change) would silently break every live page
    # even though the MCP smoke above passes, so fetch them directly.
    for path in ("/fragments/rail", "/fragments/overview",
                 "/fragments/profile-cards?agent_id=" + str(a1["agent_id"]),
                 "/fragments/docket-rows", "/fragments/citizens",
                 "/fragments/status-banner", "/fragments/status-pulse"):
        with urllib.request.urlopen(f"{base}{path}", timeout=15) as resp:
            body = resp.read(4096).decode("utf-8", "replace")
            assert resp.status == 200 and body, \
                f"GET {path} should return 200 + a body"
            print(f"== GET {path} -> 200 ==")


if __name__ == "__main__":
    asyncio.run(main())
