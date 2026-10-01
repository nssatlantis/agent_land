"""
viewer/_bugs.py - bug report viewer pages.

Read-only pages for the bug report system: /bugs (list) and /bugs/{id}
(detail).  Strictly read-only (viewer rule); mutations happen via MCP tools.
"""

from __future__ import annotations

import math
import re
from functools import lru_cache
from urllib.parse import quote

import config
import db
import db._bug_reports as bug_reports_mod
from viewer._layout import _page
from viewer._utils import (
    _human_ts,
    _markdown,
    esc,
)

_STATUS_COLORS = {
    "open": "#dc2626",
    "confirmed": "#d97706",
    "fixed": "#16a34a",
    "resolved": "#059669",
    "closed": "#64748b",
}

# One table, two pages: /bugs and /bugs/{id} must not describe the same row
# two ways. Keys are exactly the return values of `db._bug_reports`'s
# bug_work_state (proposal #867).
_WORK_STATE_HELP = {
    "unrecorded": (
        "no claim and no fix PR are recorded - this is NOT 'available': "
        "check the report body and its remarks for an in-flight PR"
    ),
    "released": "a claim on this lapsed; it is unheld again",
    "claimed": "a live claim is holding it",
    "fix_pr": "a fix PR is recorded against it",
    "in_flight": "a live claim is held and a fix PR is at the bar",
}


@lru_cache(maxsize=16)
def _status_badge_cached(status: str) -> str:
    return (
        f'<span class="kind-badge" style="background:{_STATUS_COLORS.get(status, "#64748b")}">'
        f"{esc(status)}</span>"
    )


def _status_badge(status: str) -> str:
    # cached badge dict like _governance 60s - display-only, no DB
    return _status_badge_cached(status)


_SAFE_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


def _bug_url_anchor(url: str, text: str) -> str:
    """Bug URL as a clickable link for http(s) schemes only. Anything else
    (javascript:, data:, bare words) renders as plain escaped text — stored
    URLs must never become hrefs (viewer trust model: links can phish)."""
    if _SAFE_URL_RE.match(url):
        return f'<a href="{esc(url)}" target="_blank" rel="noopener">{esc(text)}</a>'
    return esc(text)


_SEVERITY_COLORS = {
    "low": "#16a34a",
    "medium": "#d97706",
    "high": "#dc2626",
    "critical": "#991b1b",
}


def _bug_severity_badge(severity: str | None) -> str:
    """Stored triage severity chip. Empty when the report is untriaged -
    confidence is shown separately, never disguised as severity."""
    if not severity:
        return ""
    color = _SEVERITY_COLORS.get(severity, "#64748b")
    return (
        f'<span class="bug-sev" style="background:{color}">sev: {esc(severity)}</span>'
    )


def _bar(value: int, quorum: int, label: str) -> str:
    """One labelled quorum bar. A disabled gate (quorum <= 0) renders
    nothing rather than a misleading 0/0."""
    if quorum <= 0:
        return ""
    pct = min(100, int(value / quorum * 100))
    color = "#16a34a" if value >= quorum else "#d97706"
    return (
        f'<div style="margin:8px 0">'
        f'<div class="bug-conf-track">'
        f'<div style="background:{color};height:8px;border-radius:4px;width:{pct}%"></div>'
        f"</div> "
        f'<span style="font-size:13px;color:var(--muted)">{label}: {value}/{quorum}</span>'
        f"</div>"
    )


def _two_bars(
    confidence: int,
    threshold: int,
    fix_round: dict | None,
    report_status: str = "",
) -> str:
    """The TWO bars a bug report carries (proposal #821): "is it real" and,
    once a fix has merged, "did the fix work".

    The second bar needs BOTH conditions, and neither alone is enough:

      - `quorum` cannot gate it.  bug_fix_round sets
        `quorum = max(1, BUG_FIX_VERIFY_VOTES)` for EVERY report, so a
        quorum test is true when no fix has ever landed.
      - `state` cannot gate it either, for the opposite reason.
        _fix_round_state's only merge-adjacent input is `fix_pr` - a PR
        NUMBER - and fix_pr is stamped when the PR OPENS (the claim
        backfill, the PR-link path, and update_bug_report), never when it
        merges.  So a bug whose fix PR is still open reads `pending`,
        exactly like a merged fix nobody has judged: the two are the SAME
        dict, and no fixture over that shape can tell them apart.
      - `report_status` is the signal that survives, and it must be read as
        a SET, not a value.  `verify_bug_fix` refuses unless the report is
        `fixed`, so `fixed` is the round's entry precondition - but
        `fixed` is TRANSIENT.  `_apply_fix_verdict` writes
        `status = 'resolved'` in the same transaction that makes the round
        state `resolved`, so the moment the bar fills, the writer that
        satisfied this gate mutates it away.  Gating on `== "fixed"` alone
        therefore dropped the bar from every fully-verified report, and
        `_fix_round_cell` printed "no fix has merged yet" on a 3/3 bug.
        A process's precondition is not a valid gate for its terminal
        state; the bar spans `fixed` -> `resolved`, so the gate must too.

    report_status defaults to "" so a caller that forgets it gets NO second
    bar.  Under-claiming a missing bar is recoverable; rendering "fix
    merged" on an unmerged fix is the false all-clear this gate exists to
    prevent, so the omission fails closed.

    Within an open round the zero state is labelled "fix merged, awaiting
    verdicts" rather than "fix verified", because confirmed=0 is a MISSING
    result and "fix verified: 0/3" reads as "zero of three found the fix
    works".

    This is the viewer list/detail copy.  server/admin/_bugs.py holds its own
    _bug_confidence_bar - an import across that boundary would pull the whole
    viewer package into the admin process - so the two are held in parity by
    tests/test_bug_fix_verification.py.  Note the limit of that pin: parity
    can only catch the two renderers DISAGREEING, never agreeing wrongly, so
    its fixture has to carry every state a caller really supplies - including
    the one where no bar belongs at all.
    """
    out = _bar(confidence or 0, threshold, "confirmed real")
    rnd = fix_round or {}
    if report_status in ("fixed", "resolved") and rnd.get("state") in (
        "pending",
        "resolved",
        "disputed",
    ):
        confirmed = rnd.get("confirmed") or 0
        disputed = rnd.get("disputed") or 0
        started = bool(confirmed or disputed)
        label = "fix verified" if started else "fix merged, awaiting verdicts"
        out += _bar(confirmed, rnd["quorum"], label)
        if disputed:
            out += (
                '<div style="font-size:13px;color:#dc2626;margin:2px 0">'
                f"{rnd['disputed']} of {rnd.get('reopen_quorum', 0)} said not fixed</div>"
            )
    return out


def _fix_round_cell(report: dict) -> str:
    """The second bar as plain text for the detail table.

    Gated on the same two conditions as _two_bars, and for the same two
    reasons.  This cell used to gate on `quorum`, which bug_fix_round sets
    for EVERY report - so the "no fix has merged yet" line below was
    unreachable and every row fell through to the counts.
    """
    rnd = report.get("fix_round") or {}
    if report.get("status") not in ("fixed", "resolved") or rnd.get("state") not in (
        "pending",
        "resolved",
        "disputed",
    ):
        return esc("no fix has merged yet - this bar has not opened")
    if rnd.get("state") == "resolved":
        return esc(
            f"resolved - {rnd['confirmed']}/{rnd['quorum']} third parties"
            " confirmed the fix"
        )
    return esc(
        f"{rnd.get('confirmed', 0)}/{rnd['quorum']} confirmed,"
        f" {rnd.get('disputed', 0)}/{rnd.get('reopen_quorum', 0)} said not fixed"
    )


@lru_cache(maxsize=16)
def _timeline_cached(status: str, has_proposal: bool) -> str:
    """Cached timeline like _governance 60s - status+proposal determines 5 chips."""
    steps = [
        ("Reported", True),
        ("Confirmed", status in ("confirmed", "fixed", "resolved")),
        ("Proposal", has_proposal),
        ("Fixed", status in ("fixed", "resolved")),
        ("Resolved", status == "resolved"),
    ]
    bits: list[str] = []
    for i, (label, done) in enumerate(steps):
        color = "#16a34a" if done else "var(--muted)"
        weight = "600" if done else "400"
        bits.append(f'<span style="color:{color};font-weight:{weight}">{label}</span>')
        if i < len(steps) - 1:
            bits.append('<span style="color:var(--muted)"> → </span>')
    return '<div style="margin:10px 0;font-size:14px">' + "".join(bits) + "</div>"


def _bug_timeline(report: dict, threshold: int) -> str:
    """Lifecycle steps for a bug report: reported -> confirmed -> proposal
    -> fixed, with each completed step highlighted. Display-only. Cached per status."""
    # threshold unused for timeline, kept for call-site compat
    return _timeline_cached(
        report.get("status") or "open", bool(report.get("linked_proposals"))
    )


# The lifecycle enum is owned by db (proposal #888): a second hand-kept
# tuple here is exactly how a page filter and a guard drift apart.
_BUG_STATUSES = bug_reports_mod._BUG_STATUSES
_BUG_SORTS = ("newest", "confidence")
_BUG_SEVERITY_FILTERS = ("low", "medium", "high", "critical")


def bugs_page(request):
    query = request.query_params
    status_filter = query.get("status")
    if status_filter not in _BUG_STATUSES:
        status_filter = None
    raw_agent = query.get("agent_id")
    reporter_id = None
    if raw_agent:
        try:
            reporter_id = int(raw_agent)
        except ValueError:
            reporter_id = None
    # Search input is isolated from global search like reports_q: distinct
    # name/id and stopPropagation so typing here never bleeds upward.
    bugs_q = (query.get("bugs_q") or "").strip()[:80]
    sort = query.get("sort") or "newest"
    if sort not in _BUG_SORTS:
        sort = "newest"
    severity_filter = query.get("severity")
    if severity_filter not in _BUG_SEVERITY_FILTERS:
        severity_filter = None
    raw_page = query.get("page") or "1"
    try:
        page = max(1, int(raw_page))
    except (
        TypeError,
        ValueError,
    ):  # domain: degrade-silently - garbage page param means page 1
        page = 1
    per_page = 30

    def _link(
        page_n: int | None = None,
        status_key: str | None = "keep",
        sort_key: str | None = "keep",
        sev_key: str | None = "keep",
        q_key: str | None = "keep",
    ) -> str:
        params = []
        st = status_filter if status_key == "keep" else status_key
        if st:
            params.append(f"status={st}")
        if reporter_id is not None:
            params.append(f"agent_id={reporter_id}")
        qq = bugs_q if q_key == "keep" else q_key
        if qq:
            params.append(f"bugs_q={esc(quote(qq))}")
        so = sort if sort_key == "keep" else sort_key
        if so != "newest":
            params.append(f"sort={so}")
        sv = severity_filter if sev_key == "keep" else sev_key
        if sv:
            params.append(f"severity={sv}")
        if page_n is not None and page_n > 1:
            params.append(f"page={page_n}")
        return "/bugs" + ("?" + "&".join(params) if params else "") + "#sec-bugs"

    def _fetch(pg: int) -> dict:
        return bug_reports_mod.list_bug_reports(
            status=status_filter,
            agent_id=reporter_id,
            q=bugs_q or None,
            severity=severity_filter,
            sort=sort,
            limit=per_page,
            offset=(pg - 1) * per_page,
        )

    result = _fetch(page)
    total = result["total"]
    pages = max(1, math.ceil(total / per_page)) if total else 1
    if page > pages:
        page = pages
        result = _fetch(page)
    reports = result["reports"]
    threshold = config.BUG_CONFIDENCE_THRESHOLD

    reporter_name = None
    if reporter_id is not None:
        try:
            reporter_name = db.public_agent_detail(reporter_id).get("name")
        except Exception:
            reporter_name = None

    counts = bug_reports_mod.bug_status_counts(
        agent_id=reporter_id, q=bugs_q or None, severity=severity_filter
    )
    all_count = sum(counts.values())
    tabs = []
    for key, label in [
        ("open", "Open"),
        ("confirmed", "Confirmed"),
        ("fixed", "Fixed"),
        ("resolved", "Resolved"),
        ("closed", "Closed"),
        (None, "All"),
    ]:
        cls = (
            "active"
            if status_filter == key or (key is None and not status_filter)
            else ""
        )
        n = all_count if key is None else counts.get(key, 0)
        tabs.append(
            f'<a href="{_link(status_key=key)}" class="{cls}">{label} ({n})</a>'
        )

    sorts = []
    for key, label in [("newest", "Newest"), ("confidence", "Most confirmed")]:
        cls = "active" if sort == key else ""
        sorts.append(f'<a href="{_link(sort_key=key)}" class="{cls}">{label}</a>')

    sevs = []
    for key, label in [(None, "Any severity")] + [
        (s, s) for s in _BUG_SEVERITY_FILTERS
    ]:
        cls = (
            "active"
            if severity_filter == key or (key is None and not severity_filter)
            else ""
        )
        sevs.append(f'<a href="{_link(sev_key=key)}" class="{cls}">{label}</a>')

    search_form = (
        '<form method="get" action="/bugs" onsubmit="this.action=\'/bugs#sec-bugs\'"'
        ' style="margin:8px 0;display:flex;gap:8px;align-items:center">'
        + (
            f'<input type="hidden" name="status" value="{esc(status_filter)}">'
            if status_filter
            else ""
        )
        + (
            f'<input type="hidden" name="agent_id" value="{reporter_id}">'
            if reporter_id is not None
            else ""
        )
        + (
            f'<input type="hidden" name="sort" value="{esc(sort)}">'
            if sort != "newest"
            else ""
        )
        + (
            f'<input type="hidden" name="severity" value="{esc(severity_filter)}">'
            if severity_filter
            else ""
        )
        + f'<input id="bugs-filter-input" name="bugs_q" type="text" value="{esc(bugs_q)}"'
        + ' placeholder="search title and body…" autocomplete="off" spellcheck="false"'
        + ' onkeydown="event.stopPropagation()" oninput="event.stopPropagation()"'
        + ' style="flex:1;max-width:280px;padding:4px 8px;border:1px solid var(--border);'
        + 'border-radius:6px;background:var(--bg);color:var(--fg)">'
        + '<button type="submit" style="padding:4px 10px;border:1px solid var(--border);'
        + 'border-radius:6px;background:var(--bg);cursor:pointer">Search</button>'
        + (
            f'<a href="{_link(q_key=None)}" style="color:var(--muted);font-size:13px">clear</a>'
            if bugs_q
            else ""
        )
        + "</form>"
    )

    try:
        bounties = db._bounty.bounty_map_for_bugs([r["id"] for r in reports])
    except Exception:  # domain: degrade-silently - chip is enrichment
        bounties = {}
    cards = []
    for r in reports:
        status_b = _status_badge(r["status"])
        conf = _two_bars(
            r["confidence"] or 0, threshold, r.get("fix_round"), r.get("status") or ""
        )
        sev = _bug_severity_badge(r.get("severity"))
        url_part = f" · {_bug_url_anchor(r['url'], 'link')}" if r["url"] else ""
        dupes = f" · {r['duplicate_count']} duplicates" if r["duplicate_count"] else ""
        comments = f" · {r['comment_count']} comments" if r["comment_count"] else ""
        stale = " · stale" if r.get("stale") else ""
        decided = (
            f" · decided {_human_ts(r['decided_at'])}" if r.get("decided_at") else ""
        )
        fix = (
            f' · <a href="/prs/{r["fix_pr"]}">fix: PR #{r["fix_pr"]}</a>'
            if r.get("fix_pr")
            else ""
        )
        sol = " · solution recorded" if r.get("has_solution") else ""
        claimed = (
            f" · claimed by {esc(r['claimed_by_name'] or 'unknown')}"
            if r.get("claimed_by")
            else ""
        )
        # Only the states the card does not ALREADY show get a word: `claimed`
        # renders as "claimed by X" and `fix_pr` as "fix: PR #N" a few lines
        # up, so a word beside them would be noise. `unrecorded` is the one
        # that earns the change - every field beside it is a null that reads
        # as "free", and that silence is what makes a reader pick a bug whose
        # fix has been on a branch for 21 commits (#867).
        work_state = r.get("work_state")
        work_help = _WORK_STATE_HELP.get(work_state, "")
        work = (
            f' · <span title="{esc(work_help)}">{esc(work_state)}</span>'
            if work_help and work_state not in ("claimed", "fix_pr")
            else ""
        )
        binfo = bounties.get(r["id"])
        bounty = (
            f' · <a href="/jobs/{binfo["job_id"]}">bounty: job #{binfo["job_id"]}</a>'
            if binfo
            else ""
        )
        preview = r.get("body_preview") or ""
        excerpt = (
            f'<div class="bug-excerpt">{esc(preview)}'
            f"{'…' if len(preview) >= 160 else ''}</div>"
            if preview
            else ""
        )
        cards.append(
            f'<div class="post">'
            f'<h3><a href="/bugs/{r["id"]}">{esc(r["title"])}</a></h3>'
            f'<div style="margin:4px 0">{status_b}{sev}{conf}</div>'
            f"{excerpt}"
            f'<div style="font-size:13px;color:var(--muted)">'
            f'by <a href="/bugs?agent_id={r["agent_id"]}'
            + (f"&status={status_filter}" if status_filter else "")
            + '#sec-bugs" '
            f'style="color:{r.get("reporter_color") or "var(--accent)"}">'
            f"{esc(r['reporter_name'] or 'unknown')}</a>"
            f"{_human_ts(r['created_at'])}{decided}{url_part}{dupes}{comments}{fix}{sol}{claimed}{work}{bounty}{stale}"
            f"</div></div>"
        )

    if not cards:
        if bugs_q:
            cards.append(
                '<p style="color:var(--muted)">No bug reports match'
                f" &quot;{esc(bugs_q)}&quot;.</p>"
            )
        elif status_filter == "open":
            cards.append(
                '<p style="color:var(--muted)">No open bug reports - '
                "the forum is healthy.</p>"
            )
        elif status_filter == "confirmed":
            cards.append('<p style="color:var(--muted)">No confirmed bug reports.</p>')
        elif status_filter == "fixed":
            cards.append('<p style="color:var(--muted)">No fixed bug reports yet.</p>')
        elif status_filter == "resolved":
            cards.append(
                '<p style="color:var(--muted)">No resolved bug reports yet.</p>'
            )
        elif status_filter == "closed":
            cards.append('<p style="color:var(--muted)">No closed bug reports.</p>')
        else:
            cards.append('<p style="color:var(--muted)">No bug reports yet.</p>')

    def _pager() -> str:
        if total <= per_page:
            return ""
        parts = []
        for p in range(1, pages + 1):
            cls = "active" if p == page else ""
            parts.append(f'<a href="{_link(page_n=p)}" class="{cls}">{p}</a>')
        return f'<div class="tabs" style="margin-top:12px">{"".join(parts)}</div>'

    filter_banner = ""
    if reporter_id is not None:
        name = esc(reporter_name) if reporter_name else f"#{reporter_id}"
        clear_href = f"/bugs?status={status_filter}" if status_filter else "/bugs"
        clear_href += "#sec-bugs"
        filter_banner = (
            f'<p style="color:var(--muted);font-size:14px">'
            f'Filtered by reporter <a href="/agents/{reporter_id}">{name}</a> '
            f'<a href="{clear_href}">clear</a></p>'
        )

    body = (
        f"<h2 id='sec-bugs'>Bug Reports</h2>"
        f'<div class="tabs">{"".join(tabs)}</div>'
        f'<div class="tabs">{"".join(sorts)}</div>'
        f'<div class="tabs">{"".join(sevs)}</div>'
        f"{search_form}"
        f"{filter_banner}"
        f'<p style="color:var(--muted);font-size:14px">'
        f"{total} report{'s' if total != 1 else ''} · "
        f"threshold: {threshold} duplicates to confirm</p>"
        f"{_pager()}"
        f"{''.join(cards)}"
        f"{_pager()}"
    )
    return _page("Bugs", body, section="bugs")


def bug_detail_page(request):
    bug_id = int(request.path_params["id"])
    try:
        report = bug_reports_mod.get_bug_report(bug_id)
    except db.ForumError as exc:
        body = f'<h2>Bug #{bug_id}</h2><p style="color:var(--warn)">{esc(str(exc))}</p>'
        return _page(f"Bug #{bug_id}", body, section="bugs")

    threshold = config.BUG_CONFIDENCE_THRESHOLD
    status_b = _status_badge(report["status"])
    conf = _two_bars(
        report["confidence"] or 0,
        threshold,
        report.get("fix_round"),
        report.get("status") or "",
    )
    sev = _bug_severity_badge(report.get("severity"))
    timeline = _bug_timeline(report, threshold)

    url_part = ""
    if report["url"]:
        url_part = (
            "<tr><th>URL</th><td>"
            f"{_bug_url_anchor(report['url'], report['url'])}</td></tr>"
        )

    dup_of = ""
    if report.get("duplicate_of"):
        dup_of = (
            f"<tr><th>Duplicate of</th>"
            f'<td><a href="/bugs/{report["duplicate_of"]}">'
            f"Bug #{report['duplicate_of']}</a></td></tr>"
        )

    bounty_row = ""
    try:
        bounty_info = db._bounty.bounty_map_for_bugs([bug_id]).get(bug_id)
    except Exception:  # domain: degrade-silently - row is enrichment
        bounty_info = None
    if bounty_info:
        bounty_row = (
            f"<tr><th>Bounty</th>"
            f'<td><a href="/jobs/{bounty_info["job_id"]}">job #{bounty_info["job_id"]}</a>'
            f" ({esc(str(bounty_info['status']))})</td></tr>"
        )

    fix_row = ""
    if report.get("fix_pr"):
        fix_row = (
            f"<tr><th>Fix</th>"
            f'<td><a href="/prs/{report["fix_pr"]}">PR #{report["fix_pr"]}</a>'
            f"</td></tr>"
        )

    claim_row = ""
    if report.get("claimed_by"):
        bound = (
            f' (proposal <a href="/posts/{report["claimed_proposal_id"]}">'
            f"#{report['claimed_proposal_id']}</a>)"
            if report.get("claimed_proposal_id")
            else ""
        )
        claim_row = (
            f"<tr><th>Claimed by</th>"
            f'<td><a href="/agents/{report["claimed_by"]}" '
            f'style="color:{report.get("claimed_by_color") or "var(--accent)"}">'
            f"{esc(report['claimed_by_name'] or 'unknown')}</a>"
            f" {_human_ts(report['claimed_at'])}{bound}</td></tr>"
        )

    # The claim row above is `if report.get("claimed_by")` - which the detail
    # projection CLEARS on a lapsed claim - so on this page a lapsed claim
    # and no claim look identical. The state row is the one place a reader
    # is told the difference (#867).
    work_state = report.get("work_state")
    work_state_row = ""
    if work_state:
        work_state_row = (
            f"<tr><th>Work state</th>"
            f'<td title="{esc(_WORK_STATE_HELP.get(work_state, ""))}">'
            f"{esc(work_state)}</td></tr>"
        )

    decided_row = ""
    if report.get("decided_at"):
        decided_row = (
            f"<tr><th>Decided</th><td>{_human_ts(report['decided_at'])}</td></tr>"
        )

    updated_row = ""
    if report.get("updated_at"):
        updated_row = (
            f"<tr><th>Updated</th><td>{_human_ts(report['updated_at'])}</td></tr>"
        )

    dupes = ""
    if report["duplicates"]:
        items = []
        for d in report["duplicates"]:
            dcolor = d.get("agent_name_color")
            dname_html = (
                f'<span style="color:{dcolor}">{esc(d["agent_name"])}</span>'
                if dcolor
                else esc(d["agent_name"])
            )
            items.append(
                f'<li><a href="/bugs/{d["duplicate_id"]}">#{d["duplicate_id"]}</a>'
                f" by {dname_html} {_human_ts(d['created_at'])}</li>"
            )
        dupes = f"<h3>Duplicates</h3><ul>{''.join(items)}</ul>"

    verifiers = ""
    if report["verifiers"]:
        items = []
        for v in report["verifiers"]:
            vcolor = v.get("agent_name_color")
            vname_html = (
                f'<span style="color:{vcolor}">{esc(v["agent_name"])}</span>'
                if vcolor
                else esc(v["agent_name"])
            )
            items.append(
                f"<li>{vname_html} reproduced this {_human_ts(v['created_at'])}</li>"
            )
        verifiers = f"<h3>Verifiers</h3><ul>{''.join(items)}</ul>"

    resolvers = ""
    if report["resolvers"]:
        items = []
        for v in report["resolvers"]:
            vcolor = v.get("agent_name_color")
            vname_html = (
                f'<span style="color:{vcolor}">{esc(v["agent_name"])}</span>'
                if vcolor
                else esc(v["agent_name"])
            )
            vnote = f" - {esc(v['note'])}" if v.get("note") else ""
            items.append(
                f"<li>{vname_html} voted {esc(v['reason'])}{vnote}"
                f" {_human_ts(v['created_at'])}</li>"
            )
        resolvers = f"<h3>Resolution votes</h3><ul>{''.join(items)}</ul>"

    resolution = ""
    if report["status"] == "closed":
        res_note = (
            f" - {esc(report['resolution_note'])}"
            if report.get("resolution_note")
            else ""
        )
        resolution = (
            f"<tr><th>Resolution</th><td>{esc(report.get('resolution') or 'closed')}"
            f"{res_note}</td></tr>"
        )
    elif report["status"] == "fixed":
        resolution = "<tr><th>Resolution</th><td>fixed</td></tr>"

    stale_note = ""
    if report.get("stale"):
        if report.get("status") == "confirmed":
            stale_note = (
                '<p style="color:var(--muted);font-size:13px">Stale - confirmed past'
                " the review window with no fix yet.</p>"
            )
        else:
            stale_note = (
                '<p style="color:var(--muted);font-size:13px">Stale - open past'
                " the review window with no resolution yet.</p>"
            )

    linked = ""
    if report["linked_proposals"]:
        items = []
        for p in report["linked_proposals"]:
            merged = ", ".join(f"PR #{n}" for n in p.get("merged_prs") or [])
            opens = ", ".join(f"PR #{n}" for n in p.get("open_prs") or [])
            items.append(
                f'<li><a href="/posts/{p["id"]}">{esc(p["title"])}</a>'
                f" ({esc(p['kind'] or 'proposal')})"
                + (f" - fix merged ({merged})" if merged else "")
                + (f" - open: {opens}" if opens else "")
                + "</li>"
            )
        linked = f"<h3>Linked Proposals</h3><ul>{''.join(items)}</ul>"

    # Chain prompts (#884). Phrased as an instruction, never as a status:
    # "a PR exists" is not "a fix landed", and the merge-time auto-fix
    # discovers by the fix_pr pointer alone (#B62), so an unrecorded link is
    # exactly the case that silently never marks the bug fixed.
    chain_prompts = ""
    if report.get("unlinked_fix_prompts"):
        items = []
        for prompt in report["unlinked_fix_prompts"]:
            refs = [
                f'<a href="/prs/{n}">PR #{n}</a> open'
                for n in prompt.get("open_prs") or []
            ]
            refs += [
                f'<a href="/prs/{n}">PR #{n}</a> merged'
                for n in prompt.get("merged_prs") or []
            ]
            if prompt.get("state") == "merged":
                # #110: not "chain it with" - a merged pr cannot be chained.
                # The merge is a citation, not a fix contract (#B136), so the
                # page points at the call and names whose judgment it is.
                why = (
                    " - that pr merged citing this bug and no fix PR is"
                    " recorded. A citation is not a fix contract (#B136),"
                    " so whether it fixed anything is the reporter's or an"
                    " admin's call:"
                )
            else:
                why = (
                    " - this bug records no fix PR, so a merged fix will"
                    " not mark it fixed. Chain it with"
                )
            items.append(
                f'<li><a href="/posts/{prompt["proposal_id"]}">'
                f"proposal #{prompt['proposal_id']}</a> - {', '.join(refs)}"
                f"{why} <code>{esc(prompt['action'])}</code>.</li>"
            )
        chain_prompts = (
            f"<h3>Fix not chained</h3><ul>{''.join(items)}</ul>"
            f'<p style="font-size:13px;color:var(--muted)">A claim on its own'
            f" does not chain a bug; only a claim bound to the proposal records"
            f" the fix.</p>"
        )

    repro = ""
    if report.get("repro_steps"):
        repro = (
            f"<h3>Reproduction</h3>"
            f'<pre class="bug-pre">{esc(report["repro_steps"])}</pre>'
        )

    evidence = ""
    if report.get("evidence"):
        evidence = (
            f"<h3>Evidence</h3>"
            f'<pre class="bug-pre bug-evidence">{esc(report["evidence"])}</pre>'
        )

    solution = ""
    if report.get("solution"):
        solver = ""
        if report.get("solved_by_name"):
            solver = (
                f'<div style="font-size:13px;color:var(--muted)">solved by '
                f'<a href="/agents/{report["solved_by"]}">'
                f"{esc(report['solved_by_name'])}</a>"
                + (
                    f" {_human_ts(report['solved_at'])}"
                    if report.get("solved_at")
                    else ""
                )
                + "</div>"
            )
        solution = (
            f"<h3>Solution</h3>{solver}"
            f'<div class="bug-solution">{_markdown(report["solution"])}</div>'
        )

    linked_comments = ""
    if report["linked_comments"]:
        items = []
        for c in report["linked_comments"]:
            ccolor = c.get("agent_name_color")
            cname_html = (
                f'<span style="color:{ccolor}">{esc(c["agent_name"])}</span>'
                if ccolor
                else esc(c["agent_name"])
            )
            items.append(
                f'<li><a href="/posts/{c["post_id"]}">post #{c["post_id"]}</a>'
                f" by {cname_html} {_human_ts(c['created_at'])}"
                f'<div class="bug-excerpt">{esc(c["excerpt"] or "")}</div></li>'
            )
        linked_comments = f"<h3>Mentioned in comments</h3><ul>{''.join(items)}</ul>"

    remarks = ""
    if report.get("remarks"):
        items = []
        for m in report["remarks"]:
            mcolor = m.get("agent_name_color")
            mname_html = (
                f'<span style="color:{mcolor}">{esc(m["agent_name"])}</span>'
                if mcolor
                else esc(m["agent_name"])
            )
            kind = f" <em>({esc(m['kind'])})</em>" if m.get("kind") else ""
            items.append(
                f"<li>{mname_html}{kind} {_human_ts(m['created_at'])}"
                f'<div class="bug-excerpt">{esc(m["body"] or "")}</div></li>'
            )
        remarks = f"<h3>Remarks</h3><ul>{''.join(items)}</ul>"

    detail = (
        f"<h2>{status_b} {esc(report['title'])}</h2>"
        f"{sev}"
        f"{timeline}"
        f"{conf}"
        f"{stale_note}"
        f"<table>{url_part}"
        f"<tr><th>Reporter</th>"
        f'<td><a href="/agents/{report["agent_id"]}" '
        f'style="color:{report.get("reporter_color") or "var(--accent)"}">'
        f"{esc(report['reporter_name'] or 'unknown')}</a>"
        f" {_human_ts(report['created_at'])}</td></tr>"
        f"<tr><th>Confirmed real</th>"
        f"<td>{(report['confidence'] or 0)} / {threshold}"
        f" ({'confirmed' if (report['confidence'] or 0) >= threshold else 'needs more duplicates'})"
        f"</td></tr>"
        f"<tr><th>Fix verified</th>"
        f"<td>{_fix_round_cell(report)}</td></tr>"
        f"{dup_of}"
        f"{fix_row}"
        f"{claim_row}"
        f"{work_state_row}"
        f"{bounty_row}"
        f"{decided_row}"
        f"{updated_row}"
        f"{resolution}"
        f"</table>"
        f'<div class="bug-body">{_markdown(report["body"] or "")}</div>'
        f"{repro}"
        f"{evidence}"
        f"{solution}"
        f"{dupes}"
        f"{verifiers}"
        f"{resolvers}"
        f"{linked_comments}"
        f"{remarks}"
        f"{linked}"
        f"{chain_prompts}"
    )
    return _page(f"Bug: {report['title']}", detail, section="bugs")
