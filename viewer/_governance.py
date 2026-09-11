"""viewer/_governance.py - governance cohorts matrix (237:4389).

Display-only, read-only: 12 most active voters (by votes_cast) — 20 newest
proposals, cells green/red/grey for approve/oppose/abstain. Hover shows
proposal title + vote value + vote time. Reuses aggregates.list_agents and
db._proposal_tally_batch (via batch tally), cached 60s, degrade-silently.

(The approval-rate analytics panel used to live here; it moved to
viewer/_analytics.py with the /governance/analytics fold.)
"""

from __future__ import annotations

from starlette.responses import HTMLResponse

import db
import db._aggregates as aggregates
from viewer._cache import _cached
from viewer._feed_helpers import _crumb, _with_rail
from viewer._layout import POLL_MS, _page, _poll_config
from viewer._utils import esc


def _cohorts_matrix_html() -> str:
    """Build the cohorts matrix panel. Cached 60s, degrade-silently."""
    return _cached("cohorts_matrix", 60, _build_cohorts_matrix)


def _build_cohorts_matrix() -> str:
    try:
        agents = aggregates.list_agents()
        top = sorted(agents, key=lambda a: a.get("votes_cast", 0), reverse=True)[:12]
        if not top:
            return '<div class="panel"><h2>Cohorts matrix</h2><p style="color:var(--muted)">No voters yet.</p></div>'
        agent_ids = [a["id"] for a in top]
        # 20 newest proposals (any kind, newest first)
        proposals = db.list_proposals(limit=20, view="all", sort="newest")
        if not proposals:
            return '<div class="panel"><h2>Cohorts matrix</h2><p style="color:var(--muted)">No proposals yet.</p></div>'
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
                    if val == 1:
                        bg = "var(--ok)"
                        label = "\u25b2"
                    else:
                        bg = "var(--fail)"
                        label = "\u25bc"
                    tip = f"{esc(p.get('title') or '')} \u00b7 {val:+d} \u00b7 {esc(created_at)}"
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
        return f'<div class="panel"><h2>Cohorts matrix</h2><p style="color:var(--muted);font-size:14px">12 most active voters \u00d7 20 newest proposals \u00b7 cached 60s</p>{legend}{table}</div>'
    except Exception:  # noqa: BLE001  # domain: degrade-silently
        return '<div class="panel"><h2>Cohorts matrix</h2><p style="color:var(--muted)">Unavailable.</p></div>'


def _cohort_finder_html() -> str:
    """Pair-wise agreement % table N\u00d7N top 12 (237:4390) - display-only, cached 60s."""
    return _cached("cohort_finder", 60, _build_cohort_finder)


def _build_cohort_finder() -> str:
    try:
        agents = aggregates.list_agents()
        top = sorted(agents, key=lambda a: a.get("votes_cast", 0), reverse=True)[:12]
        if len(top) < 2:
            return '<div class="panel"><h2>Cohort finder</h2><p style="color:var(--muted)">Not enough voters.</p></div>'
        agent_ids = [a["id"] for a in top]
        proposals = db.list_proposals(limit=20, view="all", sort="newest")
        if not proposals:
            return '<div class="panel"><h2>Cohort finder</h2><p style="color:var(--muted)">No proposals.</p></div>'
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
        return (
            f'<div class="panel"><h2>Cohort finder</h2>'
            f'<p style="color:var(--muted);font-size:12px">Pair-wise agreement \u00b7 same vote / both voted \u00b7 20 newest proposals \u00b7 cached 60s</p>'
            f'<div style="overflow:auto"><table style="border-collapse:collapse;font-size:11px"><thead><tr><th></th>{header}</tr></thead><tbody>{rows_html}</tbody></table></div></div>'
        )
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
