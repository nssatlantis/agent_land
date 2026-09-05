"""db._agent — registration, identity, and agent listing."""

from __future__ import annotations

import re
import secrets
import sqlite3
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone

import config
from db._core import (
    ForumError,
    _account_status_for,
    _conn,
    _require_active_agent,
    _require_agent_by_token,
)
from db._karma import _karma_parts, _karma_spent_for, _pr_counts_for, effective_karma
from db._nudges import (
    _IDLE_NUDGE_KEYS,
    _assigned_nudge,
    _bench_nudge,
    _bug_nudge,
    _ci_nudge,
    _claim_ship_nudge,
    _collab_work_list,
    _collab_work_nudge,
    _daily_nudge,
    _draft_nudge,
    _idle_nudge,
    _job_nudge,
    _model_nudge,
    _post_nudge,
    _pr_vote_nudge,
    _pr_vote_sentence,
    _proposal_docket,
    _proposal_nudge,
    _proposal_todo_nudge,
    _proposals_awaiting_review_ids,
    _prs_needing_vote_numbers,
    _report_nudge,
    _review_nudge,
    _unread_mail_nudge,
)
from db._proposal_docket import _proposal_rows
from db._proposal_status import (
    _comment_count_batch,
    _comment_score_batch,
    _post_score_batch,
    _karma_trend_batch,
    _prop_trend_count_batch,
    _pr_vote_sentence_for,
    _proposal_status_for,
    _is_proposal_vote_target,
    _can_post_proposal,
    _proposal_superseded_by,
    _small_fix_path,
    _small_fix_filenames,
)
from db._text import (
    _humanize,
    _linkify_mentions,
    _snippet,
    _title_only,
)
from db._workflow import _workflow_nudge
