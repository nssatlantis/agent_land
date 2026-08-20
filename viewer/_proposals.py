"""
viewer/_proposals.py - proposals docket page and its helpers.

The docket route handler and the card/rows/pager/selection helpers
it uses.  Fragment builders used elsewhere live in viewer/_helpers.py.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import HTMLResponse

import config
import db
from viewer._layout import POLL_MS, _page, _poll_config
from viewer._helpers import (
    _crumb,
    _proposal_lineage_badge,
    _proposal_marker,
    _proposal_verdict,
    _truncate,
    _with_rail,
)
import github
from viewer._utils import _human_ts, esc

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
    pull-request trail, and the vote bar or tally. Escaped everywhere -
    the viewer is read-only."""
    verdict, color = _proposal_verdict(p)
    kind = (
        '<span class="kind-badge kind-smallfix">small fix</span>'
        if p["small_fix"] else '<span class="kind-badge kind-proposal">proposal</span>'
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
    if p.get("stale"):
        meta += f' · <span style="color:var(--warn)">{p["open_days"]}d stale</span>'
    vote_html = ""
    if p.get("locked"):
        vote_html = '<span style="color:var(--dim)">tally frozen</span>'
    elif p["small_fix"] and p.get("approved"):
        vote_html = '<span class="verdict-chip vc-ok">approved</span>'
    elif p["small_fix"]:
        vote_html = '<span style="color:var(--muted)">small fix · no votes needed</span>'
    else:
        up = p["up"]
        down = p["down"]
        threshold = p["threshold"]
        approved = p.get("approved", False)
        if up or down:
            pct = min(100, int((up / max(threshold, 1)) * 100)) if threshold else 0
            fill_cls = "vote-ok" if approved else ("vote-fail" if up - down < 0 else "vote-warn")
            verdict_label = "approved" if approved else "needs votes"
            label = f"{up} up / {down} down"
            vote_html = (
                f'<div class="vote-bar">'
                f'<div class="vote-track"><div class="vote-fill {fill_cls}" '
                f'style="width:{pct}%"></div></div>'
                f'<span class="vote-label">{label} · {esc(verdict_label)}</span></div>'
            )
        else:
            net = p["net"]
            ncolor = "var(--ok)" if net >= 0 else "var(--fail)"
            vote_html = (
                f'net <span style="color:{ncolor};font-weight:600">{net:+d}</span>'
                f' <span style="color:var(--muted)">(threshold {threshold})</span>'
            )
    preview = (
        f'<div class="post-excerpt">{esc(_truncate(p["body_preview"], config.BODY_PREVIEW_LENGTH))}</div>'
        if p.get("body_preview") else ""
    )
    prs_raw = p.get("prs") or []
    pr_trail = ""
    if prs_raw:
        repo_url = f"https://github.com/{esc(github.repo_spec())}"
        pr_numbers = [pr["pr_number"] for pr in prs_raw]
        tallies = db.pr_vote_tallies(pr_numbers)
        bits = []
        for pr in prs_raw:
            pr_cls = {"merged": "pr-merged", "open": "pr-open",
                      "declined": "pr-declined", "closed": "pr-closed"}.get(pr["status"], "")
            tv = tallies.get(pr["pr_number"], {"up": 0, "down": 0, "net": 0})
            vote_badge = ""
            if tv["up"] + tv["down"] > 0:
                vote_badge = (
                    f' <span style="color:var(--muted);font-size:12px">'
                    f'\u25b2{tv["up"]}\u25bc{tv["down"]}'
                    f'</span>'
                )
            bits.append(
                f'<a href="{repo_url}/pull/{pr["pr_number"]}" style="color:var(--accent)">'
                f'#{pr["pr_number"]}</a>'
                f'<span class="pr-chip {pr_cls}">{esc(pr["status"])}</span>'
                f'{vote_badge}'
            )
        pr_trail = (
            '<div class="pr-trail"><span class="pr-label">PRs:</span> '
            + " ".join(bits) + "</div>"
        )
    # Collaborative progress display
    if p.get("collaborative") and p.get("collaborative_closed") is None:
        merged = p.get("merged_pr_count", 0)
        goal = p.get("pr_goal")
        if goal:
            pct = min(100, int((merged / max(goal, 1)) * 100))
            fill_cls = "vote-ok" if merged >= goal else "vote-warn"
            pr_trail += (
                f'<div class="pr-trail" style="margin-top:4px">'
                f'<span class="pr-label">Progress:</span> '
                f'{merged} of {goal} PRs merged '
                f'<div class="vote-track" style="display:inline-block;width:80px;vertical-align:middle">'
                f'<div class="vote-fill {fill_cls}" style="width:{pct}%"></div></div>'
                f' {pct}%</div>'
            )
        elif merged:
            pr_trail += (
                f'<div class="pr-trail" style="margin-top:4px">'
                f'<span class="pr-label">Progress:</span> '
                f'{merged} PR{"s" if merged != 1 else ""} merged</div>'
            )
    stale_cls = " stale-card" if p.get("stale") else ""
    bounty = ""
    bt = p.get("bounty_total", 0)
    if bt:
        bounty = (
            f' <span class="verdict-chip vc-ok" title="bounty">'
            f'bounty {bt}</span>'
        )
    return (
        f'<div class="docket-card{stale_cls}">'
        f'<div class="docket-top"><h3>{kind}{_proposal_lineage_badge(p)}'
        f'<a href="/posts/{p["id"]}">{esc(p["title"])}</a>{bounty}</h3>'
        f'<div class="docket-chips">{"".join(chips)}</div></div>'
        f'<div class="docket-vote">{vote_html}</div>'
        f'<div class="meta">{meta}</div>'
        + preview + pr_trail
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
    "small_fix": "Small fixes",
    "stale": "Stale",
    "approved": "Approved",
    "review": "Review",
    "collaborative": "Collaborative",
    "merged": "Merged",
}

_DOCKET_PHASES = [
    ("Discussion", ["needs_votes", "small_fix", "stale"]),
    ("Implementation", ["approved", "review", "collaborative"]),
    ("Done", ["merged"]),
]

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
    tabs = (
        f'<a href="/proposals{_proposals_href("all", sort)}"'
        + (' class="active"' if view == "all" else "")
        + f">{_DOCKET_TITLES[view]} ({counts['all']})</a>"
    )
    for phase_name, phase_views in _DOCKET_PHASES:
        phase_tabs = "".join(
            f'<a href="/proposals{_proposals_href(v, sort)}"'
            + (' class="active"' if v == view else "")
            + f">{_DOCKET_TITLES[v]} ({counts[v]})</a>"
            for v in phase_views
        )
        tabs += f'<span class="tab-phase"><span class="tab-phase-label">{phase_name}</span>{phase_tabs}</span>'
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
    total = counts[view]
    summary = f'<div class="meta" style="margin:0 0 8px">Page {page} of {total_pages} · {total} proposals</div>'
    body = (
        _crumb("/", "overview")
        + f'<div class="panel"><h2>{_DOCKET_TITLES[view]}</h2>'
        + '<p style="color:var(--muted);font-size:15px;margin:0 0 12px">'
        "Proposals move through two phases: <b>Discussion</b> (vote on the idea, "
        "small fixes need no votes) then <b>Implementation</b> (PR is open, "
        "review or auto-merge). Only a merged proposal is done. "
        "The tabs are lenses, not partitions.</p>"
        + f'<div class="tabs">{tabs}</div>'
        + sort_row
        + summary
        + f'<div id="frag-docket-rows">{_docket_rows(view, sort, page)}</div>'
        + pager
        + "</div>"
    )
    return _page("proposals", _with_rail(body, show_proposals=False), section="proposals",
                 poll=_poll_config(
                     ("/fragments/rail?show_proposals=0", "frag-rail", POLL_MS),
                     (f"/fragments/docket-rows?view={view}&sort={sort}&page={page}", "frag-docket-rows", POLL_MS),
                 ))
