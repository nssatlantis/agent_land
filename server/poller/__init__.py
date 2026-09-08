"""server.poller — background pollers (outcome, auto-link, CI batches, vote sweep).

Split package (moved verbatim from server/poller.py): _outcome holds the
outcome ticker and maintenance, _autolink the similarity retro-link ticker,
_batches the open-PR batch workers and unified CI ticker, _vote the vote
sweep and its helpers. This facade re-exports every name so existing
importers (server._app, tests, admin views) keep working unchanged —
including tests that monkeypatch module attributes (see the facade
lookups in _outcome._maybe_gc_vote_labels/_maybe_prune_notifications).
The stdlib/third-party modules the old module imported are re-exported too,
so transitive attribute uses (poller.config, poller.db, poller.os, ...)
keep resolving to the same objects.
"""

import asyncio  # noqa: F401
import concurrent.futures as _cf  # noqa: F401
import os  # noqa: F401
import time  # noqa: F401

import config  # noqa: F401
import db  # noqa: F401
import db._staking as staking_mod  # noqa: F401
import github  # noqa: F401
import logutil  # noqa: F401
import notifications  # noqa: F401
import reports  # noqa: F401
import search  # noqa: F401

# Transitive patch target (test_auto_link patches
# server.poller._closed_pulls_page): bound here so the attribute exists,
# and _auto_link_candidates reads it via this namespace.
from github._reads import _closed_pulls_page  # noqa: F401

from ._autolink import (  # noqa: F401
    _auto_link_candidates,
    _auto_link_similar_poller,
    _auto_link_sweep,
)
from ._batches import (  # noqa: F401
    _CI_NUDGE_BODY_MAX,
    _ci_failure_poller,
    _ci_failure_sweep,
    _first_failure,
    _maybe_checkpoint_economy,
    _maybe_truncate_wal,
    _workflow_ci_green_sweep,
    sweep_pr_comments,
)
from ._outcome import (  # noqa: F401
    _NOTIFICATION_PRUNE_MAX_AGE_SECONDS,
    _PR_ROWS_BACKFILL_MAX_AGE_SECONDS,
    _VOTE_LABEL_GC_MULTIPLIER,
    _collaborative_digest_sweep,
    _drain_closed,
    _last_notification_prune,
    _last_vote_label_gc,
    _maybe_backfill_pr_rows,
    _maybe_gc_vote_labels,
    _maybe_prune_notifications,
    _pr_outcome_poller,
    _process_closed_pr,
    _sweep_orphan_vote_labels,
)
from ._vote import (  # noqa: F401
    _HOLD_LABEL,
    _ensure_local_branch_ok,
    _local_branch_cached_ok,
    _notify_proposal_watchers,
    _pr_conflict_notice,
    _pr_created_epoch,
    _pr_stall_notices,
    _pr_stall_notices_impl,
    _pr_vote_poller,
    _pr_vote_sweep,
)
