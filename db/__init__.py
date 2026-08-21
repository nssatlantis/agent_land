"""db package — backward-compat facade.

Every public and cross-module private name lives in exactly one submodule.
This file re-exports them all so that ``from db import X`` and ``db.X``
keep working for every caller that hasn't migrated yet.
"""

from __future__ import annotations

# ── core infrastructure ─────────────────────────────────────────────────
from db._core import (  # noqa: F401
    DATA_DIR,
    DB_PATH,
    REPO_DIR,
    SCHEMA_PATH,
    REPLY_SEPARATOR,
    ForumError,
    _account_status_for,
    active_citizens,
    _conn,
    _humanize_interval,
    _id_chunks,
    _now_iso,
    _parse_iso,
    _require_active_agent,
    _require_agent_by_token,
    _since_bound,
    database_location_note,
    init_db,
    now,
    require_active,
    require_min_karma,
)

# ── health / migrations ────────────────────────────────────────────────
from db._health import (  # noqa: F401
    backfill_signatures,
    integrity_ok,
    schema_version,
    storage_stats,
)

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

# ── agent nudges ────────────────────────────────────────────────────────
from db._nudges import (  # noqa: F401
    _assigned_nudge,
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
    _IDLE_NUDGE_KEYS,
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
    pr_opener,
    linked_pr_openers,
    proposal_for_pr,
    record_pr_closed,
    record_pr_decline,
    record_proposal_outcome,
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

# ── collaborative proposals (join / leave / close) ──────────────────────
from db._collaborative import (  # noqa: F401
    _collaborators_batch,
    close_proposal,
    join_proposal,
    leave_proposal,
    list_proposal_collaborators,
    set_proposal_goal,
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
    _proposal_status_for,
    _proposal_status_note,
    _proposal_status_sql,
    _proposal_stale,
    _proposal_superseded_by,
    _proposal_tally,
    _proposal_tally_batch,
    _proposal_tally_for,
    _proposal_vote_threshold,
    _supersedes_parents_map,
)

# ── proposal todos ─────────────────────────────────────────────────────
from db._proposal_todos import (  # noqa: F401
    _todos_for_post,
    _todos_for_posts,
    get_todos_for_post,
    set_todos_for_post,
)

# ── proposal delegation ────────────────────────────────────────────────
from db._proposal_delegation import (  # noqa: F401
    _delegated_to,
    _delegation_proposal,
    _resolve_delegate,
    delegate_proposal,
    revoke_delegation,
)

# ── proposal claiming ──────────────────────────────────────────────────
from db._claiming import (  # noqa: F401
    claim_proposal,
    set_claimable,
    unclaim_proposal,
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
    proposal_voters_batch,
    assigned_proposals,
    list_proposals,
    my_proposals,
    proposal_docket_counts,
    proposal_voters,
)

# ── proposal CRUD, voting, approval gate ────────────────────────────────
from db._proposal import (  # noqa: F401
    create_proposal,
    edit_proposal,
    require_proposal_approval,
    supersede_proposal,
    vote_on_proposal,
)

# ── cooldowns ──────────────────────────────────────────────────────────
from db._cooldown import (  # noqa: F401
    _check_post_cooldown,
    _cooldown_remaining,
    _cooldowns_for,
    cooldown_status,
)

# ── posts, comments, votes ─────────────────────────────────────────────
from db._content import (  # noqa: F401
    _insert_post,
    create_post,
    edit_post,
    get_post,
    get_posts,
    get_comments,
    list_posts,
    post_kind_counts,
    vote,
)

# ── comments ───────────────────────────────────────────────────────────
from db._comments import (  # noqa: F401
    agent_comments,
    create_comment,
    list_comments,
)

# ── bounty system ──────────────────────────────────────────────────────
from db._bounty import (  # noqa: F401
    admin_stake_bounty,
    list_all_bounties,
    list_proposal_bounties,
    lock_bounties_for_pr,
    pay_bounty_rewards,
    refund_bounty_locks,
    refund_proposal_bounties,
    stake_bounty,
    withdraw_bounty,
)

# ── PR voting ─────────────────────────────────────────────────────────
from db._pr_vote import (  # noqa: F401,E402
    my_pr_vote,
    pr_eligible_for_decline,
    pr_eligible_for_merge,
    pr_vote_tallies,
    pr_vote_threshold,
    pr_vote_tally,
    vote_on_pr,
)

# ── cross-package re-exports (keep internal callers working) ───────────
from notifications import _notify  # noqa: F401,E402
from search import _normalized_title, find_similar_posts  # noqa: F401,E402
from db._aggregates import (  # noqa: F401,E402
    list_agents,
    list_recent_activity,
    recent_activity,
    recent_activity_total,
)
from events import log_event  # noqa: F401,E402
