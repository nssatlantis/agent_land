"""Pins for workflow prose truth (proposal #675): the workflows/*.md checklists
must name live tools, the live category list, and no removed tools.

Rot precedent: `repo_my_proposals` / `repo_assigned_proposals` /
`proposals_ready_to_merge` plus the `other` category sat stale in
workflows/full-visit.md until proposal #673 fixed them - no test failed.
These pins fail loudly on the next drift.

Snapshot note: the prose is read ONCE at import into `_TEXTS` (plus a
length guard on full-visit.md) so all four pins judge one consistent
snapshot. The read-once shape dates from #B93, which blamed flickering
re-reads for six names reading absent; the cause turned out to be the
mention matcher missing call forms (#B93 closed invalid), so the snapshot
stays for consistency - not because re-reads were ever shown to be flaky.
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
from db._workflow import _parse_workflow_steps  # noqa: E402
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
        "accept_invoice",
        "decline_invoice",
        "claim_todo_item",
        "claim_todo_list",
        "join_proposal",
        "leave_proposal",
    }
)

# Load-bearing tools every visit leans on: each must exist in the live
# registry AND appear backticked at least once across workflows/*.md.
# Curated, not exhaustive - extend when a new checklist depends on a tool.
# Plain literals, not \x escapes: six of these names were escaped
# codepoint-by-codepoint while #B93 blamed homoglyph emission for six
# present names reading absent. The cause was the matcher, not the bytes -
# those six appear in the prose only as call forms (`claim_job(job_id)`),
# which an exact "`name`" span match cannot see, so the same six failed on
# every run, deterministically. test_spans_ascii_audit is the pin that
# guards byte-hygiene of every backticked span (prose and this file), so a
# lookalike codepoint in a tool name still fails loudly.
_LOAD_BEARING = frozenset(
    {
        "check_in",
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
        "cancel_subsidy_request",
        "notes_list",
        "notes_create_entry",
        "notes_create_category",
        "notes_read_entry",
        "notes_update_entry",
        "get_store_catalog",
        "redeem_bond",
        "get_notifications",
    }
)
###PINS-B###