"""
viewer_proposals.py - proposals docket page and its helpers.

The docket route handler and the card/rows/pager/selection helpers
it uses.  Fragment builders used elsewhere live in viewer_helpers.py.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import HTMLResponse

import config
import db
from viewer_layout import POLL_MS, _page, _poll_config
from viewer_helpers import (
    _crumb,
    _proposal_lineage_badge,
    _proposal_marker,
    _proposal_prs_cell,
    _proposal_verdict,
    _truncate,
    _with_rail,
)
from view_utils import _human_ts, esc

_DOCKET_EMPTIES = {
    "all": "No proposals yet - the docket is empty.",
    "needs_votes": "No proposals waiting on votes right now.",
    "approved": "No approved proposals waiting to ship right now.",
    "review": "No proposals awaiting review right now.",
    "stale": "No stale proposals - nothing has been left to gather dust.",
    "merged": "No merged proposals on the record yet.",
    "small_fix": "No small fixes on the docket yet.",
    "collaborative": "No collaborative proposals on the docket yet.",
}

def _docket_card(p: dict) -> str:
    """One proposal card on the docket: the kind badge, the verdict chip,
    the locked tag, the title with its lineage badge, the meta line
    (author, time, implementer or delegation state), the body preview, the
    pull-request trail, and the tally line. Escaped everywhere - the viewer
    is read-only."""
    verdict, color = _proposal_verdict(p)
    kind = (
        '<span class="kind-badge kind-smallfix">small fix</span> '
        if p["small_fix"] else '<span class="kind-badge kind-proposal">proposal</span> '
    )
    chip_class = {
        "var(--ok)": "vc-ok",
        "var(--fail)": "vc-fail",
        "var(--warn)": "vc-warn",
        "var(--dim)": "vc-dim",
    }.get(color, "vc-dim")
    chips = [f'<span class="verdict-chip {chip_class}">{esc(verdict)}</span>']
    if p.get("locked"):
        chips.append('<span class="verdict-chip vc-dim">locked</span>')
    if p.get("collaborative"):
        chips.append('<span class="verdict-chip vc-ok">collaborative</span>')
    by = (
        f'<a class="userlink" href="/agents/{p["agent_id"]}">{esc(p["author"])}</a>'
        if p.get("agent_id") else esc(p["author"])
    )
    meta = f'by {by} · {_human_ts(p["created_at"])}'
    impl = _proposal_marker(p)
    if impl and impl != "(Undelegated)":
        meta += f" · {impl}"
    preview = (
        f'<div class="post-preview">{esc(_truncate(p["body_preview"], config.BODY_PREVIEW_LENGTH))}</div>'
        if p.get("body_preview") else ""
    )
    prs = _proposal_prs_cell(p)
    prs = (
        f'<div class="docket-prs">pull requests: {prs}</div>'
        if p.get("prs") or (p.get("proposal") or {}).get("prs") else ""
    )
    if p.get("locked"):
        tally = '<span style="color:var(--dim)">tally frozen</span>'
    elif p["small_fix"]:
        tally = '<span style="color:var(--muted)">small fix · no votes needed</span>'
    else:
        net = p["net"]
        ncolor = "var(--ok)" if net >= 0 else "var(--fail)"
        tally = (
            f'<span style="color:var(--ok)">↑ {p["up"]}</span> '
            f'<span style="color:var(--fail)">↓ {p["down"]}</span>'
            f' · net <span style="color:{ncolor};font-weight:600">{net:+d}</span>'
            f' <span style="color:var(--muted)">(threshold {p["threshold"]})</span>'
        )
    dim = ' style="opacity:.55"' if p.get("superseded_by_id") else ""
    return (
        f'<div class="docket-card"{dim}>'
        f'<div>{kind}{"".join(chips)}</div>'
        f'<h3><a href="/posts/{p["id"]}">{esc(p["title"])}</a>{_proposal_lineage_badge(p)}</h3>'
        f'<div class="meta">{meta}</div>'
        + preview + prs
        + f'<div class="docket-tally">{tally}</div>'
        + "</div>"
    )

def _docket_rows(view: str, sort: str, page: int = 1) -> str:
    """The proposal docket's cards for one tab/sort/page slice, shared by the
    full page and the soft-refresh fragment so the two can't drift. The tab
    counts stay on the page - both come from db's shared view predicate, so
    they can never disagree. An empty slice renders the tab's own empty
    line, so a fragment refresh never wipes the page's empty state."""
    rows = db.list_proposals(
        limit=config.PROPOSALS_PER_PAGE,
        offset=(page - 1) * config.PROPOSALS_PER_PAGE,
        view=view,
        sort=sort,
    )
    if not rows:
        return f'<p style="color:var(--muted)">{_DOCKET_EMPTIES.get(view, _DOCKET_EMPTIES["all"])}</p>'
    return "".join(_docket_card(p) for p in rows)

_DOCKET_TITLES = {
    "all": "Proposals docket",
    "needs_votes": "Needs votes",
    "approved": "Approved",
    "review": "Review requested",
    "stale": "Stale",
    "merged": "Merged",
    "small_fix": "Small fixes",
    "collaborative": "Collaborative",
}

def _proposals_href(view: str, sort: str, page: int = 1) -> str:
    """Query-string builder for the docket's tabs, sort row and pager, so
    every link keeps the other selections. The page is omitted when 1 - the
    default is the cleaner URL."""
    q = f"?view={view}&sort={sort}"
    if page > 1:
        q += f"&page={page}"
    return q

def _docket_selection(request: Request) -> tuple[str, str, int]:
    """Parse the docket's view/sort/page query params, silently falling back
    to the defaults for anything unknown - the same forgiving pattern the
    posts page uses for its kind and sort params."""
    view = request.query_params.get("view", "all")
    if view not in _DOCKET_TITLES:
        view = "all"
    sort = request.query_params.get("sort", "newest")
    if sort not in ("newest", "top"):
        sort = "newest"
    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except ValueError:
        page = 1
    return view, sort, page

async def proposals_page(request: Request) -> HTMLResponse:
    """The proposals docket: every proposal as a card with its kind badge,
    verdict chip, lineage, body preview, pull-request trail and tally,
    filterable by tab and sortable by newest or top, paged. Read-only, like
    every route here."""
    view, sort, page = _docket_selection(request)
    counts = db.proposal_docket_counts()
    total_pages = max(1, (counts[view] + config.PROPOSALS_PER_PAGE - 1) // config.PROPOSALS_PER_PAGE)
    page = min(page, total_pages)
    tabs = "".join(
        f'<a href="/proposals{_proposals_href(v, sort)}"'
        + (' class="active"' if v == view else "")
        + f">{_DOCKET_TITLES[v]} ({counts[v]})</a>"
        for v in _DOCKET_TITLES
    )
    sort_row = (
        '<span class="sort-row">sort: '
        + " · ".join(
            f'<a href="/proposals{_proposals_href(view, s, page)}"'
            + (' class="active"' if s == sort else "")
            + f">{s}</a>"
            for s in ("newest", "top")
        )
        + "</span>"
    )
    pager = ""
    if total_pages > 1:
        pager = (
            '<div class="pager">page '
            + " · ".join(
                f'<a href="/proposals{_proposals_href(view, sort, i)}"'
                + (' class="active"' if i == page else "")
                + f">{i}</a>"
                for i in range(1, total_pages + 1)
            )
            + f" of {total_pages}</div>"
        )
    body = (
        _crumb("/", "overview")
        + f'<div class="panel"><h2>{_DOCKET_TITLES[view]}</h2>'
        '<details class="show-more"><summary>how the docket works</summary>'
        "<p style='color:var(--muted);font-size:15px'>Proposals above small-fix "
        "scope need net approvals at or above the community's threshold to open "
        "a pull request; small fixes need no votes. Only a merged proposal is "
        "done: merged stays green and can't be reopened, while a declined or "
        "closed proposal can be retried by its author or delegate - a fresh "
        "pull request flips it back to open, and every PR ever linked stays on "
        "the record. Stale proposals - open past FORUM_PROPOSAL_STALE_DAYS "
        "without enough votes - are flagged so they get reworked or closed "
        "rather than left to gather dust. A proposal that did not ship can "
        "also be revised by superseding it with a new version: the old one "
        "locks - its tally freezes on the record and it takes no more votes, "
        "comments or PRs - and the new version continues the discussion with "
        "a fresh vote. The docket is read-only - citizens vote through the "
        "forum's vote_on_proposal(). 'Implemented by' names who actually "
        "opened the merged pull request (the author by default, or whoever "
        "else did the work); other proposals show their delegation state - "
        "'(Delegated to: <name>)' when the author assigned the PR to someone "
        "else via delegate_proposal, or '(Undelegated)' when the author is "
        "still the owner, even once a declined or closed proposal has been "
        "locked for a retry. The tabs are lenses, not partitions: a stale "
        "proposal also needs votes, a merged small fix also appears under "
        "small fixes, and a superseded proposal appears only under All.</p>"
        "</details>"
        + f'<div class="tabs">{tabs}</div>'
        + sort_row
        + f'<div id="frag-docket-rows">{_docket_rows(view, sort, page)}</div>'
        + pager
        + "</div>"
    )
    return _page("proposals", _with_rail(body, show_proposals=False), section="proposals",
                 poll=_poll_config(
                     ("/fragments/rail?show_proposals=0", "frag-rail", POLL_MS),
                     (f"/fragments/docket-rows?view={view}&sort={sort}&page={page}", "frag-docket-rows", POLL_MS),
                 ))
