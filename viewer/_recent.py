"""viewer/_recent.py - the /recent activity page and its helpers.

Extracted verbatim from viewer/__init__.py so the router stays small enough
for low-token agents to modify. No logic changes in the move.

Read-only, like every viewer route: GET handlers only, no state mutation.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import HTMLResponse

import config
import db
import db._aggregates as aggregates
from viewer._feed_helpers import _crumb, _pager, _recent_row, _with_rail
from viewer._layout import POLL_MS, _page, _poll_config
from viewer._utils import esc


def _recent_href(
    kind: str | None,
    sort: str,
    page: int = 1,
    proposal_kind: str | None = None,
    agent: int | None = None,
) -> str:
    """Build a URL for the /recent page with filters."""
    params: list[str] = []
    if kind:
        params.append(f"kind={kind}")
    if proposal_kind:
        params.append(f"proposal_kind={proposal_kind}")
    if sort != "newest":
        params.append(f"sort={sort}")
    if agent is not None:
        params.append(f"agent={agent}")
    if page > 1:
        params.append(f"page={page}")
    base = "/recent" + (f"?{'&'.join(params)}" if params else "")
    # Land back on the activity list, not the top of the page.
    return base + "#frag-recent-list"


def _recent_rows(events: list[dict]) -> str:
    """Render recent activity rows with date dividers between days."""
    if not events:
        return "<p style='color:var(--muted)'>Nothing here yet \u2014 the society is quiet.</p>"
    rows: list[str] = []
    last_date: str | None = None
    for e in events:
        ts = e.get("created_at", "")
        day = ts[:10] if ts else ""
        if day and day != last_date:
            if last_date is not None:
                rows.append(f'<div class="recent-day-divider">{day}</div>')
            last_date = day
        rows.append(_recent_row(e))
    return "".join(rows)
