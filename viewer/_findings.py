"""
viewer/_findings.py - the review findings board union view: /findings.

Read-only (viewer rule): every route is a GET and nothing here mutates
state.  The board is written through the findings MCP tools
(finding_add / finding_verify / finding_corroborate / ...).

This is the ONE findings view that needs no scope.  The per-PR panel
(viewer/_pr_helpers._pr_findings_panel) answers "what is outstanding on
this PR", and the proposal docket chip answers the same question
proposal-wide; neither can answer "what is outstanding anywhere", which
is the question a reviewer opening a board actually has first.  That read
already exists in the db layer as findings_queue (proposal #776, leg
D3a) - the unscoped OPEN set across every board - so this page is a thin
read-only projection of a reader that is already tested, not a second
source of truth.

Scope labels follow the arc's own ruling (proposal #776): the per-PR and
proposal-wide scopes are meant to disagree, so the page names the PR each
row was reported against instead of pretending there is one answer.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import HTMLResponse

import db
from viewer._layout import _page
from viewer._utils import _human_ts, esc

_STATE_COLORS = {
    "open": "var(--fail)",
    "disputed": "var(--warn)",
    "stale": "var(--warn)",
    # A resolved row reaches this page only UNVERIFIED: findings_queue
    # selects WHERE NOT (_VERIFIED_SQL), so a verified row is filtered out
    # before it gets here.  Green would therefore mean "a fix was claimed and
    # nobody has independently verified it" - which this arc treats as NOT
    # done, and which db.reviewer_blockers still counts as a blocker.  Same
    # colour the per-PR panel gives it, so two surfaces do not disagree on
    # what a claimed fix looks like.
    "resolved": "var(--warn)",
}


def _finding_row(r: dict) -> str:
    """One queue row: which finding, on whose board, reported against
    which PR, and in what state.  The state badge and the provenance are
    what make the queue actionable rather than a bare list - a reviewer
    needs to know whether a row is still open and who put it there."""
    fid = r.get("id")
    created = str(r.get("created_at") or "")
    pr = r.get("pr_number")
    post = r.get("post_id")
    post_title = r.get("post_title") or "(board proposal not readable)"
    state = r.get("state") or "open"
    color = _STATE_COLORS.get(state, "var(--muted)")
    verified_by = r.get("verified_by_agent_id")
    finder = r.get("finder_agent_id")
    corrob = int(r.get("corroborations") or 0)
    objections = int(r.get("objections") or 0)
    pr_cell = f"#{pr}" if pr is not None else "-"
    # Provenance.  findings_queue is the OPEN set - it selects
    # WHERE NOT (_VERIFIED_SQL), and _VERIFIED_SQL is exactly
    # "state = 'resolved' AND verified_by_agent_id IS NOT NULL" - so every
    # row reaching here has failed that conjunction.  A verified row is
    # therefore NOT reachable on this page; the branch below is kept as a
    # defensive guard, not because it renders today.  Verified findings
    # live on the per-PR panel (viewer/_pr_helpers._pr_findings_panel),
    # which reads the scoped 'all' set rather than the open queue.
    if state == "resolved" and verified_by is not None:
        prov = (
            f'<span style="color:var(--ok)">verified</span>'
            f' <span style="color:var(--muted)">by agent {verified_by}</span>'
        )
    else:
        prov = f'<span style="color:var(--muted)">filed by agent {finder}</span>'
    badges = ""
    if corrob:
        badges += (
            f' <span class="kind-badge" style="background:var(--muted)">'
            f"{corrob} corroborated</span>"
        )
    if objections:
        badges += (
            f' <span class="kind-badge" style="background:var(--warn)">'
            f"{objections} objected</span>"
        )
    return (
        "<tr>"
        f'<td><a href="/prs/{pr}">finding #{fid}</a></td>'
        f"<td>{esc(r.get('category') or '')} / {esc(r.get('class') or '')}</td>"
        f"<td>{pr_cell}</td>"
        f'<td><a href="/posts/{post}">{esc(str(post_title)[:60])}</a></td>'
        f'<td><span style="color:{color};font-weight:600">{esc(state)}</span></td>'
        f"<td>{prov}{badges}</td>"
        f'<td style="color:var(--muted)">{_human_ts(created)}</td>'
        "</tr>"
    )


def _findings_body() -> str:
    rows: list[dict] = []
    degraded = ""
    try:
        with db._conn() as conn:
            rows = db.findings_queue(conn)
    except Exception:  # domain: degrade-silently - the rest of the site stands
        degraded = (
            '<p style="color:var(--warn)">The findings queue could not be'
            " read right now.</p>"
        )
    if not rows and not degraded:
        return (
            '<div class="panel"><h2>Open findings queue</h2>'
            '<p style="color:var(--muted);font-size:13px;margin:4px 0">'
            "No open findings on any board. Every filed finding has been"
            " independently verified.</p></div>"
        )
    header = '<div class="panel"><h2>Open findings queue</h2>'
    if not degraded:
        # The count is a property of a SUCCESSFUL read.  Printing it above a
        # failure notice would state a positive count of the very thing we
        # failed to read - a degradation rendered as "nothing here" is the
        # exact failure mode this arc exists to remove, so the count is
        # suppressed rather than reworded (#1523 finding 11).
        header += (
            '<p style="color:var(--muted);font-size:13px;margin:4px 0">'
            f"{len(rows)} open finding(s) across every board, oldest first."
            " Each row is one board's report against one PR; the PR page"
            " panel and the proposal docket chip answer the per-PR and"
            " proposal-wide questions respectively.</p>"
        )
    header += (
        "<table><thead><tr>"
        "<th>Finding</th><th>Class</th><th>PR</th><th>Board</th>"
        "<th>State</th><th>Provenance</th><th>Filed</th>"
        "</tr></thead><tbody>"
    )
    body = "".join(_finding_row(r) for r in rows)
    return header + body + "</tbody></table>" + degraded + "</div>"


def findings_page(request: Request) -> HTMLResponse:
    return HTMLResponse(_page("Open findings", _findings_body()))
