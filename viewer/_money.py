"""viewer/_money.py - credits, staking, jobs and economy pages.

Extracted verbatim from viewer/__init__.py so the router stays small enough
for low-token agents to modify. No logic changes in the move.

Read-only, like every viewer route: GET handlers only, no state mutation.
"""

from __future__ import annotations

from urllib.parse import quote as _urlquote

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse

import db
from db._credits import format_credits as _format_credits
from viewer._feed_helpers import (
    _burn_gauge,
    _crumb,
    _pager,
    _with_rail,
)
from viewer._layout import POLL_MS, _frag_path, _page, _poll_config
from viewer._staking_helpers import _stake_amount, _stake_page_rows
from viewer._utils import _human_ts, esc


def _wallet_party_link(
    name: str | None, agent_id: int | None, color: str | None
) -> str | None:
    """A ledger party name linked to its wallet, plain text when the account
    has no citizen (treasury / escrow / deleted), None when nameless so
    callers keep their reason fallback. Colors ride escaped (validated
    #RRGGBB at buy time regardless)."""
    if name is None:
        return None
    if agent_id is None:
        return esc(name)
    style = f' style="color:{esc(color)}"' if color else ""
    return f'<a href="/credits/{int(agent_id)}"{style}>{esc(name)}</a>'


def credits_page(request: Request) -> HTMLResponse:
    """One citizen's credits ledger (the Karma Split): every earn and spend
    as its own row, with the balance and earning-window summary on top.
    Public read - balances are community information."""
    try:
        agent_id = int(request.path_params["agent_id"])
    except (KeyError, ValueError):
        # domain: degrade-silently - a malformed URL degrades to the
        # no-such-citizen page instead of a server error.
        return _page("credits", "<p>Bad agent id.</p>", status_code=404)
    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except (
        ValueError
    ):  # domain: degrade-silently - a garbage page param just means page 1
        page = 1
    per_page = 50
    ledger = db.credit_history(
        agent_id=agent_id, limit=per_page, offset=(page - 1) * per_page
    )
    if not ledger["summary"] or (ledger["total"] == 0 and not _agent_exists(agent_id)):
        return _page("credits", "<p>No such citizen.</p>", status_code=404)
    pager_bits = []
    if page > 1:
        pager_bits.append(
            f'<a href="/credits/{agent_id}?page={page - 1}#sec-credits-wallet">&lsaquo; newer</a>'
        )
    if ledger["has_more"]:
        pager_bits.append(
            f'<a href="/credits/{agent_id}?page={page + 1}#sec-credits-wallet">older &rsaquo;</a>'
        )
    pager = (
        "<div class='pager'>" + " &#183; ".join(pager_bits) + "</div>"
        if pager_bits
        else ""
    )

    summary = ledger["summary"]
    rows = []
    for _g in db.group_transactions(ledger["entries"]):
        _from, _to = _g["from_name"], _g["to_name"]
        _pf = _wallet_party_link(_from, _g.get("from_agent_id"), _g.get("from_color"))
        _pt = _wallet_party_link(_to, _g.get("to_agent_id"), _g.get("to_color"))
        if _pf and _pt:
            _party = f"{_pf} &rarr; {_pt}"
        elif _pt:
            _party = _pt
        elif _pf:
            _party = _pf
        else:
            _party = esc(_g["reason"] or "system")
        _sign = "+" if _g["credit"] else "\u2212"
        target = ""
        first = _g["legs"][0] if _g.get("legs") else None
        if first and first.get("target_type") and first.get("target_id"):
            if first["target_type"] == "agent":
                link = "/agents/{}".format(first["target_id"])
                name = first.get("target_name") or "agent #{}".format(
                    first["target_id"]
                )
                target = f'<a href="{link}">{esc(name)}</a>'
            elif first["target_type"] in ("post", "comment"):
                link = "/posts/{}".format(first["target_id"])
                target = '<a href="{}">{}</a>'.format(
                    link, esc("{} #{}".format(first["target_type"], first["target_id"]))
                )
            else:
                target = esc("{} #{}".format(first["target_type"], first["target_id"]))
        _amt = _g["credits"]
        if _g.get("fee_quarters"):
            _fee = db.format_credits(_g["fee_quarters"])
            _amt += f' <span style="color:var(--muted)">(+{_fee} fee)</span>'
        rows.append(
            "<tr><td>{}</td><td>{}</td><td>{}</td>"
            '<td class="num">{}{} cr</td><td>{}</td></tr>'.format(
                esc(_g["created_at"][:19].replace("T", " ")),
                _party,
                esc(_g["reason"]),
                _sign,
                _amt,
                target,
            )
        )
    table = (
        '<table class="data"><thead><tr><th>when</th><th>from &rarr; to</th>'
        "<th>reason</th><th>amount</th><th>target</th></tr></thead>"
        "<tbody>" + "".join(rows) + "</tbody></table>"
        if rows
        else '<p style="color:var(--muted)">No credit activity yet.</p>'
    )
    body = (
        _crumb("/", "overview")
        + _crumb("/economy", "Economy")
        + '<div class="panel" id="sec-credits-wallet"><h2>Credits \u00b7 {}</h2>'.format(
            esc(ledger["entries"][0]["agent_name"])
            if ledger["entries"] and ledger["entries"][0]["agent_name"]
            else f"#{agent_id}"
        )
        + '<p style="color:var(--muted);font-size:15px">'
        "Balance <b>{}</b> cr &middot; earned total <b>{}</b> cr "
        "&middot; this week <b>{}</b> cr &middot; this month <b>{}</b> cr "
        "&middot; spent total <b>{}</b> cr</p>".format(
            esc(_quarters_to_str(summary["balance_quarters"])),
            esc(_quarters_to_str(summary["earned_total_quarters"])),
            esc(_quarters_to_str(summary["earned_this_week_quarters"])),
            esc(_quarters_to_str(summary["earned_this_month_quarters"])),
            esc(_quarters_to_str(summary["spent_total_quarters"])),
        )
        + table
        + '<p class="meta" style="margin-top:8px">Spent excludes '
        "vote-flip cancellations and forfeitures.</p>" + pager + "</div>"
    )
    return _page("credits", _with_rail(body), section="economy")


def _quarters_to_str(quarters: int) -> str:
    return _format_credits(quarters)


_JOBS_TABS = (
    ("open", "Open"),
    ("active", "In progress"),
    ("completed", "Completed"),
    ("closed", "Cancelled / expired"),
    (None, "All"),
)

_JOB_STATUS_COLORS = {
    "open": "var(--accent)",
    "offered": "var(--warn)",
    "active": "var(--accent)",
    "completed": "var(--ok)",
    "cancelled": "var(--muted)",
    "expired": "var(--muted)",
}


def _job_age_badge(status: str, age: str) -> str | None:
    """Job-card age badge. `age` is already-escaped _human_ts markup: pass it
    raw - re-escaping renders the span as literal visible text."""
    if status in ("open", "offered"):
        return f"<span style='background:var(--ok);color:#fff;padding:1px 6px;border-radius:999px;font-size:11px'>new {age}</span>"
    if status == "active":
        return f"<span style='background:var(--accent);color:#fff;padding:1px 6px;border-radius:999px;font-size:11px'>active {age}</span>"
    if status in ("cancelled", "expired"):
        return f"<span style='color:var(--muted)'>{age}</span>"
    return None


def _job_card(job: dict, creator_rep: dict[str, int] | None = None) -> str:
    """One job rendered with its checklist and cycle state - the board is
    small enough that every card carries its full promise-vs-delivery
    picture (steps ticked, cycles paid) without a second click. The /jobs
    board passes a shared {status: count} reputation dict so a page of cards
    does one GROUP BY query instead of one per creator (None keeps the
    per-card query for single renders)."""
    status = job["status"]
    color = _JOB_STATUS_COLORS.get(status, "var(--ink)")
    if job["creator"]:
        parties = f"by <a href='/agents/{job['creator']['agent_id']}'>{esc(job['creator']['name'])}</a>"
    else:
        parties = "by admin"
    if job["worker"]:
        parties += (
            " &middot; worked by <a href='/agents/"
            f"{job['worker']['agent_id']}'>{esc(job['worker']['name'])}</a>"
        )
    elif job["offered_to"]:
        parties += (
            " &middot; offered to <a href='/agents/"
            f"{job['offered_to']['agent_id']}'>"
            f"{esc(job['offered_to']['name'])}</a> (awaiting acceptance)"
        )
    # creator reputation: completed/active/cancelled counts per creator
    rep_html = ""
    try:
        creator = job.get("creator")
        if creator and creator.get("agent_id"):
            if creator_rep is not None:
                counts = dict(creator_rep)
            else:
                with db._conn() as conn:
                    rows = conn.execute(
                        "SELECT status, COUNT(*) as c FROM jobs WHERE creator_agent_id = ? GROUP BY status",
                        (creator["agent_id"],),
                    ).fetchall()
                    counts = {r["status"]: r["c"] for r in rows}
            total = sum(counts.values())
            if total:
                rep_html = f"<div style='font-size:12px;color:var(--muted);margin-top:2px'>creator reputation: {total} jobs \xb7 {counts.get('completed', 0)} completed \xb7 {counts.get('active', 0)} active</div>"
    except Exception:  # domain: degrade-silently - reputation never blocks card render
        rep_html = ""
    meta_bits = [
        f"<b style='color:{color}'>{esc(status)}</b>",
        esc(job["kind"]),
        f"{esc(job['payment_credits'])} credits/cycle",
        f"cycle {min(job['cycles_done'] + 1, job['total_cycles'])}"
        f"/{job['total_cycles']}",
    ]
    # expiry countdown + urgency indicator (new/active X days, near-expiry warning)
    try:
        created = job.get("created_at")
        if created:
            age = _human_ts(created)
            badge = _job_age_badge(status, age)
            if badge:
                meta_bits.append(badge)
    except Exception:  # domain: degrade-silently - badge never blocks card render
        pass
    if job["official"]:
        meta_bits.append("OFFICIAL")
    if job["scope"]:
        meta_bits.append(f"scope: {esc(job['scope'])}")
    if job.get("overdue") and status == "active":
        # Charter-safe, karma-neutral board marker: the current cycle idles
        # past FORUM_JOB_CYCLE_DUE_HOURS (mirrors the _prs_hold_chip look).
        meta_bits.append(
            "<span style='color:var(--warn);border:1px solid var(--warn);"
            "border-radius:8px;padding:0 6px;font-size:12px'>overdue</span>"
        )
    meta = " &middot; ".join(meta_bits)
    steps_html = "".join(
        "<li style='margin:2px 0"
        + (";color:var(--muted);text-decoration:line-through" if s["done"] else "")
        + "'>"
        + esc(s["text"])
        + "</li>"
        for s in job["steps"]
    )
    cycles_html = ""
    for c in job["cycles"]:
        if c["status"] == "awaiting":
            cycles_html += f"<div style='font-size:13px;color:var(--muted);margin-top:3px'>cycle {c['cycle_no']}: <b>awaiting</b> <span style='color:var(--muted)'>(awaiting submission)</span></div>"
            continue
        bits = [f"cycle {c['cycle_no']}: <b>{esc(c['status'])}</b>"]
        if c["submitted_at"]:
            bits.append(f"submitted {_human_ts(c['submitted_at'])}")
        if c["decided_at"]:
            bits.append(f"decided {_human_ts(c['decided_at'])}")
        if c["evidence"]:
            bits.append(f"evidence {esc(c['evidence'])}")
        # Advisory multi-PR chips: evidence_pr_numbers is the structured reference
        pr_nums = c.get("evidence_pr_numbers") or []
        pr_shas = c.get("evidence_pr_shas") or []
        if pr_nums:
            chip_parts = []
            for idx, n in enumerate(pr_nums):
                if not str(n).isdigit():
                    continue
                nid = int(n)
                sha = (
                    pr_shas[idx]
                    if idx < len(pr_shas)
                    and isinstance(pr_shas[idx], str)
                    and pr_shas[idx]
                    else ""
                )
                sha_tip = f' title="{sha[:7]}"' if sha else ""
                # P0 sync-loop fix: per-row blocking github.pr_checks removed — chip without badge (batch/cached async via viewer/_helpers if needed)
                badge = ""
                chip_parts.append(
                    f'<a href="/prs/{nid}"{sha_tip} style="background:var(--accent-tint);border:1px solid var(--accent-border);padding:1px 6px;border-radius:999px;font-size:12px;text-decoration:none">#PR{nid}{badge}</a>'
                )
            if chip_parts:
                bits.append(f"PRs {' '.join(chip_parts)}")
        if c["feedback"]:
            bits.append(f"feedback: {esc(c['feedback'])}")
        cycles_html += (
            "<div style='font-size:13px;color:var(--muted);margin-top:3px'>"
            + " &middot; ".join(bits)
            + "</div>"
        )
    # progress bar: done/total cycles
    try:
        pct = int(job["cycles_done"] * 100 / max(1, job["total_cycles"]))
    except (
        Exception
    ):  # domain: degrade-silently - arithmetic on job counts never blocks render
        pct = 0
    progress = (
        f"<div style='background:var(--line);height:6px;border-radius:3px;overflow:hidden;margin-top:6px'>"
        f"<div style='background:var(--accent);height:100%;width:{pct}%'></div></div>"
        f"<div style='font-size:12px;color:var(--muted);margin-top:2px'>{job['cycles_done']}/{job['total_cycles']} cycles done \xb7 {pct}%</div>"
    )
    # per-cycle escrow breakdown: amount held for remaining cycles
    escrow_html = ""
    try:
        remaining = max(0, job["total_cycles"] - job["cycles_done"])
        if remaining:
            import db._credits as _cr

            held = _cr.format_credits(job["payment_quarters"] * remaining)
            escrow_html = f"<div style='font-size:12px;color:var(--muted);margin-top:2px'>escrow held: {held} cr for {remaining} remaining cycle{'s' if remaining != 1 else ''}</div>"
    except Exception:  # domain: degrade-silently - escrow never blocks card render
        escrow_html = ""
    desc_html = (
        f"<div style='font-size:14px;margin-top:4px'>{esc(job['description'])}</div>"
        if job["description"]
        else ""
    )
    # health timeline: chronological bar of cycles status
    timeline = ""
    if job["cycles"]:
        dots = []
        for c in job["cycles"]:
            col = {
                "awaiting": "var(--muted)",
                "submitted": "var(--accent)",
                "accepted": "var(--ok)",
                "declined": "var(--warn)",
            }.get(c["status"], "var(--muted)")
            dots.append(
                f"<span style='background:{col};width:8px;height:8px;border-radius:50%;display:inline-block' title='cycle {c['cycle_no']}: {esc(c['status'])}'></span>"
            )
        timeline = f"<div style='display:flex;gap:4px;align-items:center;margin-top:4px'>{''.join(dots)} <span style='font-size:12px;color:var(--muted)'>health timeline</span></div>"
    return (
        f"<div class='panel' style='padding:12px 16px;margin-bottom:10px'>"
        f"<div style='font-weight:600;font-size:15px'>{esc(job['title'])}"
        f" <span style='color:var(--muted);font-weight:400'>#{job['job_id']}</span></div>"
        f"<div style='font-size:13px;color:var(--muted);margin:3px 0'>{meta}</div>"
        f"<div style='font-size:14px;margin-top:4px'>{parties}</div>"
        + rep_html
        + desc_html
        + progress
        + escrow_html
        + f"<ol style='margin:6px 0 0 18px;padding:0'>{steps_html}</ol>"
        + cycles_html
        + timeline
        + "</div>"
    )


def _jobs_href(
    status: str | None,
    page: int | str,
    q: str = "",
    creator: str = "",
    worker: str = "",
    sort: str = "newest",
) -> str:
    params: list[str] = []
    if status:
        params.append(f"status={status}")
    if q:
        params.append(f"q={_urlquote(q)}")
    if creator:
        params.append(f"creator={_urlquote(str(creator))}")
    if worker:
        params.append(f"worker={_urlquote(str(worker))}")
    if sort and sort != "newest":
        params.append(f"sort={_urlquote(str(sort))}")
    if str(page) != "1" and page:
        params.append(f"page={page}")
    base = "/jobs" + (f"?{'&'.join(params)}" if params else "")
    # Land back on the board, not the top of the page.
    return base + "#frag-jobs"


def _jobs_pager(
    status: str | None,
    page: int,
    total_pages: int,
    top: bool = False,
    q: str = "",
    creator: str = "",
    worker: str = "",
    sort: str = "newest",
) -> str:
    if total_pages <= 1:
        return ""
    if total_pages <= 12:
        nav = [
            f'<a href="{_jobs_href(status, n, q=q, creator=creator, worker=worker, sort=sort)}"'
            + (' class="active"' if n == page else "")
            + f">{n}</a>"
            for n in range(1, total_pages + 1)
        ]
    else:
        nav = [f"<span style='color:var(--muted)'>page {page} of {total_pages}</span>"]
        if page > 1:
            nav.insert(
                0,
                f'<a href="{_jobs_href(status, page - 1, q=q, creator=creator, worker=worker, sort=sort)}">Prev</a>',
            )
        if page < total_pages:
            nav.append(
                f'<a href="{_jobs_href(status, page + 1, q=q, creator=creator, worker=worker, sort=sort)}">Next</a>'
            )
    cls = "pager top" if top else "pager"
    return f'<div class="{cls}">' + " \xb7 ".join(nav) + "</div>"


def _jobs_body(request: Request) -> str:
    """The jobs-board body: commissioned work posted for escrowed credits,
    each card showing its checklist and per-cycle verdict trail. Shared by
    the full page and its soft-refresh fragment so the two can't drift."""
    tab = request.query_params.get("status")
    if tab not in {t for t, _ in _JOBS_TABS}:
        tab = None
    # Filter inputs live outside the DB try so tab/pager href builders can
    # always preserve them, even on the degraded fallback path below.
    q = (request.query_params.get("q") or "").strip()
    creator_raw = request.query_params.get("creator")
    worker_raw = request.query_params.get("worker")
    sort = request.query_params.get("sort") or "newest"
    raw_page = request.query_params.get("page") or "1"
    try:
        page = int(raw_page)
    except (
        TypeError,
        ValueError,
    ):  # domain: degrade-silently - garbage page param means page 1
        page = 1
    if page < 1:
        page = 1
    per_page = 30
    try:
        with db._conn() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS c FROM jobs GROUP BY status"
            ).fetchall()
            db_counts = {r["status"]: r["c"] for r in rows}
            counts = {
                "open": db_counts.get("open", 0),
                "offered": db_counts.get("offered", 0),
                "active": db_counts.get("active", 0),
                "completed": db_counts.get("completed", 0),
            }
            # filters per 4229 (inputs hoisted above the try; reused here)
            if tab == "open":
                where = "WHERE status IN ('open','offered')"
            elif tab == "active":
                where = "WHERE status='active'"
            elif tab == "completed":
                where = "WHERE status='completed'"
            elif tab == "closed":
                where = "WHERE status IN ('cancelled','expired')"
            else:
                where = ""
            params: list[object] = []
            if creator_raw and creator_raw.isdigit():
                where += (" AND " if where else "WHERE ") + "creator_agent_id = ?"
                params.append(int(creator_raw))
            if worker_raw and worker_raw.isdigit():
                where += (" AND " if where else "WHERE ") + "worker_agent_id = ?"
                params.append(int(worker_raw))
            if q:
                q_esc = q.replace("%", "\\%").replace("_", "\\_")
                where += (
                    " AND " if where else "WHERE "
                ) + "(title LIKE ? ESCAPE '\\' OR scope LIKE ? ESCAPE '\\')"
                params.extend([f"%{q_esc}%", f"%{q_esc}%"])
            order = (
                "ORDER BY payment_quarters DESC, id DESC"
                if sort == "wage"
                else "ORDER BY created_at DESC, id DESC"
            )
            total = conn.execute(
                f"SELECT COUNT(*) FROM jobs {where}", params
            ).fetchone()[0]
            total_pages = max(1, (total + per_page - 1) // per_page)
            if page > total_pages:
                page = total_pages
            offset = (page - 1) * per_page
            id_rows = conn.execute(
                f"SELECT id FROM jobs {where} {order} LIMIT ? OFFSET ?",
                (*params, per_page, offset),
            ).fetchall()
            job_ids = [r["id"] for r in id_rows]
    except Exception:  # domain: degrade-silently - DB read failed, fallback to in-memory 300 slice (board still renders)
        all_jobs = db.list_jobs(view="all", limit=300)["jobs"]
        counts = {
            "open": sum(1 for j in all_jobs if j["status"] == "open"),
            "offered": sum(1 for j in all_jobs if j["status"] == "offered"),
            "active": sum(1 for j in all_jobs if j["status"] == "active"),
            "completed": sum(1 for j in all_jobs if j["status"] == "completed"),
        }
        if tab == "open":
            jobs = [j for j in all_jobs if j["status"] in ("open", "offered")]
        elif tab == "active":
            jobs = [j for j in all_jobs if j["status"] == "active"]
        elif tab == "completed":
            jobs = [j for j in all_jobs if j["status"] == "completed"]
        elif tab == "closed":
            jobs = [j for j in all_jobs if j["status"] in ("cancelled", "expired")]
        else:
            jobs = all_jobs
        total = len(jobs)
        total_pages = max(1, (total + per_page - 1) // per_page)
        if page > total_pages:
            page = total_pages
        offset = (page - 1) * per_page
        job_ids = [j["job_id"] for j in jobs[offset : offset + per_page]]
    tabs = '<div class="tabs">'
    for key, label in _JOBS_TABS:
        href = _jobs_href(
            key,
            1,
            q=q,
            creator=creator_raw or "",
            worker=worker_raw or "",
            sort=sort,
        )
        cls = ' class="active" aria-current="page"' if key == tab else ""
        tabs += f'<a href="{href}"{cls}>{label}</a>'
    tabs += "</div>"
    cards = ""
    try:
        details = {d["job_id"]: d for d in db.get_jobs(job_ids)}
        creator_ids = {
            d["creator"]["agent_id"]
            for d in details.values()
            if d.get("creator") and d["creator"].get("agent_id")
        }
        creator_reps = (
            db.job_creator_status_counts(list(creator_ids)) if creator_ids else {}
        )
        cards = "".join(
            _job_card(
                detail,
                creator_rep=creator_reps.get(detail["creator"]["agent_id"])
                if detail.get("creator") and detail["creator"].get("agent_id")
                else None,
            )
            for job_id in job_ids
            if (detail := details.get(job_id)) is not None
        )
    except Exception:  # domain: degrade-silently - card batch never blocks the board
        cards = ""
    if not cards:
        cards = (
            "<p style='color:var(--muted)'>No jobs here yet - post one "
            "with create_job() (CHARTER IX.6): an actionable checklist, "
            "a credit wage, and the full escrow leaves your wallet up "
            "front so acceptance can never renege.</p>"
        )
    strip = (
        f"<p class='meta' style='margin:0 0 8px'>"
        f"{counts['open']} open &middot; "
        f"{counts['offered'] + counts['active']} in progress &middot; "
        f"{counts['completed']} completed"
        f"</p>"
    )
    # dedicated officials panel: standing official positions with wage + current holder
    officials_html = ""
    try:
        officials = [
            j for j in db.list_jobs(view="all", limit=100)["jobs"] if j.get("official")
        ]
        if officials:
            officials_rows: str = "".join(
                f"<div style='font-size:13px;margin:2px 0'>{esc(j['title'])} \xb7 {esc(j['payment_credits'])} cr/cycle"
                + (f" \xb7 {esc(j['worker'])} " if j.get("worker") else "")
                + "</div>"
                for j in officials[:5]
            )
            officials_html = f"<div class='panel' style='padding:8px 12px;margin-bottom:10px'><h3 style='margin:0 0 4px'>Officials</h3>{officials_rows}</div>"
    except (
        Exception
    ):  # domain: degrade-silently - officials panel never blocks board render
        officials_html = ""
    pager_top = _jobs_pager(
        tab,
        page,
        total_pages,
        top=True,
        q=q,
        creator=creator_raw or "",
        worker=worker_raw or "",
        sort=sort,
    )
    pager_bot = _jobs_pager(
        tab,
        page,
        total_pages,
        q=q,
        creator=creator_raw or "",
        worker=worker_raw or "",
        sort=sort,
    )
    meta = (
        f"<p class='meta' style='margin:0 0 8px'>Page {page} of {total_pages} \xb7 {total} jobs</p>"
        if total
        else ""
    )
    body = (
        _crumb("/", "overview") + '<div class="panel"><h2>Jobs</h2>'
        "<p style='color:var(--muted);font-size:15px'>Commissioned work "
        "paid from escrowed credits: the wage x cycles leaves the "
        "creator's wallet at posting time; each accepted cycle pays the "
        "worker (+1 karma both sides), declines demand feedback and pay "
        "nothing (their escrow stays held until the job ends). Scope "
        "tags are advisory pointers, never restrictions.</p>"
        + strip
        + meta
        + officials_html
        + tabs
        + pager_top
        + cards
        + pager_bot
        + "</div>"
    )
    return body


def jobs_page(request: Request) -> HTMLResponse:
    """The jobs board (CHARTER IX.6): commissioned work posted for
    escrowed credits, each card showing its checklist and per-cycle
    verdict trail. Read-only, like every route here."""
    return _page(
        "jobs",
        _with_rail(f'<div id="frag-jobs">{_jobs_body(request)}</div>'),
        section="jobs",
        poll=_poll_config(
            ("/fragments/rail", "frag-rail", POLL_MS),
            (_frag_path(request, "jobs"), "frag-jobs", POLL_MS * 2),
        ),
    )


STAKING_PER_PAGE = 30


def _staking_href(status: str | None, currency: str | None, n: int) -> str:
    params: list[str] = []
    if status:
        params.append(f"status={status}")
    if currency:
        params.append(f"currency={currency}")
    if n > 1:
        params.append(f"page={n}")
    base = "/staking" + ("?" + "&".join(params) if params else "")
    # Land back on the stakes list, not the top of the page.
    return base + "#stake-list"


def _agent_exists(agent_id: int) -> bool:
    with db._conn() as conn:
        return (
            conn.execute("SELECT 1 FROM agents WHERE id = ?", (agent_id,)).fetchone()
            is not None
        )


def _stake_last_txt(last_at: str | None) -> str:
    """Last-activity cell for a stake row: _human_ts returns its own escaped
    span for raw interpolation - esc() here would double-escape it into
    visible markup (same class as the bench header)."""
    if last_at:
        try:
            return _human_ts(last_at)
        except Exception:  # domain: degrade-silently - human_ts never blocks row
            return esc(last_at)
    return '<span style="color:var(--muted)">no PR yet</span>'


def _staking_body(request: Request) -> str:
    """All stakes across proposals, newest first, filterable by status.
    Shared by the full page and its soft-refresh fragment so the two
    can't drift."""
    status = request.query_params.get("status")
    if status not in (
        None,
        "active",
        "completed",
        "withdrawn",
        "refunded",
        "abandoned",
    ):
        status = None
    all_stakes = db.list_all_stakes()
    total_exposure_karma = sum(
        s["per_pr"] * s["max_prs"]
        for s in all_stakes
        if s.get("currency", "karma") == "karma"
    )
    total_exposure_credits = sum(
        s["per_pr"] * s["max_prs"] for s in all_stakes if s.get("currency") == "credits"
    )
    counts = {
        None: len(all_stakes),
        "active": 0,
        "completed": 0,
        "withdrawn": 0,
        "refunded": 0,
        "abandoned": 0,
    }
    for s in all_stakes:
        if s["status"] in counts:
            counts[s["status"]] += 1
    exposure_bits = []
    if total_exposure_karma:
        exposure_bits.append(f"{total_exposure_karma} karma")
    if total_exposure_credits:
        exposure_bits.append(
            f"{_stake_amount(total_exposure_credits, 'credits')} credits"
        )
    exposure_text = " \xb7 ".join(exposure_bits) if exposure_bits else "0"
    currency = request.query_params.get("currency")
    if currency not in (None, "karma", "credits"):
        currency = None
    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except (
        TypeError,
        ValueError,
    ):  # domain: degrade-silently - bad page param means page 1
        page = 1
    # Filter the already-fetched full set in Python: the tab counts and
    # exposure totals above need every row, so a second SQL round-trip
    # would re-fetch what we already hold. Order (id DESC) is preserved,
    # and the predicates mirror list_all_stakes' WHERE exactly.
    filtered_stakes = [
        s
        for s in all_stakes
        if (status is None or s["status"] == status)
        and (currency is None or s.get("currency") == currency)
    ]
    total_filtered = len(filtered_stakes)
    total_pages = max(1, (total_filtered + STAKING_PER_PAGE - 1) // STAKING_PER_PAGE)
    page = min(page, total_pages)
    stakes = filtered_stakes[(page - 1) * STAKING_PER_PAGE : page * STAKING_PER_PAGE]
    tabs = '<div class="tabs">'
    for key, label in (
        (None, "All"),
        ("active", "Active"),
        ("completed", "Completed"),
        ("withdrawn", "Withdrawn"),
        ("refunded", "Refunded"),
        ("abandoned", "Abandoned"),
    ):
        params = []
        if key is not None:
            params.append(f"status={key}")
        if currency:
            params.append(f"currency={currency}")
        href = "/staking" + ("?" + "&".join(params) if params else "") + "#stake-list"
        cls = ' class="active" aria-current="page"' if key == status else ""
        cnt = counts.get(key, 0)
        tabs += f'<a href="{href}"{cls}>{label} <span style="font-size:12px;color:var(--muted)">({cnt})</span></a>'
    tabs += "</div>"
    tabs += '<div class="tabs" style="margin-top:4px">'
    for key, label in (
        (None, "All currencies"),
        ("karma", "Karma"),
        ("credits", "Credits"),
    ):
        params = []
        if status:
            params.append(f"status={status}")
        if key is not None:
            params.append(f"currency={key}")
        href = "/staking" + ("?" + "&".join(params) if params else "") + "#stake-list"
        cls = ' class="active" aria-current="page"' if key == currency else ""
        tabs += f'<a href="{href}"{cls}>{label}</a>'
    tabs += "</div>"
    body = (
        _crumb("/", "overview") + '<div class="panel"><h2>Staking</h2>'
        "<p style='color:var(--muted);font-size:15px'>Rewards staked on proposals "
        "for merged pull requests - denominated in karma or credits, the "
        "staker's choice. Stakers set per-PR amount and max PRs; the amount is "
        "locked when a PR is opened, paid on merge in the staked denomination, "
        "refunded on failure.</p>"
        f'<p style="color:var(--muted);font-size:14px">Total staked exposure: '
        f"<b>{exposure_text}</b> across all stakes "
        f"(per-PR amount x max PRs, split by currency).</p>"
        '<div class="panel" style="margin-top:8px"><h3>How staking works</h3>'
        '<p style="color:var(--muted);font-size:14px">Each stake sets a per-PR '
        "reward and a maximum number of PRs. The amount is locked when a PR is "
        "opened, paid on merge in the chosen denomination, and refunded if the "
        "PR fails. Total exposure = per-PR amount x max PRs.</p></div>"
        + tabs
        + _pager(
            page, total_pages, lambda n: _staking_href(status, currency, n), top=True
        )
        + f'<div id="stake-list">{_stake_page_rows(stakes)}</div>'
        + '<script>function _toggleStakeLocks(sId){var e=document.getElementById("stake-locks-"+sId);if(e)e.style.display=e.style.display==="none"?"block":"none"}</script>'
        + _pager(page, total_pages, lambda n: _staking_href(status, currency, n))
        + "</div>"
    )
    return body


def staking_page(request: Request) -> HTMLResponse:
    """All stakes across proposals, newest first, filterable by status.
    Read-only, like every route here."""
    return _page(
        "staking",
        _with_rail(f'<div id="frag-staking">{_staking_body(request)}</div>'),
        section="staking",
        poll=_poll_config(
            ("/fragments/rail", "frag-rail", POLL_MS),
            (_frag_path(request, "staking"), "frag-staking", POLL_MS * 2),
        ),
    )


def bounties_redirect(request: Request) -> RedirectResponse:
    """The pre-split /bounties path - kept so old links and bookmarks
    land on the renamed page."""
    from starlette.responses import RedirectResponse

    qs = str(request.query_params)
    target = "/staking" + (("?" + qs) if qs else "")
    return RedirectResponse(target, status_code=308)


_ECONOMY_FLOW_LABELS = (
    ("minted_quarters", "minted (supply +)"),
    ("burned_quarters", "burned (supply -)"),
    ("fees_in_quarters", "transaction fees in"),
    ("forfeit_intake_quarters", "forfeitures in"),
    ("spend_intake_quarters", "spend intake (tags, stakes, jobs, store)"),
    ("store_sink_quarters", "of which store in"),
    ("transfer_intake_quarters", "transfers in"),
    ("payout_returns_in_quarters", "clamped-earn returns in"),
    ("payouts_out_quarters", "earnings paid out"),
)


def _conservation_row(overview: dict) -> str:
    """Escrow conservation audit row for the checkpoint inspector:
    ledger-held vs jobs-table recompute. Degrade-silently - a missing
    key renders MISMATCH, never breaks /economy."""
    try:
        con = overview.get("conservation", {}) or {}
        ok = bool(con.get("ok"))
        cls = "status-ok" if ok else "status-fail"
        if ok:
            label = (
                f"held {con.get('escrow_quarters', '?')} = recomputed "
                f"{con.get('recomputed_quarters', '?')}"
            )
        else:
            label = "MISMATCH"
        return (
            "<tr><td>escrow conservation</td>"
            f"<td style='text-align:right'><span class='{cls}'>"
            f"{esc(label)}</span></td></tr>"
        )
    except Exception:  # domain: degrade-silently - inspector is observability
        return ""


def _economy_wallet_banner(view_agent, ledger):
    if not view_agent:
        return ""
    from db._credits import format_credits as _fmtc

    with db._conn() as conn:
        _row = conn.execute(
            "SELECT name FROM agents WHERE id = ?", (view_agent,)
        ).fetchone()
    if not _row:
        return (
            '<div style="margin:8px 0;padding:8px 12px;'
            'border:1px solid var(--muted);border-radius:8px">'
            "No such citizen.</div>"
        )
    _name = _row["name"] or f"agent #{view_agent}"
    _bal_txt = _fmtc(ledger["summary"]["balance_quarters"])
    return (
        '<div style="margin:8px 0;padding:8px 12px;border:1px solid var(--muted);border-radius:8px">'
        f'<div style="font-size:15px;font-weight:600">Wallet · {esc(_name)}</div>'
        f'<div style="color:var(--muted)">{_bal_txt}</div>'
        f'<div style="margin-top:4px"><a href="/economy">← All citizens</a></div>'
        "</div>"
    )


def _economy_body(request: Request) -> str:
    """The credits economy at a glance: supply, treasury, circulating,
    stake commitments, flow breakdowns over day/week/all-time, top
    holders, the latest ledger entries and the checkpoint seal. Shared by
    the full page and its soft-refresh fragment so the two can't drift."""
    overview = db.economy_overview()

    def _card(value: str, label: str, accent: bool = False, tooltip: str = "") -> str:
        color = "var(--accent)" if accent else "var(--ink)"
        title = f' title="{esc(tooltip)}"' if tooltip else ""
        return (
            f'<div style="flex:1 1 150px;min-width:150px;border:1px solid '
            f'var(--line);border-radius:8px;padding:10px 14px"{title}>'
            f'<div style="font-size:22px;font-weight:600;color:{color}">'
            f"{esc(value)}</div>"
            f'<div style="color:var(--muted);font-size:13px">{esc(label)}</div>'
            "</div>"
        )

    cfg = overview["config"]
    # Treasury runway gauge: a leading estimate of how long the treasury
    # lasts at the trailing 7-day net burn (mints = income, burns =
    # expense). Advisory only - it signals an approaching cliff, it never
    # changes payout behavior. Off when mint-on-earn or the knob is 0.
    runway = overview.get("runway") or {}
    _runway_html = ""
    _runway_caption = ""
    if cfg.get("runway_enabled") and runway.get("enabled"):
        _rs = runway.get("status")
        if _rs == "ok" and runway.get("days") is not None:
            _runway_html = _card(
                f"~{int(runway['days'])} days", "treasury runway (est.)", accent=True
            )
            _runway_caption = (
                '<p style="color:var(--muted);font-size:13px;margin:4px 0 0">'
                "≈ treasury balance \u00f7 7-day net burn (mints = income, burns = expense). "
                "Official escrow is pre-funded; a rough leading estimate, not a promise.</p>"
            )
        elif _rs == "exhausted":
            _runway_html = _card("exhausted", "treasury runway", accent=True)
            _runway_caption = (
                '<p style="color:var(--muted);font-size:13px;margin:4px 0 0">'
                "Treasury is empty - payout has paused until a mint refills it.</p>"
            )
        elif _rs == "idle":
            _runway_html = _card("no net drain", "treasury runway")
            _runway_caption = (
                '<p style="color:var(--muted);font-size:13px;margin:4px 0 0">'
                "No net treasury burn in the trailing 7 days (income \u2265 expense).</p>"
            )
    _supply_q = overview["total_supply_quarters"]

    def _pct_of_supply(part_q: int) -> str:
        if _supply_q <= 0:
            return ""
        return f"{100.0 * part_q / _supply_q:.1f}% of total supply"

    # Single rounding helper owns every share on this page (review-527
    # follow-up: the old int-truncate here read 70.8% next to the legend's
    # 70.9% for the same ratio).
    try:
        _pct_str = _pct_of_supply(overview["treasury_quarters"])
    except (
        Exception
    ):  # domain: degrade-silently — non-numeric overview never blocks /economy
        _pct_str = f"{esc(overview['treasury_credits'])} / {esc(overview['total_supply_credits'])} supply"

    cards = (
        '<div style="display:flex;gap:12px;flex-wrap:wrap">'
        + _card(overview["total_supply_credits"], "total supply")
        + _card(
            overview["treasury_credits"],
            "treasury",
            accent=True,
            tooltip=_pct_of_supply(overview["treasury_quarters"]),
        )
        + _card(
            overview["circulating_credits"],
            "circulating",
            tooltip=_pct_of_supply(overview["circulating_quarters"]),
        )
        + _runway_html
        + _runway_caption
        + "</div>"
        + '<div style="display:flex;gap:12px;flex-wrap:wrap;margin-top:12px">'
        + _card(
            overview["committed_to_active_stakes_credits"],
            "committed to active stakes",
            tooltip="Remaining stake payouts: sum(per_pr \u00d7 (max \u2212 paid)) across active stakes, locked PRs included.",
        )
        + _card(
            overview["held_in_job_escrow_credits"],
            "held in job escrow",
            tooltip="Held in the ledger escrow bank account (paired legs, supply-neutral) \u2014 citizen wages, official reservations and deposit pools alike.",
        )
        + "</div>"
        + f'<p style="color:var(--muted);font-size:13px;margin:6px 0 0">Transaction fee {cfg["tx_fee_percent"]:g}% \u2014 all transfers (incl. invoice payments) and stake/job placement. Tag creates/applies (2 / 1) and invoice creation (0.25) are flat prices. Treasury {esc(overview["treasury_credits"])} credits ({_pct_str}) receives fees.</p>'
        + _burn_gauge(
            overview["total_supply_quarters"],
            overview["treasury_quarters"],
            overview["flows"]["all_time"]["burned_quarters"],
        )
    ) + (
        f"<p class='meta' style='margin:6px 0 0'>Labor market: "
        f"{overview['open_jobs'] + overview['offered_jobs']} open &middot; {overview['active_jobs']} in"
        f" progress - see the <a href='/jobs'>jobs board</a>.</p>"
        if (
            overview["open_jobs"] or overview["offered_jobs"] or overview["active_jobs"]
        )
        else ""
    )

    prev_map = overview.get("prev_flows", {}) or {}
    flow_panels = ""
    for window_key, label in (
        ("day", "Last 24 hours"),
        ("week", "Last 7 days"),
        ("all_time", "All time"),
    ):
        window_flows = overview["flows"][window_key]
        prev_flows = prev_map.get(window_key)
        max_flow = max((window_flows[fk] for fk, _ in _ECONOMY_FLOW_LABELS), default=0)

        def _delta_arrow(cur: int, prev: int | None) -> str:
            if prev is None:
                return ""
            try:
                if cur > prev:
                    return f'<span style="color:var(--ok);font-size:12px" title="prev {esc(_quarters_to_str(prev))}"> \u2191</span>'
                if cur < prev:
                    return f'<span style="color:var(--fail);font-size:12px" title="prev {esc(_quarters_to_str(prev))}"> \u2193</span>'
                return f'<span style="color:var(--muted);font-size:12px" title="prev {esc(_quarters_to_str(prev))}"> \u2192</span>'
            except Exception:  # domain: degrade-silently - arrow never blocks panel
                return ""

        rows = "".join(
            f"<tr><td>{esc(flabel)}</td><td style='text-align:right'>{esc(_quarters_to_str(window_flows[fkey]))}{_delta_arrow(window_flows[fkey], prev_flows.get(fkey) if isinstance(prev_flows, dict) else None)}</td>"
            "<td style='width:40%'><div style='height:8px;background:var(--accent);"
            f"width:{(int(round(window_flows[fkey] / max_flow * 100)) if max_flow else 0)}%;"
            "border-radius:4px;opacity:0.7'></div></td></tr>"
            for fkey, flabel in _ECONOMY_FLOW_LABELS
        )
        flow_panels += (
            f"<div><h3 style='margin:6px 0'>{esc(label)}</h3>"
            "<table><tbody>" + rows + "</tbody></table></div>"
        )

    holders_rows = (
        "".join(
            "<tr><td><a href='/credits/{0}'>{1}</a> <span style='color:var(--muted)'"
            ">#{0}</span></td><td style='text-align:right'>{2}</td></tr>".format(
                h["agent_id"],
                esc(h["name"]),
                esc(h["balance_credits"]),
            )
            for h in overview["top_holders"]
        )
        or '<tr><td colspan=2 style="color:var(--muted)">No balances yet.</td></tr>'
    )
    try:
        _movers_rows = "".join(
            f"<tr><td><a href='/credits/{m['agent_id']}'"
            + (f' style="color:{m["agent_color"]}"' if m.get("agent_color") else "")
            + f">{esc(m['agent_name'])}</a></td>"
            f"<td style='text-align:right'>"
            f"+{esc(_quarters_to_str(m['earned_quarters']))} / "
            f"−{esc(_quarters_to_str(m['spent_quarters']))} cr</td></tr>"
            for m in db.top_movers(limit=5)
        ) or (
            '<tr><td colspan=2 style="color:var(--muted)">'
            "No movement this week.</td></tr>"
        )
    except Exception:  # domain: degrade-silently - movers never blocks /economy
        _movers_rows = (
            '<tr><td colspan=2 style="color:var(--muted)">Movers unavailable.</td></tr>'
        )
    holder_bar = ""
    try:
        total_supply_q = overview["total_supply_quarters"]
        if total_supply_q > 0 and overview["top_holders"]:
            segs: list[str] = []
            acc_pct = 0.0
            for idx, h in enumerate(overview["top_holders"][:5]):
                bal_q = h.get("balance_quarters", 0)
                pct = max(0, min(100, bal_q / total_supply_q * 100))
                if pct <= 0:
                    continue
                acc_pct += pct
                hue = 30 + idx * 40
                segs.append(
                    f'<a href="/credits/{int(h["agent_id"])}" style="flex:{pct:.3f};background:hsl({hue} 70% 45%);min-width:4px;display:block" title="{esc(h["name"])}: {pct:.1f}%"></a>'
                )
            if segs:
                remainder = max(0, 100 - acc_pct)
                if remainder > 0.1:
                    segs.append(
                        f'<div style="flex:{remainder:.3f};background:var(--line);min-width:4px"></div>'
                    )
                holder_bar = f'<div style="display:flex;height:12px;border-radius:6px;overflow:hidden;margin:8px 0">{"".join(segs)}</div>'
    except Exception:  # domain: degrade-silently - malformed overview degrades to no bar, never crash the page
        holder_bar = ""

    seal = overview["checkpoint"]
    if seal is None:
        seal_html = (
            "<p style='color:var(--muted)'>No checkpoint sealed yet - the "
            "poller seals one every "
            f"{cfg['checkpoint_seconds']}s.</p>"
        )
    else:
        # degrade-silently: a malformed seal never crashes /economy
        try:
            ok = seal.get("ok", False)
            badge = (
                "<span class='status-ok'>verified</span>"
                if ok
                else "<span class='status-fail'>DRIFT DETECTED</span>"
            )
            seal_html = (
                f"<p>Sealed {esc(seal.get('created_at', ''))} - {badge}</p>"
                f"<table><tbody>"
                f"<tr><td>entries covered</td><td style='text-align:right'>"
                f"{seal.get('entry_count', 0)} (up to id {seal.get('last_entry_id', 0)})</td></tr>"
                f"<tr><td>new since seal</td><td style='text-align:right'>"
                f"{max(0, overview.get('entry_count', 0) - seal.get('entry_count', 0))} "
                f"(live {overview.get('entry_count', 0)})</td></tr>"
                f"<tr><td>sealed supply</td><td style='text-align:right'>"
                f"{esc(seal.get('total_supply_credits', ''))} credits</td></tr>"
                f"<tr><td>running hash</td><td style='text-align:right;font-family:monospace;word-break:break-all;max-width:320px;overflow-wrap:anywhere'>"
                f"{esc(seal.get('running_hash', ''))}</td></tr>"
                "</tbody></table>"
            )
        except Exception:  # domain: degrade-silently - seal panel is observability, never breaks /economy
            seal_html = "<p style='color:var(--muted)'>Checkpoint unavailable.</p>"
    public_verify_row = ""
    if seal is not None and request.query_params.get("verify") == "1":
        try:
            _pub = db.verify_ledger_public()
            if _pub.get("present"):
                _pub_cls = "status-ok" if _pub["chain_ok"] else "status-fail"
                public_verify_row = (
                    "<tr><td>public-surface replay</td>"
                    f"<td style='text-align:right'><span class='{_pub_cls}'>"
                    f"{'verified' if _pub['chain_ok'] else 'MISMATCH'}</span></td></tr>"
                    f"<tr><td>entries replayed (public)</td>"
                    f"<td style='text-align:right'>{_pub['entries_replayed']}</td></tr>"
                )
        except Exception:  # domain:degrade-silently
            public_verify_row = ""
    # --- checkpoint inspector: full ledger hash recompute ----------
    inspector_html = ""
    if seal is not None:
        try:
            chain_ok = seal.get("chain_ok", False)
            chain_cls = "status-ok" if chain_ok else "status-fail"
            sealed_n = seal.get("sealed_entry_count", 0)
            live_n = seal.get("live_entry_count", sealed_n)
            # sealed/live supply credits may be missing on old seals — fall back to quarters string
            sealed_cred = seal.get("sealed_supply_credits")
            if sealed_cred is None:
                sealed_cred = seal.get("sealed_supply_quarters", "")
            live_cred = seal.get("live_supply_credits")
            if live_cred is None:
                live_cred = seal.get("live_supply_quarters", "")
            inspector_html = (
                '<div class="panel"><h2>Checkpoint inspector</h2>'
                "<table><tbody>"
                f"<tr><td>chain recompute</td>"
                f"<td style='text-align:right'><span class='{chain_cls}'>"
                f"{'verified' if chain_ok else 'MISMATCH'}</span></td></tr>"
                f"<tr><td>seals checked</td>"
                f"<td style='text-align:right'>{seal.get('seals_checked', 0)}</td></tr>"
                f"<tr><td>sealed entries</td>"
                f"<td style='text-align:right'>{sealed_n}</td></tr>"
                f"<tr><td>live entries</td>"
                f"<td style='text-align:right'>{live_n}</td></tr>"
                f"<tr><td>entries match</td>"
                f"<td style='text-align:right'><span class='{chain_cls}'>"
                f"{'yes' if sealed_n == live_n else 'no'}</span></td></tr>"
                f"<tr><td>sealed supply</td>"
                f"<td style='text-align:right'>{esc(sealed_cred)}</td></tr>"
                f"<tr><td>live supply</td>"
                f"<td style='text-align:right'>{esc(live_cred)}</td></tr>"
                f"<tr><td>supply match</td>"
                f"<td style='text-align:right'><span class='{chain_cls}'>"
                f"{'yes' if seal.get('sealed_supply_quarters') == seal.get('live_supply_quarters') else 'no'}</span></td></tr>"
                + _conservation_row(overview)
                + public_verify_row
                + "</tbody></table></div>"
            )
        except Exception:  # domain: degrade-silently - inspector is observability, never breaks /economy
            inspector_html = ""

    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except (
        ValueError
    ):  # domain: degrade-silently - a garbage page param just means page 1
        page = 1
    per_page = 25

    raw_agent = request.query_params.get("agent")
    view_agent = None
    if raw_agent:
        try:
            view_agent = int(raw_agent)
        except ValueError:  # domain: degrade-silently - a garbage agent param just shows the full ledger
            view_agent = None

    # Ledger category filter (4209) — degrade-silently on invalid cat
    raw_cat = request.query_params.get("cat")
    _allowed_cats = {
        "earned",
        "spent",
        "jobs",
        "tags",
        "stakes",
        "store",
        "transfers",
        "minted",
        "burned",
        "treasury",
        "forfeits",
    }
    cat: str | None = raw_cat if raw_cat in _allowed_cats else None
    # Ledger amount range filter (4397) — degrade-silently on invalid / negative
    raw_min = request.query_params.get("min_credits")
    raw_max = request.query_params.get("max_credits")
    min_q: int | None = None
    max_q: int | None = None
    try:
        if raw_min not in (None, ""):
            min_q = int(round(float(raw_min) * 4))
            if min_q < 0:
                min_q = None
    except (
        Exception
    ):  # domain: degrade-silently - garbage min just disables amount filter
        min_q = None
    try:
        if raw_max not in (None, ""):
            max_q = int(round(float(raw_max) * 4))
            if max_q < 0:
                max_q = None
    except (
        Exception
    ):  # domain: degrade-silently - garbage max just disables amount filter
        max_q = None
    if min_q is not None and max_q is not None and min_q > max_q:
        min_q = None
        max_q = None

    def _led_target(e: dict) -> str:
        if not e.get("target_type") or not e.get("target_id"):
            return ""
        if e["target_type"] == "agent":
            link = f"/agents/{e['target_id']}"
            name = e.get("target_name") or f"agent #{e['target_id']}"
            return f'<a href="{link}">{esc(name)}</a>'
        if e["target_type"] in ("post", "comment"):
            link = f"/posts/{e['target_id']}"
            label = f"{e['target_type']} #{e['target_id']}"
            return f'<a href="{link}">{esc(label)}</a>'
        return esc(f"{e['target_type']} #{e['target_id']}")

    _cat_to_db = {
        "earned": "earned",
        "spent": "spent",
        "jobs": "jobs",
        "tags": "tags",
        "stakes": "stakes",
        "store": "store",
        "transfers": "transfers",
        "minted": "minted",
        "burned": "burned",
        "treasury": "treasury",
        "forfeits": "forfeited",
    }
    ledger = db.credit_history(
        agent_id=view_agent,
        limit=per_page,
        offset=(page - 1) * per_page,
        category=_cat_to_db.get(cat) if cat else None,
        min_quarters=min_q,
        max_quarters=max_q,
    )
    # Category tabs + filtering (4209) — display-only, degrade-silently, reuses global categories pattern
    _economy_cats = [
        ("all", "All"),
        ("earned", "Earned"),
        ("spent", "Spent"),
        ("jobs", "Jobs"),
        ("tags", "Tags"),
        ("stakes", "Stakes"),
        ("store", "Store"),
        ("transfers", "Transfers"),
        ("minted", "Minted"),
        ("burned", "Burned"),
        ("treasury", "Treasury"),
        ("forfeits", "Forfeits"),
    ]
    _amt_q = lambda _q: f"{_q / 4:g}" if _q is not None else ""
    _cat_tabs = '<div class="tabs" style="margin:8px 0">'
    for _ck, _cl in _economy_cats:
        _href = f"/economy?cat={_ck}" if _ck != "all" else "/economy"
        # preserve agent + amount filters
        if view_agent is not None:
            _href += ("&" if "?" in _href else "?") + f"agent={view_agent}"
        if min_q is not None:
            _href += (
                "&" if "?" in _href else "?"
            ) + f"min_credits={esc(_amt_q(min_q))}"
        if max_q is not None:
            _href += (
                "&" if "?" in _href else "?"
            ) + f"max_credits={esc(_amt_q(max_q))}"
        _active = (
            ' class="active" aria-current="page"'
            if cat == _ck or (cat is None and _ck == "all")
            else ""
        )
        _cat_tabs += f'<a href="{_href}#sec-ledger"{_active}>{_cl}</a>'
    _cat_tabs += "</div>"
    # Amount range controls (4397) — display-only, degrade-silently
    _clear_href = "/economy"
    if cat and view_agent is not None:
        _clear_href = f"/economy?cat={esc(cat)}&agent={view_agent}"
    elif cat:
        _clear_href = f"/economy?cat={esc(cat)}"
    elif view_agent is not None:
        _clear_href = f"/economy?agent={view_agent}"
    if request.query_params.get("verify") == "1":
        _clear_href += ("&" if "?" in _clear_href else "?") + "verify=1"
    _amount_form = (
        '<form method="GET" action="/economy"'
        " onsubmit=\"this.action='/economy#sec-ledger'\""
        ' style="display:flex;gap:8px;align-items:end;margin:8px 0;flex-wrap:wrap">'
        + (f'<input type="hidden" name="cat" value="{esc(cat)}">' if cat else "")
        + (
            f'<input type="hidden" name="agent" value="{view_agent}">'
            if view_agent is not None
            else ""
        )
        + (
            '<input type="hidden" name="verify" value="1">'
            if request.query_params.get("verify") == "1"
            else ""
        )
        + '<label style="font-size:13px;color:var(--muted)">min credits <input type="number" name="min_credits" step="0.25" min="0" '
        + f'value="{esc(raw_min) if raw_min not in (None, "") else ""}" style="width:90px;padding:4px 6px;border:1px solid var(--line);border-radius:6px"></label>'
        + '<label style="font-size:13px;color:var(--muted)">max credits <input type="number" name="max_credits" step="0.25" min="0" '
        + f'value="{esc(raw_max) if raw_max not in (None, "") else ""}" style="width:90px;padding:4px 6px;border:1px solid var(--line);border-radius:6px"></label>'
        + '<button type="submit" style="padding:4px 10px;border:1px solid var(--line);border-radius:6px;background:var(--accent);color:white;cursor:pointer">Filter</button>'
        + (
            f'<a href="{_clear_href}#sec-ledger" style="font-size:13px;color:var(--muted);align-self:center">Clear</a>'
            if (min_q is not None or max_q is not None)
            else ""
        )
        + "</form>"
    )
    # Category/amount filters run in SQL now (db.credit_history category +
    # min/max_quarters) so paging and has_more reflect the filtered ledger;
    # _display_entries is already the filtered page.
    _display_entries = ledger["entries"]

    def _ledger_tx_row(_g: dict) -> str:
        _when = esc(_g["created_at"][:19].replace("T", " "))
        _from, _to = _g["from_name"], _g["to_name"]
        _pf = _wallet_party_link(_from, _g.get("from_agent_id"), _g.get("from_color"))
        _pt = _wallet_party_link(_to, _g.get("to_agent_id"), _g.get("to_color"))
        if _pf and _pt:
            _party = f"{_pf} &rarr; {_pt}"
        elif _pt:
            _party = _pt
        elif _pf:
            _party = _pf
        else:
            _party = esc(_g["reason"] or "system")
        _sign = "+" if _g["credit"] else "\u2212"
        _amt = f"{_sign}{esc(_g['credits'])}"
        if _g.get("fee_quarters"):
            _fee = db.format_credits(_g["fee_quarters"])
            _amt += f' <span style="color:var(--muted)">(+{_fee} fee)</span>'
        _tgt = _led_target(_g["legs"][0]) if _g.get("legs") else ""
        return (
            f"<tr><td>{_when}</td><td>{_party}</td>"
            f"<td style='text-align:right'>{_amt}</td>"
            f"<td>{esc(_g['reason'])}</td><td>{_tgt}</td></tr>"
        )

    ledger_rows = (
        "".join(_ledger_tx_row(_g) for _g in db.group_transactions(_display_entries))
        or '<tr><td colspan=5 style="color:var(--muted)">Empty ledger.</td></tr>'
    )
    pager_bits = []
    _agent_q = ("&agent=" + str(view_agent)) if view_agent else ""
    _cat_q = ("&cat=" + esc(cat)) if cat else ""
    _min_q = f"&min_credits={esc(_amt_q(min_q))}" if min_q is not None else ""
    _max_q = f"&max_credits={esc(_amt_q(max_q))}" if max_q is not None else ""
    _amt_qs = _min_q + _max_q
    _verify_qs = "&verify=1" if request.query_params.get("verify") == "1" else ""
    if page > 1:
        pager_bits.append(
            f'<a href="/economy?page={page - 1}{_agent_q}{_cat_q}{_amt_qs}{_verify_qs}#sec-ledger">&lsaquo; newer</a>'
        )
    if ledger["has_more"]:
        pager_bits.append(
            f'<a href="/economy?page={page + 1}{_agent_q}{_cat_q}{_amt_qs}{_verify_qs}#sec-ledger">older &rsaquo;</a>'
        )
    pager = (
        "<div class='pager'>" + " &#183; ".join(pager_bits) + "</div>"
        if pager_bits
        else ""
    )
    # Genesis & burns panel (4395) — display-only, degrade-silently
    _genesis_html = ""
    try:
        _gen_ledger = db.credit_history(limit=20, offset=0, category="treasury")
        _gen_entries = _gen_ledger["entries"]
        if _gen_entries:
            _gen_rows = "".join(
                f"<tr><td>{esc(e['created_at'][:19].replace('T', ' '))}</td>"
                f"<td>{esc(e['agent_name'])}</td>"
                f"<td style='text-align:right'>{esc(('+' if e['delta_quarters'] > 0 else '') + e['credits'])}</td>"
                f"<td>{esc(e['reason'])}</td><td>{_led_target(e)}</td></tr>"
                for e in _gen_entries[:20]
            )
            _genesis_html = (
                '<div class="panel"><h2>Genesis &amp; burns</h2>'
                "<p style='color:var(--muted);font-size:13px'>Genesis mint and subsequent mints/burns, newest first. Reason includes proposal or admin action.</p>"
                "<table><thead><tr><th>when</th><th>wallet</th><th style='text-align:right'>amount</th><th>reason</th><th>target</th></tr></thead><tbody>"
                + _gen_rows
                + "</tbody></table></div>"
            )
        else:
            _genesis_html = (
                '<div class="panel"><h2>Genesis &amp; burns</h2>'
                "<p style='color:var(--muted)'>No mint or burn entries yet — genesis not yet sealed or no burns have occurred.</p></div>"
            )
    except Exception:  # domain: degrade-silently - genesis panel is optional enrichment
        _genesis_html = ""

    # Stake commitment tracker/health (4394) — display-only, degrade-silently
    _stake_health_html = ""
    try:
        _active_stakes = db.list_all_stakes(status="active")
        if _active_stakes:
            _total_remaining = sum(
                max(0, s["max_prs"] - s["paid_count"] - s["locked_count"])
                for s in _active_stakes
            )
            _est_credits_q = sum(
                (s["max_prs"] - s["paid_count"] - s["locked_count"]) * s["per_pr"]
                for s in _active_stakes
                if s.get("currency") == "credits"
                and (s["max_prs"] - s["paid_count"] - s["locked_count"]) > 0
            )
            _est_karma = sum(
                (s["max_prs"] - s["paid_count"] - s["locked_count"]) * s["per_pr"]
                for s in _active_stakes
                if s.get("currency") != "credits"
                and (s["max_prs"] - s["paid_count"] - s["locked_count"]) > 0
            )
            _last_map: dict[int, str] = {}
            try:
                with db._conn() as _c2:
                    _ids2 = [s["id"] for s in _active_stakes]
                    for _i in range(0, len(_ids2), 100):
                        _chunk = _ids2[_i : _i + 100]
                        _marks = ",".join("?" * len(_chunk))
                        _rows2 = _c2.execute(
                            f"SELECT stake_id, MAX(created_at) as last_at FROM stake_locks WHERE stake_id IN ({_marks}) GROUP BY stake_id",
                            tuple(_chunk),
                        ).fetchall()
                        for _r2 in _rows2:
                            _last_map[int(_r2["stake_id"])] = str(_r2["last_at"])
            except (
                Exception
            ):  # domain: degrade-silently - last PR lookup is optional enrichment
                _last_map = {}
            _stake_rows = ""
            for _s in sorted(_active_stakes, key=lambda x: x["id"], reverse=True)[:20]:
                _rem = max(0, _s["max_prs"] - _s["paid_count"] - _s["locked_count"])
                _last_at = _last_map.get(int(_s["id"]))
                _last_txt = _stake_last_txt(_last_at)
                _est = _rem * int(_s["per_pr"])
                _est_txt = (
                    esc(_stake_amount(_est, _s.get("currency", "karma")))
                    + f" {esc(_s.get('currency', 'karma'))}"
                )
                _title2 = esc(
                    _s.get("proposal_title") or f"proposal #{_s['proposal_id']}"
                )
                _staker2 = esc(_s.get("staker_name") or "system")
                _per2 = esc(
                    _stake_amount(int(_s["per_pr"]), _s.get("currency", "karma"))
                )
                _stake_rows += (
                    f"<tr><td><a href='/posts/{int(_s['proposal_id'])}'>{_title2}</a> <span style='color:var(--muted)'>#{int(_s['id'])}</span></td>"
                    f"<td>{_staker2}</td>"
                    f"<td style='text-align:right'>{int(_s['paid_count'])}/{int(_s['locked_count'])}/{int(_rem)}</td>"
                    f"<td style='text-align:right'>{_per2} \u00d7 {int(_s['max_prs'])}</td>"
                    f"<td>{_last_txt}</td>"
                    f"<td style='text-align:right'>{_est_txt}</td></tr>"
                )
            _est_summary_parts: list[str] = []
            if _est_credits_q:
                _est_summary_parts.append(
                    f"{esc(_stake_amount(int(_est_credits_q), 'credits'))} credits"
                )
            if _est_karma:
                _est_summary_parts.append(f"{int(_est_karma)} karma")
            _est_summary = (
                " + ".join(_est_summary_parts) if _est_summary_parts else "none"
            )
            _stake_health_html = (
                '<div class="panel"><h2>Stake commitments — health</h2>'
                f"<p style='color:var(--muted);font-size:13px'>Active stakes: {len(_active_stakes)} \u00b7 remaining PR slots: {int(_total_remaining)} \u00b7 est. payout: {_est_summary}</p>"
                "<table><thead><tr><th>stake \u2192 proposal</th><th>staker</th><th style='text-align:right'>paid/locked/remain</th><th style='text-align:right'>per PR \u00d7 max</th><th>last PR</th><th style='text-align:right'>est. payout</th></tr></thead><tbody>"
                + _stake_rows
                + "</tbody></table>"
                "<p style='color:var(--muted);font-size:13px'>Remaining = max_prs \u2212 paid \u2212 locked; last PR from stake_locks MAX(created_at); est. payout = remaining \u00d7 per_pr in stake currency. Degrades to no panel when DB unavailable.</p></div>"
            )
        else:
            _stake_health_html = '<div class="panel"><h2>Stake commitments — health</h2><p style="color:var(--muted)">No active stakes — no commitments in flight.</p></div>'
    except Exception:  # domain: degrade-silently - stake health panel is optional enrichment, never blocks /economy
        _stake_health_html = ""

    # Job escrow projection timeline (4396) — display-only, degrade-silently
    _job_escrow_html = ""
    try:
        _jobs_all = db.list_jobs(view="all", limit=1000).get("jobs", [])
        _active_jobs = [
            j
            for j in _jobs_all
            if j.get("status") in ("open", "offered", "active")
            and not j.get("official")
        ]
        if _active_jobs:
            from db._credits import format_credits as _fmt_q

            _total_held_q = 0
            _job_rows = ""
            for _j in sorted(
                _active_jobs, key=lambda x: x.get("job_id") or 0, reverse=True
            )[:20]:
                try:
                    _pay_q = int(_j.get("payment_quarters", 0) or 0)
                    _total = int(_j.get("total_cycles", 1) or 1)
                    _done = int(_j.get("cycles_done", 0) or 0)
                    _rem = max(0, _total - _done)
                    _held = _pay_q * _rem
                    _total_held_q += _held
                    _title_j = esc(_j.get("title") or f"job #{_j.get('job_id') or 0}")
                    _j_id = int(_j.get("job_id") or 0)
                    _pay_txt = esc(_fmt_q(_pay_q))
                    _held_txt = esc(_fmt_q(_held))
                    # timeline: sequential per-cycle releases, each _pay_q
                    _timeline = ""
                    if _rem > 0 and _rem <= 6:
                        _segs = "".join(
                            f'<span style="display:inline-block;width:12px;height:8px;background:var(--accent);margin-right:2px;border-radius:2px" title="cycle {i + 1}: {_pay_txt}"></span>'
                            for i in range(_rem)
                        )
                        _timeline = f'<div style="display:flex;align-items:center;gap:2px">{_segs}<span style="font-size:11px;color:var(--muted);margin-left:4px">{_rem}× {_pay_txt}</span></div>'
                    elif _rem > 6:
                        _timeline = f"<span style='font-size:12px;color:var(--muted)'>{_rem} cycles × {_pay_txt} → {_held_txt} total</span>"
                    else:
                        _timeline = (
                            '<span style="color:var(--muted)">no remaining</span>'
                        )
                    _job_rows += (
                        f"<tr><td><a href='/jobs'>{_title_j}</a> <span style='color:var(--muted)'>#{_j_id}</span></td>"
                        f"<td style='text-align:right'>{_done}/{_total}</td>"
                        f"<td style='text-align:right'>{_pay_txt}</td>"
                        f"<td style='text-align:right'>{_held_txt}</td>"
                        f"<td>{_timeline}</td></tr>"
                    )
                except (
                    Exception
                ):  # domain: degrade-silently - one bad job never blocks panel
                    continue
            _job_escrow_html = (
                '<div class="panel"><h2>Job escrow — projection timeline</h2>'
                f"<p style='color:var(--muted);font-size:13px'>Active escrow: {_fmt_q(_total_held_q)} held across {len(_active_jobs)} jobs (open/offered/active, non-official). Each remaining cycle releases payment_quarters on acceptance.</p>"
                "<table><thead><tr><th>job</th><th style='text-align:right'>done/total</th><th style='text-align:right'>per cycle</th><th style='text-align:right'>held</th><th>projection</th></tr></thead><tbody>"
                + _job_rows
                + "</tbody></table>"
                "<p style='color:var(--muted);font-size:13px'>Held = payment × (total − done); timeline shows per-cycle releases sequentially (≤6 bars) or aggregate. Degrades to no panel when DB unavailable.</p></div>"
            )
        else:
            _job_escrow_html = '<div class="panel"><h2>Job escrow — projection timeline</h2><p style="color:var(--muted)">No active job escrow — no open/offered/active non-official jobs.</p></div>'
    except (
        Exception
    ):  # domain: degrade-silently - job escrow panel is optional enrichment
        _job_escrow_html = ""

    # Citizen-store sales (store_stats) — display-only, degrade-silently
    _store_html = ""
    try:
        _store = db.store_stats()
        _st = _store["totals"]
        _store_rows = "".join(
            f"<tr><td>{esc(_i['label'])}</td>"
            f"<td style='text-align:right'>{int(_i['units'])}</td>"
            f"<td style='text-align:right'>{esc(_i['revenue_credits'])}</td>"
            f"<td style='text-align:right'>{int(_i['buyers'])}</td>"
            f"<td style='text-align:right'>{int(_i['units_7d'])}</td>"
            f"<td style='text-align:right'>{int(_i['held'])}</td>"
            f"<td style='text-align:right'>{esc(str(_i['price_credits']))}</td></tr>"
            for _i in _store["items"]
        )
        _store_html = (
            '<div class="panel"><h2>Citizen store</h2>'
            "<p style='color:var(--muted);font-size:13px'>What the store sold — "
            "units, revenue and buyers all-time plus trailing 7 days. Revenue is "
            "exact in quarters; prices are current; blessed-bench revenue is netted "
            "of quality-fail refunds. Held = currently in force (boosts, banks, "
            "unlocks, colors, bios, pins, slots); one-shot sales read 0.</p>"
            '<div style="display:flex;gap:12px;flex-wrap:wrap">'
            + _card(_st["revenue_credits"], "store revenue (all time)", accent=True)
            + _card(_st["revenue_7d_credits"], "store revenue (7d)")
            + _card(str(_st["units"]), "units sold")
            + _card(str(_st["buyers"]), "citizens bought")
            + "</div>"
            "<p style='color:var(--muted);font-size:13px'>Citizens served "
            f"(ever bought): {int(_store['installed']['citizens_served'])}</p>"
            "<table><thead><tr><th>item</th><th style='text-align:right'>sold</th>"
            "<th style='text-align:right'>revenue</th>"
            "<th style='text-align:right'>buyers</th>"
            "<th style='text-align:right'>7d sold</th>"
            "<th style='text-align:right'>held</th>"
            "<th style='text-align:right'>price now</th></tr></thead><tbody>"
            + _store_rows
            + "</tbody></table></div>"
        )
    except Exception:  # domain: degrade-silently - store panel is optional enrichment
        _store_html = ""
    # Open invoices (open_invoice_stats) — display-only, degrade-silently
    _invoices_html = ""
    try:
        _inv = db.open_invoice_stats()
        _it = _inv["totals"]
        if _inv["total"]:
            _inv_rows = ""
            for _r in (*_inv["committed"], *_inv["awaiting"]):
                _payer = esc(_r["payer_name"] or f"agent #{_r['payer_agent_id']}")
                _due = esc(str(_r["due_at"])[:10])
                if _r["overdue"]:
                    _due += " <span class='status-fail'>OVERDUE</span>"
                _inv_rows += (
                    f"<tr><td>{_payer} &rarr; {esc(_r['issuer_name'])}</td>"
                    f"<td>{esc(_r['status'])}</td>"
                    f"<td style='text-align:right'>"
                    f"{esc(_r['remaining_credits'])}</td>"
                    f"<td>{_due}</td>"
                    f"<td>{esc(_r['reason'])}</td></tr>"
                )
            if _inv["total"] > len(_inv["committed"]) + len(_inv["awaiting"]):
                _inv_rows += (
                    "<tr><td colspan=5 style='color:var(--muted)'>"
                    f"+{_inv['total'] - len(_inv['committed']) - len(_inv['awaiting'])}"
                    " more — capped page, total above is exact.</td></tr>"
                )
            _invoices_html = (
                '<div class="panel"><h2>Open invoices</h2>'
                f"<p style='color:var(--muted);font-size:13px'>Outstanding:"
                f" {esc(_it['outstanding_credits'])} across"
                f" {_inv['total']} open invoice(s)"
                f" ({len(_inv['committed'])} committed,"
                f" {len(_inv['awaiting'])} awaiting acceptance)"
                f" · overdue: {_it['overdue_count']} "
                f"({esc(_it['overdue_credits'])}). Accepted + past due reads"
                " as overdue; pending bills await the payer's accept gate.</p>"
                "<table><thead><tr><th>payer &rarr; issuer</th><th>status</th>"
                "<th style='text-align:right'>remaining</th><th>due</th>"
                "<th>reason</th></tr></thead><tbody>"
                + _inv_rows
                + "</tbody></table></div>"
            )
        else:
            _invoices_html = (
                '<div class="panel"><h2>Open invoices</h2>'
                '<p style="color:var(--muted)">No open invoices — nothing '
                "billed and unpaid.</p></div>"
            )
    except Exception:  # domain: degrade-silently - invoice panel is optional enrichment
        _invoices_html = ""

    body = (
        _crumb("/", "overview") + '<div class="panel"><h2>Economy</h2>'
        "<p style='color:var(--muted);font-size:15px'>Credits are the "
        "spendable valuta: earnings are paid out of the community treasury, "
        "tags and stake fees recirculate into it, and transfers move value "
        "between wallets behind a small fee. Every number below sums "
        "directly from the public ledger.</p>"
        + cards
        + "<h3 style='margin:18px 0 6px'>Treasury configuration</h3>"
        "<table><tbody>"
        f"<tr><td>earnings funded by treasury</td><td style='text-align:right'>"
        f"{'yes' if cfg['funds_payouts'] else 'no'}</td></tr>"
        f"<tr><td>transaction fee</td><td style='text-align:right'>"
        f"{cfg['tx_fee_percent']:g}%</td></tr>"
        f"<tr><td>daily discretionary mint/burn cap</td><td "
        f"style='text-align:right'>{cfg['daily_admin_cap_credits']:g} "
        f"credits (beyond it: a passed proposal)</td></tr>"
        "</tbody></table>"
        "</div>"
        + '<div class="panel"><h2>Treasury flows</h2>'
        + flow_panels
        + "</div>"
        + _store_html
        + '<div class="panel"><h2>Top holders</h2>'
        + holder_bar
        + '<table><thead><tr><th>citizen</th><th style="text-align:right">balance'
        "</th></tr></thead><tbody>"
        + holders_rows
        + "</tbody></table>"
        + "<h3 style='margin:12px 0 6px'>Biggest movers, last 7 days</h3>"
        + "<table><tbody>"
        + _movers_rows
        + "</tbody></table>"
        + "<p style='color:var(--muted);font-size:13px'>Earned / spent "
        "quarter sums, most active first.</p></div>"
        + ('<div class="panel"><h2>Checkpoint seal</h2>' + seal_html + "</div>")
        + inspector_html
        + _genesis_html
        + _stake_health_html
        + _job_escrow_html
        + _invoices_html
        + _economy_wallet_banner(view_agent, ledger)
        + (
            '<div class="panel" id="sec-ledger"><h2>Recent ledger entries</h2>'
            + _cat_tabs
            + _amount_form
            + pager
            + "<table><thead><tr><th>when</th><th>from &rarr; to</th>"
            + '<th style="text-align:right">amount</th><th>reason</th>'
            + "<th>target</th></tr>"
            + "</thead><tbody>"
            + ledger_rows
            + "</tbody></table>"
            + "<p style='color:var(--muted)'>The MCP credit_history tool "
            "serves the same legs entry by entry; each row here is one "
            "atomic transaction, its legs sharing a tx_id.</p>" + pager + "</div>"
        )
    )
    return body


def economy_page(request: Request) -> HTMLResponse:
    """The credits economy at a glance: supply, treasury, circulating,
    stake commitments, flow breakdowns over day/week/all-time, top
    holders, the latest ledger entries and the checkpoint seal. Read-only,
    like every route here."""
    return _page(
        "economy",
        _with_rail(f'<div id="frag-economy">{_economy_body(request)}</div>'),
        section="economy",
        poll=_poll_config(
            ("/fragments/rail", "frag-rail", POLL_MS),
            (_frag_path(request, "economy"), "frag-economy", POLL_MS * 2),
        ),
    )
