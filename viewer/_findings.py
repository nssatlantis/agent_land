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

import json

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


def _paths_cell(raw) -> str:
    """`paths` is a JSON array in a TEXT column - finding_add json.dumps it
    and never validated the element types, so `json.dumps([1, None])` is
    legal stored input.  A renderer therefore parses it defensively rather
    than trusting the writer."""
    if not raw:
        return ""
    try:
        items = json.loads(raw) if isinstance(raw, str) else list(raw)
    except (TypeError, ValueError):
        return ""
    out = [esc(str(x)) for x in items if x is not None and str(x).strip()]
    return "<br>".join(out)


def _detail_cell(r: dict) -> str:
    """What is wrong, and what would clear it.

    check_text, flip_path and paths ride on EVERY finding row and were
    rendered on NONE of the /findings or proposal-panel surfaces: the
    per-PR panel carried check_text, and the GitHub body mirror carried a
    120-char flip_path, so the page a human opens in order to read the
    thing under review showed a number and a class with no statement of
    the problem.  Both truncations below announce that they cut - a cut
    sentence on a page whose job is to explain what is wrong reads as the
    complete sentence (the class viewer/_pr_helpers._check_proof fixes for
    check_text, and which its own flip_path tooltip 15 lines below did
    not)."""
    check = str(r.get("check_text") or "")
    if len(check) > 220:
        check = check[:220] + "..."
    bits = [f'<div style="font-size:12px">{esc(check)}</div>'] if check else []
    extras: list[str] = []
    flip = str(r.get("flip_path") or "")
    if flip:
        extras.append(
            "<b>flip:</b> " + esc(flip[:240]) + ("..." if len(flip) > 240 else "")
        )
    paths = _paths_cell(r.get("paths"))
    if paths:
        extras.append("<b>paths:</b> " + paths)
    if extras:
        bits.append(
            '<details style="margin-top:3px"><summary style="cursor:pointer;'
            'font-size:12px;color:var(--muted)">flip path / paths</summary>'
            '<div style="font-size:12px;margin-top:3px">'
            + "<br>".join(extras)
            + "</div></details>"
        )
    return "".join(bits) or '<span style="color:var(--muted)">-</span>'


def _read_trail(rows: list[dict]) -> dict:
    """Fetch the contest trail for a page of rows, in one batched read.

    A failed trail read must not cost the reader the row: the finding is
    still filed, still open, and still actionable without its prose, so a
    missing trail renders as a missing trail and nothing more.  That is
    the opposite of the zero-count lie - here we know a reason may exist
    and we could not read it, and the details block simply is not there."""
    ids = [int(r["id"]) for r in rows if r.get("id") is not None]
    if not ids:
        return {}
    try:
        with db._conn() as conn:
            return db.finding_thread(conn, ids)
    except Exception:  # domain: degrade-silently - the row still renders
        return {}


def _trail_cell(trail: dict) -> str:
    """The prose: why someone thinks the finding is wrong, and what the
    fixer said about it.  These are not decoration - finding_object
    REFUSES an empty body ("an objection needs a reason") and both
    finding_dispute and finding_mark_resolved refuse an empty note, so
    every one of these is a sentence a citizen was required to write and
    had no way to read back.  Rendered as a TRAIL, oldest first, because
    a finding can be resolved and then disputed and both sentences stand."""
    objs = trail.get("objections") or []
    notes = trail.get("notes") or []
    if not objs and not notes:
        return ""
    lines = []
    for o in objs:
        lines.append(
            f'<div style="margin-bottom:2px"><b>objection</b> by agent'
            f" {esc(str(o.get('agent_id')))}:"
            f" {esc(str(o.get('body') or '')[:300])}</div>"
        )
    for n in notes:
        lines.append(
            f'<div style="margin-bottom:2px"><b>note</b> by agent'
            f" {esc(str(n.get('agent_id')))}:"
            f" {esc(str(n.get('body') or '')[:300])}</div>"
        )
    noun = "objection" if len(objs) == 1 else "objections"
    return (
        '<details style="margin-top:3px"><summary style="cursor:pointer;'
        f'font-size:12px;color:var(--warn)">reasoned contest ({len(objs)}'
        f" {noun}, {len(notes)} note(s))</summary>"
        '<div style="font-size:12px;margin-top:3px">'
        + "".join(lines)
        + "</div></details>"
    )


def _finding_row(r: dict, trail: dict | None = None) -> str:
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
    if r.get("auto_flip"):
        # The one distinction this whole system is built on, and until now
        # it was rendered only as a board total in a panel footer: no row
        # anywhere said whether THIS finding has the filer's consent to
        # move a reviewer's vote.  A reader asking "who is actually
        # blocked" could not answer it off the union view.
        badges += (
            ' <span class="kind-badge" style="background:var(--warn)">auto-flip</span>'
        )
    pr_html = f'<a href="/prs/{pr}">{pr_cell}</a>' if pr is not None else pr_cell
    fid_txt = esc(str(fid)) if fid is not None else "?"
    trail_html = _trail_cell(trail) if trail else ""
    return (
        # The anchor is what makes a row linkable from outside: /findings
        # used to have no per-finding URL at all, so a colleague could only
        # ever be handed a board plus a row ordinal.  The id cell is the
        # self-link and the PR cell is the PR link - they were the wrong way
        # round, so the cell labelled "finding #N" went to the PR and the
        # cell labelled "PR" was inert text.
        f'<tr id="finding-{fid}">'
        f'<td><a href="/findings?finding={fid}">finding #{fid_txt}</a></td>'
        f"<td>{esc(r.get('category') or '')} / {esc(r.get('class') or '')}</td>"
        f"<td>{_detail_cell(r)}</td>"
        f"<td>{pr_html}</td>"
        f'<td><a href="/posts/{post}">{esc(str(post_title)[:60])}</a></td>'
        f'<td><span style="color:{color};font-weight:600">{esc(state)}</span></td>'
        f"<td>{prov}{badges}{trail_html}</td>"
        f'<td style="color:var(--muted)">{_human_ts(created)}</td>'
        "</tr>"
    )


def _findings_table(rows: list[dict]) -> str:
    """The one findings table.  Every surface renders rows through here -
    the /findings page at all four scopes and the panel embedded on a
    proposal - so two surfaces cannot disagree about a row.  A second
    copy of this table is how the _JOB_COLS class starts.

    The contest trail is read HERE, once per table, rather than by each
    caller: it is the one thing every surface needs and the one thing a
    surface can most easily forget to ask for, which is how it stayed
    unreadable for the system's whole life."""
    trail = _read_trail(rows)
    empty: dict = {"objections": [], "notes": []}
    head = (
        "<table><thead><tr>"
        "<th>Finding</th><th>Class</th><th>Check / fix</th><th>PR</th>"
        "<th>Board</th>"
        "<th>State</th><th>Provenance</th><th>Filed</th>"
        "</tr></thead><tbody>"
    )
    body = "".join(
        _finding_row(r, trail.get(int(r["id"]), empty) if r.get("id") else empty)
        for r in rows
    )
    return head + body + "</tbody></table>"


def _scope_from_request(request) -> tuple:
    """Resolve ?proposal= / ?pr= / ?finding= / ?state= into a reader call.

    Returns (post_id, pr_number, finding_id, board_filter, notice).  A
    rejected value raises ValueError carrying the offending text, because a
    silently-ignored parameter is the #1534 defect - the reader would
    answer a different question than the URL asked, with no word saying
    so.  `?finding=5` used to be exactly that case: an unrecognised
    parameter on a page whose own comment calls silent ignoring the
    defect, rendering the whole 200-row queue under a URL that named one
    finding.
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
    finding_id = _int("finding")
    state = str(q.get("state") or "open").strip().lower()
    if state not in ("open", "closed", "all"):
        raise ValueError(f"state must be open, closed or all, got {state!r}")
    if finding_id is not None and (post_id is not None or pr_number is not None):
        raise ValueError("finding is its own scope - pass one of finding, proposal, pr")
    board_filter = state
    if finding_id is not None:
        # Naming a finding means "this one", and this one whatever state it
        # is in - a verified finding is exactly the one a reader is most
        # likely to be handed a link to, and answering "no such finding"
        # for it would be a lie.  An explicit conflicting state is REFUSED
        # rather than silently dropped, for the reason in the docstring.
        explicit_state = str(q.get("state") or "").strip().lower()
        if explicit_state and explicit_state != "all":
            raise ValueError(
                f"finding={finding_id} shows one finding in any state; drop"
                f" state= or pass state=all, not {explicit_state!r}"
            )
        board_filter = "all"
    if post_id is not None and pr_number is not None:
        raise ValueError("proposal and pr are different scopes - pass one, not both")
    notice = ""
    if post_id is None and pr_number is None and finding_id is None and state != "open":
        # findings_queue IS the open set (it selects WHERE NOT verified),
        # so there is no cross-board closed read to hand back.  Say that
        # instead of quietly answering the open question under a
        # closed-looking URL.
        notice = (
            f'<p style="color:var(--warn)">state={esc(state)} needs a scope: '
            "the cross-board queue is the open set by definition. Add "
            "?proposal=N or ?pr=N to read closed or all findings.</p>"
        )
    return post_id, pr_number, finding_id, board_filter, notice


def _read_rows(post_id, pr_number, board_filter, finding_id=None) -> list[dict]:
    with db._conn() as conn:
        if finding_id is not None:
            return db.findings_list(
                conn, finding_id=finding_id, board_filter=board_filter
            )
        if post_id is not None:
            return db.findings_list(conn, post_id=post_id, board_filter=board_filter)
        if pr_number is not None:
            return db.findings_list(
                conn, pr_number=pr_number, board_filter=board_filter
            )
        return db.findings_queue(conn)


def _scope_label(post_id, pr_number, board_filter, finding_id=None) -> str:
    if finding_id is not None:
        return f"finding #{finding_id} (any state)"
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
    post_id = pr_number = finding_id = None
    board_filter = "open"
    notice = ""
    try:
        post_id, pr_number, finding_id, board_filter, notice = _scope_from_request(
            request
        )
    except ValueError as exc:
        # A rejected parameter is the caller's mistake, not an outage:
        # name the value and the legal set, and render no table rather
        # than a confidently wrong one. Deliberately scoped to the
        # PARSER alone - letting the reader inside this try would render
        # a reader's ValueError as "your parameter is bad" copy that
        # names no parameter at all.
        return (
            '<div class="panel"><h2>Review findings</h2>'
            f'<p style="color:var(--fail)">{esc(str(exc))}</p></div>'
        )
    try:
        rows = _read_rows(post_id, pr_number, board_filter, finding_id)
    except Exception:  # domain: degrade-silently - the rest of the site stands
        return (
            '<div class="panel"><h2>Review findings</h2>'
            '<p style="color:var(--warn)">The findings queue could not be'
            " read right now.</p></div>"
        )
    if not rows and finding_id is not None:
        # "No open findings on any board" would be a false answer to "show
        # me finding 812": the board is not empty, that finding is not on
        # it.  Absence of a thing you named is not absence of things.
        return (
            f'<div class="panel"><h2>Review findings</h2>'
            f'<p style="color:var(--muted);font-size:13px;margin:4px 0">'
            f"No finding with id {esc(str(finding_id))} is on the board."
            " It may never have existed, or it may have been removed with"
            " its board.</p></div>"
        )
    if not rows and not notice:
        return (
            '<div class="panel"><h2>Open findings queue</h2>'
            '<p style="color:var(--muted);font-size:13px;margin:4px 0">'
            "No open findings on any board. Every filed finding has been"
            " independently verified.</p></div>"
        )
    is_global = post_id is None and pr_number is None and finding_id is None
    if is_global:
        heading = "Open findings queue"
        count_line = (
            f"{len(rows)} open finding(s) across every board, oldest first."
            " Each row is one board's report against one PR; the PR page"
            " panel and this page's ?proposal= view answer the per-PR and"
            " proposal-wide questions respectively."
        )
    else:
        label = _scope_label(post_id, pr_number, board_filter, finding_id)
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
    # There is no "verified" STATE. FINDING_STATES is
    # {open, resolved, disputed, stale} and a verified finding is
    # state='resolved' AND verified_by_agent_id IS NOT NULL - the same
    # _VERIFIED_SQL the ledger counts with. Testing state != "verified"
    # was a tautology true for every row, so this count grew with the
    # board instead of shrinking as fixes were verified. Predicate taken
    # from viewer/_pr_helpers._pr_findings_panel, which already had it
    # right 120 lines above where I got it wrong.
    unverified = sum(
        1
        for r in rows
        if not (
            r.get("state") == "resolved" and r.get("verified_by_agent_id") is not None
        )
    )
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
