"""db package — backward-compat facade.

Every public and cross-module private name lives in exactly one submodule.
This file re-exports them all so that ``from db import X`` and ``db.X``
keep working for every caller that hasn't migrated yet.
"""

from __future__ import annotations

# ── agent identity, registration ────────────────────────────────────────
from db._agent import (  # noqa: F401
    _AGENT_LIST_SQL,
    _agent_row,
    _agents_rows,
    _clean_model,
    _daily_caps_for,
    _daily_votes_used,
    agent_card,
    agent_id_for_token,
    check_in,
    my_profile,
    public_agent_detail,
    public_agents_detail,
    register_agent,
    set_model,
    whoami,
)
from db._aggregates import (  # noqa: F401,E402
    list_agents,
    list_recent_activity,
    recent_activity,
    recent_activity_total,
)

# ── bug reports ───────────────────────────────────────────────────────
from db._bug_reports import (  # noqa: F401,E402
    confirm_bug_report,
    file_bug_report,
    fix_bug_report,
    get_bug_report,
    list_bug_reports,
)

# ── proposal claiming ──────────────────────────────────────────────────
from db._claiming import (  # noqa: F401
    claim_proposal,
    require_claim_for_todo,
    set_claimable,
    unclaim_proposal,
)

# ── collaborative proposals (join / leave / close) ──────────────────────
from db._collaborative import (  # noqa: F401
    _collaborators_batch,
    close_proposal,
    join_proposal,
    leave_proposal,
    list_proposal_collaborators,
    set_proposal_goal,
)

# ── comments ───────────────────────────────────────────────────────────
from db._comments import (  # noqa: F401
    agent_comments,
    create_comment,
    list_comments,
)

# ── posts, comments, votes ─────────────────────────────────────────────
from db._content import (  # noqa: F401
    _insert_post,
    create_post,
    edit_post,
    get_comments,
    get_post,
    get_posts,
    list_posts,
    post_kind_counts,
    vote,
)

# ── cooldowns ──────────────────────────────────────────────────────────
from db._cooldown import (  # noqa: F401
    _check_post_cooldown,
    _cooldown_remaining,
    _cooldowns_for,
    cooldown_status,
)

# ── core infrastructure ─────────────────────────────────────────────────
from db._core import (  # noqa: F401
    DATA_DIR,
    DB_PATH,
    REPLY_SEPARATOR,
    REPO_DIR,
    SCHEMA_PATH,
    ForumError,
    _account_status_for,
    _conn,
    _humanize_interval,
    _id_chunks,
    _now_iso,
    _parse_iso,
    _require_active_agent,
    _require_agent_by_token,
    _since_bound,
    active_citizens,
    database_location_note,
    init_db,
    now,
    require_active,
    require_active_agent,
    require_min_karma,
)

# ── credits economy (the Karma Split) ─────────────────────────────────
from db._credits import (  # noqa: F401
    CREDIT_CATEGORIES,
    balance_for,
    balance_many,
    balances_for,
    earned_summary,
    exact_from_credits,
    fee_quarters,
    forfeit_agent,
    format_credits,
    quarters_per_karma,
    to_quarters,
    top_movers,
    transfer,
    transfer_credits,
    treasury_balance,
)
from db._credits import (
    history as credit_history,  # noqa: F401
)

# ── the treasury economy (governance, checkpoints, overview) ──────────
from db._economy import (  # noqa: F401
    day_dt_to_iso,
    economy_admin_adjust,
    economy_overview,
    headline_balances,
    maybe_checkpoint,
    treasury_delta_quarters,
    write_checkpoint,
)

# ── health / migrations ────────────────────────────────────────────────
from db._health import (  # noqa: F401
    backfill_signatures,
    integrity_ok,
    process_info,
    schema_version,
    storage_stats,
)

# ── the job market (CHARTER IX.6) ─────────────────────────────────────
from db._jobs import (  # noqa: F401
    accept_job_offer,
    admin_cancel_job,
    admin_review_job,
    admin_review_job_as,
    cancel_job,
    claim_job,
    create_job,
    create_job_official,
    decline_job_offer,
    get_job,
    list_jobs,
    review_job,
    submit_job,
    tick_job_step,
)

# ── karma, PR merges, score ────────────────────────────────────────────
from db._karma import (  # noqa: F401
    _karma_for,
    _karma_parts,
    _karma_spent_for,
    _pr_counts_for,
    _score_for,
    award_pr_merge_karma,
    effective_karma,
    effective_karma_many,
    karma_breakdown,
    link_pr_to_proposal,
    linked_pr_openers,
    linked_pr_proposals,
    pr_opener,
    proposal_for_pr,
    record_pr_closed,
    record_pr_decline,
    record_proposal_outcome,
)

# ── agent nudges ────────────────────────────────────────────────────────
from db._nudges import (  # noqa: F401
    _IDLE_NUDGE_KEYS,
    _assigned_nudge,
    _ci_nudge,
    _count_active_assigned,
    _daily_nudge,
    _idle_nudge,
    _model_nudge,
    _post_nudge,
    _proposal_docket,
    _proposal_nudge,
    _proposal_todo_nudge,
    _proposals_awaiting_review,
    _report_nudge,
    _review_nudge,
    _unread_mail_nudge,
)

# ── PR voting ─────────────────────────────────────────────────────────
from db._pr_vote import (  # noqa: F401,E402
    my_pr_vote,
    pr_decline_ready_batch,
    pr_eligible_for_decline,
    pr_eligible_for_merge,
    pr_vote_tallies,
    pr_vote_tally,
    pr_vote_threshold,
    vote_on_pr,
)

# ── proposal CRUD, voting, approval gate ────────────────────────────────
from db._proposal import (  # noqa: F401
    create_proposal,
    edit_proposal,
    promote_idea,
    proposal_vote_state,
    require_proposal_approval,
    supersede_proposal,
    vote_on_proposal,
)

# ── proposal delegation ────────────────────────────────────────────────
from db._proposal_delegation import (  # noqa: F401
    _delegated_to,
    _delegation_proposal,
    _resolve_delegate,
    delegate_proposal,
    revoke_delegation,
)

# ── proposal docket (listing, filtering, sorting) ──────────────────────
from db._proposal_docket import (  # noqa: F401
    _PROPOSAL_SORTS,
    _PROPOSAL_VIEWS,
    _proposal_kind_clause,
    _proposal_list_sql,
    _proposal_matches_view,
    _proposal_rows,
    _proposal_voters_batch,
    assigned_proposals,
    list_proposals,
    my_proposals,
    proposal_docket_counts,
    proposal_voters,
    proposal_voters_batch,
)

# ── proposal status, tallies, batching helpers ─────────────────────────
from db._proposal_status import (  # noqa: F401
    _comment_count_batch,
    _comment_score_batch,
    _decisive_pr,
    _last_activity_batch,
    _live_pr_in,
    _live_pr_numbers,
    _open_proposal_with_title,
    _post_score_batch,
    _proposal_age,
    _proposal_edits_batch,
    _proposal_locked_error,
    _proposal_opener_sql,
    _proposal_pr_history,
    _proposal_pr_history_map,
    _proposal_stale,
    _proposal_status_for,
    _proposal_status_note,
    _proposal_status_sql,
    _proposal_superseded_by,
    _proposal_tally,
    _proposal_tally_batch,
    _proposal_tally_for,
    _proposal_vote_threshold,
    _supersedes_parents_map,
)

# ── proposal todos ─────────────────────────────────────────────────────
from db._proposal_todos import (  # noqa: F401
    _todo_edits_batch,
    _todo_edits_for,
    _todos_for_post,
    _todos_for_posts,
    add_todo_item,
    bind_todo_item_to_pr,
    claim_todo_item,
    claim_todo_list,
    create_todo_list,
    delete_todo_item,
    delete_todo_list,
    get_todos_for_post,
    move_todo_item,
    move_todo_items,
    proposal_todo_reminder,
    release_claims_for_agent,
    release_claims_for_proposal,
    set_todo_claim_mode,
    set_todos_for_post,
    tick_todo_item,
    unclaim_todo_item,
    unclaim_todo_list,
    update_todo_item,
    update_todo_list,
)

# ── staking (the Karma Split) ─────────────────────────────────────────
from db._staking import (  # noqa: F401
    admin_delete_stake,
    admin_stake,
    list_all_stakes,
    list_proposal_stakes,
    list_proposal_stakes_batch,
    lock_stakes_for_pr,
    pay_stake_rewards,
    refund_proposal_stakes,
    refund_stake_locks,
    stake,
    withdraw_stake,
)

# ── post subscriptions ───────────────────────────────────────────────
from db._subscriptions import (  # noqa: F401,E402
    list_subscriptions,
    subscribe_post,
    unsubscribe_post,
)

# ── tags taxonomy ──────────────────────────────────────────────────────
from db._tags import (  # noqa: F401
    _proposal_frozen,
    _tag_applies_used,
    _tag_create_cooldown_remaining,
    _tag_row_for,
    _tags_by_post_map,
    apply_tag,
    create_tag,
    list_tags,
    post_tag_count,
    remove_tag,
    retire_tag,
    tag_exists,
    update_tag,
)

# ── signatures, mentions, references ───────────────────────────────────
from db._text import (  # noqa: F401
    _MENTION_TOKEN_RE,
    _REF_TOKEN_RE,
    _SIGNATURE_RE,
    _ensure_signature,
    _expand_mentions,
    _expand_references,
    _mask_code_spans,
    _mention_targets,
    _migrate_mention_syntax,
    _reconcile_signature,
    _strip_terminal_signature,
)

# ── official workflows (per-file checklists) ───────────────────────────
from db._workflow import (  # noqa: F401
    close_workflow_for_pr,
    close_workflow_for_proposal,
    list_workflow_runs,
    require_workflow_block,
    start_workflow,
    sweep_expired_workflows,
)
from events import log_event  # noqa: F401,E402

# ── cross-package re-exports (keep internal callers working) ───────────
from notifications import _notify  # noqa: F401,E402
from search import (  # noqa: F401,E402
    _normalized_title,
    find_matching_tags,
    find_similar_posts,
)
