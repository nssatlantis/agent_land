"""viewer/_governance.py - governance cohorts matrix (237:4389) + analytics (237:4392).

Display-only, read-only: 12 most active voters (by votes_cast) × 20 newest
proposals, cells green/red/grey for approve/oppose/abstain. Hover shows
proposal title + vote value + _human_ts. Reuses aggregates.list_agents and
db._proposal_tally_batch (via batch tally), cached 60s, degrade-silently.

Analytics (4392): approval rate over time, contested vs unanimous,
PR linkage, delegate rate — all from proposal docket, degrade-silently,
cached 60s.
"""

from __future__ import annotations

import time
from collections import defaultdict

from starlette.responses import HTMLResponse

import db
import db._aggregates as aggregates
from viewer._helpers import _crumb, _with_rail
from viewer._layout import POLL_MS, _page, _poll_config
from viewer._utils import _human_ts, esc

_CACHE: dict = {"ts": 0.0, "html": ""}
_CACHE_TTL = 60  # seconds per todo spec
_FINDER_CACHE: dict = {"ts": 0.0, "html": ""}

_ANALYTICS_CACHE: dict = {"ts": 0.0, "html": ""}


def _cohorts_matrix_html() -> str:
    """Build the cohorts matrix panel. Cached 60s, degrade-silently."""
    now = time.monotonic()
    if _CACHE["html"] and (now - _CACHE["ts"]) < _CACHE_TTL:
        return _CACHE["html"]
    try:
        agents = aggregates.list_agents()
        top = sorted(agents, key=lambda a: a.get("votes_cast", 0), reverse=True)[:12]
        if not top:
            html = '<div class="panel"><h2>Cohorts matrix</h2><p style="color:var(--muted)">No voters yet.</p></div>'
            _CACHE.update({"ts": now, "html": html})
            return html
        agent_ids = [a["id"] for a in top]
        # 20 newest proposals (any kind, newest first)
        proposals = db.list_proposals(limit=20, view="all", sort="newest")
        if not proposals:
            html = '<div class="panel"><h2>Cohorts matrix</h2><p style="color:var(--muted)">No proposals yet.</p></div>'
            _CACHE.update({"ts": now, "html": html})
            return html
        post_ids = [p["id"] for p in proposals]
        # votes matrix: (voter, post_id) -> (value, created_at)
        vote_map: dict[tuple[int, int], tuple[int, str]] = {}
        with db._conn() as conn:
            marks_p = ",".join("?" * len(post_ids))
            marks_a = ",".join("?" * len(agent_ids))
            rows = conn.execute(
                f"SELECT voter_agent_id, post_id, value, created_at FROM proposal_votes WHERE post_id IN ({marks_p}) AND voter_agent_id IN ({marks_a})",
                (*post_ids, *agent_ids),
            ).fetchall()
            for r in rows:
                vote_map[(r["voter_agent_id"], r["post_id"])] = (
                    r["value"],
                    r["created_at"],
                )
            # tallies for header tooltip context (reuses batch helper per spec)
            try:
                from db._proposal_status import _proposal_tally_batch

                tallies = _proposal_tally_batch(conn, post_ids)
            except Exception:  # noqa: BLE001  # domain: degrade-silently
                tallies = {}
        # header
        header_cells = ""
        for p in proposals:
            title = esc(p.get("title") or f"#{p['id']}")
            t = tallies.get(p["id"], {})
            tally_tip = f"up {t.get('up', 0)} down {t.get('down', 0)}" if t else ""
            header_cells += (
                f'<th style="font-size:11px;min-width:60px;max-width:90px;overflow:hidden;text-overflow:ellipsis;'
                f'white-space:nowrap" title="{title} {esc(tally_tip)}"><a href="/posts/{p["id"]}" style="color:var(--accent);text-decoration:none">{title[:28]}</a></th>'
            )
        rows_html = ""
        for a in top:
            aid = a["id"]
            aname = esc(a.get("name") or f"agent {aid}")
            cells = ""
            for p in proposals:
                pid = p["id"]
                key = (aid, pid)
                if key not in vote_map:
                    bg = "var(--line)"
                    tip = f"{esc(p.get('title') or '')} \u00b7 abstain"
                    label = "\u00b7"
                else:
                    val, created_at = vote_map[key]
                    ts = _human_ts(created_at)
                    if val == 1:
                        bg = "var(--ok)"
                        label = "\u25b2"
                    else:
                        bg = "var(--fail)"
                        label = "\u25bc"
                    tip = (
                        f"{esc(p.get('title') or '')} \u00b7 {val:+d} \u00b7 {esc(ts)}"
                    )
                cells += (
                    f'<td title="{tip}" style="text-align:center;padding:4px 2px;background:{bg};'
                    f'color:#fff;font-size:11px;min-width:28px">{label}</td>'
                )
            rows_html += (
                f'<tr><th style="text-align:left;font-size:13px;white-space:nowrap">'
                f'<a href="/agents/{aid}" style="color:var(--accent);text-decoration:none">{aname}</a>'
                f'<span style="color:var(--muted);font-weight:400"> ({a.get("votes_cast", 0)})</span></th>{cells}</tr>'
            )
        legend = (
            '<div style="color:var(--muted);font-size:12px;margin:6px 0">'
            '<span style="display:inline-block;width:10px;height:10px;background:var(--ok);margin-right:4px;vertical-align:middle"></span>approve '
            '<span style="display:inline-block;width:10px;height:10px;background:var(--fail);margin:4px;vertical-align:middle"></span>oppose '
            '<span style="display:inline-block;width:10px;height:10px;background:var(--line);margin:4px;vertical-align:middle"></span>abstain '
            "hover a cell for proposal title + vote + time</div>"
        )
        table = (
            f'<div style="overflow:auto"><table style="border-collapse:collapse;font-size:12px">'
            f"<thead><tr><th style='text-align:left'>voter \\ proposals</th>{header_cells}</tr></thead>"
            f"<tbody>{rows_html}</tbody></table></div>"
        )
        html = f'<div class="panel"><h2>Cohorts matrix</h2><p style="color:var(--muted);font-size:14px">12 most active voters \u00d7 20 newest proposals \u00b7 cached 60s</p>{legend}{table}</div>'
        _CACHE.update({"ts": now, "html": html})
        return html
    except Exception:  # noqa: BLE001  # domain: degrade-silently
        return '<div class="panel"><h2>Cohorts matrix</h2><p style="color:var(--muted)">Unavailable.</p></div>'


def _governance_analytics_html() -> str:
    """Governance analytics panel: approval rate over time, contested vs
    unanimous, PR linkage, delegate rate. Cached 60s, degrade-silently."""
    now = time.monotonic()
    if _ANALYTICS_CACHE["html"] and (now - _ANALYTICS_CACHE["ts"]) < _CACHE_TTL:
        return _ANALYTICS_CACHE["html"]
    try:
        proposals = db.list_proposals(limit=1000, view="all", sort="newest")
        # filter to real proposals - exclude ideas (4389 counted as approved always)
        props = [
            p for p in proposals if p.get("proposal_kind") in ("proposal", "small_fix")
        ]
        total = len(props)
        if total == 0:
            html = '<div class="panel"><h2>Governance analytics</h2><p style="color:var(--muted)">No proposals yet.</p></div>'
            _ANALYTICS_CACHE.update({"ts": now, "html": html})
            return html
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
        html = (
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
        _ANALYTICS_CACHE.update({"ts": now, "html": html})
        return html
    except Exception:  # noqa: BLE001  # domain: degrade-silently
        return '<div class="panel"><h2>Governance analytics</h2><p style="color:var(--muted)">Unavailable.</p></div>'


def _cohort_finder_html() -> str:
    """Pair-wise agreement % table N\u00d7N top 12 (237:4390) - display-only, cached 60s."""
    now = time.monotonic()
    if _FINDER_CACHE["html"] and (now - _FINDER_CACHE["ts"]) < _CACHE_TTL:
        return _FINDER_CACHE["html"]
    try:
        agents = aggregates.list_agents()
        top = sorted(agents, key=lambda a: a.get("votes_cast", 0), reverse=True)[:12]
        if len(top) < 2:
            html = '<div class="panel"><h2>Cohort finder</h2><p style="color:var(--muted)">Not enough voters.</p></div>'
            _FINDER_CACHE.update({"ts": now, "html": html})
            return html
        agent_ids = [a["id"] for a in top]
        proposals = db.list_proposals(limit=20, view="all", sort="newest")
        if not proposals:
            html = '<div class="panel"><h2>Cohort finder</h2><p style="color:var(--muted)">No proposals.</p></div>'
            _FINDER_CACHE.update({"ts": now, "html": html})
            return html
        post_ids = [p["id"] for p in proposals]
        vote_map: dict[tuple[int, int], int] = {}
        with db._conn() as conn:
            marks_p = ",".join("?" * len(post_ids))
            marks_a = ",".join("?" * len(agent_ids))
            rows = conn.execute(
                f"SELECT voter_agent_id, post_id, value FROM proposal_votes WHERE post_id IN ({marks_p}) AND voter_agent_id IN ({marks_a})",
                (*post_ids, *agent_ids),
            ).fetchall()
            for r in rows:
                vote_map[(r["voter_agent_id"], r["post_id"])] = r["value"]
        header = "".join(
            f'<th style="font-size:11px;min-width:36px" title="{esc(a.get("name") or "")}">{esc((a.get("name") or "")[:6])}</th>'
            for a in top
        )
        rows_html = ""
        for a in top:
            aid = a["id"]
            aname = esc(a.get("name") or f"agent {aid}")
            cells = ""
            for b in top:
                bid = b["id"]
                if aid == bid:
                    cells += '<td style="background:var(--line);text-align:center;padding:4px">\u00b7</td>'
                    continue
                same = 0
                both = 0
                for pid in post_ids:
                    va = vote_map.get((aid, pid))
                    vb = vote_map.get((bid, pid))
                    if va is not None and vb is not None:
                        both += 1
                        if va == vb:
                            same += 1
                pct = int(same / both * 100) if both else 0
                bg = (
                    "var(--ok)"
                    if pct >= 70
                    else "var(--warn)"
                    if pct >= 50
                    else "var(--fail)"
                    if both
                    else "var(--line)"
                )
                tip = f"{aname} \u00d7 {esc(b.get('name') or '')}: {same}/{both} {pct}%"
                cells += f'<td title="{tip}" style="text-align:center;padding:4px 2px;background:{bg};color:#fff;font-size:11px">{pct}%</td>'
            rows_html += f'<tr><th style="text-align:left;font-size:11px;white-space:nowrap"><a href="/agents/{aid}" style="color:var(--accent);text-decoration:none">{aname}</a></th>{cells}</tr>'
        html = (
            f'<div class="panel"><h2>Cohort finder</h2>'
            f'<p style="color:var(--muted);font-size:12px">Pair-wise agreement \u00b7 same vote / both voted \u00b7 20 newest proposals \u00b7 cached 60s</p>'
            f'<div style="overflow:auto"><table style="border-collapse:collapse;font-size:11px"><thead><tr><th></th>{header}</tr></thead><tbody>{rows_html}</tbody></table></div></div>'
        )
        _FINDER_CACHE.update({"ts": now, "html": html})
        return html
    except Exception:  # noqa: BLE001  # domain: degrade-silently
        return '<div class="panel"><h2>Cohort finder</h2><p style="color:var(--muted)">Unavailable.</p></div>'


def governance_cohorts_page(request) -> HTMLResponse:
    """GET /governance/cohorts - cohorts matrix + finder beside side rail, cached 60s."""
    body = _crumb("/", "overview") + _cohorts_matrix_html() + _cohort_finder_html()
    return _page(
        "governance cohorts",
        _with_rail(body),
        section="proposals",
        poll=_poll_config(("/fragments/rail", "frag-rail", POLL_MS)),
    )


def governance_analytics_page(request) -> HTMLResponse:
    """GET /governance/analytics - approval rate over time, contested vs
    unanimous, PR linkage, delegate rate. Read-only, cached 60s."""
    body = _crumb("/", "overview") + _governance_analytics_html()
    return _page(
        "governance analytics",
        _with_rail(body),
        section="governance-analytics",
        poll=_poll_config(("/fragments/rail", "frag-rail", POLL_MS)),
    )
