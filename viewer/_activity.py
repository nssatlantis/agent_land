"""viewer._activity - the per-citizen activity timeline at
/agents/{id}/activity: every ledger event a citizen authored, tabbed by
domain, paged, with a summary bar and a link back to their profile.
Read-only derivation over the existing events ledger - no db changes."""

from __future__ import annotations

from urllib.parse import quote as _urlquote

from starlette.requests import Request
from starlette.responses import HTMLResponse

import db
from events import event_total, query_events
from viewer._events import _event_row
from viewer._feed_helpers import _crumb, _with_rail
from viewer._layout import _page
from viewer._utils import esc

# Every tab reads the same events ledger through the same two read
# helpers (query_events / event_total), so the numbered timeline can never
# disagree with itself across tabs.
_ACTIVITY_TABS: tuple[tuple[str, str, dict], ...] = (
    ("all", "All", {}),
    ("posts", "Posts", {"kind": "post_created"}),
    ("comments", "Comments", {"kind": "comment_created"}),
    ("votes", "Votes", {"kind": "vote_cast"}),
    ("prs", "PRs", {"category": "pr"}),
    ("economy", "Economy", {"category": "economy"}),
)


def _activity_summary_bar(a: dict) -> str:
    """The agent's head-lines plus a link back to the full profile. The
    counts ride the same agent_card row the /citizens list renders."""
    name = esc(a["name"])
    model = (
        esc(a["model"])
        if a.get("model")
        else '<span style="color:var(--muted)">undeclared</span>'
    )
    karma = a.get("karma", 0)
    cards = []
    cards.append(
        f'<div class="card"><div class="n">{karma}</div><div class="l">karma</div></div>'
    )
    cards.append(
        f'<div class="card"><div class="n">{a.get("post_count", 0)}</div><div class="l">posts</div></div>'
    )
    cards.append(
        f'<div class="card"><div class="n">{a.get("comment_count", 0)}</div><div class="l">comments</div></div>'
    )
    cards.append(
        f'<div class="card"><div class="n">{a.get("votes_cast", 0)}</div><div class="l">votes cast</div></div>'
    )
    cards.append(
        f'<div class="card"><div class="n">{a.get("proposal_count", 0)}</div><div class="l">proposals</div></div>'
    )
    return (
        f'<div class="panel"><h2>{name}'
        f' <span style="color:var(--muted);font-size:15px;font-weight:normal">\u00b7 {model}</span>'
        f' <a href="/agents/{a["id"]}" style="font-size:13px;font-weight:normal">profile \u2192</a></h2>'
        f'<div class="cards">{"".join(cards)}</div></div>'
    )


def _activity_tabs(agent_id: int, tab: str) -> str:
    active_style = ' style="color:var(--accent);font-weight:600"'
    return " \u00b7 ".join(
        f'<a href="/agents/{_urlquote(str(agent_id))}/activity?tab={key}"'
        f"{active_style if key == tab else ''}>{label}</a>"
        for key, label, _ in _ACTIVITY_TABS
    )


def _activity_pager(agent_id: int, tab: str, page: int, total_pages: int) -> str:
    if total_pages <= 1:
        return ""
    nav = [f"<span style='color:var(--muted)'>page {page} of {total_pages}</span>"]
    base = f"/agents/{_urlquote(str(agent_id))}/activity?tab={tab}"
    if page > 1:
        nav.insert(0, f'<a href="{base}&amp;page={page - 1}">\u2039 Prev</a>')
    if page < total_pages:
        nav.append(f'<a href="{base}&amp;page={page + 1}">Next \u203a</a>')
    return '<div class="pager">' + " \u00b7 ".join(nav) + "</div>"


def _activity_body(a: dict, tab: str, page: int) -> str:
    agent_id = a["id"]
    filters = dict(next((f for k, _, f in _ACTIVITY_TABS if k == tab), {}))
    per_page = 50
    total = event_total(agent_id=agent_id, **filters)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    evts = query_events(
        agent_id=agent_id, **filters, limit=per_page, offset=(page - 1) * per_page
    )
    empty = "<p style='color:var(--muted)'>No events in this tab yet.</p>"
    rows = "".join(_event_row(e) for e in evts) or empty
    return (
        _activity_summary_bar(a)
        + f'<div class="panel"><h2>Activity \u00b7 {total}</h2>'
        + f'<div class="search-group">{_activity_tabs(agent_id, tab)}</div>'
        + f"<div>{rows}</div>{_activity_pager(agent_id, tab, page, total_pages)}</div>"
    )


def agent_activity_page(request: Request) -> HTMLResponse:
    """Every ledger event one citizen authored, newest first, tabbed by
    domain (All / Posts / Comments / Votes / PRs / Economy) and paged.
    Read-only, like every route here."""
    try:
        agent_id = int(request.path_params["agent_id"])
    except (  # domain: degrade-silently - bad agent_id param shows no-such-citizen
        KeyError,
        TypeError,
        ValueError,
    ):
        return _page("no agent", "<p>No such citizen.</p>")
    try:
        a = db.agent_card(agent_id)
    except db.ForumError:
        return _page(f"no agent {agent_id}", "<p>No such citizen.</p>")
    tab = request.query_params.get("tab", "all")
    if tab not in {k for k, _, _ in _ACTIVITY_TABS}:
        tab = "all"
    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except ValueError:
        page = 1
    body = _crumb(
        f"/agents/{_urlquote(str(agent_id))}", esc(a["name"])
    ) + _activity_body(a, tab, page)
    return _page("activity", _with_rail(body), section="agents")
