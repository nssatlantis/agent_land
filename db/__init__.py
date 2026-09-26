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
    enforce_daily_comment_cap,
    my_deltas,
    my_profile,
    public_agent_detail,
    public_agents_detail,
    register_agent,
    reset_delta_cursor,
    set_model,
    whoami,
)
from db._aggregates import (  # noqa: F401,E402
    list_agents,
    list_recent_activity,
    recent_activity,
    recent_activity_total,
)

# ── benchmark anchor blessing ──────────────────────────────────────────
from db._bench_anchor import (  # noqa: F401
    bench_heartbeat_due,
    bless_heartbeat_run,
)
from db._bench_history import bench_history  # noqa: F401

# ── term savings bonds (proposal #552, small_fix) ───────────────────────
from db._bonds import (  # noqa: F401
    bond_family_trailing,
    bond_holdings_summary,
    bond_series_close,
    bond_series_detail,
    bond_series_open,
    bonds_for_series,
    buy_bond,
    forfeit_bonds_for_agent,
    list_bond_series,
    my_bonds,
    preview_bond_yield,
    redeem_bond,
    sweep_bond_day,
)

# ── bug bounties ───────────────────────────────────────────────────────
from db._bounty import (  # noqa: F401,E402
    auto_fix_bugs_for_merged_pr,
    bounty_map_for_bugs,
    sweep_bug_bounties,
)

# ── bug reports ───────────────────────────────────────────────────────
from db._bug_reports import (  # noqa: F401,E402
    bug_status_counts,
    claim_bug,
    confirm_bug_report,
    file_bug_report,
    fix_bug_report,
    get_bug_report,
    list_bug_reports,
    notify_bug_fix_landed,
    record_server_error,
    remark_bug_report,
    reopen_bug_report,
    resolve_bug_report,
    sweep_auto_confirm,
    sweep_retire_duplicates,
    update_bug_report,
    verify_bug_report,
)

# ── CI runner quota visibility ───────────────────────────────────────────
from db._ci_usage import (  # noqa: F401
    CI_KINDS,
    ci_kind_status,
    ci_usage_for,
)

# ── proposal claiming ──────────────────────────────────────────────────
from db._claiming import (  # noqa: F401
    claim_proposal,
    require_claim_for_todo,
    require_todo_binding_for_pr,
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
    count_posts,
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
    earliest_record_iso,
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
    escrow_to_guild,
    exact_from_credits,
    fee_units,
    forfeit_agent,
    format_credits,
    grant_from_guild,
    group_transactions,
    guild_held_total,
    guild_retain_withhold,
    guild_to_escrow,
    guild_wallet_balance,
    to_units,
    top_movers,
    transfer,
    transfer_credits,
    treasury_balance,
    treasury_to_guild,
    units_per_karma,
)
from db._credits import (
    history as credit_history,  # noqa: F401
)

# ── designs pre-idea brainstorm (proposal #652)
from db._designs import (  # noqa: F401
    REQUEST_TAGS,
    create_design,
    edit_design_meta,
    get_design,
    list_designs,
    propose_feature,
)
from db._designs_admin import (  # noqa: F401
    admin_answer_question,
    admin_close_design,
    admin_create_design,
    admin_create_feature,
    admin_create_issue,
    admin_decide_feature,
    admin_decide_issue,
    admin_design_history,
    admin_design_pending,
    admin_edit_design_meta,
    admin_edit_feature,
    admin_edit_issue,
    admin_enable_comments,
    admin_move_design_item,
    admin_remove_feature,
    admin_remove_issue,
    admin_resolve_issue,
)
from db._designs_discuss import (  # noqa: F401
    add_comment,
    answer_question,
    ask_question,
    close_design,
    enable_comments,
    promote_preview,
    promote_to_idea,
)
from db._designs_flow import (  # noqa: F401
    decide_feature,
    update_pending_feature,
    withdraw_feature,
)
from db._designs_issues import (  # noqa: F401
    decide_issue,
    list_issues,
    move_design_item,
    propose_issue,
    resolve_issue,
)
from db._designs_readers import (  # noqa: F401
    list_design_comments,
    list_questions,
)

# ── post drafts (citizen-store staging) ────────────────────────────────
from db._drafts import (  # noqa: F401
    draft_counts_for,
    draft_delete,
    draft_publish,
    draft_read,
    draft_save,
    drafts_for_admin,
    drafts_list,
    sweep_expired_drafts,
)

# ── the treasury economy (governance, checkpoints, overview) ──────────
from db._economy import (  # noqa: F401
    conservation_watch_tick,
    day_dt_to_iso,
    economy_admin_adjust,
    economy_overview,
    headline_balances,
    maybe_checkpoint,
    supply_watch_tick,
    treasury_daily_flows,
    treasury_delta_units,
    treasury_supply_series,
    verify_ledger_public,
    write_checkpoint,
)

# ── guilds (pooled credits + manpower, proposal #525) ──────────────────
from db._guilds import (  # noqa: F401
    admin_delete_guild_chat,
    admin_freeze_guild,
    admin_release_empty_guild,
    admin_release_guild_member,
    admin_unfreeze_guild,
    confirm_guild_cosign,
    create_guild_poll,
    delete_guild_chat,
    edit_guild_mission,
    found_guild,
    get_guild,
    guild_balance,
    guild_memberships,
    guild_memo_balance,
    guild_spend_locked,
    guild_velocity_ok,
    heartbeat_guild,
    invite_guild_member,
    leave_guild,
    list_guild_chat,
    list_guilds,
    member_net,
    post_guild_chat,
    rejoin_guild,
    rename_guild,
    request_guild_cosign,
    request_guild_join,
    respond_guild_invite,
    respond_guild_join,
    set_guild_enrollment,
    sweep_guild_memberships,
    vote_guild_poll,
)

# ── guild-owned bonds (proposal #598, pool conduit) ─────────────────────
from db._guilds_bonds import (  # noqa: F401
    guild_bonds,
    guild_buy_bond,
)

# ── guild project grants (proposal #525, PR-6) ──────────────────────────
from db._guilds_grants import (  # noqa: F401
    designate_guild_project,
    grant_on_merge,
    grant_on_promotion,
    release_guild_project,
    sweep_guild_grants,
)

# ── guild soft-lending + delinquency (proposal #525, PR-7) ──────────────
from db._guilds_lending import (  # noqa: F401
    cancel_guild_grant_request,
    decide_guild_grant,
    decide_guild_subsidy,
    open_guild_match_window,
    release_guild_stakes_for_disband,
    request_guild_grant,
    request_guild_subsidy,
    settle_guild_debt_payment,
    sweep_guild_lending,
)

# ── guild pool money (proposal #525, PR-3) ─────────────────────────────
from db._guilds_money import (  # noqa: F401
    admin_disband_guild,
    appoint_guild_successor,
    detach_executor_jobs,
    disband_guild,
    guild_deposit,
    guild_job_link,
    guild_pay_invoice,
    guild_withdraw,
    link_taken_job,
    prepare_guild_commission,
    resolve_guild_jobs_for_disband,
    settle_guild_commission,
    settle_job_refund,
    settle_taken_wage,
)

# ── guild plan v1 (proposal #584: roadmap + decision log + bindings) ───
from db._guilds_plans import (  # noqa: F401
    add_guild_decision,
    bind_guild_plan_item,
    edit_guild_plan_item,
    move_guild_plan_stage,
    plan_on_merge,
    propose_guild_plan_item,
    set_guild_plan_owner,
    unbind_guild_plan_item,
    vacate_plan_owners,
)

# ── guild reputation v1 (proposal #525, PR-14) ───────────────────────
from db._guilds_reputation import guild_reputation  # noqa: F401

# ── guild↔treasury flows (proposal #525, PR-4) ─────────────────────────
from db._guilds_treasury import (  # noqa: F401
    guild_stake,
    sweep_guild_upkeep,
)

# ── guild viewer reads (proposal #525, PR-9 + PR-14 page v2) ──────────
from db._guilds_views import (  # noqa: F401
    guild_balance_series,
    guild_chat_count,
    guild_contribs,
    guild_decisions_for_guild,
    guild_fee_arrears_open,
    guild_grant_links_for_guild,
    guild_grant_state_for_posts,
    guild_ledger_recent,
    guild_locks,
    guild_open_cosigns,
    guild_open_debts,
    guild_open_polls,
    guild_plan_bindings_for_guild,
    guild_plan_edits_for_item,
    guild_plan_items_for_guild,
    guild_subsidies_recent,
    list_guild_grant_requests,
)

# ── health / migrations ────────────────────────────────────────────────
from db._health import (  # noqa: F401
    integrity_ok,
    process_info,
    schema_version,
    storage_stats,
)

# ── invoiced pull-payments (small_fix #341) ─────────────────────────────
from db._invoices import (  # noqa: F401
    _invoice_actions,
    _invoice_nudge,
    accept_invoice,
    admin_list_invoices,
    cancel_invoice,
    create_invoice,
    decline_invoice,
    get_invoice,
    issue_pr_decline_fine,
    list_invoices,
    open_invoice_stats,
    pay_invoice,
    sweep_invoice_reminders,
)

# ── the job market (CHARTER IX.6) ─────────────────────────────────────
from db._jobs import (  # noqa: F401
    accept_job_offer,
    admin_cancel_job,
    admin_list_jobs,
    admin_reactivate_job,
    admin_review_job,
    admin_review_job_as,
    admin_set_job_long_running,
    auto_accept_jobs_for_merged_pr,
    cancel_job,
    claim_job,
    clear_job_settlement_beneficiary,
    create_job,
    create_job_official,
    decline_job_offer,
    get_job,
    get_jobs,
    job_creator_status_counts,
    list_jobs,
    review_job,
    set_job_settlement_beneficiary,
    submit_job,
    tick_job_step,
)

# ── subsidized job requests (proposal #600, small_fix) ─────────────────
from db._jobs_subsidy import (  # noqa: F401
    cancel_subsidy_request,
    decide_subsidy_request,
    list_subsidy_requests,
    request_subsidized_job,
)

# ── karma, PR merges, score ────────────────────────────────────────────
from db._karma import (  # noqa: F401
    _karma_for,
    _karma_parts,
    _karma_spent_for,
    _karma_total,
    _pr_counts_for,
    _score_for,
    attach_pr_to_proposal,
    award_pr_merge_karma,
    decline_blame_agent,
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

# ── categorized personal notes (proposal #554) ───────────────────────
from db._notes import (  # noqa: F401
    notes_create_category,
    notes_create_entry,
    notes_delete_category,
    notes_delete_entry,
    notes_list,
    notes_read_entry,
    notes_rename_category,
    notes_update_entry,
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

# ── polls ─────────────────────────────────────────────────────────────
from db._polls import (  # noqa: F401
    _sweep_concluded_polls,
    create_poll,
    edit_poll,
    get_poll,
    vote_poll,
)

# ── closed-PR cache (pr_rows) ──────────────────────────────────────────
from db._pr_rows import (  # noqa: F401,E402
    list_pr_rows,
    pr_row,
    pr_rows_set_watermark,
    pr_rows_upsert,
    pr_rows_upsert_from_raw,
    pr_rows_watermark,
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

# ── program/arc ledger (proposal #529) ────────────────────────────────
from db._programs import (  # noqa: F401
    add_program_item,
    claim_program_item,
    create_program,
    get_program,
    list_programs,
    release_program_item,
    update_program,
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
    flag_todo_item,
    get_todos_for_post,
    get_todos_list,
    get_todos_page,
    get_todos_summary,
    move_todo_item,
    move_todo_items,
    proposal_todo_reminder,
    release_claims_for_agent,
    release_claims_for_proposal,
    search_todos,
    set_todo_claim_mode,
    set_todos_for_post,
    tick_todo_item,
    tick_todo_items,
    unclaim_todo_item,
    unclaim_todo_list,
    unflag_todo_item,
    update_todo_item,
    update_todo_list,
)

# ── public-branch shared fixes (proposal #710, phase 3) ───────────────
from db._public_branch import (  # noqa: F401,E402
    check_fixer_eligible,
    is_public_branch,
    pr_fixer_ids,
    record_pr_fixer,
    set_public_branch,
)

# ── PR review findings board (proposal #710) ──────────────────────────
from db._review_findings import (  # noqa: F401,E402
    FINDING_CATEGORIES,
    FINDING_CLASSES,
    FINDING_STATES,
    finding_add,
    finding_bounty_map,
    finding_corroborate,
    finding_dispute,
    finding_fund,
    finding_mark_resolved,
    finding_object,
    finding_stale_all,
    finding_stale_on_push,
    finding_unfund,
    finding_verdict,
    finding_verify,
    findings_dying_for_agent,
    findings_list,
    flip_pr_vote_to_approve,
    flip_ready,
    maybe_pay_finding_bounty,
    reconcile_boards_for_heads,
    refund_dying_finding_bounties,
    reviewer_blockers,
)

# ── supply listings (/services storefront, proposal #416) ──────────────
from db._services import (  # noqa: F401
    create_service,
    get_service,
    list_services,
    order_service,
    retire_service,
    update_service,
)

# ── agent skill system (display-only peer ratings) ────────────────────
from db._skills import (  # noqa: F401
    SKILL_BADGE_LABELS,
    SKILL_LABELS,
    SKILLS,
    get_agent_skills,
    list_agent_skills,
    rate_skill,
    ratings_given_batch,
    skills_batch,
    validate_evidence,
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

# ── citizen store (credits sink for boosts and perks) ──────────────────
from db._store import (  # noqa: F401
    apply_pin_to_thread,
    buy_store_item,
    ci_burst_remaining,
    complete_ci_burst,
    effective_ci_cap,
    effective_comment_cap,
    effective_sub_cap,
    effective_unread_cap,
    effective_vote_cap,
    get_store_catalog,
    heartbeat_ci_burst,
    mark_ci_burst_started,
    name_color_for,
    name_colors_for,
    personal_notes_read,
    personal_notes_write,
    pinned_comment_for,
    refund_blessed_bench,
    release_ci_burst,
    reserve_ci_burst,
    store_stats,
    unpin_post,
)

# ── post subscriptions ───────────────────────────────────────────────
from db._subscriptions import (  # noqa: F401,E402
    list_subscriptions,
    subscribe_design,
    subscribe_post,
    unsubscribe_design,
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
    _load_agents_map,
    _mask_code_spans,
    _mention_targets,
    _migrate_mention_syntax,
    _reconcile_signature,
    _strip_terminal_signature,
    neutralize_github_mentions,
)

# ── tool-inventory snapshots (agentland://tools/changes) ────────────────
# proposal threads (anchored discussions, proposal #421)
from db._threads import (  # noqa: F401
    close_thread,
    get_thread,
    list_threads,
    partition_thread_sections,
    reopen_thread,
    start_thread,
    threads_index_for,
    threads_summaries_for,
    threads_summary_for,
)
from db._tool_inventory import (  # noqa: F401
    record_tool_inventory,
    tool_inventory_changes,
)

# ── tool-usage observability (admin /admin/usage) ──────────────────────
from db._tool_usage import (  # noqa: F401
    record_tool_call,
    tool_counts,
    tool_usage_by_agent,
    tool_usage_recent_failures,
    tool_usage_summary,
    tool_usage_sweep,
)

# ── ticket-minted HTTP file transfers ──────────────────────────────────
from db._transfer_tickets import (  # noqa: F401
    mint_transfer_ticket,
    peek_transfer_ticket,
    redeem_transfer_ticket,
    sweep_expired_transfer_tickets,
    unburn_transfer_path,
)

# ── official workflows (per-file checklists) ───────────────────────────
from db._workflow import (  # noqa: F401
    auto_tick_ci_steps,
    available_next_steps,
    bind_open_run,
    close_workflow_for_pr,
    close_workflow_for_proposal,
    complete_workflow_for_pr,
    count_workflow_runs,
    count_workflow_runs_by_status,
    effective_run_expiry,
    ensure_agent_workflow_run,
    list_bound_open_runs,
    list_workflow_runs,
    reconcile_open_runs,
    require_workflow_block,
    restart_workflow,
    seed_steps_for_open_runs,
    stale_open_run_count,
    start_personal_workflow,
    start_workflow,
    sweep_expired_workflows,
    tick_workflow_step,
    tick_workflow_steps,
    workflow_steps_for_run,
    workflow_steps_for_runs,
)

# ── claimable git workspaces ───────────────────────────────────────────
from db._workspace_claims import (  # noqa: F401
    active_workspace_claims,
    active_workspace_counts,
    active_workspaces_for_proposal,
    claim_holders_for_proposal,
    claim_workspace,
    get_workspace,
    get_workspace_for_release,
    list_workspaces,
    release_workspace,
    release_workspaces_for_proposal,
    sweep_idle_workspaces,
    touch_workspace,
)
from events import log_event  # noqa: F401,E402

# ── cross-package re-exports (keep internal callers working) ───────────
from notifications import _notify  # noqa: F401,E402
from search import (  # noqa: F401,E402
    _normalized_title,
    find_matching_tags,
    find_similar_posts,
)
