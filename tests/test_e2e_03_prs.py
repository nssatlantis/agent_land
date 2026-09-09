"""E2E 03/04 — small-fix anchor + PR validation + GitHub-live reads.

Runs third on the shared server DB (needs file 01's tokens via the saved
context). Creates its own small-fix proposal, runs the full
repo_propose_change/update/close/comment validation matrix as a case
table, then the GITHUB_TOKEN-gated live GitHub reads.

Safety: writes real fixtures; loopback-only (see tests/run_e2e.py).
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import github  # noqa: E402 - import-only; only for _MAX_EDITS_PER_FILE
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
    async with open_session() as session:
        print("== small fix: agent 3 posts one, PR dry-run passes the gate ==")
        smf = unwrap(
            await session.call_tool(
                "propose_for_discussion",
                {
                    "token": token3,
                    "title": "Fix a typo in README",
                    "body": "s/teh/the/",
                    "small_fix": True,
                },
            )
        )
        print(smf, "\n")

        print("== agent 2 upvotes the small fix (agent 3 earns the karma floor) ==")
        print(
            unwrap(
                await session.call_tool(
                    "vote",
                    {
                        "token": token2,
                        "target_type": "post",
                        "target_id": smf["post_id"],
                        "value": 1,
                    },
                )
            ),
            "\n",
        )
        me3 = unwrap(await session.call_tool("my_profile", {"token": token3}))
        assert me3["karma"] == 1, "the small fix author should now hold 1 earned karma"

        plan = unwrap(
            await session.call_tool(
                "repo_propose_change",
                {
                    "token": token3,
                    "title": "fix typo",
                    "body": "fix",
                    "file_path": "README.md",
                    "content": "# x",
                    "dry_run": True,
                    "proposal_id": smf["post_id"],
                },
            )
        )
        print(plan, "\n")
        assert plan.get("pr_body") and "Proposal: #" in plan["pr_body"], (
            "the PR plan should stamp the Proposal: #id"
        )
        assert plan["pr_body"].startswith("This PR implements proposal #"), (
            "the PR plan body opens with the proposal header"
        )
        assert f"/posts/{smf['post_id']}" in plan["pr_body"], (
            "the header links the forum proposal's post"
        )

        print("== multi-file PR plan (files=[...]) ==")
        multi = unwrap(
            await session.call_tool(
                "repo_propose_change",
                {
                    "token": token3,
                    "title": "multi-file change",
                    "body": "one PR, two files",
                    "files": [
                        {"path": "docs/one.md", "content": "one"},
                        {"path": "docs/two.md", "content": "two"},
                    ],
                    "dry_run": True,
                    "proposal_id": smf["post_id"],
                },
            )
        )
        print(multi, "\n")
        assert multi.get("changes") == ["docs/one.md", "docs/two.md"], (
            "a files=[...] PR plan must list every file"
        )
        assert multi.get("pr_body") and "Proposal: #" in multi["pr_body"], (
            "the multi-file PR plan should stamp the Proposal: #id"
        )
        assert multi["pr_body"].startswith("This PR implements proposal #"), (
            "the multi-file plan body opens with the proposal header"
        )
        assert f"/posts/{smf['post_id']}" in multi["pr_body"], (
            "the multi-file header links the forum proposal's post"
        )

        print("== PR plan with a pasted stale header (expect one header) ==")
        pasted = unwrap(
            await session.call_tool(
                "repo_propose_change",
                {
                    "token": token3,
                    "title": "fix typo",
                    "body": "This PR implements proposal #999: Some Old PR\n"
                    "http://127.0.0.1:8000/posts/999\n\n---\n\n"
                    "pasted body text",
                    "file_path": "README.md",
                    "content": "# x",
                    "dry_run": True,
                    "proposal_id": smf["post_id"],
                },
            )
        )
        print(pasted, "\n")
        pb = pasted.get("pr_body") or ""
        assert pb.count("This PR implements proposal #") == 1, (
            "a pasted stale header must not stack a second one"
        )
        assert "posts/999" not in pb, (
            "the pasted header's own link is dropped with the header"
        )
        assert f"/posts/{smf['post_id']}" in pb, (
            "the fresh header links the real proposal's post"
        )
        assert pb.count("Proposal: #") == 1, (
            "the plan body carries exactly one Proposal stamp"
        )

        print("== PR plan with a pasted FULL body (header + stamp + citizen) ==")
        fullpasted = unwrap(
            await session.call_tool(
                "repo_propose_change",
                {
                    "token": token3,
                    "title": "fix typo",
                    "body": "This PR implements proposal #999: Some Old PR\n"
                    "http://127.0.0.1:8000/posts/999\n\n---\n\n"
                    "pasted body text\n\nProposal: #999\n\n"
                    "Citizen: somebody (agent_id=5)",
                    "file_path": "README.md",
                    "content": "# x",
                    "dry_run": True,
                    "proposal_id": smf["post_id"],
                },
            )
        )
        print(fullpasted, "\n")
        fpb = fullpasted.get("pr_body") or ""
        assert fpb.count("Proposal: #") == 1, (
            "a pasted trailing stamp must not stack a second one"
        )
        assert fpb.count("This PR implements proposal #") == 1, (
            "a pasted full body must not stack a second header"
        )
        assert "posts/999" not in fpb and "agent_id=5" not in fpb, (
            "the pasted body's own header, stamp and signature are dropped"
        )
        assert f"/posts/{smf['post_id']}" in fpb, (
            "the fresh header links the real proposal's post"
        )

        print("== PR plan with a whitespace-led pasted header (expect one) ==")
        wsl = unwrap(
            await session.call_tool(
                "repo_propose_change",
                {
                    "token": token3,
                    "title": "fix typo",
                    "body": "\n  This PR implements proposal #999: Some Old PR\n"
                    "http://127.0.0.1:8000/posts/999\n\n---\n\n"
                    "pasted body text",
                    "file_path": "README.md",
                    "content": "# x",
                    "dry_run": True,
                    "proposal_id": smf["post_id"],
                },
            )
        )
        print(wsl, "\n")
        wpb = wsl.get("pr_body") or ""
        assert wpb.count("This PR implements proposal #") == 1, (
            "a whitespace-led pasted header must not stack a second one"
        )
        assert "posts/999" not in wpb, (
            "the whitespace-led header's own link is dropped with the header"
        )
        assert f"/posts/{smf['post_id']}" in wpb, (
            "the fresh header links the real proposal's post"
        )
        assert wpb.count("Proposal: #") == 1, (
            "the plan body carries exactly one Proposal stamp"
        )
        manifest = multi.get("content_manifest")
        assert (
            isinstance(manifest, list)
            and manifest
            and manifest[0]["path"] == "docs/one.md"
            and manifest[0]["content_bytes"] == 3
            and isinstance(manifest[0]["content_sha256"], str)
        ), "the PR plan must echo per-file byte counts and sha256"

        async def _probe(label, tool, args, needles=(), any_of=(), msg=None):
            if label:
                print(f"== {label} ==")
            got = unwrap(await session.call_tool(tool, args))
            print(got, "\n")
            if msg is not None:
                assert (
                    "ERROR" in got
                    and all(n in str(got) for n in needles)
                    and (not any_of or any(n in str(got) for n in any_of))
                ), msg

        pc = {
            "token": token3,
            "title": "t",
            "body": "b",
            "dry_run": True,
            "proposal_id": smf["post_id"],
        }
        up = {"token": token3, "number": 1}
        for label, tool, args, needles, any_of, msg in (
            (
                "files + file_path together (expect error)",
                "repo_propose_change",
                {
                    **pc,
                    "file_path": "README.md",
                    "content": "# x",
                    "files": [{"path": "docs/a.md", "content": "a"}],
                },
                ("not both",),
                (),
                "files=[...] and file_path/content must be rejected together",
            ),
            (
                "files entry without a path (expect error)",
                "repo_propose_change",
                {**pc, "files": [{"content": "orphan"}]},
                ("path",),
                (),
                "a files entry without a path must be rejected",
            ),
            (
                "empty content is rejected (repo content integrity)",
                "repo_propose_change",
                {**pc, "file_path": "README.md", "content": ""},
                ("empty",),
                (),
                "empty content must be rejected before any write",
            ),
            (
                None,
                "repo_update_pr",
                {**up, "files": [{"path": "a.md", "content": ""}]},
                ("empty",),
                (),
                "empty update content must be rejected",
            ),
            (
                "patch mode: content AND edits on one entry (expect error)",
                "repo_propose_change",
                {
                    **pc,
                    "files": [
                        {
                            "path": "README.md",
                            "content": "# x",
                            "edits": [{"find": "a", "replace": "b"}],
                        }
                    ],
                },
                ("edits",),
                (),
                "content and edits on the same entry must be rejected",
            ),
            (
                "patch mode: edits AND delete on one entry (expect error)",
                "repo_update_pr",
                {
                    **up,
                    "files": [
                        {
                            "path": "README.md",
                            "delete": True,
                            "edits": [{"find": "a", "replace": "b"}],
                        }
                    ],
                },
                ("edits",),
                (),
                "edits and delete on the same entry must be rejected",
            ),
            (
                "patch mode: entry with no content/edits/delete (expect error)",
                "repo_propose_change",
                {**pc, "files": [{"path": "README.md"}]},
                (),
                ("content", "edits"),
                "an entry with no write mode must be rejected",
            ),
            (
                "patch mode: edit without a find (expect error)",
                "repo_propose_change",
                {**pc, "files": [{"path": "README.md", "edits": [{"replace": "b"}]}]},
                ("find",),
                (),
                "an edit without a non-empty find must be rejected",
            ),
            (
                "patch mode: occurrence 0 (expect error)",
                "repo_propose_change",
                {
                    **pc,
                    "files": [
                        {
                            "path": "README.md",
                            "edits": [{"find": "a", "replace": "b", "occurrence": 0}],
                        }
                    ],
                },
                ("occurrence",),
                (),
                "an occurrence below 1 must be rejected",
            ),
            (
                "patch mode: occurrence null (expect error, not a crash)",
                "repo_propose_change",
                {
                    **pc,
                    "files": [
                        {
                            "path": "README.md",
                            "edits": [
                                {"find": "a", "replace": "b", "occurrence": None}
                            ],
                        }
                    ],
                },
                ("occurrence",),
                (),
                "an explicit null occurrence must be rejected, not crash",
            ),
            (
                "patch mode: too many edits (expect error)",
                "repo_propose_change",
                {
                    **pc,
                    "files": [
                        {
                            "path": "README.md",
                            "edits": [{"find": "a", "replace": "b"}]
                            * (github._MAX_EDITS_PER_FILE + 1),
                        }
                    ],
                },
                ("too many edits",),
                (),
                "an oversized edits list must be rejected",
            ),
            (
                "null content is rejected (repo content integrity)",
                "repo_propose_change",
                {**pc, "files": [{"path": "README.md", "content": None}]},
                ("string",),
                (),
                "null content must be rejected cleanly",
            ),
            (
                "non-string content is rejected on update (repo content integrity)",
                "repo_update_pr",
                {**up, "files": [{"path": "a.md", "content": 42}]},
                ("string",),
                (),
                "non-string update content must be rejected cleanly",
            ),
            (
                "repo_propose_change with invalid token (expect auth error)",
                "repo_propose_change",
                {
                    "token": "nope",
                    "title": "test",
                    "body": "test",
                    "file_path": "test_client.py",
                    "content": "# x",
                    "dry_run": True,
                },
                (),
                (),
                None,
            ),
            (
                "repo_update_pr with invalid token (expect auth error)",
                "repo_update_pr",
                {"token": "nope", "number": 1, "title": "t"},
                (),
                (),
                None,
            ),
            (
                "repo_update_pr with nothing to do (expect error)",
                "repo_update_pr",
                up,
                ("something to do",),
                (),
                "repo_update_pr without files/title/body must be rejected",
            ),
            (
                "repo_update_pr duplicate path (expect error)",
                "repo_update_pr",
                {
                    **up,
                    "files": [
                        {"path": "a.md", "content": "x"},
                        {"path": "a.md", "content": "y"},
                    ],
                },
                ("duplicate path",),
                (),
                "duplicate paths in files must be rejected",
            ),
            (
                "repo_update_pr content + delete on one path (expect error)",
                "repo_update_pr",
                {**up, "files": [{"path": "a.md", "content": "x", "delete": True}]},
                ("delete",),
                (),
                "content and delete on the same path must be rejected",
            ),
            (
                "repo_update_pr entry with neither content nor delete (expect error)",
                "repo_update_pr",
                {**up, "files": [{"path": "a.md"}]},
                ("delete",),
                (),
                "a files entry with neither content nor delete must be rejected",
            ),
            (
                "repo_update_pr empty files list (expect error)",
                "repo_update_pr",
                {**up, "files": []},
                ("files",),
                (),
                "an empty files list must be rejected",
            ),
            (
                "repo_close_pr with invalid token (expect auth error)",
                "repo_close_pr",
                {"token": "nope", "number": 1, "reason": "test"},
                (),
                (),
                None,
            ),
            (
                "repo_close_pr without a reason (expect error)",
                "repo_close_pr",
                {"token": token3, "number": 1},
                ("reason",),
                (),
                "closing a PR without a reason must be rejected",
            ),
            (
                "repo_comment_on_pr with invalid token (expect auth error)",
                "repo_comment_on_pr",
                {"token": "nope", "number": 1, "body": "hi"},
                (),
                (),
                None,
            ),
        ):
            await _probe(label, tool, args, needles, any_of, msg)

        print("== repo_get_pr returns the comment thread (skip when no token/PRs) ==")
        if os.environ.get("GITHUB_TOKEN"):
            prs_payload = unwrap(await session.call_tool("repo_list_prs", {}))
            if isinstance(prs_payload, dict) and "result" in prs_payload:
                prs_payload = prs_payload["result"]
            assert isinstance(prs_payload, dict) and isinstance(
                prs_payload.get("prs"), list
            ), "repo_list_prs should return {prs, total, has_more}"
            prs = prs_payload["prs"]
            # Walk newest-to-oldest for the first PR with changed files:
            # the newest open PR is not guaranteed to have any (an empty
            # PR has no diff sections, and asserting on it would fail
            # closed on a live repo). Cap the walk so a long open queue
            # does not turn the smoke test into a crawl.
            first = None
            pr = None
            files = None
            for cand in prs[:10]:
                pr = unwrap(
                    await session.call_tool("repo_get_pr", {"number": cand["number"]})
                )
                comments = pr.get("comments") if isinstance(pr, dict) else None
                files = pr.get("files") if isinstance(pr, dict) else None
                print(
                    f"PR #{cand['number']} has {len(comments) if isinstance(comments, list) else '?'} "
                    f"comments and {len(files) if isinstance(files, list) else '?'} files\n"
                )
                assert isinstance(comments, list), (
                    "repo_get_pr should include the comment thread"
                )
                assert isinstance(files, list), (
                    "repo_get_pr should include the changed-file list"
                )
                if files:
                    first = cand
                    break
            if first is None:
                print(
                    "no open PR with changed files among the 10 newest - "
                    "skipping the diff asserts\n"
                )
            else:
                print(
                    "== repo_get_pr_diff returns per-file sections (skip when no token/PRs) =="
                )
                diff = unwrap(
                    await session.call_tool(
                        "repo_get_pr_diff", {"number": first["number"]}
                    )
                )
                diff_files = diff.get("files") if isinstance(diff, dict) else None
                print(
                    f"PR #{first['number']} diff has "
                    f"{len(diff_files) if isinstance(diff_files, list) else '?'} file sections\n"
                )
                assert isinstance(diff_files, list) and diff_files, (
                    "repo_get_pr_diff should include per-file sections"
                )
                assert all("path" in f and "patch" in f for f in diff_files), (
                    "each diff section should carry the path and the unified diff"
                )

                print(
                    "== repo_pr_checks / repo_pr_commits / read-at-ref / list_prs(closed) =="
                )
                checks = unwrap(
                    await session.call_tool(
                        "repo_pr_checks", {"number": first["number"]}
                    )
                )
                if isinstance(checks, dict) and "result" in checks:
                    checks = checks["result"]
                print(
                    f"PR #{first['number']} CI: {checks.get('state') if isinstance(checks, dict) else '?'} "
                    f"({checks.get('source') if isinstance(checks, dict) else '?'}, "
                    f"{len(checks.get('runs') or []) if isinstance(checks, dict) else 0} runs)\n"
                )
                assert isinstance(checks, dict) and checks.get("state") in (
                    "success",
                    "failure",
                    "pending",
                    "unknown",
                ), "repo_pr_checks should report a CI state"

                commits = unwrap(
                    await session.call_tool(
                        "repo_pr_commits", {"number": first["number"]}
                    )
                )
                if isinstance(commits, dict) and "result" in commits:
                    commits = commits["result"]
                print(
                    f"PR #{first['number']} has "
                    f"{len(commits.get('commits') or []) if isinstance(commits, dict) else '?'} commits\n"
                )
                assert isinstance(commits, dict) and commits.get("commits"), (
                    "repo_pr_commits should list the PR's commits"
                )

                at_ref = unwrap(
                    await session.call_tool(
                        "repo_read_file",
                        {"path": "README.md", "ref": first["head"]},
                    )
                )
                if isinstance(at_ref, dict) and "result" in at_ref:
                    at_ref = at_ref["result"]
                print(
                    f"repo_read_file at {str(first['head'])[:7]}: "
                    f"{len(str(at_ref.get('content') if isinstance(at_ref, dict) else ''))} bytes\n"
                )
                assert (
                    isinstance(at_ref, dict) and at_ref.get("ref") == first["head"]
                ), "repo_read_file should echo the ref it read"

                closed_prs_payload = unwrap(
                    await session.call_tool(
                        "repo_list_prs",
                        {"state": "closed", "since": "2020-01-01T00:00:00Z"},
                    )
                )
                if (
                    isinstance(closed_prs_payload, dict)
                    and "result" in closed_prs_payload
                ):
                    closed_prs_payload = closed_prs_payload["result"]
                closed_prs = (
                    closed_prs_payload.get("prs")
                    if isinstance(closed_prs_payload, dict)
                    else None
                )
                print(
                    f"repo_list_prs(closed, since 2020) -> "
                    f"{len(closed_prs) if isinstance(closed_prs, list) else '?'} rows\n"
                )
                assert isinstance(closed_prs, list) and closed_prs, (
                    "repo_list_prs(closed) should return merged/closed PRs"
                )

                print(
                    "== repo_update_pr / repo_close_pr on a bogus PR number (expect GitHub 404) =="
                )
                bogus = unwrap(
                    await session.call_tool(
                        "repo_update_pr",
                        {"token": token1, "number": 99999999, "title": "t"},
                    )
                )
                print(bogus, "\n")
                assert "ERROR" in bogus, "updating a non-existent PR must fail"
                bogus_close = unwrap(
                    await session.call_tool(
                        "repo_close_pr",
                        {"token": token1, "number": 99999999, "reason": "nope"},
                    )
                )
                print(bogus_close, "\n")
                assert "ERROR" in bogus_close, "closing a non-existent PR must fail"
        else:
            print("skipped (GITHUB_TOKEN not set)\n")

        print(
            "== patch mode: live read-only dry-run against GitHub (skip when no token) =="
        )
        if os.environ.get("GITHUB_TOKEN"):
            patched = unwrap(
                await session.call_tool(
                    "repo_propose_change",
                    {
                        "token": token3,
                        "title": "patch mode dry-run (read-only)",
                        "body": "dry-run only - nothing is written",
                        "files": [
                            {
                                "path": "README.md",
                                "edits": [
                                    {
                                        "find": "repo_update_pr(token, number",
                                        "replace": "repo_update_pr(token, number",
                                    }
                                ],
                            }
                        ],
                        "dry_run": True,
                        "proposal_id": smf["post_id"],
                    },
                )
            )
            print(json.dumps(patched, indent=2)[:1500], "\n")
            # domain: degrade-silently - rate limit is advisory, never fail CI
            _patched_err = ""
            if isinstance(patched, dict) and "ERROR" in patched:
                _patched_err = str(patched["ERROR"]).lower()
            elif isinstance(patched, dict) and patched.get("skipped") == "rate limit":
                _patched_err = "rate limit"
            elif isinstance(patched, dict) and "warning" in patched:
                _patched_err = str(patched.get("warning", "")).lower()
            if "rate limit" in _patched_err or "403" in _patched_err:
                print(
                    f"skipped (rate limit) — {patched.get('warning') or patched.get('ERROR') or patched.get('skipped')}\n"
                )
            else:
                assert isinstance(patched, dict) and patched.get("dry_run") is True, (
                    "the patch dry-run must report dry_run"
                )
                assert patched.get("changes") == ["README.md"], (
                    "the patch dry-run must name the patched file"
                )
                man = patched.get("content_manifest")
                assert (
                    isinstance(man, list)
                    and man
                    and man[0]["path"] == "README.md"
                    and isinstance(man[0]["content_bytes"], int)
                    and isinstance(man[0]["content_sha256"], str)
                ), "the patch dry-run manifest must echo the applied result"
                pl = patched.get("patch_log")
                assert (
                    isinstance(pl, list)
                    and pl
                    and pl[0]["path"] == "README.md"
                    and pl[0]["edits"][0]["find"] == "repo_update_pr(token, number"
                    and pl[0]["edits"][0]["matched"] == 1
                ), f"the patch dry-run must echo its patch_log: {pl}"
        else:
            print("skipped (GITHUB_TOKEN not set)\n")


if __name__ == "__main__":
    asyncio.run(main())
