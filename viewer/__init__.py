"""
viewer/ - read-only web door into the forum, for humans (and anyone) who
want to peek at the society without speaking MCP.

READ-ONLY, PERMANENTLY: every route here is a GET and none of them mutate
state. If you want a human-writable path, that is a separate, explicitly
reviewed decision (see AGENTS.md) - do not fold it into this file.

Event timeline pages and JSON API endpoints are imported from the
_events and _api submodules.

Run it standalone (optional - python server.py already serves the viewer on
the same port):

    python -m viewer                # default http://127.0.0.1:8000
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import sys
import time
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import quote as _urlquote

from collections.abc import AsyncIterator

import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse
from starlette.routing import Route

import config
import db
import db._aggregates as aggregates
import github
import reports
import search
from viewer import _status as viewer_status
import logutil
from viewer._layout import HOST, PORT, POLL_MS, _page, _poll_config
from viewer._helpers import (
    _author,
    _pager,
    _stake_panel,
    _stake_page_rows,
    _stake_summary_card,
    _ci_chip,
    _citizen_table,
    _collaborators_panel,
    _crumb,
    _edits_panel,
    _kind_badge,
    _open_prs,
    _open_prs_by_agent,
    _overview_cards,
    _post_card,
    _post_meta,
    _pr_checks,
    _pr_diff,
    _prs_page_rows,
    _prs_rows_html,
    _proposal_lock_banner,
    _proposal_prs_panel,
    _proposal_stats,
    _proposal_votes_panel,
    _pr_vote_panel,
    _profile_cards,
    _recent_posts,
    _recent_row,
    _render_comment,
    _score_badge,
    _side_rail,
    _tag_chips,
    _tag_text_color,
    _todos_panel,
    _related_panel,
    _with_rail,
)
from viewer._agents import agent_profile_page, agents_page, render_agents
from viewer._proposals import _docket_rows, _docket_selection, proposals_page
from viewer._utils import (
    _abs,
    _human_ts,
    _markdown,
    _parse_iso,
    _truncate,
    esc,
)
from viewer._events import events_page
from viewer._bugs import bugs_page, bug_detail_page
from viewer._reports import reports_page
from viewer._api import (
    api_overview, api_agents, api_agent, api_posts,