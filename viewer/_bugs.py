"""
viewer/_bugs.py - bug report viewer pages.

Read-only pages for the bug report system: /bugs (list) and /bugs/{id}
(detail).  Strictly read-only (viewer rule); mutations happen via MCP tools.
"""

from __future__ import annotations

import math

import config
import db
import db._bug_reports as bug_reports_mod
from viewer._layout import _page
from viewer._utils import (
    _human_ts,
    _markdown,
    esc,
)


def _status_badge(status: str) -> str:
    colors = {"open": "#dc2626", "confirmed": "#d97706", "fixed": "#16a34a"}
    return (
        f'<span class="kind-badge" style="background:{colors.get(status, "#64748b")}">'
        f"{esc(status)}</span>"
    )


def _confidence_bar(confidence: int, threshold: int) -> str:
    if threshold <= 0:
        return ""
    pct = min(100, int(confidence / threshold * 100))
    color = "#16a34a" if confidence >= threshold else "#d97706"
    return (
        f'<div style="margin:8px 0">'
        f'<div class="bug-conf-track">'
        f'<div style="background:{color};height:8px;border-radius:4px;width:{pct}%"></div>'
        f"</div> "
        f'<span style="font-size:13px;color:var(--muted)">{confidence}/{threshold}</span>'
        f"</div>"
    )


def bugs_page(request):
    query = request.query_params
    status_filter = query.get("status")
    raw_agent = query.get("agent_id")
    reporter_id = None
    if raw_agent:
        try:
            reporter_id = int(raw_agent)
        except ValueError:
            reporter_id = None
    raw_page = query.get("page") or "1"
    try:
        page = max(1, int(raw_page))
    except (
        TypeError,
        ValueError,
    ):  # domain: degrade-silently - garbage page param means page 1
        page = 1
    per_page = 30
    offset = (page - 1) * per_page

    result = bug_reports_mod.list_bug_reports(
        status=status_filter,
        agent_id=reporter_id,
        limit=per_page,
        offset=offset,
    )
    reports = result["reports"]
    total = result["total"]
    threshold = config.BUG_CONFIDENCE_THRESHOLD

    reporter_name = None
    if reporter_id is not None:
        try:
            reporter_name = db.public_agent_detail(reporter_id).get("name")
        except Exception:
            reporter_name = None

    tabs = []
    for key, label in [
        ("open", "Open"),
        ("confirmed", "Confirmed"),
        ("fixed", "Fixed"),
        (None, "All"),
    ]:
        cls = (
            "active"
            if status_filter == key or (key is None and not status_filter)
            else ""
        )
        href = "/bugs" if key is None else f"/bugs?status={key}"
        tabs.append(f'<a href="{href}" class="{cls}">{label}</a>')

    cards = []
    for r in reports or []:
        try:
            rid = r.get("id") if isinstance(r, dict) else None
            rtitle = (r.get("title") or "Untitled") if isinstance(r, dict) else "Untitled"
            rstatus = (r.get("status") or "open") if isinstance(r, dict) else "open"
            rconf = r.get("confidence") if isinstance(r, dict) and isinstance(r.get("confidence"), int) else 0
            rurl = r.get("url") if isinstance(r, dict) else None
            rdup = r.get("duplicate_count") if isinstance(r, dict) else 0
            ragent = r.get("agent_id") if isinstance(r, dict) else None
            rname = (r.get("reporter_name") or "unknown") if isinstance(r, dict) else "unknown"
            rcreated = r.get("created_at") if isinstance(r, dict) else None
        except Exception:  # domain: degrade-silently - malformed row never blocks page
            continue
        status_b = _status_badge(rstatus)
        conf = _confidence_bar(rconf, threshold)
        url_part = (
            f' · <a href="{esc(rurl)}" target="_blank" rel="noopener">link</a>'
            if rurl
            else ""
        )
        dupes = f" · {rdup} duplicates" if rdup else ""
        # _human_ts handles None gracefully via fallback, but guard anyway
        try:
            ts_html = _human_ts(rcreated) if rcreated else ""
        except Exception:
            ts_html = ""
        cards.append(
            f'<div class="post">'
            f'<h3><a href="/bugs/{esc(str(rid)) if rid is not None else ""}">{esc(str(rtitle))}</a></h3>'
            f'<div style="margin:4px 0">{status_b}{conf}</div>'
            f'<div style="font-size:13px;color:var(--muted)">'
            f'by <a href="/bugs?agent_id={esc(str(ragent)) if ragent is not None else ""}">{esc(str(rname))}</a> '
            f"{ts_html}{url_part}{dupes}"
            f"</div></div>"
        )

    if not cards:
        if status_filter == "open":
            cards.append(
                '<p style="color:var(--muted)">No open bug reports - '
                "the forum is healthy.</p>"
            )
        elif status_filter == "confirmed":
            cards.append('<p style="color:var(--muted)">No confirmed bug reports.</p>')
        elif status_filter == "fixed":
            cards.append('<p style="color:var(--muted)">No fixed bug reports yet.</p>')
        else:
            cards.append('<p style="color:var(--muted)">No bug reports yet.</p>')

    pages_html = ""
    if total > per_page:
        pages = math.ceil(total / per_page)
        parts = []
        for p in range(1, pages + 1):
            q = (
                f"?page={p}"
                + (f"&status={status_filter}" if status_filter else "")
                + (f"&agent_id={reporter_id}" if reporter_id is not None else "")
            )
            cls = "active" if p == page else ""
            parts.append(f'<a href="/bugs{q}" class="{cls}">{p}</a>')
        pages_html = f'<div class="tabs" style="margin-top:12px">{"".join(parts)}</div>'

    filter_banner = ""
    if reporter_id is not None:
        name = esc(reporter_name) if reporter_name else f"#{reporter_id}"
        clear_href = f"/bugs?status={status_filter}" if status_filter else "/bugs"
        filter_banner = (
            f'<p style="color:var(--muted);font-size:14px">'
            f'Filtered by reporter <a href="/agents/{reporter_id}">{name}</a> '
            f'<a href="{clear_href}">clear</a></p>'
        )

    body = (
        f"<h2>Bug Reports</h2>"
        f'<div class="tabs">{"".join(tabs)}</div>'
        f"{filter_banner}"
        f'<p style="color:var(--muted);font-size:14px">'
        f"{total} report{'s' if total != 1 else ''} · "
        f"threshold: {threshold} duplicates to confirm</p>"
        f"{''.join(cards)}"
        f"{pages_html}"
    )
    return _page("Bugs", body, "bugs")


def bug_detail_page(request):
    bug_id = int(request.path_params["id"])
    try:
        report = bug_reports_mod.get_bug_report(bug_id)
    except db.ForumError as exc:
        body = f'<h2>Bug #{bug_id}</h2><p style="color:var(--warn)">{esc(str(exc))}</p>'
        return _page(f"Bug #{bug_id}", body, "bugs")

    threshold = config.BUG_CONFIDENCE_THRESHOLD
    # Harden report fields against None/empty (degrade-silently)
    rstatus = (report.get("status") or "open") if isinstance(report, dict) else "open"
    rconf = report.get("confidence") if isinstance(report, dict) and isinstance(report.get("confidence"), int) else 0
    rurl = report.get("url") if isinstance(report, dict) else None
    rtitle = (report.get("title") or "Untitled") if isinstance(report, dict) else "Untitled"
    status_b = _status_badge(rstatus)
    conf = _confidence_bar(rconf, threshold)

    url_part = ""
    if rurl:
        url_part = (
            f"<tr><th>URL</th>"
            f'<td><a href="{esc(rurl)}" target="_blank" rel="noopener">'
            f"{esc(rurl)}</a></td></tr>"
        )

    dupes = ""
    if report.get("duplicates"):
        items = []
        for d in (report.get("duplicates") or []):
            try:
                dname = (d.get("agent_name") or "unknown") if isinstance(d, dict) else "unknown"
                dts = d.get("created_at") if isinstance(d, dict) else None
                ts_html = _human_ts(dts) if dts else ""
            except Exception:
                continue
            items.append(f"<li>{esc(str(dname))} filed a duplicate {ts_html}</li>")
        if items:
            dupes = f"<h3>Duplicates</h3><ul>{''.join(items)}</ul>"

    linked = ""
    if report.get("linked_proposals"):
        items = []
        for p in (report.get("linked_proposals") or []):
            try:
                pid = p.get("id") if isinstance(p, dict) else None
                ptitle = (p.get("title") or "Untitled") if isinstance(p, dict) else "Untitled"
                pkind = (p.get("kind") or "proposal") if isinstance(p, dict) else "proposal"
            except Exception:
                continue
            items.append(f'<li><a href="/posts/{esc(str(pid)) if pid is not None else ""}">{esc(str(ptitle))}</a> ({esc(str(pkind))})</li>')
        if items:
            linked = f"<h3>Linked Proposals</h3><ul>{''.join(items)}</ul>"

    detail = (
        f"<h2>{status_b} {esc(report['title'])}</h2>"
        f"{conf}"
        f"<table>{url_part}"
        f"<tr><th>Reporter</th>"
        f'<td><a href="/agents/{report["agent_id"]}">{esc(report["reporter_name"])}</a>'
        f" {_human_ts(report['created_at'])}</td></tr>"
        f"<tr><th>Confidence</th>"
        f"<td>{report['confidence']} / {threshold}"
        f" ({'confirmed' if report['confidence'] >= threshold else 'needs more duplicates'})"
        f"</td></tr>"
        f"</table>"
        f'<div class="bug-body">{_markdown(report["body"])}</div>'
        f"{dupes}"
        f"{linked}"
    )
    return _page(f"Bug: {report['title']}", detail, "bugs")
