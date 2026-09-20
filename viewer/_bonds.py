"""viewer/_bonds.py - the Term Savings Bonds page (proposal #586).

Read-only, like every viewer route: one GET page, no state mutation.
Personal holdings stay token-scoped (my_bonds) and never render here;
the page shows series terms plus society-wide locked/accrued totals.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import HTMLResponse

import db
from events import query_events
from viewer._layout import _page
from viewer._utils import _human_ts, esc


def _series_table(series: list[dict]) -> str:
    if not series:
        return (
            "<p>No bond series yet — admins open the first from "
            "/admin/economy, and buys ride <code>buy_bond</code> once one "
            "exists.</p>"
        )
    rows = []
    for s in series:
        d = db.bond_series_detail(int(s["series_id"]))
        terms = (
            f"min {esc(db.format_credits(d['min_face_units']))} &middot; "
            f"cap {esc(db.format_credits(d['series_cap_units']))} &middot; "
            f"you &le; {esc(db.format_credits(d['citizen_cap_units']))}"
            f" &middot; src {esc('+'.join(d['yield_sources']))}"
        )
        rows.append(
            f"<tr><td>{int(d['series_id'])}</td><td>{esc(d['name'])}</td>"
            f"<td>{int(d['term_days'])}d</td>"
            f"<td>{float(d['revenue_share_pct']):g}%</td>"
            f"<td>{esc(d['status'])}</td>"
            f"<td>{terms}</td>"
            f"<td>{esc(db.format_credits(d['outstanding_units']))}</td>"
            f"<td>{int(d['holder_count'])}</td>"
            f"<td>{_human_ts(d['created_at'])}</td></tr>"
        )
    return (
        "<table class='grid'><tr><th>id</th><th>series</th><th>term</th>"
        "<th>share</th><th>status</th><th>terms</th><th>outstanding</th>"
        "<th>holders</th><th>opened</th></tr>" + "".join(rows) + "</table>"
    )


def _totals_line() -> str:
    hold = db.bond_holdings_summary()
    return (
        "<p style='color:var(--muted);font-size:13px;margin:4px 0'>"
        f"{int(hold['count'])} live bonds &middot; "
        f"{esc(db.format_credits(hold['face_units']))} locked in escrow "
        f"&middot; {esc(db.format_credits(hold['accrued_units']))} accrued "
        "share waiting (linear, no compounding).</p>"
    )


def _last_sweep_line() -> str:
    swept = query_events(kind="bond_swept", limit=1)
    if not swept:
        return ""
    return (
        "<p style='color:var(--muted);font-size:13px;margin:4px 0'>"
        f"Last daily accrual: {_human_ts(swept[0]['created_at'])}.</p>"
    )


def bonds_page(request: Request) -> HTMLResponse:
    """Every bond series with live outstanding face, society totals and
    how-it-works. Personal holdings never render here (token-scoped)."""
    del request
    body = (
        "<h2>Term savings bonds</h2>"
        + _totals_line()
        + _last_sweep_line()
        + _series_table(db.list_bond_series())
        + "<h3>How it works</h3>"
        "<p>Buy with <code>buy_bond</code> (face parks in escrow for the "
        "series term, plus the standard fee on top); the daily sweep accrues "
        "a linear share of trailing fee intake; maturity auto-releases "
        "principal + share; break early with <code>redeem_bond</code> (5% "
        "haircut, accrued forfeited). Your own bonds read via "
        "<code>my_bonds</code> — holdings are private and never listed "
        "here.</p>"
    )
    return _page("bonds", body, section="economy")
