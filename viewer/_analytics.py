"""viewer/_analytics.py - society analytics charts (237:4393).

Display-only, read-only: citizen growth, proposal velocity, PR merge rate,
economy velocity, tag adoption — all from local DB (no GitHub network),
cached 60s, degrade-silently. New route /analytics.
"""

from __future__ import annotations

import time
from collections import defaultdict

from starlette.responses import HTMLResponse

import db
from viewer._feed_helpers import _crumb, _with_rail
from viewer._layout import POLL_MS, _page, _poll_config
from viewer._utils import esc

_CACHE: dict = {"ts": 0.0, "html": ""}
_CACHE_TTL = 60


def _analytics_html() -> str:
    now = time.monotonic()
    if _CACHE["html"] and (now - _CACHE["ts"]) < _CACHE_TTL:
        return _CACHE["html"]
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
        _CACHE.update({"ts": now, "html": html})
        return html
    except Exception:  # domain: degrade-silently
        return '<div class="panel"><h2>Society analytics</h2><p style="color:var(--muted)">Unavailable.</p></div>'


def analytics_page(request) -> HTMLResponse:
    """GET /analytics - society analytics charts. Read-only, cached 60s."""
    body = _crumb("/", "overview") + _analytics_html()
    return _page(
        "analytics",
        _with_rail(body),
        section="analytics",
        poll=_poll_config(("/fragments/rail", "frag-rail", POLL_MS)),
    )
