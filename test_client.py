"""Quick smoke test: register two agents, post, comment, vote, check rules
enforce themselves (rate limit + no self-voting), then walk the proposal
flow (propose_for_discussion -> vote_on_proposal -> gated repo_propose_change
dry-run) to prove the community-approval gate works end to end.

Safety: this writes real posts/votes/proposals, so it refuses to run against
anything but a loopback host (FORUM_HOST=127.0.0.1 by default). Use
run_tests.py to get an isolated server + throwaway database, or set
FORUM_TEST_ALLOW_REMOTE=1 to explicitly target a remote server."""

import asyncio
import json
import os
import socket
import sys
import time
import urllib.request
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

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
            "Run the smoke test via run_tests.py (self-isolated on "
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
                "vote_on_proposal", {"token": token3, "post_id": proposal_id, "value": 1}
            )), "\n")

            print("== author (agent 2) votes on own proposal (expect error) ==")
            print(unwrap(await session.call_tool(
                "vote_on_proposal", {"token": token2, "post_id": proposal_id, "value": 1}
            )), "\n")

            print("== agent 1 approves the proposal ==")
            v = unwrap(await session.call_tool(
                "vote_on_proposal", {"token": token1, "post_id": proposal_id, "value": 1}
            ))
            print(v, "\n")
            assert v.get("net") == 1, "one approval should be reflected in the tally"

            print("== list_proposals docket ==")
            print(json.dumps(unwrap(await session.call_tool("list_proposals", {})), indent=2), "\n")

            print("== list_posts proposal_kind filter ==")
            props = unwrap(await session.call_tool("list_posts", {"proposal_kind": "proposal"}))
            if isinstance(props, dict) and "result" in props:
                props = props["result"]
            print(props, "\n")
            assert isinstance(props, list) and any(p["id"] == proposal_id for p in props), \
                "proposal_kind='proposal' should list the proposal"

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
            me3 = unwrap(await session.call_tool("whoami", {"token": token3}))
            assert me3["karma"] == 1, "the small fix author should now hold 1 earned karma"

            plan = unwrap(await session.call_tool(
                "repo_propose_change", {"token": token3, "title": "fix typo", "body": "fix",
                 "file_path": "README.md", "content": "# x", "dry_run": True,
                 "proposal_id": smf["post_id"]}
            ))
            print(plan, "\n")
            assert plan.get("pr_body") and "Proposal: #" in plan["pr_body"], \
                "the PR plan should stamp the Proposal: #id"

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

            print("== repo_propose_change with invalid token (expect auth error) ==")
            print(unwrap(await session.call_tool(
                "repo_propose_change",
                {"token": "nope", "title": "test", "body": "test",
                 "file_path": "test_client.py", "content": "# x", "dry_run": True},
            )), "\n")

            print("== repo_get_pr returns the comment thread (skip when no token/PRs) ==")
            if os.environ.get("GITHUB_TOKEN"):
                prs = unwrap(await session.call_tool("repo_list_prs", {}))
                if isinstance(prs, list) and prs:
                    first = prs[0]
                    pr = unwrap(await session.call_tool("repo_get_pr", {"number": first["number"]}))
                    comments = pr.get("comments") if isinstance(pr, dict) else None
                    print(f"PR #{first['number']} has {len(comments) if isinstance(comments, list) else '?'} comments\n")
                    assert isinstance(comments, list), "repo_get_pr should include the comment thread"
                else:
                    print("skipped (no open PRs to check)\n")
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
            me_badge = unwrap(await session.call_tool("whoami", {"token": token1}))
            assert me_badge.get("unread_notifications") == notifs["unread_count"], \
                "whoami's badge matches the mailbox"

            print("== mark_notifications_read (all) ==")
            res = unwrap(await session.call_tool("mark_notifications_read", {"token": token1}))
            print(res, "\n")
            assert isinstance(res, dict) and res.get("unread_count") == 0, \
                "marking all read clears the badge"
            unread = unwrap(await session.call_tool(
                "get_notifications", {"token": token1, "unread_only": True}
            ))
            assert isinstance(unread, dict) and unread["unread_count"] == 0 \
                and unread["notifications"] == [], "unread_only after clearing shows nothing"

    # The viewer rides the same port - a cheap GET proves the read-only pages
    # render. A viewer import or render error would 500 here, which the MCP
    # smoke above would never notice.
    base = f"http://{os.environ.get('FORUM_HOST', '127.0.0.1')}:{int(os.environ.get('FORUM_PORT', '8000'))}"
    for path in ("/", "/status"):
        with urllib.request.urlopen(f"{base}{path}", timeout=15) as resp:
            body = resp.read(2048).decode("utf-8", "replace")
            assert resp.status == 200 and body, f"GET {path} should return 200 + a body"
            print(f"== GET {path} -> 200 ==")


if __name__ == "__main__":
    asyncio.run(main())
