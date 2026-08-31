"""viewer._pulse - the /pulse society dashboard: a live activity trend, a
governance pipeline funnel and an economy strip, all read-only derivations
over existing db helpers (no db/schema changes)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from starlette.requests import Request
from starlette.responses import HTMLResponse

import db
import db._aggregates as aggregates
from events import query_events
from viewer._feed_helpers import _crumb, _with_rail
from viewer._layout import POLL_MS, _page, _poll_config
from viewer._utils import esc

# ---------------------------------------------------------- pulse panels --

_FUNNEL_VIEWS = ("all", "needs_votes", "approved", "review", "merged")
_FUNNEL_LABELS = {
    "all": "all",
    "needs_votes": "needs votes",
    "approved": "approved",
    "review": "in review",
    "merged": "merged",
}
_FUNNEL_CHIP_VIEWS = (
    ("stale", "stale"),
    ("small_fix", "small fixes"),
    ("collaborative", "collaborative"),
    ("unclaimed", "unclaimed"),
    ("staking", "staking"),
    ("ideas", "ideas"),
)


def _activity_trend() -> str:
    """A 14-day activity series derived from the events ledger (bucketed by
    UTC day, client-side) plus a 'last 7d vs prior 7d' delta, and the
    all-time activity total. recent_activity_total() has no window, so the
    daily series comes from query_events(since=...) - disclosed in the PR."""
    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    rows = query_events(since=since, limit=2000)
    per_day: dict[str, int] = {}
    for e in rows:
        day = e["created_at"][:10]
        per_day[day] = per_day.get(day, 0) + 1
    days: list[str] = []
    for i in range(13, -1, -1):
        days.append((now - timedelta(days=i)).strftime("%Y-%m-%d"))
    series = [per_day.get(d, 0) for d in days]
    last7 = sum(series[7:])
    prior7 = sum(series[:7])
    delta = last7 - prior7
    mx = max(series) or 1
    bars = []
    for i, n in enumerate(series):
        h = max(2, round(n / mx * 28))
        x = i * 11
        bars.append(
            f'<rect x="{x}" y="{34 - h}" width="8" height="{h}" rx="1.5"'
            f' fill="var(--accent)" opacity="{1 if n else 0.15}"/>'
        )
    svg = (
        f'<svg width="154" height="36" viewBox="0 0 154 36" role="img"'
        f' aria-label="society activity per day over the last 14 days">{"".join(bars)}</svg>'
    )
    trend_color = "var(--ok)" if delta >= 0 else "var(--fail)"
    delta_html = (
        f'<span style="color:{trend_color};font-weight:600">{delta:+d}</span>'
        f" last 7d vs the 7 before"
    )
    headline = aggregates.recent_activity_total()
    return (
        f'<div class="panel"><h2>Activity trend</h2>'
        f'<div class="cards">'
        f'<div class="card"><div class="n">{headline}</div><div class="l">actions on record</div></div>'
        f'<div class="card"><div class="n">{last7}</div><div class="l">last 7 days</div></div>'
        f'<div class="card"><div class="n">{prior7}</div><div class="l">prior 7 days</div></div>'
        f"</div>{svg}<p class='meta'>{delta_html}</p></div>"
    )


def _governance_funnel() -> str:
    """The proposal pipeline as a funnel: everything, then what still needs
    votes, what cleared the bar, what is under review, and what shipped."""
    counts = db.proposal_docket_counts()
    cells = []
    for i, view in enumerate(_FUNNEL_VIEWS):
        if i:
            cells.append('<span class="muted">\u2192</span>')
        cells.append(
            f'<a class="funnel-chip" href="/proposals?view={view}">'
            f"<b>{counts.get(view, 0)}</b> {_FUNNEL_LABELS[view]}</a>"
        )
    chips = " ".join(
        f'<a class="tag" style="color:var(--muted)" href="/proposals?view={view}">{label}</a>'
        for view, label in _FUNNEL_CHIP_VIEWS
    )
    return (
        f'<div class="panel"><h2>Governance pipeline</h2>'
        f'<div class="search-group">{" ".join(cells)}</div>'
        f'<p class="meta">{chips}</p></div>'
    )


def _economy_strip() -> str:
    """The economy headline: supply, treasury and circulating credits plus the
    24h net supply movement and what is committed to stakes and jobs."""
    eo = db.economy_overview()
    day = (eo.get("flows") or {}).get("day") or {}
    minted = day.get("minted_quarters", 0)
    burned = day.get("burned_quarters", 0)
    delta = minted - burned
    color = "var(--ok)" if delta >= 0 else "var(--fail)"
    committed = eo.get("committed_to_active_stakes_credits", "0")
    escrow = eo.get("held_in_job_escrow_credits", "0")
    jobs_open = eo.get("open_jobs", 0)
    jobs_active = eo.get("active_jobs", 0)
    return (
        f'<div class="panel"><h2>Economy</h2><div class="cards">'
        f'<div class="card"><div class="n">{esc(eo["total_supply_credits"])}</div><div class="l">supply</div></div>'
        f'<div class="card"><div class="n">{esc(eo["treasury_credits"])}</div><div class="l">treasury</div></div>'
        f'<div class="card"><div class="n">{esc(eo["circulating_credits"])}</div><div class="l">circulating</div></div>'
        f"</div>"
        f'<p class="meta">24h net <span style="color:{color};font-weight:600">{delta:+d}</span> quarters '
        f"(minted {minted} \xb7 burned {burned}) \xb7 committed to stakes {esc(committed)} \xb7 "
        f"job escrow {esc(escrow)} \xb7 jobs {jobs_active} active / {jobs_open} open</p></div>"
    )


def _pulse_panels() -> str:
    """The three pulse panels, shared by the full /pulse page and its
    soft-refresh fragment so the two can never drift. Read-only."""
    return _activity_trend() + _governance_funnel() + _economy_strip()


# ------------------------------------------------------------- full page --


def pulse_page(request: Request) -> HTMLResponse:
    """The /pulse society dashboard: activity trend, governance funnel and
    economy strip beside the side rail, soft-refreshed on a heavy 30s poll
    (plus the usual rail poll). Read-only, like every route here."""
    body = (
        _crumb("/", "overview")
        + '<div class="panel" style="border:none;background:none">'
        + '<div id="frag-pulse-panels">'
        + _pulse_panels()
        + "</div></div>"
    )
    return _page(
        "pulse",
        _with_rail(body),
        section="pulse",
        poll=_poll_config(
            ("/fragments/rail", "frag-rail", POLL_MS),
            ("/fragments/pulse-panels", "frag-pulse-panels", 30000),
        ),
    )
