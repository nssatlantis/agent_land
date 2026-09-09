"""server.tools.repo — repo MCP tools package (split from server/tools/repo.py).

_ticker holds the debounce pool and snapshot readers; _reads the
read-only tools; _propose PR opening; _pr_ops acting on open PRs;
_govern CI runs, delegation and workflow runs. This facade re-exports
every name so all existing importers (server/__init__, server/poller,
server/admin, tests) keep working unchanged.
"""

from __future__ import annotations

from ._govern import (  # noqa: F401
    _MANAGED_WORKFLOW_KEYS,
    _ci_watch_url_for,
    claim_proposal,
    delegate_proposal,
    repo_ci_run,
    repo_restart_workflow,
    repo_workflow_status,
    repo_workflow_step,
    revoke_delegation,
    set_claimable,
    unclaim_proposal,
)
from ._pr_ops import (  # noqa: F401
    repo_close_pr,
    repo_comment_on_pr,
    repo_resolve_conflicts,
    repo_update_pr,
    vote_on_prs,
)
from ._propose import (  # noqa: F401
    link_pr_to_todo_item,
    repo_propose_change,
)
from ._reads import (  # noqa: F401
    proposals_ready_to_merge,
    repo_assigned_proposals,
    repo_get_pr,
    repo_get_pr_diff,
    repo_list_prs,
    repo_list_tree,
    repo_list_workflow_runs,
    repo_my_proposals,
    repo_my_prs,
    repo_pr_checks,
    repo_pr_commits,
    repo_read_file,
    repo_search,
    similar_prs,
)
from ._ticker import (  # noqa: F401
    _IN_FLIGHT,
    _PENDING,
    _PENDING_LOCK,
    _REQUEUE_ATTEMPTS,
    PENDING,
    _cancel_ticker,
    _debounce_ticker,
    _ensure_ticker,
    debounced_enqueue,
    in_flight_snapshot,
    pending_prs_snapshot,
    pending_snapshot_with_deadlines,
    requeue_attempts_snapshot,
)


def __getattr__(name: str):
    """Forward the mutable ticker task live: _ensure/_cancel_ticker
    REBIND _TICKER_TASK (they don't mutate it), so a static from-import
    above would freeze None forever and the shutdown-await in server/_app
    plus the /admin/ci ticker panel would never see the live task. Every
    other ticker global is mutated in place and safe to bind once."""
    if name == "_TICKER_TASK":
        from . import _ticker

        return _ticker._TICKER_TASK
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
