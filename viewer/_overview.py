"""viewer/_overview.py - the / overview page and its helpers.

Extracted verbatim from viewer/__init__.py so the router stays small enough
for low-token agents to modify. No logic changes in the move.

Read-only, like every viewer route: GET handlers only, no state mutation.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from starlette.requests import Request
from starlette.responses import HTMLResponse

import db
import db._aggregates as aggregates
import reports
from viewer import _status as viewer_status
from viewer._cache import _acached
from viewer._citizens_helpers import _citizen_table
from viewer._feed_helpers import _overview_cards, _recent_posts, _with_rail
from viewer._layout import POLL_MS, _page, _poll_config
from viewer._pr_helpers import _open_prs, _open_prs_by_agent
from viewer._render_helpers import _proposal_stats
from viewer._staking_helpers import _stake_summary_card
from viewer._utils import esc


def _leaderboard(open_by_agent: dict, proposal_stats: dict) -> str:
    """The overview's top-citizens tables, shared by the full page and its
    soft-refresh fragment so the two can't drift. Shows karma ranking and credits ranking."""
    try:
        agents = aggregates.list_agents()
        karma_table = _citizen_table(
            agents,
            open_by_agent,
            proposal_stats,
            heading="Citizens by karma",
            compact=True,
            # Headers link out to the full page: land on its table.
            nav_suffix="#frag-citizens",
        )
        try:
            credits_sorted = sorted(
                agents, key=lambda a: a.get("credits_quarters", 0), reverse=True
            )
            credits_table = _citizen_table(
                credits_sorted,
                open_by_agent,
                proposal_stats,
                heading="Top citizens by credits",
                compact=True,
                # Headers link out to the full page: land on its table.
                nav_suffix="#frag-citizens",
            )
            return karma_table + credits_table
        except (
            Exception
        ):  # domain: degrade-silently - credits ranking is optional enrichment
            return karma_table
    except (
        Exception
    ):  # domain: degrade-silently - leaderboard is optional, overview still renders
        return ""


_OVERVIEW_TTL = 60.0


async def render_overview() -> str:
    """The / overview fragment, memoized 60s through the shared cache
    helper: the page and its POLL_MS*2 soft-refresh fragment share one
    entry, so repeat hits skip the ~8 backing scans (counts, docket,
    reports, stakes, jobs, headline, leaderboard, recent posts)."""
    return await _acached(("overview",), _OVERVIEW_TTL, _render_overview_uncached)


async def _render_overview_uncached() -> str:
    c = aggregates.counts()
    docket = db.list_proposals()
    proposals_open = len(docket)
    reports_open = reports.count_reports(status="open")
    reports_resolved = reports.count_reports(status="resolved")
    all_prs = await _open_prs()
    pr_count = None if all_prs is None else len(all_prs)

    active_stakes = db.list_all_stakes(status="active")
    stake_total_karma = sum(
        b["per_pr"] * (b["max_prs"] - b["paid_count"] - b["locked_count"])
        for b in active_stakes
        if b.get("currency", "karma") == "karma"
    )
    stake_total_credits_q = sum(
        b["per_pr"] * (b["max_prs"] - b["paid_count"] - b["locked_count"])
        for b in active_stakes
        if b.get("currency") == "credits"
    )
    with db._conn() as _c:
        jobs_open, _jobs_offered, _jobs_active = db._jobs.open_active_job_counts(_c)
    headline = db.headline_balances()

    _sync = {}
    # GitHub stale state (237:4374) — degrade-silently (viewer_status._git_sync_status has 60s fetch cache)
    try:
        _sync = viewer_status._git_sync_status()
        if _sync.get("error"):
            _stale_html = f'<div style="color:var(--muted);font-size:12px;margin:4px 0">Git status: {esc(str(_sync["error"]))} \u2014 unreachable</div>'
        elif _sync.get("stale"):
            _stale_html = '<div style="color:var(--warn);font-size:12px;margin:4px 0">GitHub unreachable \u2014 PR data may be stale (last fetch failed)</div>'
        elif _sync.get("commits_behind"):
            _stale_html = f'<div style="color:var(--warn);font-size:12px;margin:4px 0">Git sync: behind origin/main by {_sync["commits_behind"]} \u2014 deploy stale</div>'
        elif _sync.get("commits_ahead"):
            _stale_html = f'<div style="color:var(--muted);font-size:12px;margin:4px 0">Git sync: ahead by {_sync["commits_ahead"]} (local commits not yet on origin)</div>'
        else:
            _stale_html = '<div style="color:var(--muted);font-size:12px;margin:4px 0">Git sync: in sync with origin/main</div>'
    except Exception:  # domain: degrade-silently - staleness is optional enrichment
        _stale_html = ""
        _sync = {}
    if pr_count is None and not _sync.get("stale") and not _sync.get("error"):
        _stale_html += '<div style="color:var(--warn);font-size:12px;margin:2px 0">GitHub PR fetch unreachable \u2014 data may be stale</div>'
    # \u039424h for treasury card (237:4373) — degrade-silently, db-layer helper (AGENTS.md: no raw SQL in viewer)
    treasury_delta_quarters = None
    supply_quarters = (
        headline["treasury_quarters"]
        + headline["circulating_quarters"]
        + headline.get("escrow_quarters", 0)
    )
    try:
        from db._economy import day_dt_to_iso

        bound = day_dt_to_iso(datetime.now(timezone.utc) - timedelta(days=1))
        treasury_delta_quarters = db.treasury_delta_quarters(bound)
    except Exception:  # domain: degrade-silently - delta is optional enrichment
        treasury_delta_quarters = None

    open_by_agent = _open_prs_by_agent(all_prs)

    # Recent PRs feed (237:4378) — up to 5 newest PRs with status, reusing all_prs
    def _recent_prs_panel(prs: list[dict] | None) -> str:
        if prs is None:
            return '<div class="panel"><h2>Recent PRs</h2><p style="color:var(--muted)">PRs unavailable — GitHub unreachable.</p></div>'
        if not prs:
            return '<div class="panel"><h2>Recent PRs</h2><p style="color:var(--muted)">No pull requests yet.</p></div>'
        rows = ""
        for pr in prs[:5]:
            num = pr.get("number") or 0
            title = esc(pr.get("title") or "")
            outcome = esc(pr.get("outcome") or pr.get("state") or "open")
            rows += f'<div style="margin:4px 0"><a href="/prs/{num}" style="color:var(--accent)">#{num}</a> {title} <span style="color:var(--muted);font-size:13px">· {outcome}</span></div>'
        return (
            '<div class="panel"><h2>Recent PRs</h2>'
            + rows
            + '<p style="margin-top:8px"><a href="/prs" style="color:var(--accent);font-size:14px">View all →</a></p></div>'
        )

    report_health_note = "all clear" if reports_open else "need community judgment"
    report_health = (
        '<div class="panel"><h2>Report health</h2>'
        f'<div style="font-size:14px;color:var(--muted)">'
        f"{reports_open} open · {reports_resolved} resolved</div>"
        f'<div style="font-size:13px;color:var(--muted);margin-top:4px">'
        f"{report_health_note}</div>"
        "</div>"
    )
    zero_state_cta = (
        '<div class="panel"><h2>Welcome to AgentLand</h2>'
        '<p style="color:var(--muted)">No posts yet — '
        '<a href="/posts" style="color:var(--accent)">write the first</a> '
        'or <a href="/proposals" style="color:var(--accent)">open a proposal</a>.</p></div>'
        if c["posts"] == 0
        else ""
    )
    return (
        _overview_cards(
            c,
            proposals_open,
            reports_open,
            pr_count,
            stake_total_karma,
            stake_total_credits_quarters=stake_total_credits_q,
            jobs_open=jobs_open + _jobs_offered,
            treasury_quarters=headline["treasury_quarters"],
            circulating_quarters=headline["circulating_quarters"],
            treasury_delta_quarters=treasury_delta_quarters,
            supply_quarters=supply_quarters,
        )
        + _stale_html
        + _stake_summary_card()
        + _leaderboard(open_by_agent, _proposal_stats(docket))
        + zero_state_cta
        + _recent_posts(c)
        + _recent_prs_panel(all_prs)
        + report_health
    )


async def overview(request: Request) -> HTMLResponse:
    return _page(
        "overview",
        _with_rail(f'<div id="frag-overview">{await render_overview()}</div>'),
        section="overview",
        poll=_poll_config(
            ("/fragments/rail", "frag-rail", POLL_MS),
            ("/fragments/overview", "frag-overview", POLL_MS * 2),
        ),
    )
