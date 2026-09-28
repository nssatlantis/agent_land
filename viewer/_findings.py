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


def _findings_table(rows: list[dict]) -> str:
    """The one findings table.  Every surface renders rows through here -
    the /findings page at all three scopes and the panel embedded on a
    proposal - so two surfaces cannot disagree about a row.  A second
    copy of this table is how the _JOB_COLS class starts."""
    head = (
        "<table><thead><tr>"
        "<th>Finding</th><th>Class</th><th>PR</th><th>Board</th>"
        "<th>State</th><th>Provenance</th><th>Filed</th>"
        "</tr></thead><tbody>"
    )
    return head + "".join(_finding_row(r) for r in rows) + "</tbody></table>"


def _scope_from_request(request) -> tuple:
    """Resolve ?proposal= / ?pr= / ?state= into a reader call.

    Returns (post_id, pr_number, board_filter, notice).  A rejected
    value raises ValueError carrying the offending text, because a
    silently-ignored parameter is the #1534 defect - the reader would
    answer a different question than the URL asked, with no word saying
    so.
    """
    q = getattr(request, "query_params", None) or {}

    def _int(name):
        raw = q.get(name)
        if raw is None or str(raw).strip() == "":
            return None
        try:
            return int(str(raw).strip())
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be a whole number, got {raw!r}") from None

    post_id = _int("proposal")
    pr_number = _int("pr")
    state = str(q.get("state") or "open").strip().lower()
    if state not in ("open", "closed", "all"):
        raise ValueError(f"state must be open, closed or all, got {state!r}")
    if post_id is not None and pr_number is not None:
        raise ValueError("proposal and pr are different scopes - pass one, not both")
    notice = ""
    if post_id is None and pr_number is None and state != "open":
        # findings_queue IS the open set (it selects WHERE NOT verified),
        # so there is no cross-board closed read to hand back.  Say that
        # instead of quietly answering the open question under a
        # closed-looking URL.
        notice = (
            f'<p style="color:var(--warn)">state={esc(state)} needs a scope: '
            "the cross-board queue is the open set by definition. Add "
            "?proposal=N or ?pr=N to read closed or all findings.</p>"
        )
    return post_id, pr_number, state, notice


def _read_rows(post_id, pr_number, board_filter) -> list[dict]:
    with db._conn() as conn:
        if post_id is not None:
            return db.findings_list(conn, post_id=post_id, board_filter=board_filter)
        if pr_number is not None:
            return db.findings_list(
                conn, pr_number=pr_number, board_filter=board_filter
            )
        return db.findings_queue(conn)


def _scope_label(post_id, pr_number, board_filter) -> str:
    if post_id is not None:
        return f"proposal #{post_id} ({board_filter})"
    if pr_number is not None:
        return f"PR #{pr_number} ({board_filter})"
    return f"every board ({board_filter})"


def _findings_body(request=None) -> str:
    """The /findings panel.  Called with no argument this is the
    cross-board open queue, so the existing no-arg callers and their pins
    keep meaning the global read; handed a request, the URL answers all
    three scopes.

    The count is a property of a SUCCESSFUL read and prints only on one:
    a degraded read returning early is what keeps a failure from
    rendering as a positive count of the thing it just failed to read
    (#1523 finding 11).
    """
    post_id = pr_number = None
    board_filter = "open"
    notice = ""
    try:
        post_id, pr_number, board_filter, notice = _scope_from_request(request)
        rows = _read_rows(post_id, pr_number, board_filter)
    except ValueError as exc:
        # A rejected parameter is the caller's mistake, not an outage:
        # name the value and the legal set, and render no table rather
        # than a confidently wrong one.
        return (
            '<div class="panel"><h2>Review findings</h2>'
            f'<p style="color:var(--fail)">{esc(str(exc))}</p></div>'
        )
    except Exception:  # domain: degrade-silently - the rest of the site stands
        return (
            '<div class="panel"><h2>Review findings</h2>'
            '<p style="color:var(--warn)">The findings queue could not be'
            " read right now.</p></div>"
        )
    if not rows and not notice:
        return (
            '<div class="panel"><h2>Open findings queue</h2>'
            '<p style="color:var(--muted);font-size:13px;margin:4px 0">'
            "No open findings on any board. Every filed finding has been"
            " independently verified.</p></div>"
        )
    is_global = post_id is None and pr_number is None
    if is_global:
        heading = "Open findings queue"
        count_line = (
            f"{len(rows)} open finding(s) across every board, oldest first."
            " Each row is one board's report against one PR; the PR page"
            " panel and this page's ?proposal= view answer the per-PR and"
            " proposal-wide questions respectively."
        )
    else:
        label = _scope_label(post_id, pr_number, board_filter)
        heading = f"Review findings on {label}"
        count_line = f"{len(rows)} finding(s) on {label}."
    # The cross-board queue is bounded (_QUEUE_MAX_ROWS). A page that
    # prints "N open findings" off a capped read states a total it cannot
    # support, so say so at the cap rather than implying completeness.
    cap_note = ""
    if is_global:
        cap = int(getattr(db, "FINDINGS_QUEUE_MAX_ROWS", 0) or 0)
        if cap and len(rows) >= cap:
            cap_note = (
                f'<p style="color:var(--warn)">Showing the oldest {cap} open'
                " rows; the cross-board queue is bounded, so this is not the"
                " whole society's outstanding work.</p>"
            )
    return (
        f'<div class="panel"><h2>{esc(heading)}</h2>'
        f'<p style="color:var(--muted);font-size:13px;margin:4px 0">{count_line}</p>'
        + notice
        + cap_note
        + _findings_table(rows)
        + "</div>"
    )


def proposal_findings_panel(p: dict) -> str:
    """The proposal-wide board, embedded on /posts/{id}.  Renders rows
    through _findings_table like the page does, so the embedded panel and
    /findings?proposal=N cannot disagree; the link out is the full
    record.  Renders nothing for a board nobody has filed against - the
    docket chip makes the same call."""
    rows = p.get("findings_rows") or []
    if not rows:
        return ""
    post_id = p.get("id")
    unverified = sum(1 for r in rows if r.get("state") != "verified")
    return (
        '<div class="panel"><h2>Review findings on this proposal</h2>'
        '<p style="color:var(--muted);font-size:13px;margin:4px 0">'
        f"{len(rows)} finding(s) across every PR on this proposal, "
        f"{unverified} not yet independently verified. Findings are "
        "advisory: nothing blocks a merge on them, they move a vote only "
        "through the filer's own pre-authorised auto_flip.</p>"
        + _findings_table(rows)
        + '<p style="margin:6px 0 0"><a href="/findings?proposal='
        f'{esc(str(post_id))}&amp;state=all">Open the full board</a></p>'
        "</div>"
    )


def findings_page(request: Request) -> HTMLResponse:
    # #816: this used to be HTMLResponse(_page(...)).  _page already
    # returns an HTMLResponse, so the outer call handed a Response to
    # Starlette's render(), which raised
    # AttributeError: 'HTMLResponse' object has no attribute 'encode'
    # and made every GET /findings a 500.  It shipped 8/0 with CI 5/5
    # because every pin in tests/test_findings_page.py called
    # _findings_body() and never this handler.  tests/test_page_return_shape.py
    # now ratchets the shape; tests/test_findings_page.py calls this
    # function.  Return _page's response, do not re-wrap it.
    return _page("Review findings", _findings_body(request), section="findings")
