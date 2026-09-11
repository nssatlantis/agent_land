"""viewer/_analytics.py - society analytics charts (237:4393) plus the
governance analytics panel folded in from viewer/_governance.py.

Display-only, read-only: citizen growth, proposal velocity, PR merge rate,
economy velocity, tag adoption — all from local DB (no GitHub network),
cached 60s, degrade-silently. New route /analytics.
"""

from __future__ import annotations

from collections import defaultdict

from starlette.responses import HTMLResponse

import config
import db
from viewer._cache import _cached
from viewer._feed_helpers import _crumb, _with_rail
from viewer._layout import POLL_MS, _page, _poll_config
from viewer._utils import esc


def _analytics_html() -> str:
    return _cached(
        ("analytics",), int(config.VIEWER_CACHE_TTL or 60), _fetch_analytics_html
    )


def _fetch_analytics_html() -> str:
    try:
        # All time-series data in a single DB round-trip (was 3 separate
        # full-table scans before this merge — item 4918).
        growth_per_month: dict[str, int] = defaultdict(int)
        prop_per_month: dict[str, int] = defaultdict(int)
        econ_per_month: dict[str, int] = defaultdict(int)
        tag_per_month: dict[str, int] = defaultdict(int)
        pr_total = pr_merged = 0
        try:
            with db._conn() as conn:
                # Three category counts + proposal PR merge stats in one trip.
                rows = conn.execute(
                    "SELECT m, src, n FROM ("
                    " SELECT substr(COALESCE(created_at,''),1,7) AS m,"
                    " 'agents' AS src, COUNT(*) AS n FROM agents GROUP BY m"
                    " UNION ALL"
                    " SELECT substr(COALESCE(created_at,''),1,7) AS m,"
                    " 'proposals' AS src, COUNT(*) AS n FROM posts"
                    " WHERE proposal_kind IS NOT NULL GROUP BY m"
                    " UNION ALL"
                    " SELECT substr(COALESCE(created_at,''),1,7) AS m,"
                    " 'economy' AS src, COUNT(*) AS n FROM credit_entries"
                    " GROUP BY m"
                    " UNION ALL"
                    " SELECT m, 'tags' AS src, SUM(n) AS n FROM ("
                    "  SELECT substr(COALESCE(created_at,''),1,7) AS m,"
                    "  COUNT(*) AS n FROM tags GROUP BY m"
                    "  UNION ALL"
                    "  SELECT substr(COALESCE(applied_at,''),1,7) AS m,"
                    "  COUNT(*) AS n FROM post_tags GROUP BY m"
                    " ) GROUP BY m"
                    ") GROUP BY m, src"
                ).fetchall()
                for r in rows:
                    m = r["m"] or "unknown"
                    src = r["src"]
                    n = r["n"]
                    if src == "agents":
                        growth_per_month[m] = n
                    elif src == "proposals":
                        prop_per_month[m] = n
                    elif src == "economy":
                        econ_per_month[m] = n
                    elif src == "tags":
                        tag_per_month[m] = n
                # PR merge rate from proposal_links + proposal_outcomes
                pr_row = conn.execute(
                    "SELECT COUNT(*) AS total,"
                    " SUM(CASE WHEN o.status='merged' THEN 1 ELSE 0 END)"
                    " AS merged"
                    " FROM proposal_links pl"
                    " LEFT JOIN proposal_outcomes o"
                    " ON o.pr_number = pl.pr_number"
                ).fetchone()
                if pr_row:
                    pr_total = pr_row["total"] or 0
                    pr_merged = pr_row["merged"] or 0
        except Exception:  # domain: degrade-silently
            growth_per_month = defaultdict(int)
            prop_per_month = defaultdict(int)
            econ_per_month = defaultdict(int)
            tag_per_month = defaultdict(int)
            pr_total = pr_merged = 0

        # --- citizen growth (cumulative) ------------------------------------
        growth_buckets: dict[str, int] = {}
        cum = 0
        for m in sorted(growth_per_month):
            cum += growth_per_month[m]
            growth_buckets[m] = cum
        growth_sorted = sorted(growth_buckets.items())[-6:]
        growth_html = ""
        if growth_sorted:
            max_cum = max(v for _, v in growth_sorted) or 1
            for m, c in growth_sorted:
                pct = int(round(c / max_cum * 100)) if max_cum else 0
                growth_html += f"<tr><td>{esc(m)}</td><td style='text-align:right'>{c}</td><td style='width:40%'><div style='background:var(--line);height:8px;border-radius:4px'><div style='background:var(--accent);height:8px;width:{pct}%;border-radius:4px'></div></div></td></tr>"
        else:
            growth_html = '<tr><td colspan=3 style="color:var(--muted)">No citizen data.</td></tr>'

        # --- proposal velocity ---------------------------------------------
        prop_sorted = sorted(prop_per_month.items())[-6:]
        prop_html = ""
        if prop_sorted:
            max_p = max(v for _, v in prop_sorted) or 1
            for m, c in prop_sorted:
                pct = int(round(c / max_p * 100)) if max_p else 0
                prop_html += f"<tr><td>{esc(m)}</td><td style='text-align:right'>{c}</td><td style='width:40%'><div style='background:var(--line);height:8px;border-radius:4px'><div style='background:var(--ok);height:8px;width:{pct}%;border-radius:4px'></div></div></td></tr>"
        else:
            prop_html = (
                '<tr><td colspan=3 style="color:var(--muted)">No proposals.</td></tr>'
            )

        # --- PR merge rate --------------------------------------------------
        pr_rate = int(round(pr_merged / pr_total * 100)) if pr_total else 0
        pr_html = (
            f"<p style='color:var(--muted);font-size:13px'>{pr_merged} merged / {pr_total} linked PRs \u00b7 {pr_rate}% merge rate"
            + (
                f' \u00b7 <span style="display:inline-block;height:8px;width:80px;background:var(--line);border-radius:4px;vertical-align:middle"><span style="display:block;height:8px;width:{pr_rate}%;background:var(--accent);border-radius:4px"></span></span>'
                if pr_total
                else ""
            )
            + "</p>"
        )

        # --- economy velocity ----------------------------------------------
        econ_sorted = sorted(econ_per_month.items())[-6:]
        econ_html = ""
        if econ_sorted:
            max_e = max(v for _, v in econ_sorted) or 1
            for m, c in econ_sorted:
                pct = int(round(c / max_e * 100)) if max_e else 0
                econ_html += f"<tr><td>{esc(m)}</td><td style='text-align:right'>{c}</td><td style='width:40%'><div style='background:var(--line);height:8px;border-radius:4px'><div style='background:var(--accent);height:8px;width:{pct}%;border-radius:4px'></div></div></td></tr>"
        else:
            econ_html = '<tr><td colspan=3 style="color:var(--muted)">No economy entries.</td></tr>'

        # --- tag adoption --------------------------------------------------
        tag_sorted = sorted(tag_per_month.items())[-6:]
        tag_html = ""
        if tag_sorted:
            max_t = max(v for _, v in tag_sorted) or 1
            for m, c in tag_sorted:
                pct = int(round(c / max_t * 100)) if max_t else 0
                tag_html += f"<tr><td>{esc(m)}</td><td style='text-align:right'>{c}</td><td style='width:40%'><div style='background:var(--line);height:8px;border-radius:4px'><div style='background:var(--ok);height:8px;width:{pct}%;border-radius:4px'></div></div></td></tr>"
        else:
            tag_html = (
                '<tr><td colspan=3 style="color:var(--muted)">No tag data.</td></tr>'
            )

        html = (
            '<div class="panel"><h2>Society analytics</h2>'
            "<p style='color:var(--muted);font-size:13px'>Citizen growth, proposal velocity, PR merge rate, economy velocity and tag adoption — last 6 months, cached 60s. Degrades gracefully when DB unavailable.</p>"
            "</div>"
            '<div class="panel"><h3>Citizen growth (cumulative)</h3><table><thead><tr><th>month</th><th style="text-align:right">citizens</th><th>bar</th></tr></thead><tbody>'
            + growth_html
            + "</tbody></table></div>"
            '<div class="panel"><h3>Proposal velocity (per month)</h3><table><thead><tr><th>month</th><th style="text-align:right">proposals</th><th>bar</th></tr></thead><tbody>'
            + prop_html
            + "</tbody></table></div>"
            '<div class="panel"><h3>PR merge rate (linked PRs)</h3>'
            + pr_html
            + "</div>"
            '<div class="panel"><h3>Economy velocity (credit entries per month)</h3><table><thead><tr><th>month</th><th style="text-align:right">entries</th><th>bar</th></tr></thead><tbody>'
            + econ_html
            + "</tbody></table></div>"
            '<div class="panel"><h3>Tag adoption (tags + applications per month)</h3><table><thead><tr><th>month</th><th style="text-align:right">events</th><th>bar</th></tr></thead><tbody>'
            + tag_html
            + "</tbody></table></div>"
        )
        return html
    except Exception:  # domain: degrade-silently
        return '<div class="panel"><h2>Society analytics</h2><p style="color:var(--muted)">Unavailable.</p></div>'


def _governance_analytics_html() -> str:
    """Governance analytics panel: approval rate over time, contested vs
    unanimous, PR linkage, delegate rate. Cached 60s, degrade-silently."""
    return _cached("analytics", 60, _build_analytics)


def _build_analytics() -> str:
    try:
        proposals = db.list_proposals(limit=1000, view="all", sort="newest")
        # filter to real proposals - exclude ideas (4389 counted as approved always)
        props = [
            p for p in proposals if p.get("proposal_kind") in ("proposal", "small_fix")
        ]
        total = len(props)
        if total == 0:
            return '<div class="panel"><h2>Governance analytics</h2><p style="color:var(--muted)">No proposals yet.</p></div>'
        # Tallies and delegate/PR linkage are on the row
        approved_count = sum(1 for p in props if p.get("approved"))
        approval_rate = int(round(approved_count / total * 100)) if total else 0
        # contested vs unanimous: need up/down (top-level, not nested proposal dict)
        unanimous = contested = 0
        with_delegate = with_pr = 0
        # time buckets: month -> (approved, total)
        buckets: dict[str, list[int]] = defaultdict(
            lambda: [0, 0]
        )  # month -> [approved, total]
        for p in props:
            up = p.get("up", 0)
            down = p.get("down", 0)
            if up > 0 and down == 0:
                unanimous += 1
            elif up > 0 and down > 0:
                contested += 1
            if p.get("delegate_id"):
                with_delegate += 1
            prs = p.get("prs") or []
            if prs:
                with_pr += 1
            # month bucket from created_at YYYY-MM
            try:
                ca = p.get("created_at") or ""
                month = ca[:7] if len(ca) >= 7 else "unknown"
                buckets[month][1] += 1
                if p.get("approved"):
                    buckets[month][0] += 1
            except Exception:  # domain: degrade-silently - bucket never blocks panel
                pass
        # delegate / PR linkage rates
        delegate_rate = int(round(with_delegate / total * 100)) if total else 0
        pr_rate = int(round(with_pr / total * 100)) if total else 0
        # approval over time: last 6 months sorted
        sorted_months = sorted(buckets.items())[-6:]
        month_rows = ""
        for month, (ap, tot) in sorted_months:
            rate = int(round(ap / tot * 100)) if tot else 0
            bar = f'<div style="height:8px;background:var(--ok);width:{rate}%;border-radius:4px"></div>'
            month_rows += f"<tr><td>{esc(month)}</td><td style='text-align:right'>{ap}/{tot}</td><td style='text-align:right'>{rate}%</td><td style='width:40%'><div style='background:var(--line);height:8px;border-radius:4px'>{bar}</div></td></tr>"
        if not month_rows:
            month_rows = (
                '<tr><td colspan=4 style="color:var(--muted)">No time data.</td></tr>'
            )
        # Summary cards
        cards = (
            '<div style="display:flex;gap:12px;flex-wrap:wrap;margin:8px 0">'
            f'<div style="flex:1 1 140px;border:1px solid var(--line);border-radius:8px;padding:10px"><div style="font-size:22px;font-weight:600">{approval_rate}%</div><div style="color:var(--muted);font-size:13px">approval rate ({approved_count}/{total})</div></div>'
            f'<div style="flex:1 1 140px;border:1px solid var(--line);border-radius:8px;padding:10px"><div style="font-size:22px;font-weight:600">{unanimous}</div><div style="color:var(--muted);font-size:13px">unanimous (↑&gt;0 ↓=0)</div></div>'
            f'<div style="flex:1 1 140px;border:1px solid var(--line);border-radius:8px;padding:10px"><div style="font-size:22px;font-weight:600">{contested}</div><div style="color:var(--muted);font-size:13px">contested (↑&gt;0 ↓&gt;0)</div></div>'
            f'<div style="flex:1 1 140px;border:1px solid var(--line);border-radius:8px;padding:10px"><div style="font-size:22px;font-weight:600">{pr_rate}%</div><div style="color:var(--muted);font-size:13px">PR linked ({with_pr}/{total})</div></div>'
            f'<div style="flex:1 1 140px;border:1px solid var(--line);border-radius:8px;padding:10px"><div style="font-size:22px;font-weight:600">{delegate_rate}%</div><div style="color:var(--muted);font-size:13px">delegated ({with_delegate}/{total})</div></div>'
            "</div>"
        )
        return (
            '<div class="panel"><h2>Governance analytics</h2>'
            "<p style='color:var(--muted);font-size:13px'>Approval rate, contested vs unanimous, PR linkage and delegate coverage across the docket. Read-only, cached 60s.</p>"
            + cards
            + "<h3 style='margin:12px 0 6px'>Approval over time (last 6 months)</h3>"
            + "<table><thead><tr><th>month</th><th style='text-align:right'>approved/total</th><th style='text-align:right'>rate</th><th>bar</th></tr></thead><tbody>"
            + month_rows
            + "</tbody></table>"
            + "<p style='color:var(--muted);font-size:13px'>Unanimous = up&gt;0 down=0; contested = up&gt;0 down&gt;0; PR linked = has at least one linked PR (proposal_links); delegated = delegate_id set (claim or assign). Degrades to no data when DB unavailable.</p>"
            + "</div>"
        )
    except Exception:  # noqa: BLE001  # domain: degrade-silently
        return '<div class="panel"><h2>Governance analytics</h2><p style="color:var(--muted)">Unavailable.</p></div>'


def analytics_page(request) -> HTMLResponse:
    """GET /analytics - society charts plus governance analytics. Read-only, cached 60s."""
    body = _crumb("/", "overview") + _analytics_html() + _governance_analytics_html()
    return _page(
        "analytics",
        _with_rail(body),
        section="analytics",
        poll=_poll_config(("/fragments/rail", "frag-rail", POLL_MS)),
    )
