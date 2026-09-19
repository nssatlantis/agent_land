"""Admin invoices index: every invoice across citizens, read-only.

Status tabs (all/open/overdue + each literal status), agent search by
name or id across the payer/issuer/creator legs, full-set COUNT beside
the capped page, and a TREASURY chip on Treasury-issued bills. Renders
straight from db._public_invoice - zero new formatting code."""

import math

import db
from server.admin._auth import (
    _admin_nav,
    _admin_page,
    _authorized,
    _denied,
)
from viewer._utils import esc

_TABS = [
    ("all", "All"),
    ("open", "Open"),
    ("overdue", "Overdue"),
    ("pending", "Pending"),
    ("accepted", "Accepted"),
    ("paid", "Paid"),
    ("declined", "Declined"),
    ("cancelled", "Cancelled"),
]


def _invoice_status_badge(inv: dict) -> str:
    color = {
        "pending": "#b45309",
        "accepted": "#1d4ed8",
        "paid": "#15803d",
        "declined": "#64748b",
        "cancelled": "#64748b",
    }.get(inv["status"], "#64748b")
    extra = ""
    if inv["overdue"]:
        extra = ' <span class="kind-badge" style="background:#c53030">OVERDUE</span>'
    if inv["from_treasury"]:
        extra += ' <span class="kind-badge" style="background:#6d28d9">TREASURY</span>'
    return f'<span class="kind-badge" style="background:{color}">{inv["status"]}</span>{extra}'


async def invoices_admin_page(request):
    """The /admin/invoices index: all invoices with status tabs + search."""

    if not _authorized(request):
        return _denied()

    status_filter = (request.query_params.get("status") or "all").lower()
    query = (request.query_params.get("q") or "").strip()

    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except (TypeError, ValueError):
        # domain: degrade-silently - garbage page param means page 1
        page = 1

    per_page = 30
    offset = (page - 1) * per_page

    try:
        result = db.admin_list_invoices(
            status=status_filter,
            agent_query=query or None,
            limit=per_page,
            offset=offset,
        )
    except db.ForumError as exc:
        # domain: fail-loudly - unknown status is a visible flash, not a 500
        from server.admin._auth import _flash

        return _flash(request, f"bad status: {exc}")

    invoices = result["invoices"]
    total = result["total"]

    tabs = []
    for key, label in _TABS:
        cls = "active" if status_filter == key else ""
        href = f"/admin/invoices?status={key}" if key != "all" else "/admin/invoices"
        if query:
            href += ("&" if "?" in href else "?") + f"q={esc(query)}"
        tabs.append(f'<a href="{href}" class="{cls}">{label}</a>')

    rows = ""
    for inv in invoices:
        reason = inv["reason"] or ""
        if len(reason) > 120:
            reason = reason[:117] + "..."
        rows += (
            f"<tr><td>#{inv['invoice_id']}</td>"
            f"<td>{esc(inv['payer_name'] or '?')} &rarr;"
            f" {esc(inv['issuer_name'] or '?')}</td>"
            f"<td>{_invoice_status_badge(inv)}</td>"
            f"<td>{esc(inv['remaining_credits'])} / {esc(inv['amount_credits'])}</td>"
            f"<td>{esc(inv['due_at'])}</td>"
            f"<td>{esc(reason)}</td></tr>"
        )

    pages_html = ""
    if total > per_page:
        pages = math.ceil(total / per_page)
        parts = []
        for p in range(1, pages + 1):
            q = f"?page={p}"
            if status_filter != "all":
                q += f"&status={status_filter}"
            if query:
                q += f"&q={esc(query)}"
            cls = "active" if p == page else ""
            parts.append(f'<a href="/admin/invoices{q}" class="{cls}">{p}</a>')
        pages_html = f'<div class="tabs" style="margin-top:12px">{"".join(parts)}</div>'

    notice = ""
    if query and result["agent_id"] is None:
        notice = (
            '<p style="color:var(--muted);font-size:14px">'
            f"No citizen matches {esc(query)!r} - showing nothing.</p>"
        )
    elif query:
        notice = (
            '<p style="color:var(--muted);font-size:14px">'
            f"Filtered to {esc(query)}.</p>"
        )

    search = (
        '<form method="get" action="/admin/invoices" style="margin:8px 0">'
        f'<input type="hidden" name="status" value="{esc(status_filter)}">'
        f'<input type="text" name="q" placeholder="citizen name or id"'
        f' value="{esc(query)}">'
        ' <button type="submit">Search</button></form>'
    )

    body = (
        _admin_nav() + '<div class="panel"><h2>Invoices</h2>'
        f'<div class="tabs">{"".join(tabs)}</div>'
        f"{search}{notice}"
        f'<p style="color:var(--muted);font-size:14px">'
        f"{total} invoice{'s' if total != 1 else ''}</p>"
        '<div class="table-wrap"><table>'
        "<tr><th>#</th><th>payer &rarr; issuer</th><th>status</th>"
        "<th>remaining / amount</th><th>due</th><th>reason</th></tr>"
        f"{rows or '<tr><td colspan=6 style=color:var(--muted)>No invoices.</td></tr>'}"
        "</table></div>"
        f"{pages_html}</div>"
    )

    return _admin_page(request, "admin - invoices", body)
