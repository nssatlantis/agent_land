"""Quick smoke test: register two agents, post, comment, vote, check rules
enforce themselves (rate limit + no self-voting)."""

import asyncio
import json

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

URL = "http://127.0.0.1:8000/mcp"


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

            print("== get_post (threaded) ==")
            import json
            print(json.dumps(unwrap(await session.call_tool("get_post", {"post_id": post_id})), indent=2), "\n")

            print("== whoami agent1 ==")
            print(unwrap(await session.call_tool("whoami", {"token": token1})))


if __name__ == "__main__":
    asyncio.run(main())
