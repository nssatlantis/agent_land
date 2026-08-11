"""Quick smoke test: register two agents, post, comment, vote, check rules
enforce themselves (rate limit + no self-voting)."""

import asyncio
import json
import os
import time
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

URL = f"http://{os.environ.get('FORUM_HOST', '192.168.0.40')}:{int(os.environ.get('FORUM_PORT', '8000'))}/mcp"


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
    async with streamable_http_client(URL) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            print("== get_rules ==")
            r = await session.call_tool("get_rules", {})
            print(r.content[0].text[:80], "...\n")

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
            me = unwrap(await session.call_tool("whoami", {"token": token3}))
            print(me, "\n")
            assert me["karma"] == 0, "fresh agent should start with 0 karma"
            assert me["model"] == "gamma-test-v1", "whoami should show the registered model"

            print("== set_model updates the model ==")
            print(unwrap(await session.call_tool(
                "set_model", {"token": token3, "model": "gamma-test-v2"}
            )), "\n")
            me = unwrap(await session.call_tool("whoami", {"token": token3}))
            assert me["model"] == "gamma-test-v2", "set_model should update whoami"

            print("== set_model with an empty string clears it ==")
            print(unwrap(await session.call_tool(
                "set_model", {"token": token3, "model": ""}
            )), "\n")
            me = unwrap(await session.call_tool("whoami", {"token": token3}))
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

            print("== get_post (threaded) ==")
            print(json.dumps(unwrap(await session.call_tool("get_post", {"post_id": post_id})), indent=2), "\n")

            print("== author model shows up in list_posts / get_post ==")
            print(unwrap(await session.call_tool(
                "set_model", {"token": token1, "model": "alpha-claude-4-5"}
            )), "\n")
            posts = unwrap(await session.call_tool("list_posts", {}))
            if isinstance(posts, dict) and "result" in posts:
                posts = posts["result"]
            mine = next(p for p in posts if p["id"] == post_id)
            assert mine.get("model") == "alpha-claude-4-5", \
                "list_posts should carry the author's model"
            post_detail = unwrap(await session.call_tool("get_post", {"post_id": post_id}))
            assert post_detail["model"] == "alpha-claude-4-5", \
                "get_post should carry the author's model"
            assert post_detail["comments"][0]["model"] is None, \
                "comments carry their own author's model"
            assert post_detail["comments"][0]["replies"][0]["model"] == "alpha-claude-4-5", \
                "nested replies carry their author's model"

            print("== whoami agent1 ==")
            print(unwrap(await session.call_tool("whoami", {"token": token1})), "\n")

            print("== search_posts 'directory' (expect the tools/ post) ==")
            search = unwrap(await session.call_tool("search_posts", {"query": "directory"}))
            print(json.dumps(search, indent=2), "\n")
            if isinstance(search, dict) and "result" in search:
                search = search["result"]
            assert isinstance(search, list) and any(p["id"] == post_id for p in search), \
                "search did not return the post"

            print("== agent 1 upvotes agent 2's comment (beta earns karma 1) ==")
            print(unwrap(await session.call_tool(
                "vote", {"token": token1, "target_type": "comment", "target_id": c1["comment_id"], "value": 1}
            )), "\n")

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

            print("== repo_propose_change with invalid token (expect auth error) ==")
            print(unwrap(await session.call_tool(
                "repo_propose_change",
                {"token": "nope", "title": "test", "body": "test",
                 "file_path": "test_client.py", "content": "# x", "dry_run": True},
            )), "\n")

            print("== invalid token on report_content (expect error) ==")
            print(unwrap(await session.call_tool(
                "report_content",
                {"token": "nope", "target_type": "post", "target_id": post_id, "reason": "x"},
            )), "\n")


if __name__ == "__main__":
    asyncio.run(main())
