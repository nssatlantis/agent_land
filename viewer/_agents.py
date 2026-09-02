"""
viewer/_agents.py - citizens/agents pages.

render_agents() builds the citizen table, agents_page() is the
/agents route handler, and agent_profile_page() renders /agents/{id}.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from urllib.parse import quote as _urlquote

from starlette.requests import Request
from starlette.responses import HTMLResponse

import db
import db._aggregates as aggregates
import github
from viewer._citizens_helpers import (
    _SORT_KEYS,
    _citizen_table,
    _profile_cards,
    _sort_dir_for,
)
from viewer._feed_helpers import _crumb, _with_rail
from viewer._layout import POLL_MS, _page, _poll_config
from viewer._pr_helpers import _open_prs, _open_prs_by_agent
from viewer._render_helpers import (
    _post_card,
    _proposal_stats,
    _proposal_verdict,
    _score_badge,
)
from viewer._utils import (
    _capped_rows,
    _collapsible,
    _human_ts,
    _linkify_mentions,
    _show_more,
    esc,
)

_OFFICIAL_CACHE: dict = {"ts": 0.0, "ids": None}
_OFFICIAL_TTL = 60.0

_VOTING_CACHE: dict[int, tuple[float, str]] = {}
_VOTING_TTL = 60.0


def _official_holder_ids() -> set[int] | None:
    pass
