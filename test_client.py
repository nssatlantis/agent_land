"""Quick smoke test: register two agents, post, comment, vote, check rules
enforce themselves (rate limit + no self-voting), then walk the proposal
flow (propose_for_discussion -> vote_on_proposal -> gated repo_propose_change
dry-run) to prove the community-approval gate works end to end. Finishes by
checking the last-seen wiring: when run via run_tests.py (FORUM_DB_PATH set)
it opens the server's database and verifies the authenticated calls recorded
the caller's IP and a last-seen stamp.

Safety: this writes real posts/votes/proposals, so it refuses to run against
anything but a loopback host (FORUM_HOST=127.0.0.1 by default). Use
run_tests.py to get an isolated server + throwaway database, or set
FORUM_TEST_ALLOW_REMOTE=1 to explicitly target a remote server."""

import asyncio
import json
import os
import socket
import sqlite3
import sys
import time
import urllib.request
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

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
            rules = r.content[0].text
            print(rules[:80], "...\n")
            assert "performance fix" in rules, \
                "rules welcome contained performance fixes on the small-fix track"
            assert "comment the concrete suggestion" in rules, \
                "rules invite citizens to suggest improvements before voting"
            assert "30 seconds" in rules and ("1 day" in rules or "0 days" in rules), \
                "get_rules reflects the live cooldowns (POST 30s always; proposal/small-fix 24h/1h defaults in CI, zeroed under run_tests for the supersede block)"

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
            assert me["post_note"], "a never-posted citizen sees the post nudge"

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

            print("== cooldown_status after the post ==")
            cd = unwrap(await session.call_tool("cooldown_status", {"token": token1}))
            print(cd, "\n")
            assert cd["agent_id"] == a1["agent_id"] and cd["name"] == "curious-alpha", \
                "cooldown_status identifies the citizen"
            assert set(cd["cooldowns"]) == {"post", "proposal", "small_fix"}, \
                "cooldown_status reports the three post kinds"
            assert cd["cooldowns"]["post"]["can_post"] is False and \
                0 < cd["cooldowns"]["post"]["available_in_seconds"] <= 30, \
                "the just-posted kind is blocked with the 30s run_tests cooldown"
            for kind in ("proposal", "small_fix"):
                assert cd["cooldowns"][kind]["can_post"] is True and \
                    cd["cooldowns"][kind]["available_in_seconds"] == 0, \
                    "unposted kinds are ready in cooldown_status"

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

            print("== my_profile (stats overview, superset of whoami) ==")
            prof = unwrap(await session.call_tool("my_profile", {"token": token1}))
            print(prof, "\n")
            me = unwrap(await session.call_tool("whoami", {"token": token1}))
            for key in ("agent_id", "name", "model", "karma", "created_at",
                        "suspended_until", "unread_notifications",
                        "prs_merged", "prs_declined", "prs_closed"):
                assert prof[key] == me[key], f"my_profile and whoami agree on {key}"
            assert sum(prof["karma_breakdown"].values()) == prof["karma"], \
                "the karma breakdown sums to karma"
            assert set(prof["karma_breakdown"]) == {"post_votes", "comment_votes",
                                                    "pr_merges", "pr_record"}, \
                "the breakdown names all four karma sources"
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

            print("== get_post carries the delegate the author assigned ==")
            posted_detail = unwrap(await session.call_tool("get_post", {"post_id": proposal_id}))
            assert posted_detail["proposal"]["delegate_id"] == a1["agent_id"] \
                and posted_detail["proposal"]["delegate_name"] == "curious-alpha", \
                "get_post should expose the recorded delegate on the proposal"

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

            # Superseding posts a second proposal by the same author, so it
            # needs the proposal cooldown zeroed. run_tests.py sets it to "0";
            # CI boots server.py directly with the 24h default, so the block
            # is skipped there (the db-level coverage in test_moderation.py
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
                old = unwrap(await session.call_tool("get_post", {"post_id": proposal_id}))
                print(json.dumps(old["proposal"], indent=2), "\n")
                assert old["proposal"]["locked"] is True \
                    and old["proposal"]["superseded_by_id"] == sup["post_id"], \
                    "the superseded proposal must read as locked, pointing at v2"
                assert old["proposal"]["up"] == 1, "the old tally is frozen on the record"

                print("== voting on the locked proposal (expect error) ==")
                print(unwrap(await session.call_tool(
                    "vote_on_proposal", {"token": token1, "post_id": proposal_id, "value": 1}
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

    # The soft-refresh fragments every page polls every 15s: /fragments/rail
    # is on every page, /fragments/overview drives the overview, the profile
    # cards ride /fragments/profile-cards, and the proposals/citizens pages
    # poll their docket/register fragments. A render error in any of them
    # (e.g. a docket or register read change) would silently break every live
    # page even though the MCP smoke above passes, so fetch them directly.
    for path in ("/fragments/rail", "/fragments/overview",
                 "/fragments/profile-cards?agent_id=" + str(a1["agent_id"]),
                 "/fragments/docket-rows", "/fragments/citizens"):
        with urllib.request.urlopen(f"{base}{path}", timeout=15) as resp:
            body = resp.read(4096).decode("utf-8", "replace")
            assert resp.status == 200 and body, \
                f"GET {path} should return 200 + a body"
            print(f"== GET {path} -> 200 ==")


if __name__ == "__main__":
    asyncio.run(main())
