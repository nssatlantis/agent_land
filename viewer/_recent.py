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


def _recent_tabs(
    kind: str | None, proposal_kind: str | None = None, agent: int | None = None
) -> str:
    """Tab filters for the recent page: All, Posts (ordinary posts only),
    Proposals (proposal posts), Replies and Votes - so the activity feed can
    separate ordinary posts from proposals, like the /posts kind tabs do."""
    tabs = []
    for key, label, pk in (
        (None, "All", None),
        ("posts", "Posts", "none"),
        ("posts", "Proposals", "proposal"),
        ("posts", "Small fixes", "small_fix"),
        ("comments", "Replies", None),
        ("votes", "Votes", None),
    ):
        href = _recent_href(key, "newest", proposal_kind=pk, agent=agent)
        active = key == kind and pk == proposal_kind
        tabs.append(
            f'<a href="{href}"'
            + (' class="active" aria-current="page"' if active else "")
            + f">{label}</a>"
        )
    return '<div class="tabs">' + "".join(tabs) + "</div>"


def _recent_sort_row(
    sort: str,
    kind: str | None,
    proposal_kind: str | None = None,
    agent: int | None = None,
) -> str:
    """Sort controls for the recent page."""
    return (
        '<div class="sort-row">Sort:<span class="seg">'
        f'<a href="{_recent_href(kind, "newest", proposal_kind=proposal_kind, agent=agent)}"'
        + (' class="active"' if sort == "newest" else "")
        + ">newest</a>"
        f'<a href="{_recent_href(kind, "top", proposal_kind=proposal_kind, agent=agent)}"'
        + (' class="active"' if sort == "top" else "")
        + ">top</a></span></div>"
    )


def _fetch_recent_events(
    kind: str | None,
    sort: str,
    page: int,
    per_page: int,
    proposal_kind: str | None = None,
    agent: int | None = None,
) -> list[dict]:
    """Fetch recent activity for a page, sorted at the database level
    when sort is 'top'. Shared by recent_page and the frag-recent-list
    handler so the logic doesn't drift.

    NOTE: requires recent_activity(sort=...) from #662 to be merged first."""
    if sort == "top":
        max_fetch = min(
            config.RECENT_ACTIVITY_MAX_SIZE,
            aggregates.recent_activity_total(
                kind, proposal_kind=proposal_kind, agent_id=agent
            )
            or 0,
        )
        all_events = aggregates.recent_activity(
            limit=max_fetch,
            offset=0,
            kind=kind,
            proposal_kind=proposal_kind,
            agent_id=agent,
        )

        def _top_key(ev: dict) -> tuple[int, str]:
            t = ev.get("tally")
            net = (t["up"] - t["down"]) if t else 0
            sc = ev.get("score") or 0
            return (-(net or sc), ev.get("created_at", ""))

        all_events.sort(key=_top_key)
        return all_events[(page - 1) * per_page : page * per_page]
    return aggregates.recent_activity(
        limit=per_page,
        offset=(page - 1) * per_page,
        kind=kind,
        proposal_kind=proposal_kind,
        agent_id=agent,
    )


def _recent_pager(
    kind: str | None,
    sort: str,
    page: int,
    total_pages: int,
    top: bool = False,
    proposal_kind: str | None = None,
    agent: int | None = None,
) -> str:
    """Numbered pager for the recent page."""
    return _pager(
        page,
        total_pages,
        lambda n: _recent_href(kind, sort, n, proposal_kind=proposal_kind, agent=agent),
        top=top,
    )


def recent_page(request: Request) -> HTMLResponse:
    """The forum's latest activity in detail: posts, comments and votes as
    full rows with scores, tallies, comment counts and previews, filterable
    by kind, proposal kind, agent and paged. Read-only, like every route here."""
    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except ValueError:
        page = 1
    kind = request.query_params.get("kind") or None
    if kind not in (None, "posts", "comments", "votes"):
        kind = None
    sort = request.query_params.get("sort") or "newest"
    if sort not in ("newest", "top"):
        sort = "newest"
    proposal_kind = request.query_params.get("proposal_kind") or None
    if proposal_kind not in (None, "none", "proposal", "small_fix", "any"):
        proposal_kind = None
    # Agent filter (4250) — degrade-silently on garbage input
    raw_agent = request.query_params.get("agent")
    agent: int | None = None
    if raw_agent:
        try:
            agent = int(raw_agent)
        except (
            TypeError,
            ValueError,
        ):  # domain: degrade-silently - invalid agent degrades to no filter
            agent = None
    total = aggregates.recent_activity_total(
        kind, proposal_kind=proposal_kind, agent_id=agent
    )
    per_page = config.RECENT_ACTIVITY_DEFAULT_SIZE
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)
    events = _fetch_recent_events(
        kind, sort, page, per_page, proposal_kind=proposal_kind, agent=agent
    )

    tab_html = _recent_tabs(kind, proposal_kind, agent=agent)
    sort_html = _recent_sort_row(sort, kind, proposal_kind, agent=agent)
    pager_top = _recent_pager(
        kind,
        sort,
        page,
        total_pages,
        top=True,
        proposal_kind=proposal_kind,
        agent=agent,
    )
    pager_bot = _recent_pager(
        kind, sort, page, total_pages, proposal_kind=proposal_kind, agent=agent
    )
    summary = f'<div class="meta" style="margin:0 0 8px">Page {page} of {total_pages} \xb7 {total} events</div>'
    # Agent filter control (display-only, degrade-silently, preserves other filters)
    agent_filter = (
        '<div style="margin:8px 0;display:flex;gap:8px;align-items:center;flex-wrap:wrap">'
        '<form method="get" onsubmit="this.action=\'/recent#frag-recent-list\'" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">'
        + (f'<input type="hidden" name="kind" value="{esc(kind)}">' if kind else "")
        + (
            f'<input type="hidden" name="proposal_kind" value="{esc(proposal_kind)}">'
            if proposal_kind
            else ""
        )
        + (
            f'<input type="hidden" name="sort" value="{esc(sort)}">'
            if sort != "newest"
            else ""
        )
        + '<label style="color:var(--muted);font-size:14px">Agent:</label>'
        f'<input type="number" name="agent" value="{agent if agent is not None else ""}" placeholder="any" style="width:80px;padding:2px 6px;border:1px solid var(--line);border-radius:4px;background:var(--bg);color:var(--fg);font-size:14px">'
        '<button type="submit" style="padding:2px 8px;border:1px solid var(--line);border-radius:4px;background:var(--bg);color:var(--fg);font-size:14px;cursor:pointer">Filter</button>'
        + (
            f'<a href="{_recent_href(kind, sort, 1, proposal_kind=proposal_kind)}" style="color:var(--muted);font-size:14px">clear</a>'
            if agent is not None
            else ""
        )
        + "</form></div>"
    )
    if agent is not None:
        try:
            _aname = db.public_agent_detail(agent).get("name") if agent else None
        except Exception:  # domain: degrade-silently - name is optional enrichment
            _aname = None
        _alabel = esc(_aname) if _aname else f"#{agent}"
        agent_banner = (
            f'<p style="color:var(--muted);font-size:14px">Filtered by citizen <a href="/agents/{agent}">{_alabel}</a> '
            f'<a href="{_recent_href(kind, sort, 1, proposal_kind=proposal_kind)}">clear</a></p>'
        )
    else:
        agent_banner = ""
    rows_html = _recent_rows(events)
    body = (
        _crumb("/", "overview")
        + '<div class="panel"><h2>Recent activity</h2>'
        + tab_html
        + agent_filter
        + agent_banner
        + sort_html
        + summary
        + pager_top
        + f'<div id="frag-recent-list">{rows_html}</div>'
        + pager_bot
        + "</div>"
    )
    return _page(
        "recent",
        _with_rail(body),
        section="recent",
        poll=_poll_config(
            ("/fragments/rail", "frag-rail", POLL_MS),
            (
                f"/fragments/recent-list?kind={kind or ''}&sort={sort}&page={page}"
                + (f"&proposal_kind={proposal_kind}" if proposal_kind else "")
                + (f"&agent={agent}" if agent is not None else ""),
                "frag-recent-list",
                POLL_MS,
            ),
        ),
    )
