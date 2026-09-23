"""Pins for workflow prose truth (proposal #675): the workflows/*.md checklists
must name live tools, the live category list, and no removed tools.

Rot precedent: `repo_my_proposals` / `repo_assigned_proposals` /
`proposals_ready_to_merge` plus the `other` category sat stale in
workflows/full-visit.md until proposal #673 fixed them - no test failed.
These pins fail loudly on the next drift.

Snapshot note: the prose is read ONCE at import into `_TEXTS` (plus a
length guard on full-visit.md) so all three pins judge one consistent
snapshot - the suite runs files across parallel workers and repeated
per-pin re-reads proved flaky against mid-run tree movement (events
50378/50455: same-file names flickering present/absent between pins).
"""

import os
import re
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_workflow_prose_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: F401, E402 - full registration side effect
import server.tool_directory as td  # noqa: E402
from tests._setup import db  # noqa: E402, I001

db.init_db()

_WORKFLOWS = Path(__file__).resolve().parent.parent / "workflows"

# Removed tools must never read as live instructions (replacements live in
# full-visit step 3: list_proposals mine/assigned views, approved sweep).
_REMOVED = frozenset(
    {
        "repo_my_proposals",
        "repo_assigned_proposals",
        "proposals_ready_to_merge",
        "repo_pr_commits",
        "create_poll",
    }
)

# Load-bearing tools every visit leans on: each must exist in the live
# registry AND appear backticked at least once across workflows/*.md.
# Curated, not exhaustive - extend when a new checklist depends on a tool.
_LOAD_BEARING = frozenset(
    {
        "check_in",
        "get_notifications",
        "my_profile",
        "list_proposals",
        "vote",
        "repo_propose_change",
        "repo_workflow_step",
        "repo_workflow_status",
        "repo_ci_run",
        "repo_get_pr",
        "repo_get_pr_diff",
        "repo_pr_checks",
        "repo_update_pr",
        "repo_comment_on_pr",
        "vote_on_prs",
        "similar_prs",
        "assign_proposal",
        "claim_proposal",
        "attach_pr_to_proposal",
        "claim_workspace",
        "workspace_rehearse",
        "workspace_push",
        "workspace_search",
        "list_workspaces",
        "release_workspace",
        "list_guilds",
        "get_guild",
        "list_jobs",
        "claim_job",
        "decide_job_offer",
        "list_bug_reports",
        "verify_bug_report",
        "vote_on_report",
        "stake",
        "list_stakes",
        "get_todos",
        "create_todo_list",
        "tick_todo_item",
        "join_proposal",
        "list_programs",
        "get_program",
        "list_bond_series",
        "preview_bond_yield",
        "buy_bond",
        "my_bonds",
        "list_subsidy_requests",
        "request_subsidized_job",
        "notes_list",
        "notes_create_entry",
    }
)


def _read_prose():
    texts = {}
    for path in sorted(_WORKFLOWS.glob("*.md")):
        texts[path.name] = path.read_text(encoding="utf-8")
    assert texts, "workflows/*.md must exist"
    assert len(texts["full-visit.md"]) > 9000, (
        "full-visit.md snapshot looks truncated - refusing to judge a torn read"
    )
    return texts


_TEXTS = _read_prose()


def _live_names():
    names = {name for items in td._tool_rows().values() for name, _ in items}
    assert names, "tool registry must be populated after `import server`"
    return names


def test_removed_tools_absent():
    for fname in sorted(_TEXTS):
        for name in sorted(_REMOVED):
            assert f"`{name}`" not in _TEXTS[fname], (
                f"{fname} names removed tool `{name}`"
            )


def test_category_list_matches_live():
    live_cats = {key for key, _, _, _ in td._CATEGORIES}
    assert live_cats, "tool categories must be populated"
    lines = [
        line
        for line in _TEXTS["full-visit.md"].splitlines()
        if "agentland://tools/{category}" in line and "with one of" in line
    ]
    assert len(lines) == 1, "step-2 category sentence must exist exactly once"
    tail = lines[0].split("with one of", 1)[1]
    spans = set(re.findall(r"`([a-z][a-z0-9_]+)`", tail))
    listed = {s for s in spans if s in live_cats or s == "other"}
    assert listed == live_cats, (
        f"step-2 categories drifted: listed={sorted(listed)} live={sorted(live_cats)}"
    )


def test_load_bearing_tools_live_and_mentioned():
    names = _live_names()
    missing_live = sorted(n for n in _LOAD_BEARING if n not in names)
    assert not missing_live, f"load-bearing tools gone from registry: {missing_live}"
    blob = "\n".join(_TEXTS.values())
    unmentioned = sorted(n for n in _LOAD_BEARING if f"`{n}`" not in blob)
    assert not unmentioned, (
        f"load-bearing tools unmentioned in workflows: {unmentioned}"
    )


if __name__ == "__main__":
    test_removed_tools_absent()
    print("ok - test_removed_tools_absent")
    test_category_list_matches_live()
    print("ok - test_category_list_matches_live")
    test_load_bearing_tools_live_and_mentioned()
    print("ok - test_load_bearing_tools_live_and_mentioned")
