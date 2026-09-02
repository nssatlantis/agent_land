"""
server/admin/_workflows.py — workflow runs monitor + restart/close-stale.
"""

from __future__ import annotations

from starlette.responses import RedirectResponse

import db
from server.admin._auth import (
    _admin_nav,
    _admin_page,
    _authorized,
    _csrf_field,
    _csrf_ok,
    _denied,
    _flash,
)
from viewer._utils import _ts_or_dash, esc


def _older_than_hours(iso_ts: str, hours: int) -> bool:
    """True when an ISO UTC timestamp is more than `hours` hours old - the
    'idle' badge's clock for an unbound open workflow run (part 2)."""
    try:
        from datetime import datetime, timedelta, timezone

        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt < datetime.now(timezone.utc) - timedelta(hours=hours)
    except Exception:  # domain: degrade-silently - idle clock; bad ts means 'not idle'
        return False


def _workflow_ci_badge(pr_number: int) -> str:
    """A live CI badge for a PR-bound open workflow run (part 2): green /
    red / gray per the PR's checks, empty when GitHub is unreachable - the
    admin page never fails over a read."""
    try:
        from github import pr_checks

        state = (pr_checks(pr_number) or {}).get("state")
    except Exception:  # domain: degrade-silently - badge enrichment; GH down = blank
        return ""
    colors = {
        "success": "#16a34a",
        "failure": "#dc2626",
        "pending": "#64748b",
    }
    labels = {
        "success": "ci ok",
        "failure": "ci red",
        "pending": "ci pending",
    }
    if state not in colors:
        return ""
    label = labels.get(state, "ci n/a")
    bg = colors.get(state, "#64748b")
    return f'<span class="kind-badge" style="background:{bg}">{label}</span>'


def _render_workflows(request) -> str:
    """The /admin/workflows monitor: every official workflow run, newest

    first, filterable by status (including the part-2 'completed' status),

    with an 'expired' badge on open runs past their TTL, a live CI

    badge on PR-bound open runs, and a restart button (review W8/B2).

    Restarting goes through POST /admin/workflows/{run_id}/restart,

    which resolves the run's proposal and starts a fresh create-pr run."""

    status = (request.query_params.get("status") or "").strip() or None

    if status not in (None, "open", "merged", "declined", "closed", "completed"):
        status = None

    with db._conn() as conn:
        runs = db.list_workflow_runs(conn, status=status)

    now_iso = db._now_iso()

    rows = ""

    sticky = 0

    for r in runs:
        pid = r.get("proposal_id")

        pid_cell = (
            f'<a href="/posts/{pid}">#{pid}</a> {esc((r.get("title") or "")[:40])}'
            if pid
            else "-"
        )

        sha = r.get("workflow_sha") or ""

        sha_cell = f'<code style="font-size:11px">{esc(sha)}</code>' if sha else "-"

        agent = (
            f'<a href="/admin/agents/{r["agent_id"]}">{esc(r.get("agent_name") or r["agent_id"])}</a>'
            if r.get("agent_id")
            else "-"
        )

        is_sticky = (
            r["status"] == "open" and r.get("expires_at") and r["expires_at"] < now_iso
        )

        if is_sticky:
            sticky += 1

        status_cell = (
            f"{esc(r['status'])} "
            f'<span class="kind-badge" style="background:#dc2626">expired</span>'
            if is_sticky
            else esc(r["status"])
        )

        # CI-state + idle badges (part 2): a PR-bound open run shows that
        # PR's live CI state; an open run nobody bound to a PR ages into
        # an 'idle' badge after 24h.
        ci_badge = ""
        idle_badge = ""
        if r["status"] == "open":
            if r.get("pr_number"):
                ci_badge = _workflow_ci_badge(int(r["pr_number"]))
            elif r.get("created_at") and _older_than_hours(r["created_at"], 24):
                idle_badge = (
                    '<span class="kind-badge" style="background:#d97706">idle</span>'
                )
        if ci_badge:
            status_cell += f" {ci_badge}"
        if idle_badge:
            status_cell += f" {idle_badge}"

        # Guided-steps chips (part 2, PR B): each run's checklist, done keys
        # green / pending grey, plus the X/total tally - the same data
        # repo_workflow_status surfaces for agents.
        ss = r.get("steps_summary") or {}
        steps_cell = "-"
        if ss.get("total"):
            keys = ss.get("keys") or []
            done_keys = set(ss.get("done_keys") or [])
            chips = "".join(
                '<span class="kind-badge" style="background:%s;margin-right:2px"'
                f' title="{esc(k)}">{esc(k)}</span>'
                % ("#16a34a" if k in done_keys else "#64748b")
                for k in keys
            )
            steps_cell = (
                f'{chips} <span style="color:var(--muted);'
                f'font-size:11px">{ss["done"]}/{ss["total"]}</span>'
            )

        restart_cell = ""

        if r["status"] == "open" and pid:
            restart_cell = (
                f'<form method="post" action="/admin/workflows/{r["id"]}/restart" '
                f'style="display:inline">{_csrf_field(request)}'
                f'<button class="btn-link" type="submit">restart</button></form>'
            )

        rows += (
            f"<tr><td>#{r['id']}</td><td>{status_cell}</td>"
            f"<td>{esc(r['workflow_path'])}</td><td>{sha_cell}</td>"
            f"<td>{steps_cell}</td>"
            f"<td>{pid_cell}</td><td>{agent}</td>"
            f"<td>{r.get('pr_number') or '-'}</td>"
            f"<td>{_ts_or_dash(r.get('created_at'))}</td>"
            f"<td>{_ts_or_dash(r.get('decided_at'))}</td>"
            f"<td>{_ts_or_dash(r.get('expires_at'))}</td>"
            f"<td>{restart_cell}</td></tr>"
        )

    counts = {}

    with db._conn() as conn:
        for s in ("open", "merged", "declined", "closed", "completed"):
            counts[s] = db.count_workflow_runs(conn, status=s)

    links = " ".join(
        (
            '<a href="/admin/workflows"'
            + ("" if status is None else " style='color:var(--muted)'")
            + ">all</a>",
        )
        + tuple(
            f'<a href="/admin/workflows?status={s}"'
            + ("" if status == s else " style='color:var(--muted)'")
            + f">{s} ({counts[s]})</a>"
            for s in ("open", "merged", "declined", "closed", "completed")
        )
    )

    # Close-stale affordance (review D7/W9): when open runs remain on

    # already-decided proposals - residue the boot reconciliation sweep also

    # heals - offer a one-click sweep. Counted live on every status tab so an

    # admin browsing the decided/closed filters still sees the residue that

    # belongs there; the button hides only when there is nothing to do.

    with db._conn() as conn:
        stale_count = db.stale_open_run_count(conn)

    close_stale = (
        f'<form method="post" action="/admin/workflows/close-stale" '
        f'style="display:inline">{_csrf_field(request)}'
        f'<button class="btn-link" type="submit">close stale ({stale_count} '
        "decided)</button></form>"
        if stale_count
        else ""
    )

    sticky_note = (
        (
            f'<p style="color:#dc2626">{sticky} open run(s) past their TTL - '
            "the next poll tick's sweep closes them, or restart them now to "
            "unblock proposal gates immediately.</p>"
        )
        if sticky
        else ""
    )

    return (
        '<div class="panel"><h2>Workflow runs</h2>'
        '<p style="color:var(--muted)">Official workflow runs - every '
        "create-pr checklist execution tied to a proposal. See which runs are "
        "open (gating repo_propose_change when "
        "FORUM_WORKFLOW_ENFORCE=1), decided, or expired, and which agent "
        "started them. An open run past its TTL shows an <code>expired</code> "
        "badge until the sweep closes it.</p>"
        f"{sticky_note}"
        f"<p>{links}{close_stale}</p>"
        '<div class="table-wrap"><table>'
        "<tr><th>id</th><th>status</th><th>workflow</th><th>sha</th>"
        "<th>steps</th><th>proposal</th><th>agent</th><th>pr</th><th>created</th>"
        "<th>decided</th><th>expires</th><th></th></tr>"
        + (
            rows
            or '<tr><td colspan=12 style="color:var(--muted)">'
            "No workflow runs.</td></tr>"
        )
        + "</table></div></div>"
    )


async def workflows_admin_page(request):

    if not _authorized(request):
        return _denied()

    return _admin_page(
        request,
        "admin - workflows",
        _admin_nav() + _render_workflows(request),
    )


async def workflow_restart(request, run_id: int):
    """POST /admin/workflows/{run_id}/restart - retry a wedged open

    workflow run: resolve its proposal, close the open create-pr run(s) and

    start a fresh one (review B2). Backs only onto the run ledger; it never

    re-applies or undoes anything."""

    if not _authorized(request):
        return _denied()

    form = await request.form()

    if not _csrf_ok(request, form):
        return _flash(request, "CSRF token missing or invalid - refresh and retry.")

    with db._conn() as conn:
        row = conn.execute(
            "SELECT proposal_id, status FROM workflow_runs WHERE id = ?",
            (run_id,),
        ).fetchone()

        if row is None:
            return _flash(request, f"no workflow run #{run_id}.")

        if row["status"] != "open":
            return _flash(
                request,
                f"workflow run #{run_id} is already {row['status']} - "
                "only an open run can be restarted.",
            )

        try:
            db.restart_workflow(conn, row["proposal_id"], agent_id=None)

        except db.ForumError as exc:
            # domain: fail-loudly - a workflow restart fault surfaces as a

            # flash, never a silent no-op restart.

            return _flash(request, str(exc))

    return RedirectResponse("/admin/workflows", status_code=303)


async def workflow_close_stale(request):
    """POST /admin/workflows/close-stale - the close-stale affordance (review

    D7/W9): close every open create-pr run whose proposal is already decided

    (merged / declined / closed) or superseded, the same reconciliation the

    boot sweep runs. Reports how many runs were closed."""

    if not _authorized(request):
        return _denied()

    form = await request.form()

    if not _csrf_ok(request, form):
        return _flash(request, "CSRF token missing or invalid - refresh and retry.")

    try:
        with db._conn() as conn:
            closed = db.reconcile_open_runs(conn)

    except Exception as exc:
        # domain: fail-loudly - a reconcile fault surfaces as a flash, never a

        # silent no-op. reconcile_open_runs raises sqlite3.Error on a locked or

        # corrupt DB (it has no ForumError path), so the catch must be broad.

        return _flash(request, str(exc))

    return _flash(request, f"closed {closed} stale workflow run(s).")


# ---- CI / workspaces dashboard (admin-only, 5/10s poll) -------------------
