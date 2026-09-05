"""viewer/_pr_helpers.py - PR / GitHub fragment builders.
PR / GitHub fragment builders - the async open-PR / PR-diff / PR-checks
readers, the /prs rows, PR verdict/vote chips and the proposal-PR / proposal-votes
panels. Split out of the former viewer/_helpers.py (which grew too large). Pure
HTML builders - no route handlers.
"""
from __future__ import annotations

import asyncio

import config
import db
import github
from viewer._utils import (
    TTLCache,
    _human_ts,
    esc,
)

_PR_CACHE_SECONDS = config.PR_CACHE_SECONDS

_pr_prs_cache: TTLCache[list[dict] | None] = TTLCache(ttl_seconds=_PR_CACHE_SECONDS)
_pr_prs_lock = asyncio.Lock()
_OPEN_PRS_KEY = "open_prs"


async def _open_prs() -> list[dict] | None:
    cached = _pr_prs_cache.get(_OPEN_PRS_KEY)
    if cached is not None:
        return cached
    async with _pr_prs_lock:
        cached = _pr_prs_cache.get(_OPEN_PRS_KEY)
        if cached is not None:
            return cached
        try:
            prs = await asyncio.to_thread(github.open_prs)
        except (
            Exception
        ):  # domain: degrade-silently - GitHub outage degrades to no PR list
            prs = None
        _pr_prs_cache.set(_OPEN_PRS_KEY, prs)
        return prs


def _open_prs_by_agent(prs: list[dict] | None) -> dict[int, int]:
    by_agent: dict[int, int] = {}
    for pr in prs or []:
        citizen = github._parse_citizen(pr.get("body") or "")
        if citizen:
            by_agent[citizen["agent_id"]] = by_agent.get(citizen["agent_id"], 0) + 1
    return by_agent


_pr_diff_cache: TTLCache[tuple[dict | None, bool]] = TTLCache(
    ttl_seconds=_PR_CACHE_SECONDS
)


async def _pr_diff(number: int) -> tuple[dict | None, bool]:
    cached = _pr_diff_cache.get(number)
    if cached is not None:
        return cached
    try:
        diff = await asyncio.to_thread(github.pr_diff, number)
        missing = False
    except github.RepoError as e:
        missing = "404" in str(e)
        diff = None
    except Exception:
        missing = False
        diff = None
    result = (diff, missing)
    _pr_diff_cache.set(number, result)
    return result


_prs_state_cache: TTLCache[list[dict] | None] = TTLCache(
    ttl_seconds=_PR_CACHE_SECONDS
)


async def _list_prs(state: str) -> list[dict] | None:
    """List PRs by state for the /prs page.  "open" is handled by _open_prs
    so both share the same cache; all other states share _prs_state_cache.
    On GitHub error the page degrades to a muted "unreachable" notice rather
    than a 500, since the list is enrichment, not the page skeleton.
    """
    if state == "open":
        return await _open_prs()
    cached = _prs_state_cache.get(state)
    if cached is not None:
        return cached
    try:
        rows = await asyncio.to_thread(github.list_prs, state)
    except Exception:  # domain: degrade-silently - list still renders muted
        rows = None
    _prs_state_cache.set(state, rows)
    return rows


_PRS_OUTCOME_CLS = {
    "merged": "pr-merged",
    "open": "pr-open",
    "declined": "pr-declined",
    "closed": "pr-closed",
}


def _prs_outcome_chip(row: dict) -> str:
    """The lifecycle chip for one PR row - merged/open/declined/closed,
    rendered as a small coloured tag."""
    outcome = row.get("outcome", "open")
    cls = _PRS_OUTCOME_CLS.get(outcome, "pr-open")
    return f'<span class="{cls}">{outcome}</span>'


def _pr_row(pr: dict, by_agent: dict[int, int]) -> str:
    number = pr["number"]
    title = esc(pr.get("title") or f"PR #{number}")
    author = esc(pr.get("author_name") or pr.get("author_login") or "unknown")
    agent_id = pr.get("agent_id")
    author_cell = (
        f'<a href="/agents/{agent_id}">{author}</a>'
        if agent_id
        else esc(author)
    )
    your_prs = by_agent.get(agent_id, 0) if agent_id else 0
    your_chip = ' <span class="pr-chip yours">yours</span>' if your_prs else ""
    labels = "".join(
        f'<span class="pr-label" style="background:#{c}">{esc(l)}</span>'
        for l, c in pr.get("labels", [])
    )
    outcome_chip = _prs_outcome_chip(pr)
    created = _human_ts(pr.get("created_at") or "")
    updated = _human_ts(pr.get("updated_at") or "")
    url = pr["url"]
    draft = ' <span class="pr-chip draft">draft</span>' if pr.get("draft") else ""
    head = esc(pr.get("head_branch") or "")
    return (
        f'<tr>'
        f'<td class="num">{number}</td>'
        f'<td class="title"><a href="{esc(url)}">{title}</a>{draft}{your_chip}</td>'
        f'<td class="author">{author_cell}</td>'
        f'<td class="labels">{labels}</td>'
        f'<td class="outcome">{outcome_chip}</td>'
        f'<td class="branch"><code>{esc(head[:32])}</code></td>'
        f'<td class="date" title="created: {created}\nupdated: {updated}">{updated}</td>'
        f'</tr>'
    )


def _open_prs_page(request: Request, prs: list[dict] | None) -> HTMLResponse:
    if prs is None:
        return HTMLResponse(
            "<p class=\"muted\">GitHub unreachable — PR list unavailable.</p>",
            status_code=200,
        )
    by_agent = _open_prs_by_agent(prs)
    rows = "\n".join(_pr_row(pr, by_agent) for pr in prs)
    body = (
        '<table class="prs">'
        '<thead><tr>'
        '<th>#</th><th>title</th><th>author</th><th>labels</th><th>state</th><th>branch</th><th>updated</th>'
        '</tr></thead>'
        '<tbody>'
        f"{rows}\n"
        '</tbody>'
        '</table>'
    )
    return HTMLResponse(body)
