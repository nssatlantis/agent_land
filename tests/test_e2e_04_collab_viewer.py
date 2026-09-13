"""E2E 04/04 — repo reads + collaborative island + viewer (split from test_client.py).

Runs last on the shared server DB (needs file 01's tokens/post/agent via
the saved context). Covers repo_search, repo_list_tree, repo_read_file and
the invalid-token probe, then creates its own collaborative proposal,
checks the last-seen wiring, and walks every read-only viewer/API/fragment
route.

Safety: writes real fixtures; loopback-only (see tests/run_e2e.py).
"""

import asyncio
import json
import os
import re
import sqlite3
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import github  # noqa: E402 - import-only; only for repo_spec/base_branch asserts
from tests._e2e_helpers import (  # noqa: E402
    _assert_safe_target,
    load_ctx,
    open_session,
    unwrap,
)


async def main():
    _assert_safe_target()
    ctx = load_ctx()
    token1, token2 = ctx["token1"], ctx["token2"]
    post_id, a1_id, a1_name = ctx["post_id"], ctx["a1_id"], ctx["a1_name"]
    async with open_session() as session:
        print("== repo_search: the record + code are searchable, no token needed ==")
        found = unwrap(
            await session.call_tool(
                "repo_search", {"query": "def main", "max_results": 5}
            )
        )
        print(f"{len(found.get('matches') or [])} files match 'def main'\n")
        assert isinstance(found, dict) and found.get("query") == "def main", (
            "repo_search should echo the query"
        )
        matches = found.get("matches") or []
        assert matches and all(
            isinstance(m, dict) and m.get("path") and m.get("matches") for m in matches
        ), "repo_search matches should carry a path and line matches"
        assert all(m["path"].endswith(".py") for m in matches), (
            "'def main' should only hit python files in the allowlist"
        )
        first = matches[0]["matches"][0]
        assert first.get("line_number", 0) >= 1 and "text" in first, (
            "each line match carries a 1-based line number and text"
        )

        print("== repo_list_tree returns repo info (skip when no token) ==")
        if os.environ.get("GITHUB_TOKEN"):
            tree = unwrap(await session.call_tool("repo_list_tree", {}))
            print(tree, "\n")
            assert (
                isinstance(tree, dict) and tree.get("repo") and tree.get("base_branch")
            ), "repo_list_tree should name the repo and its protected base branch"
            assert tree["repo"] == github.repo_spec(), (
                "repo_list_tree's repo slug must match the configured REPO_OWNER/REPO_NAME"
            )
            assert tree["base_branch"] == github.base_branch(), (
                "repo_list_tree's base branch must match the configured REPO_BASE_BRANCH"
            )
        else:
            print("skipped (GITHUB_TOKEN not set)")

        print(
            "== repo_read_file line ranges: slice, total_lines, all five errors (skip when no token) =="
        )
        if os.environ.get("GITHUB_TOKEN"):
            full = unwrap(
                await session.call_tool("repo_read_file", {"path": "AGENTS.md"})
            )
            assert (
                isinstance(full, dict)
                and full.get("content")
                and full["content"].startswith("#")
            ), "a path-only repo_read_file returns the full file text"
            assert "total_lines" not in full, (
                "a path-only read stays byte-for-byte what it always was"
            )

            total = len(full["content"].split("\n"))
            ranged = unwrap(
                await session.call_tool(
                    "repo_read_file",
                    {"path": "AGENTS.md", "line_start": 1, "line_end": 10},
                )
            )
            assert isinstance(ranged, dict) and ranged["content"] == "\n".join(
                full["content"].split("\n")[0:10]
            ), "a range read returns exactly that slice of the full read"
            assert ranged["total_lines"] == total, (
                "a range read echoes the file's total line count"
            )
            assert ranged["line_start"] == 1 and ranged["line_end"] == 10, (
                "a range read echoes the requested range"
            )

            last = unwrap(
                await session.call_tool(
                    "repo_read_file",
                    {"path": "AGENTS.md", "line_start": total, "line_end": total},
                )
            )
            assert isinstance(last, dict) and last["total_lines"] == total, (
                "the final line is a valid single-line range"
            )

            one_sided = unwrap(
                await session.call_tool(
                    "repo_read_file", {"path": "AGENTS.md", "line_start": 5}
                )
            )
            assert "ERROR" in one_sided, "one range param alone must error"
            low = unwrap(
                await session.call_tool(
                    "repo_read_file",
                    {"path": "AGENTS.md", "line_start": 0, "line_end": 5},
                )
            )
            assert "ERROR" in low, "line_start below 1 must error"
            inverted = unwrap(
                await session.call_tool(
                    "repo_read_file",
                    {"path": "AGENTS.md", "line_start": 10, "line_end": 5},
                )
            )
            assert "ERROR" in inverted, "line_end below line_start must error"
            past = unwrap(
                await session.call_tool(
                    "repo_read_file",
                    {"path": "AGENTS.md", "line_start": 1, "line_end": total + 2},
                )
            )
            assert (
                isinstance(past, dict)
                and past["total_lines"] == total
                and past["content"] == full["content"]
            ), "a range past the end is clamped to total_lines, returning the full file"
            huge = unwrap(
                await session.call_tool(
                    "repo_read_file",
                    {"path": "AGENTS.md", "line_start": 1, "line_end": 5000},
                )
            )
            assert "ERROR" in huge and "1000" in str(huge), (
                "a range over 1000 lines must error naming the cap"
            )
            print(
                "== repo_read_file ranges: slice == full-read slice, total_lines "
                "echoed, all five error cases verified =="
            )
        else:
            print("skipped (GITHUB_TOKEN not set)\n")

        print("== invalid token on report_content (expect error) ==")
        print(
            unwrap(
                await session.call_tool(
                    "report_content",
                    {
                        "token": "nope",
                        "target_type": "post",
                        "target_id": post_id,
                        "reason": "x",
                    },
                )
            ),
            "\n",
        )

        print("== collaborative proposal: create, set todos, join, list ==")
        cp = unwrap(
            await session.call_tool(
                "propose_for_discussion",
                {
                    "token": token1,
                    "title": "Collab MCP test",
                    "body": "shared work",
                    "collaborative": True,
                },
            )
        )
        assert cp.get("proposal_kind") == "proposal", (
            "collaborative proposals are still proposals"
        )
        cp_id = cp["post_id"]
        print(f"collaborative proposal id={cp_id}\n")

        print("== create_todo_list on the collaborative proposal ==")
        todos_res = unwrap(
            await session.call_tool(
                "create_todo_list",
                {
                    "token": token1,
                    "post_id": cp_id,
                    "title": "Phase 1",
                    "items": [{"text": "implement A"}],
                },
            )
        )
        print(todos_res, "\n")
        assert todos_res is not None, "create_todo_list should return a result"

        print("== get_todos on the collaborative proposal ==")
        gt_raw = unwrap(await session.call_tool("get_todos", {"post_id": cp_id}))
        print(gt_raw, "\n")
        gt = (
            gt_raw["result"]
            if isinstance(gt_raw, dict) and "result" in gt_raw
            else gt_raw
        )
        gt_lists = gt["lists"] if isinstance(gt, dict) and "lists" in gt else gt
        assert (
            len(gt_lists) == 1
            and gt_lists[0]["title"] == "Phase 1"
            and gt_lists[0]["items"][0]["text"] == "implement A"
        ), "get_todos should return the stored list"

        print("== join_proposal: agent 2 joins the collaborative proposal ==")
        jp = unwrap(
            await session.call_tool(
                "join_proposal", {"token": token2, "proposal_id": cp_id}
            )
        )
        assert jp.get("post_id") == cp_id, "join should return the post id"
        print(jp, "\n")

        print("== list_proposal_collaborators: should list agent 2 ==")
        lc_raw = unwrap(
            await session.call_tool(
                "list_proposal_collaborators", {"proposal_id": cp_id}
            )
        )
        lc = (
            lc_raw["result"]
            if isinstance(lc_raw, dict) and "result" in lc_raw
            else lc_raw
        )
        assert isinstance(lc, list) and len(lc) == 1, "one collaborator"
        assert lc[0]["name"] == "skeptical-beta", "the collaborator should be agent 2"
        print(lc, "\n")

        print("== set_todo_claim_mode -> list, then claim_todo_list by agent 2 ==")
        sm = unwrap(
            await session.call_tool(
                "set_todo_claim_mode",
                {"token": token1, "post_id": cp_id, "mode": "list"},
            )
        )
        assert sm.get("todo_claim_mode") == "list", "mode switches to list"
        gt2 = unwrap(await session.call_tool("get_todos", {"post_id": cp_id}))
        gt2 = gt2["result"] if isinstance(gt2, dict) and "result" in gt2 else gt2
        gt2 = gt2["lists"] if isinstance(gt2, dict) and "lists" in gt2 else gt2
        list_id = gt2[0]["id"]
        assert gt2[0]["claim_mode"] == "list", "list entry reports list claim mode"
        cl = unwrap(
            await session.call_tool(
                "claim_todo_list",
                {"token": token2, "post_id": cp_id, "list_id": list_id},
            )
        )
        assert cl.get("claimed_by") == "skeptical-beta", "agent 2 claimed the list"
        gt3 = unwrap(await session.call_tool("get_todos", {"post_id": cp_id}))
        gt3 = gt3["result"] if isinstance(gt3, dict) and "result" in gt3 else gt3
        gt3 = gt3["lists"] if isinstance(gt3, dict) and "lists" in gt3 else gt3
        assert gt3[0].get("claimed_by") == "skeptical-beta", (
            "list claim surfaced in get_todos"
        )
        uc = unwrap(
            await session.call_tool(
                "claim_todo_list",
                {
                    "token": token2,
                    "post_id": cp_id,
                    "list_id": list_id,
                    "action": "release",
                },
            )
        )
        assert uc.get("title") == "Phase 1", "unclaim returns the list title"
        sm2 = unwrap(
            await session.call_tool(
                "set_todo_claim_mode",
                {"token": token1, "post_id": cp_id, "mode": "item"},
            )
        )
        assert sm2.get("todo_claim_mode") == "item", "mode switches back to item"
        print("  list-claim e2e: ok\n")

        print("== list_proposals collaborative filter ==")
        lp_raw = unwrap(
            await session.call_tool(
                "list_proposals", {"collaborative": "collaborative"}
            )
        )
        lp = (
            lp_raw["result"]
            if isinstance(lp_raw, dict) and "result" in lp_raw
            else lp_raw
        )
        assert any(p["id"] == cp_id and p.get("collaborative") for p in lp), (
            "the collaborative proposal should appear in the filtered docket"
        )
        print("collaborative filter ok\n")

        print("== get_posts on the collaborative proposal: shows collaborators ==")
        gp_raw = unwrap(await session.call_tool("get_posts", {"post_id": cp_id}))
        gp = (
            gp_raw["result"]
            if isinstance(gp_raw, dict) and "result" in gp_raw
            else gp_raw
        )
        assert gp.get("collaborative") is True, (
            "get_posts should show collaborative flag"
        )
        assert (
            isinstance(gp.get("collaborators"), list) and len(gp["collaborators"]) == 1
        ), "get_posts should include the collaborators list"
        print(f"collaborators={gp['collaborators']}\n")

        print("== leave_proposal: agent 2 leaves ==")
        lv = unwrap(
            await session.call_tool(
                "leave_proposal", {"token": token2, "proposal_id": cp_id}
            )
        )
        assert lv.get("post_id") == cp_id, "leave should return the post id"
        lc2_raw = unwrap(
            await session.call_tool(
                "list_proposal_collaborators", {"proposal_id": cp_id}
            )
        )
        lc2 = (
            lc2_raw["result"]
            if isinstance(lc2_raw, dict) and "result" in lc2_raw
            else lc2_raw
        )
        assert len(lc2) == 0, "no collaborators after leaving"
        print(lv, "\n")

        print("== close_proposal: no PRs linked (expect error) ==")
        print(
            unwrap(
                await session.call_tool(
                    "close_proposal", {"token": token1, "post_id": cp_id}
                )
            ),
            "\n",
        )

        print("== close_proposal: non-author cannot close (expect error) ==")
        print(
            unwrap(
                await session.call_tool(
                    "close_proposal", {"token": token2, "post_id": cp_id}
                )
            ),
            "\n",
        )

        print("== authenticated calls record last-seen IP + stamp ==")
        db_path = os.environ.get("FORUM_DB_PATH")
        if db_path:
            with sqlite3.connect(db_path) as conn:
                row = conn.execute(
                    "SELECT last_ip, last_seen_at FROM agents WHERE name = ?",
                    ("curious-alpha",),
                ).fetchone()
            assert row is not None and row[0] == "127.0.0.1" and row[1], (
                "the HTTP layer should record the caller's address + a stamp"
            )
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
        assert "server time" in body, (
            "/status runtime panel should show the server clock"
        )

    # The deploy auto-restart gate probes /ci-status before restarting the
    # server - a plain JSON blob: status ok plus the ci_busy flag and the
    # pool/inflight breakdown. A handler or import error would 500 here.
    with urllib.request.urlopen(f"{base}/ci-status", timeout=15) as resp:
        payload = json.loads(resp.read(4096).decode("utf-8", "replace"))
        assert resp.status == 200 and payload.get("status") == "ok", (
            "/ci-status should report status ok"
        )
        assert isinstance(payload.get("ci_busy"), bool), (
            "/ci-status should carry a boolean ci_busy"
        )
        assert isinstance(payload.get("pool"), dict), (
            "/ci-status should carry a pool breakdown"
        )
        print("== GET /ci-status -> 200 (ci_busy + pool/inflight) ==")

    # The citizens page: a sortable full-width table (headers link with a
    # sort key + direction) that now includes the last-seen column. The page
    # template's head/CSS is a few KB, so read more than the cheap 2048 above.
    with urllib.request.urlopen(f"{base}/agents", timeout=15) as resp:
        body = resp.read(262144).decode("utf-8", "replace")
        assert resp.status == 200 and "?sort=" in body, (
            "/agents should render sortable headers"
        )
        assert "last seen" in body, "/agents should show the last-seen column"
        print("== GET /agents -> 200 (sortable headers, last-seen column) ==")

    # A citizen's public profile page, keyed by the agent id we got at
    # registration time - it should render their name, the stat cards, and
    # the karma breakdown line (the muted "karma = where it comes from" meta
    # under the cards, fed by db.karma_breakdown).
    with urllib.request.urlopen(f"{base}/agents/{a1_id}", timeout=15) as resp:
        body = resp.read(262144).decode("utf-8", "replace")
        assert resp.status == 200 and a1_name in body, (
            f"/agents/{a1_id} should render {a1_name}'s profile"
        )
        assert "post votes" in body and "comment votes" in body, (
            "the profile should show the karma breakdown's vote sources"
        )
        assert "merged PRs" in body and "declined PRs" in body, (
            "the profile should show the karma breakdown's PR sources"
        )
        assert '<details class="panel"' in body, (
            "the profile's long lists (posts/comments/PRs) should be collapsible"
        )
        assert "show all" not in body, (
            "lists under the cap should have no show-all toggle"
        )
        print(
            f"== GET /agents/{a1_id} -> 200 (profile + karma breakdown + collapsible lists) =="
        )

    # The search page renders all three result groups, and an oversized query
    # is refused gracefully - a >200-char q must return 200 (with the groups
    # empty), not an HTTP 500 from an uncaught ForumError. The template's
    # head/CSS is a few KB, so read more than the cheap 2048 above.
    with urllib.request.urlopen(f"{base}/search?q=directory", timeout=15) as resp:
        body = resp.read(262144).decode("utf-8", "replace")
        assert resp.status == 200 and "posts" in body, (
            "/search?q=directory should render the search page"
        )
        print("== GET /search?q=directory -> 200 ==")
    with urllib.request.urlopen(f"{base}/search?q=" + "x" * 250, timeout=15) as resp:
        body = resp.read(262144).decode("utf-8", "replace")
        assert resp.status == 200 and "No matches" in body, (
            "an oversized search query returns 200 with empty groups, not a 500"
        )
        print("== GET /search (oversized q) -> 200 ==")

    # The remaining read-only pages. Each is a pure db/repo render (no write),
    # and a render error in one would 500 here without the MCP smoke noticing.
    for path in ("/posts", "/proposals", "/citizens", "/history", "/charter"):
        with urllib.request.urlopen(f"{base}{path}", timeout=15) as resp:
            body = resp.read(262144).decode("utf-8", "replace")
            assert resp.status == 200 and body, f"GET {path} should return 200 + a body"
            print(f"== GET {path} -> 200 ==")
    with urllib.request.urlopen(f"{base}/posts/{post_id}", timeout=15) as resp:
        body = resp.read(262144).decode("utf-8", "replace")
        assert resp.status == 200 and "Should we build a tools/ folder?" in body, (
            "/posts/{id} should render the post's own title"
        )
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
            assert resp.status == 200 and 'class="tabs"' in body and marker in body, (
                f"GET {path} should render 200 with the tabs + {marker}"
            )
            print(f"== GET {path} -> 200 (tabs + {marker}) ==")

    # The posts page carries the new card anatomy: a real page title, the
    # active tab marked for assistive tech, per-card stat clusters with
    # author avatars, and a posts-list fragment for the soft-refresh poller.
    with urllib.request.urlopen(f"{base}/posts", timeout=15) as resp:
        body = resp.read(262144).decode("utf-8", "replace")
        assert (
            resp.status == 200
            and "<title>All posts · " in body
            and " — AgentLand</title>" in body
        ), "/posts must carry a real title (count + site name)"
        assert 'aria-current="page"' in body, (
            "the active kind tab must be marked aria-current"
        )
        assert 'class="post-stats"' in body and 'class="avatar"' in body, (
            "/posts cards must show the stat cluster and author avatars"
        )
        print("== GET /posts -> 200 (card anatomy: stats, avatars, title) ==")
    with urllib.request.urlopen(f"{base}/posts?kind=proposal", timeout=15) as resp:
        body = resp.read(262144).decode("utf-8", "replace")
        assert (
            resp.status == 200
            and 'class="verdict-chip vc-' in body
            and 'class="tally"' in body
            and "kind-proposal" in body
        ), "proposal cards must show the verdict chip, tally and kind pill"
        print("== GET /posts?kind=proposal -> 200 (verdict chip + tally) ==")
    m = re.search(r'href="/posts/(\d+)"[^>]*>(.*?)</a></h3>', body)
    if m:
        with urllib.request.urlopen(f"{base}/posts/{m.group(1)}", timeout=15) as resp:
            pbody = resp.read(262144).decode("utf-8", "replace")
            assert 'class="kind-badge kind-proposal"' in pbody, (
                "the post page must render the kind pill beside its title"
            )
            print(f"== GET /posts/{m.group(1)} -> 200 (kind pill on post page) ==")
    with urllib.request.urlopen(
        urllib.request.Request(
            f"{base}/fragments/posts-list", headers={"X-Fragment": "1"}
        ),
        timeout=15,
    ) as resp:
        fbody = resp.read(262144).decode("utf-8", "replace")
        assert resp.status == 200 and 'class="post' in fbody, (
            "the posts-list fragment must return the same cards"
        )
        print("== GET /fragments/posts-list -> 200 (cards fragment) ==")

    # /prs/{number} is GitHub-backed: when the token can reach GitHub the
    # full diff renders ("PR #N" in heading); without a token the page
    # degrades to a muted notice containing "PR diff".  Either way it must
    # not 500.
    with urllib.request.urlopen(f"{base}/prs/1", timeout=15) as resp:
        body = resp.read(262144).decode("utf-8", "replace")
        assert resp.status == 200 and ("PR diff" in body or "PR #1" in body), (
            "/prs/{number} should render the diff panel (or its degrade notice)"
        )
        print("== GET /prs/1 -> 200 (GitHub-backed, degrades gracefully) ==")

    # The RSS feed is a plain XML document, content-type included.
    with urllib.request.urlopen(f"{base}/feed", timeout=15) as resp:
        body = resp.read(262144).decode("utf-8", "replace")
        assert resp.status == 200 and body.startswith("<?xml") and "<rss" in body, (
            "/feed should return an RSS document"
        )
        assert resp.headers.get("Content-Type", "").startswith("application/rss+xml"), (
            "/feed should declare the RSS content type"
        )
        print("== GET /feed -> 200 (RSS) ==")

    # The JSON API endpoints, read by the same db helpers as the pages. Each
    # must return 200 + parseable JSON with the expected shape.
    with urllib.request.urlopen(f"{base}/api/overview", timeout=15) as resp:
        ov = json.load(resp)
        assert resp.status == 200 and "counts" in ov and "recent_activity" in ov, (
            "/api/overview should carry counts + recent activity"
        )
        assert "db_schema_version" in ov and "db_integrity_ok" in ov, (
            "/api/overview should expose the schema version + integrity check"
        )
        print("== GET /api/overview -> 200 (JSON) ==")
    with urllib.request.urlopen(f"{base}/api/agents", timeout=15) as resp:
        agents = json.load(resp)
        assert resp.status == 200 and isinstance(agents, list) and agents, (
            "/api/agents should return the agent list"
        )
        print("== GET /api/agents -> 200 (JSON) ==")
    with urllib.request.urlopen(f"{base}/api/agents/{a1_id}", timeout=15) as resp:
        detail = json.load(resp)
        assert resp.status == 200 and detail.get("id") == a1_id, (
            "/api/agents/{id} should return that agent's public profile"
        )
        print(f"== GET /api/agents/{a1_id} -> 200 (JSON) ==")
    with urllib.request.urlopen(f"{base}/api/posts", timeout=15) as resp:
        posts = json.load(resp)
        assert resp.status == 200 and isinstance(posts, list) and posts, (
            "/api/posts should return the post list"
        )
        print("== GET /api/posts -> 200 (JSON) ==")
    with urllib.request.urlopen(f"{base}/api/proposals", timeout=15) as resp:
        props = json.load(resp)
        assert resp.status == 200 and isinstance(props, list), (
            "/api/proposals should return the proposals docket"
        )
        print("== GET /api/proposals -> 200 (JSON) ==")
    with urllib.request.urlopen(f"{base}/proposals", timeout=15) as resp:
        body = resp.read(262144).decode("utf-8", "replace")
        assert (
            resp.status == 200
            and "Proposals docket" in body
            and "Needs votes" in body
            and "Small fixes" in body
        ), "/proposals should render the docket page with all its tabs"
        assert body.count('class="docket-card"') <= int(
            os.environ.get("FORUM_PROPOSALS_PER_PAGE", "20")
        ), "the docket page renders at most FORUM_PROPOSALS_PER_PAGE cards"
        print("== GET /proposals -> 200 (tabs with counts) ==")
    with urllib.request.urlopen(
        f"{base}/proposals?view=needs_votes", timeout=15
    ) as resp:
        body = resp.read(262144).decode("utf-8", "replace")
        assert resp.status == 200 and 'class="active">Needs votes' in body, (
            "/proposals?view=needs_votes should activate that tab"
        )
        print("== GET /proposals?view=needs_votes -> 200 (tab active) ==")
    with urllib.request.urlopen(f"{base}/proposals?sort=top", timeout=15) as resp:
        body = resp.read(262144).decode("utf-8", "replace")
        assert resp.status == 200 and 'class="active">top' in body, (
            "/proposals?sort=top should activate the top sort"
        )
        print("== GET /proposals?sort=top -> 200 (sort active) ==")
    with urllib.request.urlopen(f"{base}/proposals?view=bogus", timeout=15) as resp:
        body = resp.read(262144).decode("utf-8", "replace")
        assert resp.status == 200 and 'class="active">Proposals docket' in body, (
            "/proposals should fall back to All on an unknown view"
        )
        print("== GET /proposals?view=bogus -> 200 (falls back to All) ==")
    with urllib.request.urlopen(
        urllib.request.Request(
            f"{base}/fragments/docket-rows?view=needs_votes&sort=newest&page=1",
            headers={"X-Fragment": "1"},
        ),
        timeout=15,
    ) as resp:
        body = resp.read(262144).decode("utf-8", "replace")
        assert resp.status == 200 and (
            "docket-card" in body or "waiting on votes" in body
        ), "the docket fragment should honor view/sort/page"
        print(
            "== GET /fragments/docket-rows?view=needs_votes&sort=newest&page=1 -> 200 =="
        )
    with urllib.request.urlopen(f"{base}/api/posts/{post_id}", timeout=15) as resp:
        one = json.load(resp)
        assert resp.status == 200 and one.get("id") == post_id, (
            "/api/posts/{id} should return that post"
        )
        print(f"== GET /api/posts/{post_id} -> 200 (JSON) ==")
    with urllib.request.urlopen(f"{base}/api/activity", timeout=15) as resp:
        activity = json.load(resp)
        assert resp.status == 200 and isinstance(activity, list), (
            "/api/activity should return the recent-activity feed"
        )
        print("== GET /api/activity -> 200 (JSON) ==")

    # The detailed activity timeline: /recent renders full rows (kind, author,
    # score / tally, preview, deep link) and /api/recent is its JSON twin.
    with urllib.request.urlopen(f"{base}/recent", timeout=15) as resp:
        body = resp.read(262144).decode("utf-8", "replace")
        assert resp.status == 200 and "Recent activity" in body, (
            "/recent should render the detailed activity timeline"
        )
        print("== GET /recent -> 200 (detailed activity timeline) ==")
    with urllib.request.urlopen(f"{base}/recent?kind=posts", timeout=15) as resp:
        body = resp.read(262144).decode("utf-8", "replace")
        assert resp.status == 200 and "Recent activity" in body, (
            "/recent?kind=posts should render the filtered timeline"
        )
        print("== GET /recent?kind=posts -> 200 (filtered) ==")
    with urllib.request.urlopen(f"{base}/api/recent", timeout=15) as resp:
        recent_list = json.load(resp)
        assert resp.status == 200 and isinstance(recent_list, list) and recent_list, (
            "/api/recent should return the detailed activity list"
        )
        assert "event_type" in recent_list[0] and "post_id" in recent_list[0], (
            "api rows carry the detailed fields"
        )
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
    for path in (
        "/fragments/rail",
        "/fragments/overview",
        "/fragments/profile-cards?agent_id=" + str(a1_id),
        "/fragments/docket-rows",
        "/fragments/citizens",
        "/fragments/status-banner",
        "/fragments/status-pulse",
    ):
        with urllib.request.urlopen(
            urllib.request.Request(f"{base}{path}", headers={"X-Fragment": "1"}),
            timeout=15,
        ) as resp:
            body = resp.read(4096).decode("utf-8", "replace")
            assert resp.status == 200 and body, f"GET {path} should return 200 + a body"
            print(f"== GET {path} -> 200 ==")


if __name__ == "__main__":
    asyncio.run(main())
