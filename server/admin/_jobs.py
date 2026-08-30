"""
server/admin/_jobs.py — job-market governance (render + actions).

Single file per user preference (cap 1000-1250). Covers the dashboard panel,
the full /admin/jobs manager, job detail, and all POST actions (create
official, close, review, stake).
"""

from __future__ import annotations

from urllib.parse import quote as _urlquote

from starlette.responses import RedirectResponse

import db
from server.admin._auth import (
    _admin_nav,
    _admin_page,
    _admin_user,
    _authorized,
    _csrf_field,
    _csrf_ok,
    _denied,
    _flash,
    _safe_referer,
)
from viewer._utils import esc


async def create_stake(request):

    if not _authorized(request):
        return _denied()

    form = await request.form()

    if not _csrf_ok(request, form):
        return _flash(request, "CSRF token missing or invalid - refresh and retry.")

    try:
        per_pr = float(form.get("per_pr") or 0)

        max_prs = int(form.get("max_prs") or 0)

    except (ValueError, TypeError):
        return _flash(request, "per_pr must be a number and max_prs an integer.")

    currency = form.get("currency") or "credits"

    try:
        db.admin_stake(
            _admin_user(request),
            request.path_params["id"],
            per_pr,
            max_prs,
            currency=currency,
        )

    except db.ForumError as exc:
        return _flash(request, str(exc))

    return RedirectResponse(_safe_referer(request, "/admin"), status_code=303)


async def delete_stake(request):

    if not _authorized(request):
        return _denied()

    form = await request.form()

    if not _csrf_ok(request, form):
        return _flash(request, "CSRF token missing or invalid - refresh and retry.")

    try:
        stake_id = int(request.path_params["stake_id"])

    except (
        TypeError,
        ValueError,
    ):  # domain:fail-loudly - bad path param surfaces as flash
        return _flash(request, "bad stake id.")

    try:
        db.admin_delete_stake(_admin_user(request), stake_id)

    except (
        db.ForumError
    ) as exc:  # domain:fail-loudly - delete gate refusal surfaces as flash
        return _flash(request, str(exc))

    return RedirectResponse(_safe_referer(request, "/admin"), status_code=303)


def _render_jobs(request) -> str:
    """The job-market governance panel: create OFFICIAL positions

    (treasury-paid, longer cycles, karma floor waived for the sponsor)

    and close any unfinished job - the money path is the shared one, so

    a citizen job closed here refunds its unearned escrow exactly like a

    creator-initiated cancel."""

    open_jobs = db.list_jobs(view="open", limit=100)["jobs"]

    active_jobs = [
        j
        for j in db.list_jobs(view="all", limit=200)["jobs"]
        if j["status"] == "active"
    ]

    rows = ""

    for j in open_jobs + active_jobs:
        if j["status"] == "active":
            who = esc(j["worker"] or "-")

        elif j.get("offered_to"):
            who = "offer to " + esc(j["offered_to"])

        else:
            who = "<span style='color:var(--muted)'>open on the board</span>"

        close_form = (
            f"<form method='post' action='/admin/jobs/{j['job_id']}/close'"
            f" style='display:inline'>{_csrf_field(request)}"
            f"<label><input type='checkbox' name='confirm' required> confirm</label> "
            f"<button type='submit' style='color:#c53030'>close</button></form>"
        )

        review_form = ""

        if j["status"] == "active" and j["official"] and j["creator"] == "admin":
            review_form = (
                f" <form method='post' action='/admin/jobs/{j['job_id']}/review'"
                f" style='display:inline'>{_csrf_field(request)}"
                f"<select name='action' style='font-size:11px'>"
                f"<option value='accept'>accept</option>"
                f"<option value='decline'>decline</option></select> "
                f"<input name='feedback' placeholder='feedback' "
                f"style='width:100px;font-size:11px'> "
                f"<button type='submit' style='color:#2f855a'>review</button>"
                f"</form>"
            )

        rows += (
            f"<tr><td>#{j['job_id']}</td><td>{esc(j['title'])}"
            f"{' <b>OFFICIAL</b>' if j['official'] else ''}</td>"
            f"<td>{esc(j['status'])}</td><td>{esc(j['creator'])}</td>"
            f"<td>{who}</td><td>{esc(j['payment_credits'])} cr x "
            f"{j['cycles_done']}/{j['total_cycles']}</td>"
            f"<td>{close_form}{review_form}</td></tr>"
        )

    jobs_table = (
        '<div class="table-wrap"><table>'
        "<tr><th>id</th><th>title</th><th>status</th><th>creator</th>"
        "<th>worker</th><th>wage/cycles</th><th>close</th></tr>"
        + (
            rows
            or '<tr><td colspan=7 style="color:var(--muted)">'
            "No open or in-progress jobs.</td></tr>"
        )
        + "</table></div>"
    )

    create_form = (
        '<div class="panel"><h2>Create official position</h2>'
        '<p style="color:var(--muted)">Standing civic roles paid from '
        "the community treasury per accepted cycle - no escrow is taken. "
        "Optionally name a sponsor citizen who reviews work and earns "
        "creator-side karma; leave blank for a pure admin position. "
        "Use offer_to to hold the position for one specific citizen "
        "(they must still accept). Steps go one per line.</p>"
        '<form method="post" action="/admin/jobs/create-official">'
        + _csrf_field(request)
        + '<input name="title" placeholder="title (e.g. Chronicler)" required '
        'style="width:300px;margin-right:6px">'
        '<input name="creator" placeholder="sponsor citizen (optional)" '
        'style="width:170px;margin-right:6px"><br>'
        '<textarea name="description" placeholder="description" rows="2" '
        'style="width:640px;margin-top:8px"></textarea><br>'
        '<textarea name="steps" placeholder="checklist steps - one per line"'
        ' rows="4" required style="width:640px;margin-top:8px"></textarea><br>'
        '<input name="payment_credits" placeholder="credits/cycle (e.g. 2)"'
        ' required style="width:180px;margin-right:6px;margin-top:8px">'
        '<select name="kind" style="margin-right:6px">'
        '<option value="recurring">recurring</option>'
        '<option value="one_time">one_time</option></select> '
        '<input name="cycles" placeholder="cycles" value="7" '
        'style="width:80px;margin-right:6px">'
        '<input name="scope" placeholder="scope hint (e.g. HISTORY.md)" '
        'style="width:220px;margin-right:6px">'
        '<input name="offer_to" placeholder="offer to (optional)" '
        'style="width:190px;margin-right:6px">'
        '<button type="submit" style="margin-top:8px">create position</button>'
        "</form></div>"
    )

    return (
        '<div class="panel"><h2>Jobs</h2>'
        '<p style="color:var(--muted)">Open and in-progress jobs on the '
        "/jobs board. Closing one returns any unearned escrow to its "
        "creator and notifies both parties - officials hold no escrow, "
        "so closing them moves nothing. "
        '<a href="/admin/jobs">Open full jobs manager &rarr;</a></p>'
        + jobs_table
        + "</div>"
        + create_form
    )


def _render_jobs_manager(request) -> str:
    """Dedicated /admin/jobs manager: beautiful overview + moderation.

    Admins create only OFFICIAL positions, but can moderate any job (close)

    and review/process any OFFICIAL position ΓÇö sponsorless via admin_review_job,

    sponsored via admin_review_job_as with on_behalf_of audit. Citizen jobs

    are not reviewable here (use their creator token)."""

    # Filter tabs

    status_filter = (request.query_params.get("status") or "all").lower()

    q = (request.query_params.get("q") or "").strip().lower()

    all_jobs = db.list_jobs(view="all", limit=300)["jobs"]

    # Counts for header

    counts = {
        "open": sum(1 for j in all_jobs if j["status"] == "open"),
        "offered": sum(1 for j in all_jobs if j["status"] == "offered"),
        "active": sum(1 for j in all_jobs if j["status"] == "active"),
        "completed": sum(1 for j in all_jobs if j["status"] == "completed"),
        "cancelled": sum(1 for j in all_jobs if j["status"] == "cancelled"),
        "expired": sum(1 for j in all_jobs if j["status"] == "expired"),
    }

    # Filter

    filtered = all_jobs

    if status_filter != "all":
        if status_filter == "closed":
            filtered = [j for j in filtered if j["status"] in ("cancelled", "expired")]

        else:
            filtered = [j for j in filtered if j["status"] == status_filter]

    if q:
        filtered = [
            j
            for j in filtered
            if q in j["title"].lower()
            or q in (j["scope"] or "").lower()
            or q in j["creator"].lower()
        ]

    # Tabs

    tabs = ""

    for key, label in [
        ("all", "All"),
        ("open", "Open"),
        ("offered", "Offered"),
        ("active", "Active"),
        ("completed", "Completed"),
        ("closed", "Closed"),
    ]:
        active = ' class="active" aria-current="page"' if key == status_filter else ""

        href = f"/admin/jobs?status={key}" + (f"&q={_urlquote(q)}" if q else "")

        cnt = (
            sum(counts.values())
            if key == "all"
            else (
                counts.get(key, 0)
                if key != "closed"
                else counts["cancelled"] + counts["expired"]
            )
        )

        tabs += f'<a href="{href}"{active}>{label} <span style="color:var(--muted)">({cnt})</span></a> '

    # Stats bar

    stats = (
        f'<div style="display:flex;gap:12px;flex-wrap:wrap;margin:8px 0 12px;font-size:13px">'
        f'<span class="badge" style="background:#2563eb;color:white;padding:2px 8px;border-radius:999px">Active {counts["active"]}</span>'
        f'<span style="color:var(--muted)">Open {counts["open"]} ┬╖ Offered {counts["offered"]} ┬╖ Completed {counts["completed"]} ┬╖ Closed {counts["cancelled"] + counts["expired"]}</span>'
        f"</div>"
    )

    # Search

    search = (
        f'<form method="get" action="/admin/jobs" style="margin:8px 0">'
        f'<input type="hidden" name="status" value="{esc(status_filter)}">'
        f'<input name="q" value="{esc(q)}" placeholder="filter title / scope / creator" style="width:260px">'
        f' <button type="submit">filter</button> <a href="/admin/jobs" style="margin-left:8px">clear</a>'
        f"</form>"
    )

    # Cards ΓÇö beautiful overview

    cards = ""

    for j in filtered[:100]:
        detail = db.get_job(j["job_id"])

        # Status color

        col = {
            "open": "#2563eb",
            "offered": "#b45309",
            "active": "#0ea5e9",
            "completed": "#15803d",
            "cancelled": "var(--muted)",
            "expired": "var(--muted)",
        }.get(detail["status"], "var(--muted)")

        # Steps

        steps_html = "".join(
            f"<li style='margin:2px 0;{'color:var(--muted);text-decoration:line-through' if s['done'] else ''}'>{esc(s['text'])}</li>"
            for s in detail["steps"]
        )

        # Cycles

        cycles_html = ""

        for c in detail["cycles"]:
            if c["status"] == "awaiting":
                continue

            bits = [f"cycle {c['cycle_no']}: <b>{esc(c['status'])}</b>"]

            if c["evidence"]:
                bits.append(f"evidence {esc(c['evidence'])}")

            pr_nums = c.get("evidence_pr_numbers") or []

            if pr_nums:
                chips = " ".join(
                    f'<a href="/prs/{int(n)}" style="background:var(--accent-tint);border:1px solid var(--accent-border);padding:1px 6px;border-radius:999px;font-size:12px;text-decoration:none">#PR{int(n)}</a>'
                    for n in pr_nums
                    if str(n).isdigit()
                )

                if chips:
                    bits.append(f"PRs {chips}")

            if c["feedback"]:
                bits.append(f"feedback: {esc(c['feedback'])}")

            cycles_html += f"<div style='font-size:13px;color:var(--muted);margin-top:3px'>{' &middot; '.join(bits)}</div>"

        # Review form for OFFICIAL active + submitted

        review_html = ""

        if detail["status"] == "active" and detail["official"]:
            # Find submitted cycle

            sub = next(
                (c for c in detail["cycles"] if c["status"] == "submitted"), None
            )

            if sub:
                is_sponsored = detail["creator"] is not None

                sponsor = (
                    esc(detail["creator"]["name"]) if detail["creator"] else "admin"
                )

                audit_note = (
                    f"on behalf of sponsor <b>{sponsor}</b> (creator +1 karma)"
                    if is_sponsored
                    else "as pure admin (no sponsor karma)"
                )

                review_html = (
                    f'<div style="margin-top:8px;padding:8px;background:var(--accent-tint);border:1px solid var(--accent-border);border-radius:8px">'
                    f'<div style="font-size:13px;margin-bottom:6px">Review cycle {sub["cycle_no"]} ΓÇö {audit_note} ┬╖ evidence: {esc(sub["evidence"] or "-")}</div>'
                    f'<form method="post" action="/admin/jobs/{j["job_id"]}/review" style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">'
                    f"{_csrf_field(request)}"
                    f'<select name="action" style="font-size:13px"><option value="accept">accept ΓÇö pay + karma</option><option value="decline">decline ΓÇö feedback required</option></select>'
                    f'<input name="feedback" placeholder="feedback if decline" style="width:220px;font-size:13px">'
                    f'<label style="font-size:12px"><input type="checkbox" name="punish" value="1"> punish -2 karma</label> '
                    f'<button type="submit" style="background:var(--ok);color:white">review</button>'
                    f"</form></div>"
                )

        # Close form for any open/offered/active (moderation)

        close_html = ""

        if detail["status"] in ("open", "offered", "active"):
            close_html = (
                f'<form method="post" action="/admin/jobs/{j["job_id"]}/close" style="display:inline;margin-left:8px">'
                f"{_csrf_field(request)}"
                f'<label style="font-size:12px"><input type="checkbox" name="confirm" required> confirm close</label> '
                f'<button type="submit" style="color:#c53030;font-size:12px">close (refund escrow if any)</button></form>'
            )

        official_badge = (
            '<span style="background:#7c3aed;color:white;padding:1px 6px;border-radius:999px;font-size:11px">OFFICIAL</span>'
            if detail["official"]
            else ""
        )

        status_badge = f'<span style="background:{col};color:white;padding:1px 6px;border-radius:999px;font-size:11px">{esc(detail["status"])}</span>'

        cards += (
            f'<div class="panel" style="padding:12px 16px;margin-bottom:10px;border-left:4px solid {col}">'
            f'<div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap">'
            f'<div style="font-weight:600">{esc(detail["title"])} <span style="color:var(--muted);font-weight:400">#{detail["job_id"]}</span> '
            f"{official_badge} "
            f"{status_badge}</div>"
            f'<div style="font-size:13px;color:var(--muted)">{esc(detail["payment_credits"])} cr ├ù {detail["cycles_done"]}/{detail["total_cycles"]} ┬╖ scope: {esc(detail["scope"] or "-")}</div>'
            f"</div>"
            f'<div style="font-size:13px;color:var(--muted);margin:4px 0">by {esc(detail["creator"]["name"]) if detail["creator"] else "admin"} &middot; '
            f"{'worked by ' + esc(detail['worker']['name']) if detail['worker'] else ('offer to ' + esc(detail['offered_to']['name']) if detail['offered_to'] else 'open on board')}</div>"
            f'<div style="font-size:14px;margin-top:4px">{esc(detail["description"] or "")}</div>'
            f'<ol style="margin:6px 0 0 18px;padding:0">{steps_html}</ol>'
            f"{cycles_html}"
            f"{review_html}"
            f'<div style="margin-top:8px">{close_html}</div>'
            f"</div>"
        )

    if not cards:
        cards = '<p style="color:var(--muted)">No jobs match filter.</p>'

    create_form = (
        '<div class="panel" style="border:2px dashed var(--border)"><h2>Create official position</h2>'
        '<p style="color:var(--muted)">Standing civic roles ΓÇö treasury-paid per accepted cycle. Sponsor optional (earns creator karma); blank = pure admin. Offer_to holds for one citizen.</p>'
        '<form method="post" action="/admin/jobs/create-official">'
        + _csrf_field(request)
        + '<input name="title" placeholder="title (e.g. Chronicler)" required style="width:300px;margin-right:6px">'
        '<input name="creator" placeholder="sponsor citizen (optional)" style="width:170px;margin-right:6px"><br>'
        '<textarea name="description" placeholder="description" rows="2" style="width:640px;margin-top:8px"></textarea><br>'
        '<textarea name="steps" placeholder="checklist steps ΓÇö one per line" rows="4" required style="width:640px;margin-top:8px"></textarea><br>'
        '<input name="payment_credits" placeholder="credits/cycle (e.g. 2)" required style="width:180px;margin-right:6px;margin-top:8px">'
        '<select name="kind" style="margin-right:6px"><option value="recurring">recurring</option><option value="one_time">one_time</option></select> '
        '<input name="cycles" placeholder="cycles" value="7" style="width:80px;margin-right:6px">'
        '<input name="scope" placeholder="scope hint (e.g. HISTORY.md)" style="width:220px;margin-right:6px">'
        '<input name="offer_to" placeholder="offer to (optional)" style="width:190px;margin-right:6px">'
        '<button type="submit" style="margin-top:8px">create position</button></form></div>'
    )

    return (
        '<div class="panel"><h2>Jobs manager</h2>'
        '<p style="color:var(--muted)">Moderate any job (close ΓåÆ refund) and review/process <b>official</b> positions ΓÇö sponsorless as admin, sponsored on behalf of sponsor (audit +1 karma to sponsor). Citizen jobs are not reviewable here.</p>'
        + stats
        + tabs
        + search
        + cards
        + "</div>"
        + create_form
    )


async def jobs_detail_page(request):

    if not _authorized(request):
        return _denied()

    job_id = int(request.path_params["id"])

    try:
        detail = db.get_job(job_id)

    except db.ForumError as exc:
        # domain: fail-loudly - get_job failure surfaces as flash, never silent

        return _flash(request, str(exc))

    # Reuse manager card styling but full page

    col = {
        "open": "#2563eb",
        "offered": "#b45309",
        "active": "#0ea5e9",
        "completed": "#15803d",
        "cancelled": "var(--muted)",
        "expired": "var(--muted)",
    }.get(detail["status"], "var(--muted)")

    steps_html = "".join(
        f"<li style='margin:2px 0;{'color:var(--muted);text-decoration:line-through' if s['done'] else ''}'>{esc(s['text'])}</li>"
        for s in detail["steps"]
    )

    cycles_html = ""

    for c in detail["cycles"]:
        bits = [f"cycle {c['cycle_no']}: <b>{esc(c['status'])}</b>"]

        if c["evidence"]:
            bits.append(f"evidence {esc(c['evidence'])}")

        pr_nums = c.get("evidence_pr_numbers") or []

        if pr_nums:
            chips = " ".join(
                f'<a href="/prs/{int(n)}">#PR{int(n)}</a>'
                for n in pr_nums
                if str(n).isdigit()
            )

            if chips:
                bits.append(f"PRs {chips}")

        if c["feedback"]:
            bits.append(f"feedback: {esc(c['feedback'])}")

        cycles_html += f"<div style='font-size:13px;color:var(--muted);margin-top:3px'>{' &middot; '.join(bits)}</div>"

    # Review form if official + submitted

    review_html = ""

    if detail["status"] == "active" and detail["official"]:
        sub = next((c for c in detail["cycles"] if c["status"] == "submitted"), None)

        if sub:
            is_sponsored = detail["creator"] is not None

            sponsor = esc(detail["creator"]["name"]) if detail["creator"] else "admin"

            audit_note = (
                f"on behalf of sponsor <b>{sponsor}</b>"
                if is_sponsored
                else "as pure admin"
            )

            review_html = (
                f'<div class="panel" style="background:var(--accent-tint);border:1px solid var(--accent-border)"><h3>Review cycle {sub["cycle_no"]}</h3>'
                f'<p style="font-size:13px">{audit_note} ┬╖ evidence: {esc(sub["evidence"] or "-")}</p>'
                f'<form method="post" action="/admin/jobs/{job_id}/review" style="display:flex;gap:6px">'
                f"{_csrf_field(request)}"
                f'<select name="action"><option value="accept">accept</option><option value="decline">decline</option></select>'
                f'<input name="feedback" placeholder="feedback if decline" style="width:260px">'
                f'<label style="font-size:12px"><input type="checkbox" name="punish" value="1"> punish -2 karma</label> '
                f'<button type="submit" style="background:var(--ok);color:white">review</button></form></div>'
            )

    close_html = ""

    if detail["status"] in ("open", "offered", "active"):
        close_html = (
            f'<div class="panel"><h3>Moderate</h3><form method="post" action="/admin/jobs/{job_id}/close">'
            f"{_csrf_field(request)}"
            f'<label><input type="checkbox" name="confirm" required> confirm close (refund escrow if any)</label> '
            f'<button type="submit" style="color:#c53030">close job</button></form></div>'
        )

    body = (
        _admin_nav()
        + f'<div class="panel" style="border-left:4px solid {col}"><h2>{esc(detail["title"])} <span style="color:var(--muted)">#{detail["job_id"]}</span> '
        + (
            '<span style="background:#7c3aed;color:white;padding:1px 6px;border-radius:999px;font-size:11px">OFFICIAL</span> '
            if detail["official"]
            else ""
        )
        + f'<span style="background:{col};color:white;padding:1px 6px;border-radius:999px;font-size:11px">{esc(detail["status"])}</span></h2>'
        + f'<p style="color:var(--muted)">{esc(detail["payment_credits"])} cr ├ù {detail["cycles_done"]}/{detail["total_cycles"]} ┬╖ scope: {esc(detail["scope"] or "-")} ┬╖ kind: {esc(detail["kind"])}</p>'
        + f"<p>by {esc(detail['creator']['name']) if detail['creator'] else 'admin'} &middot; "
        + (f"worked by {esc(detail['worker']['name'])}" if detail["worker"] else "open")
        + "</p>"
        + f"<p>{esc(detail['description'] or '')}</p>"
        + f'<ol style="margin:6px 0 0 18px">{steps_html}</ol>'
        + cycles_html
        + "</div>"
        + review_html
        + close_html
    )

    return _admin_page(request, f"admin - job #{job_id}", body)


async def create_official_job(request):

    if not _authorized(request):
        return _denied()

    form = await request.form()

    if not _csrf_ok(request, form):
        return _flash(request, "CSRF token missing or invalid - refresh and retry.")

    steps = [s.strip() for s in str(form.get("steps") or "").splitlines() if s.strip()]

    try:
        result = db.create_job_official(
            _admin_user(request),
            str(form.get("creator") or "") or None,
            str(form.get("title") or ""),
            str(form.get("description") or ""),
            float(form.get("payment_credits") or 0),
            steps,
            kind=str(form.get("kind") or "recurring"),
            cycles=int(form.get("cycles") or 1),
            scope=str(form.get("scope") or ""),
            offer_to=str(form.get("offer_to") or "") or None,
        )

    except (ValueError, TypeError) as exc:
        # domain: fail-loudly - bad form input surfaces as a flash, never

        # a silent default.

        return _flash(request, f"bad form input: {exc}")

    except db.ForumError as exc:
        # domain: fail-loudly - the gate's refusal is the feature; surface it verbatim

        return _flash(request, str(exc))

    return _flash(
        request,
        f"OFFICIAL position #{result['job_id']} '{result['title']}' "
        f"created ({result['payment_credits']} credits/cycle x "
        f"{result['total_cycles']}, sponsor "
        f"{result['creator']['name'] if result['creator'] else 'admin'}) "
        "- it is on the /jobs board.",
    )


async def admin_close_job(request):

    if not _authorized(request):
        return _denied()

    form = await request.form()

    if not _csrf_ok(request, form):
        return _flash(request, "CSRF token missing or invalid - refresh and retry.")

    job_id = int(request.path_params["id"])

    try:
        result = db.admin_cancel_job(_admin_user(request), job_id)

    except db.ForumError as exc:
        # domain: fail-loudly - the gate's refusal is the feature; surface it verbatim

        return _flash(request, str(exc))

    return _flash(
        request,
        f"Job #{job_id} '{result['title']}' closed"
        + (
            " (no escrow moved - official position)."
            if result["official"]
            else " - unearned escrow returned to its creator."
        ),
    )


async def admin_review_job(request):

    if not _authorized(request):
        return _denied()

    form = await request.form()

    if not _csrf_ok(request, form):
        return _flash(request, "CSRF token missing or invalid - refresh and retry.")

    job_id = int(request.path_params["id"])

    action = str(form.get("action") or "")

    feedback = str(form.get("feedback") or "")

    punish = bool(form.get("punish"))

    try:
        result = db.admin_review_job(
            _admin_user(request),
            job_id,
            action,
            feedback,
            punish=punish,
        )

    except db.ForumError as exc:
        # Sponsored officials fall through to on_behalf_of path ΓÇö same audit, creator karma preserved

        if "sponsorless" in str(exc) or "sponsorless official" in str(exc):
            try:
                result = db.admin_review_job_as(
                    _admin_user(request),
                    job_id,
                    action,
                    feedback,
                    punish=punish,
                )

            except db.ForumError as exc2:
                # domain: fail-loudly - gate refusal is the feature

                return _flash(request, str(exc2))

            verb = "accepted" if action == "accept" else "declined"

            sponsor = result["creator"]["name"] if result.get("creator") else "admin"

            return _flash(
                request,
                f"Job #{job_id} '{result['title']}': cycle {result['cycles_done']}"
                f" {verb} on behalf of {sponsor}.",
            )

        # domain: fail-loudly - the gate's refusal is the feature; surface it verbatim

        return _flash(request, str(exc))

    verb = "accepted" if action == "accept" else "declined"

    return _flash(
        request,
        f"Job #{job_id} '{result['title']}': cycle {result['cycles_done']} {verb}.",
    )


async def jobs_manager_page(request):

    if not _authorized(request):
        return _denied()

    return _admin_page(
        request, "admin - jobs", _admin_nav() + _render_jobs_manager(request)
    )
