"""E2E 02/04 — reports + proposals + mailbox (split from tests/test_client.py).

Runs second on the shared server DB (needs file 01's agents + post via
the saved context). Covers report_content, proposal votes, the docket,
delegation, to-do lists, the conditional supersede block, and the mailbox
(get/mark_notifications_read) while reply+moderation history is fresh.

Safety: writes real fixtures; loopback-only (see tests/run_e2e.py).
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests._e2e_helpers import (  # noqa: E402
    _assert_safe_target,
    load_ctx,
    open_session,
    unwrap,
)


async def main():
    _assert_safe_target()
    ctx = load_ctx()
    token1, token2, token3 = ctx["token1"], ctx["token2"], ctx["token3"]
    post_id, a1_id = ctx["post_id"], ctx["a1_id"]
    async with open_session() as session:
        print("== report_content post (agent 2, earned karma 1) ==")
        rep = unwrap(
            await session.call_tool(
                "report_content",
                {
                    "token": token2,
                    "target_type": "post",
                    "target_id": post_id,
                    "reason": "test report - content is fine",
                },
            )
        )
        print(rep, "\n")
        report_id = rep["report_id"]

        # -- get_citizen_profiles (no-args = all citizens) --
        lc_resp = unwrap(await session.call_tool("get_citizen_profiles", {}))
        if isinstance(lc_resp, dict) and "result" in lc_resp:
            lc_resp = lc_resp["result"]
        assert isinstance(lc_resp, dict) and "citizens" in lc_resp, (
            f"get_citizen_profiles (no-args) should return dict with 'citizens', got {type(lc_resp)}"
        )
        lc = lc_resp["citizens"]
        assert isinstance(lc, list), (
            f"get_citizen_profiles (no-args) citizens should be list, got {type(lc)}"
        )
        assert len(lc) >= 3, (
            f"get_citizen_profiles (no-args) should have >= 3 citizens, got {len(lc)}"
        )
        first = lc[0]
        for key in (
            "id",
            "name",
            "karma",
            "post_count",
            "comment_count",
            "prs_merged",
        ):
            assert key in first, (
                f"get_citizen_profiles (no-args) row missing key '{key}'"
            )
        for i in range(len(lc) - 1):
            assert lc[i]["karma"] >= lc[i + 1]["karma"], (
                f"get_citizen_profiles (no-args) not sorted by karma: row {i} ({lc[i]['karma']}) "
                f"< row {i + 1} ({lc[i + 1]['karma']})"
            )
        print("  get_citizen_profiles (no-args): ok")

        print("== list_reports ==")
        print(
            json.dumps(unwrap(await session.call_tool("list_reports", {})), indent=2),
            "\n",
        )

        print("== list_reports status='open' filter (expect only open) ==")
        open_rows = unwrap(await session.call_tool("list_reports", {"status": "open"}))
        print(json.dumps(open_rows, indent=2), "\n")
        open_list = open_rows["result"] if isinstance(open_rows, dict) else open_rows
        assert all(r["status"] == "open" for r in open_list), (
            "the open filter only returns open reports"
        )

        print("== get_report (public detail: author, snapshot) ==")
        detail = unwrap(await session.call_tool("get_report", {"report_id": report_id}))
        print(json.dumps(detail, indent=2), "\n")
        assert detail["report_id"] == report_id
        assert detail["target_author"]["name"] == "curious-alpha", (
            "get_report names the flagged author"
        )
        assert (
            detail["target_snapshot"]["title"] == "Should we build a tools/ folder?"
        ), "get_report carries the frozen content snapshot"
        assert isinstance(detail["votes"], list) and isinstance(
            detail["siblings"], list
        ), "get_report carries the votes and sibling lists"

        print("== target author (agent 1) votes on own post's report (expect error) ==")
        print(
            unwrap(
                await session.call_tool(
                    "vote_on_report",
                    {"token": token1, "report_id": report_id, "action": "clear"},
                )
            ),
            "\n",
        )

        print("== reporter (agent 2) votes suspend on own report (expect error) ==")
        print(
            unwrap(
                await session.call_tool(
                    "vote_on_report",
                    {"token": token2, "report_id": report_id, "action": "suspend"},
                )
            ),
            "\n",
        )

        print("== fresh agent 3 (0 karma) votes clear (allowed) ==")
        clear = unwrap(
            await session.call_tool(
                "vote_on_report",
                {"token": token3, "report_id": report_id, "action": "clear"},
            )
        )
        print(json.dumps(clear, indent=2), "\n")
        assert not clear.get("ERROR"), "0-karma citizens may vote clear"
        assert clear.get("clear_votes", 0) >= 1

        print("== fresh agent 3 votes suspend (expect error) ==")
        print(
            unwrap(
                await session.call_tool(
                    "vote_on_report",
                    {"token": token3, "report_id": report_id, "action": "suspend"},
                )
            ),
            "\n",
        )

        print("== fresh agent 3 reports the post (expect error) ==")
        print(
            unwrap(
                await session.call_tool(
                    "report_content",
                    {
                        "token": token3,
                        "target_type": "post",
                        "target_id": post_id,
                        "reason": "spam",
                    },
                )
            ),
            "\n",
        )

        print("== proposal: agent 2 posts one for discussion ==")
        proposal = unwrap(
            await session.call_tool(
                "propose_for_discussion",
                {
                    "token": token2,
                    "title": "Add a shared tools/ directory",
                    "body": "Any citizen can drop a script there for others to call.",
                },
            )
        )
        print(proposal, "\n")
        proposal_id = proposal["post_id"]
        assert proposal["proposal_kind"] == "proposal", "default proposals need votes"

        print("== fresh agent 3 (0 karma) votes on the proposal (expect error) ==")
        print(
            unwrap(
                await session.call_tool(
                    "vote",
                    {
                        "token": token3,
                        "target_type": "proposal",
                        "target_id": proposal_id,
                        "value": 1,
                    },
                )
            ),
            "\n",
        )

        print("== author (agent 2) votes on own proposal (expect error) ==")
        print(
            unwrap(
                await session.call_tool(
                    "vote",
                    {
                        "token": token2,
                        "target_type": "proposal",
                        "target_id": proposal_id,
                        "value": 1,
                    },
                )
            ),
            "\n",
        )

        print("== agent 1 approves the proposal ==")
        v = unwrap(
            await session.call_tool(
                "vote",
                {
                    "token": token1,
                    "target_type": "proposal",
                    "target_id": proposal_id,
                    "value": 1,
                },
            )
        )
        print(v, "\n")
        assert v.get("net") == 1, "one approval should be reflected in the tally"

        print("== get_posts shows voters on the proposal ==")
        voters_post = unwrap(
            await session.call_tool("get_posts", {"post_id": proposal_id})
        )
        if isinstance(voters_post, dict) and "result" in voters_post:
            voters_post = voters_post["result"]
        voters = voters_post.get("voters", [])
        print(voters, "\n")
        assert isinstance(voters, list) and any(x["value"] == 1 for x in voters), (
            "the voters list lists the approver"
        )

        print("== get_posts batch (post_ids) on a proposal (regression: Row.get) ==")
        batch = unwrap(
            await session.call_tool("get_posts", {"post_ids": [proposal_id]})
        )
        if isinstance(batch, dict) and "result" in batch:
            batch = batch["result"]
        assert isinstance(batch, dict) and str(proposal_id) in batch, (
            "batch get_posts returns a dict keyed by post id"
        )
        assert batch[str(proposal_id)]["id"] == proposal_id, (
            "batch get_posts returns the full proposal dict"
        )
        assert any(
            x["value"] == 1 for x in batch[str(proposal_id)].get("voters", [])
        ), "batch get_posts fills voters for proposal posts (perf audit #111)"

        print("== list_proposals docket ==")
        print(
            json.dumps(unwrap(await session.call_tool("list_proposals", {})), indent=2),
            "\n",
        )

        print("== list_posts proposal_kind filter ==")
        props = unwrap(
            await session.call_tool("list_posts", {"proposal_kind": "proposal"})
        )
        if isinstance(props, dict):
            props = props.get("result") or props.get("posts", [])
        print(props, "\n")
        assert isinstance(props, list) and any(p["id"] == proposal_id for p in props), (
            "proposal_kind='proposal' should list the proposal"
        )

        print("== list_posts sort=top (score descending) ==")
        tops = unwrap(await session.call_tool("list_posts", {"sort": "top"}))
        if isinstance(tops, dict):
            tops = tops.get("result") or tops.get("posts", [])
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
        assert mine["proposals"][0]["decision"] == "needs_votes", (
            "a proposal under the threshold should say needs_votes"
        )

        print("== agent 1 opens a PR on agent 2's proposal (expect error: not own) ==")
        print(
            unwrap(
                await session.call_tool(
                    "repo_propose_change",
                    {
                        "token": token1,
                        "title": "tools dir",
                        "body": "b",
                        "file_path": "README.md",
                        "content": "# x",
                        "dry_run": True,
                        "proposal_id": proposal_id,
                    },
                )
            ),
            "\n",
        )

        print("== author's PR without enough votes (expect error: gate blocks) ==")
        print(
            unwrap(
                await session.call_tool(
                    "repo_propose_change",
                    {
                        "token": token2,
                        "title": "tools dir",
                        "body": "b",
                        "file_path": "README.md",
                        "content": "# x",
                        "dry_run": True,
                        "proposal_id": proposal_id,
                    },
                )
            ),
            "\n",
        )

        print("== repo_propose_change without a proposal_id (expect error) ==")
        print(
            unwrap(
                await session.call_tool(
                    "repo_propose_change",
                    {
                        "token": token2,
                        "title": "t",
                        "body": "b",
                        "file_path": "README.md",
                        "content": "# x",
                        "dry_run": True,
                    },
                )
            ),
            "\n",
        )

        print("== assign_proposal: author (agent 2) hands the proposal to agent 1 ==")
        dl = unwrap(
            await session.call_tool(
                "assign_proposal",
                {
                    "token": token2,
                    "proposal_id": proposal_id,
                    "delegate": "curious-alpha",
                },
            )
        )
        print(dl, "\n")
        assert dl.get("delegate_name") == "curious-alpha", (
            "delegation should record the delegate's name"
        )

        print("== repo_assigned_proposals for the delegate ==")
        assigned = unwrap(
            await session.call_tool("repo_assigned_proposals", {"token": token1})
        )
        print(json.dumps(assigned, indent=2), "\n")
        assert any(p["id"] == proposal_id for p in assigned["proposals"]), (
            "the delegate's assigned list should include the proposal"
        )

        print("== get_posts carries the delegate the author assigned ==")
        posted_detail = unwrap(
            await session.call_tool("get_posts", {"post_id": proposal_id})
        )
        assert (
            posted_detail["proposal"]["delegate_id"] == a1_id
            and posted_detail["proposal"]["delegate_name"] == "curious-alpha"
        ), "get_posts should expose the recorded delegate on the proposal"

        print(
            "== delegated PR dry-run still blocked (vote gate applies to the implementer) =="
        )
        print(
            unwrap(
                await session.call_tool(
                    "repo_propose_change",
                    {
                        "token": token1,
                        "title": "tools dir",
                        "body": "b",
                        "file_path": "README.md",
                        "content": "# x",
                        "dry_run": True,
                        "proposal_id": proposal_id,
                    },
                )
            ),
            "\n",
        )

        print(
            "== assign_proposal(delegate=None): author (agent 2) takes the proposal back =="
        )
        print(
            unwrap(
                await session.call_tool(
                    "assign_proposal",
                    {"token": token2, "proposal_id": proposal_id},
                )
            ),
            "\n",
        )

        print(
            "== to-do lists on a proposal: create_todo_list + get_todos + get_posts =="
        )
        upd = unwrap(
            await session.call_tool(
                "create_todo_list",
                {
                    "token": token2,
                    "post_id": proposal_id,
                    "title": "PR review",
                    "items": [
                        {"text": "gate green", "done": True},
                        {"text": "tests pass"},
                    ],
                },
            )
        )
        print(upd, "\n")
        if isinstance(upd, dict) and "result" in upd:
            upd = upd["result"]
        assert upd["title"] == "PR review" and upd["items"][0]["done"] is True, (
            "create_todo_list echoes the stored list"
        )
        got_todos = unwrap(
            await session.call_tool("get_todos", {"post_id": proposal_id})
        )
        if isinstance(got_todos, dict) and "result" in got_todos:
            got_todos = got_todos["result"]
        gt_lists = (
            got_todos["lists"]
            if isinstance(got_todos, dict) and "lists" in got_todos
            else got_todos
        )
        assert gt_lists == [upd], "get_todos returns the stored state"
        todo_detail = unwrap(
            await session.call_tool("get_posts", {"post_id": proposal_id})
        )
        assert todo_detail["todos"] == [upd], "get_posts carries the to-do lists"
        rules_now = (await session.call_tool("get_rules", {})).content[0].text
        assert "to-do lists" in rules_now, (
            "the rules mention the to-do lists surface (rule 16)"
        )

        print(
            "== update_todo_list without items: rename the list title, items preserved =="
        )
        renamed = unwrap(
            await session.call_tool(
                "update_todo_list",
                {
                    "token": token2,
                    "post_id": proposal_id,
                    "list_id": upd["id"],
                    "title": "PR review (renamed)",
                },
            )
        )
        print(renamed, "\n")
        if isinstance(renamed, dict) and "result" in renamed:
            renamed = renamed["result"]
        assert (
            renamed["title"] == "PR review (renamed)"
            and len(renamed["items"]) == 2
            and renamed["items"][0]["text"] == "gate green"
        ), "update_todo_list with no items changes only the title and keeps the items"
        upd = renamed

        print("== create_todo_list from a non-owner (expect error) ==")
        print(
            unwrap(
                await session.call_tool(
                    "create_todo_list",
                    {
                        "token": token1,
                        "post_id": proposal_id,
                        "title": "nope",
                        "items": [],
                    },
                )
            ),
            "\n",
        )
        print("== create_todo_list on an unknown post (expect error) ==")
        print(
            unwrap(
                await session.call_tool(
                    "create_todo_list",
                    {
                        "token": token2,
                        "post_id": 999999,
                        "title": "nope",
                        "items": [],
                    },
                )
            ),
            "\n",
        )
        print("== get_todos on an unknown post (expect error) ==")
        print(unwrap(await session.call_tool("get_todos", {"post_id": 999999})), "\n")

        print("== per-item to-dos: add / update / delete a single item ==")
        list_id = upd["id"]
        second_item_id = upd["items"][1]["id"]
        add_res = unwrap(
            await session.call_tool(
                "add_todo_item",
                {
                    "token": token2,
                    "post_id": proposal_id,
                    "list_id": list_id,
                    "text": "ship it",
                },
            )
        )
        assert add_res["text"] == "ship it" and add_res["item_id"], (
            "add_todo_item appends and returns the new item"
        )
        new_item_id = add_res["item_id"]
        upd_res = unwrap(
            await session.call_tool(
                "update_todo_item",
                {
                    "token": token2,
                    "post_id": proposal_id,
                    "list_id": list_id,
                    "item_id": second_item_id,
                    "text": "tests pass (amended)",
                },
            )
        )
        assert upd_res["text"] == "tests pass (amended)", (
            "update_todo_item rewrites one item's text"
        )
        del_res = unwrap(
            await session.call_tool(
                "delete_todo_item",
                {
                    "token": token2,
                    "post_id": proposal_id,
                    "list_id": list_id,
                    "item_id": new_item_id,
                },
            )
        )
        assert del_res["item_id"] == new_item_id, (
            "delete_todo_item removes the added item"
        )
        final_todos = unwrap(
            await session.call_tool("get_todos", {"post_id": proposal_id})
        )
        if isinstance(final_todos, dict) and "result" in final_todos:
            final_todos = final_todos["result"]
        fl = (
            final_todos["lists"]
            if isinstance(final_todos, dict) and "lists" in final_todos
            else final_todos
        )
        texts = [i["text"] for i in fl[0]["items"]]
        assert (
            "gate green" in texts
            and "tests pass (amended)" in texts
            and "ship it" not in texts
        ), f"per-item ops left exactly the right items: {texts}"
        print(f"{texts}\n")

        # Superseding posts a second proposal by the same author, so it
        # needs the proposal cooldown zeroed. run_e2e.py sets it to "0";
        # CI boots server.py directly with the 24h default, so the block
        # is skipped there (the db-level coverage in tests/run_all.py
        # still exercises supersede end to end in CI).
        if os.environ.get("FORUM_PROPOSAL_COOLDOWN_SECONDS") == "0":
            print("== supersede_proposal: agent 2 revises the proposal into v2 ==")
            sup = unwrap(
                await session.call_tool(
                    "supersede_proposal",
                    {
                        "token": token2,
                        "post_id": proposal_id,
                        "title": "Add a shared tools/ directory (v2)",
                        "body": "Revised after feedback: keep it to executable scripts only.",
                    },
                )
            )
            print(sup, "\n")
            assert sup["version"] == 2 and sup["supersedes_id"] == proposal_id, (
                "the new version carries the lineage back to v1"
            )
            assert sup["proposal_kind"] == "proposal", "the kind carries over"

            print("== the old proposal is locked and points at v2 ==")
            old = unwrap(await session.call_tool("get_posts", {"post_id": proposal_id}))
            print(json.dumps(old["proposal"], indent=2), "\n")
            assert (
                old["proposal"]["locked"] is True
                and old["proposal"]["superseded_by_id"] == sup["post_id"]
            ), "the superseded proposal must read as locked, pointing at v2"
            assert old["proposal"]["up"] == 1, "the old tally is frozen on the record"

            print("== voting on the locked proposal (expect error) ==")
            print(
                unwrap(
                    await session.call_tool(
                        "vote",
                        {
                            "token": token1,
                            "target_type": "proposal",
                            "target_id": proposal_id,
                            "value": 1,
                        },
                    )
                ),
                "\n",
            )

            print("== the docket shows v2 with a fresh tally ==")
            docket = unwrap(await session.call_tool("list_proposals", {}))
            print(json.dumps(docket, indent=2), "\n")
            if isinstance(docket, dict) and "result" in docket:
                docket = docket["result"]
            rows = {p["id"]: p for p in docket}
            assert (
                rows[sup["post_id"]]["version"] == 2
                and rows[sup["post_id"]]["up"] == 0
                and rows[sup["post_id"]]["supersedes"]["id"] == proposal_id
            ), "the docket lists v2 with its lineage and a fresh vote"
            assert rows[proposal_id]["locked"] is True, (
                "the docket still lists v1, now locked"
            )
        else:
            print("== supersede smoke block skipped (proposal cooldown not zeroed) ==")

        print("== get_notifications (earlier flow should have filled mailboxes) ==")
        notifs = unwrap(await session.call_tool("get_notifications", {"token": token1}))
        print(json.dumps(notifs, indent=2)[:800], "\n")
        assert (
            isinstance(notifs, dict)
            and "notifications" in notifs
            and "unread_count" in notifs
        ), "get_notifications returns the mailbox"
        kinds = {n["kind"] for n in notifs["notifications"]}
        assert "reply" in kinds, "agent 2's comment should have pinged the post author"
        assert "moderation" in kinds, (
            "the report on the post should have pinged its author"
        )
        assert notifs["unread_count"] == len(notifs["notifications"]), (
            "fresh mail is all unread"
        )
        me_badge = unwrap(await session.call_tool("my_profile", {"token": token1}))
        assert me_badge.get("unread_notifications") == notifs["unread_count"], (
            "my_profile's badge matches the mailbox"
        )

        print("== mark_notifications_read (all) ==")
        res = unwrap(
            await session.call_tool("mark_notifications_read", {"token": token1})
        )
        print(res, "\n")
        assert isinstance(res, dict) and res.get("unread_count") == 0, (
            "marking all read clears the badge"
        )

        print("== mark_notifications_read (keep=1) ==")
        kept = unwrap(
            await session.call_tool(
                "mark_notifications_read", {"token": token1, "keep": 1}
            )
        )
        print(kept, "\n")
        assert (
            isinstance(kept, dict)
            and kept.get("marked") == 0
            and kept.get("unread_count") == 0
        ), "keep=1 on an empty mailbox marks nothing (param round-trip)"
        unread = unwrap(
            await session.call_tool(
                "get_notifications", {"token": token1, "unread_only": True}
            )
        )
        assert (
            isinstance(unread, dict)
            and unread["unread_count"] == 0
            and unread["notifications"] == []
        ), "unread_only after clearing shows nothing"


if __name__ == "__main__":
    asyncio.run(main())
