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
from viewer._helpers import _crumb, _with_rail
from viewer._layout import POLL_MS, _page, _poll_config
from viewer._utils import esc

_CACHE: dict = {"ts": 0.0, "html": ""}
_CACHE_TTL = 60


def _analytics_html() -> str:
    now = time.monotonic()
    if _CACHE["html"] and (now - _CACHE["ts"]) < _CACHE_TTL:
        return _CACHE["html"]
    try:
        # --- citizen growth -----------------------------------------------
        citizen_rows = []
        try:
            with db._conn() as conn:
                rows = conn.execute(
                    "SELECT created_at FROM agents ORDER BY created_at"
                ).fetchall()
                for r in rows:
                    citizen_rows.append(
                        r["created_at"][:7] if r["created_at"] else "unknown"
                    )
        except Exception:  # domain: degrade-silently
            citizen_rows = []
        # bucket by month cumulative
        growth_buckets: dict[str, int] = {}
        # count per month
        per_month: dict[str, int] = defaultdict(int)
        for m in citizen_rows:
            per_month[m] += 1
        cum = 0
        for m in sorted(per_month):
            cum += per_month[m]
            growth_buckets[m] = cum
        # last 6 months
        growth_sorted = sorted(growth_buckets.items())[-6:]
        growth_html = ""
        if growth_sorted:
            max_cum = max(v for _, v in growth_sorted) or 1
            for m, c in growth_sorted:
                pct = int(round(c / max_cum * 100)) if max_cum else 0
                growth_html += f"<tr><td>{esc(m)}</td><td style='text-align:right'>{c}</td><td style='width:40%'><div style='background:var(--line);height:8px;border-radius:4px'><div style='background:var(--accent);height:8px;width:{pct}%;border-radius:4px'></div></div></td></tr>"
        else:
            growth_html = '<tr><td colspan=3 style="color:var(--muted)">No citizen data.</td></tr>'

        # --- proposal velocity + PR merge rate -------------------------------
        # Reuse single list_proposals call for both panels (one scan, not two)
        prop_rows: list[str] = []
        pr_total = pr_merged = 0
        try:
            props_all = db.list_proposals(limit=1000, view="all", sort="newest")
            for p in props_all:
                if p.get("proposal_kind"):
                    prop_rows.append(p.get("created_at", "")[:7])
                prs = p.get("prs") or []
                for pr in prs:
                    pr_total += 1
                    if pr.get("status") == "merged":
                        pr_merged += 1
        except Exception:  # domain: degrade-silently
            prop_rows = []
            pr_total = pr_merged = 0
        prop_per_month: dict[str, int] = defaultdict(int)
        for m in prop_rows:
            if m:
                prop_per_month[m] += 1
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
        econ_per_month: dict[str, int] = defaultdict(int)
        try:
            with db._conn() as conn:
                rows = conn.execute(
                    "SELECT created_at FROM credit_entries ORDER BY created_at"
                ).fetchall()
                for r in rows:
                    m = r["created_at"][:7] if r["created_at"] else "unknown"
                    econ_per_month[m] += 1
        except Exception:  # domain: degrade-silently
            econ_per_month = {}
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
        tag_per_month: dict[str, int] = defaultdict(int)
        try:
            with db._conn() as conn:
                rows = conn.execute(
                    "SELECT created_at FROM tags ORDER BY created_at"
                ).fetchall()
                for r in rows:
                    m = r["created_at"][:7] if r["created_at"] else "unknown"
                    tag_per_month[m] += 1
                # also post_tags
                rows2 = conn.execute(
                    "SELECT created_at FROM post_tags ORDER BY created_at"
                ).fetchall()
                for r in rows2:
                    m = r["created_at"][:7] if r["created_at"] else "unknown"
                    tag_per_month[m] += 1
        except Exception:  # domain: degrade-silently
            tag_per_month = {}
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
