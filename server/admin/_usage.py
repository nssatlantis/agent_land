"""server/admin/_usage.py — admin-only tool-usage observability.

Read-only admin overview of what MCP tools work well: per-tool call counts /
success rate / duration (all recorded calls, merged from the long-term
`tool_usage` aggregate and the recent `tool_calls` ledger), the most recent
failures with their reasons, and per-agent usage. Sits purely on
db._tool_usage readers; no public viewer surface, no new events-ledger kind.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import HTMLResponse

import db
from server.admin._auth import _admin_nav, _admin_page, _authorized, _denied
from viewer._utils import esc


def _pct(ok: int, total: int) -> str:
    if total <= 0:
        return "—"
    return f"{100.0 * ok / total:.1f}%"


async def usage_admin_page(request: Request) -> HTMLResponse:
    if not _authorized(request):
        return _denied()
    try:  # domain: degrade-silently - stats are advisory; a query failure
        # just renders empty panels rather than breaking the admin page.
        tools = db.tool_usage_summary(50)
        failures = db.tool_usage_recent_failures(50)
        by_agent = db.tool_usage_by_agent(50)
    except Exception:
        tools, failures, by_agent = [], [], []

    # Tool summary panel.
    if tools:
        tool_rows = ""
        for t in tools:
            tool_rows += (
                f"<tr><td>{esc(t['tool'])}</td><td>{t['calls']}</td>"
                f"<td>{_pct(t['ok'], t['calls'])}</td>"
                f"<td>{t['failed']}</td>"
                f"<td>{t['total_duration_ms'] / 1000:.2f}s</td></tr>"
            )
        tool_table = (
            "<table><thead><tr><th>tool</th><th>calls</th><th>success</th>"
            "<th>failed</th><th>total time</th></tr></thead>"
            f"<tbody>{tool_rows}</tbody></table>"
        )
    else:
        tool_table = '<p style="color:var(--muted)">No recorded tool calls yet.</p>'

    # Recent failures panel (drill-down: which agent, which reason).
    if failures:
        fail_rows = ""
        for f in failures:
            who = f["agent_name"] or f"(agent {f['agent_id']})"
            fail_rows += (
                f"<tr><td>{esc(f['created_at'][:19])}</td>"
                f"<td>{esc(f['tool'])}</td>"
                f"<td>{esc(who)}</td>"
                f'<td style="max-width:420px">{esc(f["note"])}</td></tr>'
            )
        fail_table = (
            "<h3>Recent failures</h3>"
            "<table><thead><tr><th>when</th><th>tool</th><th>agent</th>"
            "<th>reason</th></tr></thead>"
            f"<tbody>{fail_rows}</tbody></table>"
        )
    else:
        fail_table = ""

    # Per-agent usage panel.
    if by_agent:
        agent_rows = ""
        for a in by_agent:
            agent_rows += (
                f"<tr><td>{esc(a['agent_name'])}</td><td>{a['calls']}</td>"
                f"<td>{a['ok']}</td><td>{a['failed']}</td>"
                f"<td>{_pct(a['ok'], a['calls'])}</td></tr>"
            )
        agent_table = (
            "<h3>Usage by agent (recent window)</h3>"
            "<table><thead><tr><th>agent</th><th>calls</th><th>ok</th>"
            "<th>failed</th><th>success</th></tr></thead>"
            f"<tbody>{agent_rows}</tbody></table>"
        )
    else:
        agent_table = ""

    body = (
        _admin_nav()
        + '<div class="panel"><h2>Tool usage — admin</h2>'
        + tool_table
        + fail_table
        + agent_table
        + "</div>"
    )
    return _admin_page(request, "admin — tool usage", body)
