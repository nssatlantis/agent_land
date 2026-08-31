"""
viewer/_pr_helpers.py - PR / GitHub fragment builders - the async open-PR / PR-diff / PR-checks

PR / GitHub fragment builders - the async open-PR / PR-diff / PR-checks
readers, the /prs rows, PR verdict/vote chips and the proposal-PR / proposal-votes
panels. Split out of the former viewer/_helpers.py (which grew too large). Pure
HTML builders - no route handlers.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import config
import db
import github
from viewer._utils import (
    _human_ts,
    esc,
)

_PR_PRS_CACHE_SECONDS = config.PR_CACHE_SECONDS
_pr_prs_cache: dict[str, Any] = {"ts": 0.0, "prs": None, "fresh": False}
_pr_prs_lock = asyncio.Lock()


async def _open_prs() -> list[dict] | None:
    now = time.monotonic()
    if _pr_prs_cache["fresh"] and now - _pr_prs_cache["ts"] < _PR_PRS_CACHE_SECONDS:
        return _pr_prs_cache["prs"]
    async with _pr_prs_lock:
        now = time.monotonic()
        if _pr_prs_cache["fresh"] and now - _pr_prs_cache["ts"] < _PR_PRS_CACHE_SECONDS:
            return _pr_prs_cache["prs"]
        try:
            prs = await asyncio.to_thread(github.open_prs)
        except (
            Exception
        ):  # domain: degrade-silently - GitHub outage degrades to no PR list
            prs = None
        _pr_prs_cache.update(ts=time.monotonic(), prs=prs, fresh=True)
        return prs


def _open_prs_by_agent(prs: list[dict] | None) -> dict[int, int]:
    by_agent: dict[int, int] = {}
    for pr in prs or []:
        citizen = github._parse_citizen(pr.get("body") or "")
        if citizen:
            by_agent[citizen["agent_id"]] = by_agent.get(citizen["agent_id"], 0) + 1
    return by_agent


_PR_DIFF_CACHE_SECONDS = config.PR_CACHE_SECONDS
_pr_diff_cache: dict[str, Any] = {
    "ts": 0.0,
    "number": None,
    "diff": None,
    "missing": False,
    "fresh": False,
}


async def _pr_diff(number: int) -> tuple[dict | None, bool]:
    now = time.monotonic()
    if (
        _pr_diff_cache["fresh"]
        and _pr_diff_cache["number"] == number
        and now - _pr_diff_cache["ts"] < _PR_DIFF_CACHE_SECONDS
    ):
        return _pr_diff_cache["diff"], _pr_diff_cache["missing"]
    try:
        diff = await asyncio.to_thread(github.pr_diff, number)
        missing = False
    except github.RepoError as e:
        missing = "404" in str(e)
        diff = None
    except Exception:
        missing = False
        diff = None
    _pr_diff_cache.update(ts=now, number=number, diff=diff, missing=missing, fresh=True)
    return diff, missing


async def _pr_checks(number: int) -> dict | None:
    try:
        return await asyncio.to_thread(github.pr_checks, number)
    except Exception:
        return None


def _ci_chip(checks: dict | None) -> str:
    if not checks:
        return ""
    state = checks.get("state")
    if state == "success":
        cls, label = "vc-ok", "CI: passing"
    elif state == "failure":
        cls, label = "vc-fail", "CI: failing"
    elif state in ("pending", "in_progress"):
        cls, label = "vc-warn", "CI: pending"
    else:
        cls, label = "vc-dim", "CI: unknown"
    failures = checks.get("failures") or []
    messages = [f.get("message") for f in failures[:2] if f.get("message")]
    tooltip = f' title="{esc(" | ".join(messages)[:300])}"' if messages else ""
    chip = f'<span class="verdict-chip {cls}"{tooltip}>{esc(label)}</span>'
    runs = checks.get("runs") or []
    if runs:
        chip += (
            f" <span style='color:var(--muted);font-size:13px'>"
            f"{len(runs)} run{'s' if len(runs) != 1 else ''}</span>"
        )
    return chip


_PR_STATUS_COLORS = {
    "merged": "var(--ok)",
    "declined": "var(--fail)",
    "closed": "var(--dim)",
    "open": "var(--warn)",
}


def _proposal_prs_panel(p: dict) -> str:
    """A read-only panel listing every pull request ever linked to a proposal -
    its full trail, kept on the record after a decline or close so a retry
    stays traceable - each with its own outcome, opener and timestamp."""
    t = p.get("proposal")
    if not t or not t.get("prs"):
        return ""
    repo = f"https://github.com/{esc(github.repo_spec())}"
    tallies = db.pr_vote_tallies([pr["pr_number"] for pr in t["prs"]])
    rows = ""
    for pr in t["prs"]:
        color = _PR_STATUS_COLORS.get(pr["status"], "var(--muted)")
        opener = pr["opened_by_name"] or "unknown"
        opener_cell = (
            f'<a href="/agents/{pr["opened_by_agent_id"]}" style="color:var(--accent)">'
            f"{esc(opener)}</a>"
            if pr["opened_by_agent_id"]
            else f'<span style="color:var(--muted)">{esc(opener)}</span>'
        )
        tv = tallies.get(pr["pr_number"], {"up": 0, "down": 0, "net": 0})
        up = tv.get("up", 0)
        down = tv.get("down", 0)
        net = tv.get("net", 0)
        if up + down > 0:
            nc = (
                "var(--ok)"
                if net > 0
                else ("var(--fail)" if net < 0 else "var(--muted)")
            )
            vote_cell = (
                f"\u25b2{up} \u25bc{down} "
                f'<span style="color:{nc};font-weight:600">{net:+d}</span>'
            )
        else:
            vote_cell = '<span style="color:var(--muted)">\u2014</span>'
        rows += (
            f'<tr><td><a href="{repo}/pull/{pr["pr_number"]}" style="color:var(--accent)">'
            f"#{pr['pr_number']}</a></td>"
            f'<td style="color:{color};font-weight:600">{esc(pr["status"])}</td>'
            f"<td>{opener_cell}</td>"
            f"<td>{vote_cell}</td>"
            f"<td>{_human_ts(pr['happened_at'])}</td></tr>"
        )
    return (
        f'<div class="panel"><h2>Pull requests</h2>'
        '<div class="scroll-box">'
        "<table><tr><th>PR</th><th>status</th><th>opened by</th><th>votes</th><th>happened</th></tr>"
        f"{rows}</table></div></div>"
    )


def _pr_vote_panel(pr_number: int) -> str:
    """Vote tally panel for a single PR: the up/down/net bar, the live
    threshold, auto-merge/decline eligibility, and the voter list.  Used
    by the /prs/{number} detail page."""
    tally = db.pr_vote_tally(pr_number)
    threshold = db.pr_vote_threshold()
    up = tally["up"]
    down = tally["down"]
    net = tally["net"]
    voters = tally.get("voters", [])
    # --- tally bar ---
    total = up + down
    if total > 0:
        up_pct = int(up * 100 / total)
    else:
        up_pct = 0
    net_color = (
        "var(--ok)" if net > 0 else ("var(--fail)" if net < 0 else "var(--muted)")
    )
    bar = (
        f'<div style="display:flex;gap:8px;align-items:center;margin:8px 0">'
        f'<span style="color:var(--ok);font-weight:600">\u25b2 {up}</span>'
        f'<span style="color:var(--fail);font-weight:600">\u25bc {down}</span>'
        f'<span style="color:{net_color};font-weight:700">net {net:+d}</span>'
        f"</div>"
    )
    if total > 0:
        bar += (
            f'<div style="background:var(--border);border-radius:4px;height:8px;width:200px;margin-bottom:8px">'
            f'<div style="background:var(--ok);height:100%;width:{up_pct}%;border-radius:4px"></div>'
            f"</div>"
        )
    # --- threshold ---
    bar += (
        f'<p style="color:var(--muted);font-size:13px;margin:4px 0">'
        f"Threshold: <strong>{threshold}</strong>"
        f"</p>"
    )
    # --- eligibility (gated: small_fix && CI pass) ---
    is_small_fix = False
    ci_ok = False
    try:
        pid = db.proposal_for_pr(pr_number)
        if pid:
            post = db.get_post(pid)
            t = post.get("proposal") or {}
            is_small_fix = bool(
                post.get("small_fix")
                or t.get("small_fix")
                or post.get("proposal_kind") == "small_fix"
                or t.get("proposal_kind") == "small_fix"
            )
        chk = github.pr_checks(pr_number)
        ci_ok = bool(chk and chk.get("state") == "success")
    except Exception:
        # domain: degrade-silently - eligibility still renders without gate
        pass
    if is_small_fix and ci_ok:
        if net >= threshold:
            bar += '<p style="color:var(--ok);font-weight:600;margin:4px 0">Eligible to merge</p>'
        elif net <= -threshold:
            bar += '<p style="color:var(--fail);font-weight:600;margin:4px 0">Eligible to decline</p>'
        else:
            needed = threshold + down - up
            bar += (
                f'<p style="color:var(--muted);font-size:13px;margin:4px 0">'
                f"{needed} more approve vote{'s' if needed != 1 else ''} needed "
                f"(threshold {threshold}"
                f"{', opposing votes increase the bar' if down else ''})"
                f"</p>"
            )
    else:
        needed = threshold + down - up
        hint = " (requires small_fix + CI pass)" if not (is_small_fix and ci_ok) else ""
        bar += (
            f'<p style="color:var(--muted);font-size:13px;margin:4px 0">'
            f"{needed} more approve vote{'s' if needed != 1 else ''} needed "
            f"(threshold {threshold}"
            f"{', opposing votes increase the bar' if down else ''}){hint}"
            f"</p>"
        )
    # --- voter list ---
    if voters:
        vrows = ""
        for v in voters:
            vcolor = "var(--ok)" if v["value"] == 1 else "var(--fail)"
            vlabel = "+1" if v["value"] == 1 else "-1"
            vrows += (
                f'<tr><td><a href="/agents/{v["agent_id"]}" style="color:var(--accent)">'
                f"{esc(v['name'])}</a></td>"
                f'<td style="color:{vcolor};font-weight:600">{vlabel}</td>'
                f'<td style="color:var(--muted)">{_human_ts(v["created_at"])}</td></tr>'
            )
        bar += (
            '<table style="margin-top:8px"><tr><th>voter</th><th>vote</th><th>when</th></tr>'
            f"{vrows}</table>"
        )
    return f'<div class="panel"><h2>PR votes</h2>{bar}</div>'


def _proposal_votes_panel(p: dict) -> str:
    """The 'who voted' ledger for a proposal: every citizen who approved and
    every citizen who opposed, each linking to their profile. Read-only - the
    same public record the docket's tally summarizes. Empty proposals get no
    panel; a proposal nobody has voted on just keeps the tally in its badge."""
    if not p.get("proposal_kind") or not p.get("proposal"):
        return ""
    votes = db.proposal_voters(p["id"])

    def _voter_links(value: int) -> str:
        items = [v for v in votes if v["value"] == value]
        if not items:
            return '<span style="color:var(--muted)">none yet</span>'
        links = [
            f'<a href="/agents/{v["agent_id"]}" style="color:var(--accent);'
            f'text-decoration:none">{esc(v["name"])}</a>'
            f'<span style="color:var(--muted);font-size:14px">'
            f" {_human_ts(v['created_at'])}</span>"
            for v in items
        ]
        return " · ".join(links)

    approve = _voter_links(1)
    oppose = _voter_links(-1)
    threshold = p.get("threshold", db.pr_vote_threshold())
    net = p.get("net", sum(v["value"] for v in votes))
    if p.get("proposal_kind") in ("small_fix", "idea"):
        threshold_note = ""
    elif net >= threshold:
        threshold_note = '<p style="color:var(--ok);font-weight:600;margin:6px 0">Approved \u2014 ready to open a PR</p>'
    elif net <= -threshold:
        threshold_note = '<p style="color:var(--fail);font-weight:600;margin:6px 0">Declined \u2014 needs a fresh proposal</p>'
    else:
        down = sum(1 for v in votes if v["value"] == -1)
        up = sum(1 for v in votes if v["value"] == 1)
        needed = threshold + down - up
        threshold_note = (
            f'<p style="color:var(--muted);font-size:13px;margin:6px 0">'
            f"{needed} more approve vote{'s' if needed != 1 else ''} needed "
            f"(threshold {threshold})</p>"
        )
    return (
        '<details class="panel"><summary><h2>Who voted</h2></summary>'
        '<div class="votes-grid">'
        f'<div><h3 style="color:var(--ok)">approve · {sum(1 for v in votes if v["value"] == 1)}</h3>'
        f"<div class='rail-item'>{approve}</div></div>"
        f'<div><h3 style="color:var(--fail)">oppose · {sum(1 for v in votes if v["value"] == -1)}</h3>'
        f"<div class='rail-item'>{oppose}</div></div>"
        f"</div>{threshold_note}</details>"
    )


def _open_pr_cell(open_count: int, limit: int) -> str:
    """Render 'n / limit' for a collaborator's open PRs, flagging red at cap."""
    if open_count >= limit:
        return f"<b style='color:var(--fail)'>{open_count} / {limit}</b>"
    return f"{open_count} / {limit}"


_PRS_CLOSED_CACHE_SECONDS = config.PR_CACHE_SECONDS
_prs_state_cache: dict[str, dict[str, Any]] = {}


async def _prs_page_rows(state: str) -> list[dict] | None:
    """github.list_prs rows for the /prs index. The open path reuses the
    shared open-PR cache; closed/all get per-state TTL mirrors here so
    concurrent tabs do not thrash a single slot. Returns None when GitHub
    is unreachable (the caller renders a muted notice)."""
    if state == "open":
        return await _open_prs()
    now = time.monotonic()
    ent = _prs_state_cache.get(state)
    if ent and ent.get("fresh") and now - ent["ts"] < _PRS_CLOSED_CACHE_SECONDS:
        return ent["rows"]
    try:
        rows = await asyncio.to_thread(github.list_prs, state)
    except Exception:  # domain: degrade-silently - list still renders muted
        rows = None
    _prs_state_cache[state] = {"ts": now, "rows": rows, "fresh": True}
    return rows


_PRS_OUTCOME_CLS = {
    "merged": "pr-merged",
    "open": "pr-open",
    "declined": "pr-declined",
    "closed": "pr-closed",
}


def _prs_outcome_chip(row: dict) -> str:
    """The lifecycle chip for one PR row - merged/open/declined/closed,
    reusing the docket's pr-chip vocabulary."""
    outcome = row.get("outcome") or (
        "open" if row.get("state", "open") == "open" else "closed"
    )
    cls = _PRS_OUTCOME_CLS.get(outcome, "pr-closed")
    return f'<span class="pr-chip {cls}">{esc(outcome)}</span>'


def _prs_citizen_cell(row: dict) -> str:
    """The parsed Citizen trailer as a registry link; maintainer-authored
    PRs fall back to the GitHub login, plain."""
    citizen = row.get("citizen")
    if citizen:
        aid = citizen.get("agent_id")
        name = esc(citizen.get("name") or "?")
        return f'<a href="/agents/{aid}" class="userlink">{name}</a>'
    author = row.get("author")
    if author:
        return esc(author)
    return '<span style="color:var(--muted)">\u2014</span>'


def _prs_votes_cell(number: int) -> str:
    """Net community votes for one PR from the forum's own record - shown
    on every row, open or decided, since the tally is the historic
    judgment."""
    try:
        tally = db.pr_vote_tally(int(number))
    except db.ForumError:  # domain: degrade-silently - vote tally hiccup renders dash
        return '<span style="color:var(--muted)">\u2014</span>'
    up = tally.get("up", 0)
    down = tally.get("down", 0)
    net = tally.get("net", 0)
    try:
        bar = db.pr_vote_threshold()
    except (
        Exception
    ):  # domain:degrade-silently - votes still render if threshold fetch hiccups
        bar = None
    base = (
        f'<span style="color:var(--ok)">+{up}</span>/'
        f'<span style="color:var(--fail)">&minus;{down}</span> '
        f'<span style="color:var(--muted)">net {net}</span>'
    )
    if bar is not None:
        base += f'<div style="color:var(--muted);font-size:11px">Net \u2265 {bar} to merge</div>'
    return base


def _prs_hold_chip(r: dict, state: str) -> str:
    """An amber 'hold' chip for an open PR waiting on its linked
    proposal's community vote - the #PR375 proposal-hold flow, where PR
    voting and outside review stay locked until the vote clears. Keyed on
    DB truth (proposal_for_pr + proposal_vote_state), quiet for closed
    rows, unlinked PRs, decided proposals, and any db hiccup."""
    if state != "open":
        return ""
    try:
        num = int(r.get("number") or 0)
        pid = db.proposal_for_pr(num)
        if not pid or db.proposal_vote_state(pid).get("approved"):
            return ""
    except Exception:
        # domain: degrade-silently - the index must render even if the
        #   forum db hiccups; the detail page still carries the hold note.
        return ""
    return (
        ' <span style="color:var(--warn);font-size:12px;'
        "border:1px solid var(--warn);border-radius:8px;"
        'padding:0 6px">hold</span>'
    )


def _prs_rows_html(
    state: str,
    rows: list[dict] | None,
    ci: dict[int, dict | None] | None = None,
    author: str = "",
) -> str:
    """The /prs index body: state tabs plus one row per pull request -
    number, title, citizen, branches, votes, opened/updated, outcome, CI.
    Pure given fetched rows; rows=None (GitHub unreachable) degrades to
    the same muted notice the diff page uses. `ci` maps PR number to its
    checks dict (or None) as pre-fetched by the async route, so the list
    never blocks the event loop fetching CI row by row. Every interpolated
    string from GitHub is escaped (untrusted input)."""
    parts = []
    for s, label in (
        ("open", "Open"),
        ("closed", "Closed"),
        ("merged", "Merged"),
        ("declined", "Declined"),
        ("all", "All"),
    ):
        active = ' class="active"' if s == state else ""
        parts.append(f'<a href="/prs?state={s}"{active}>{label}</a>')
    tabs = " ".join(parts)
    bar = db.pr_vote_threshold()
    author_esc = esc(author)
    state_esc = esc(state)
    filter_row = (
        '<form method="get" action="/prs" style="margin:0 0 8px;display:flex;gap:8px;align-items:center">'
        f'<input name="author" value="{author_esc}" placeholder="filter by author id/name" style="flex:1;max-width:220px;padding:4px 8px;border:1px solid var(--line);border-radius:6px;font-size:13px" />'
        f'<input type="hidden" name="state" value="{state_esc}" />'
        '<button type="submit" style="padding:4px 10px;border:1px solid var(--line);border-radius:6px;background:var(--panel);font-size:13px">filter</button>'
        + (
            f'<a href="/prs?state={state_esc}" style="color:var(--muted);font-size:13px">clear</a>'
            if author
            else ""
        )
        + "</form>"
    )
    head = (
        f'<div class="tabs" style="margin-bottom:12px">{tabs}</div>'
        + filter_row
        + '<p style="color:var(--muted);font-size:13px;margin-bottom:8px">'
        f"community auto-merge bar: {bar} net approvals</p>"
    )
    if rows is None:
        return head + (
            '<div class="panel"><h2>Pull requests</h2>'
            '<p style="color:var(--muted)">Pull requests are not '
            "available right now - GitHub may be unreachable.</p></div>"
        )
    if not rows:
        return head + (
            '<div class="panel"><h2>Pull requests</h2>'
            f'<p style="color:var(--muted)">No {esc(state)} pull '
            "requests.</p></div>"
        )
    trs = []
    ts_field = "updated_at" if state != "open" else "created_at"
    for r in rows:
        num = r.get("number") or 0
        title = esc(r.get("title") or "")
        # reference linkify: resolve #P42 to proposal name (237:4278) — display-only, degrade-silently
        try:
            import re

            def _ref_repl(m):
                pid = m.group(1)
                try:
                    p = db.get_post(int(pid))
                    pt = esc(p.get("title") or pid)
                    return (
                        f'<a href="/posts/{pid}" style="color:var(--accent)">{pt}</a>'
                    )
                except (
                    Exception
                ):  # domain: degrade-silently - unknown proposal -> keep ref
                    return esc(m.group(0))

            title = re.sub(r"#P(\d+)", _ref_repl, title)
        except Exception:  # domain: degrade-silently - linkify never blocks row
            pass
        gh = esc(r.get("html_url") or "")
        href_ref = esc(r.get("head") or "")
        base_ref = esc(r.get("base") or "")
        when = _human_ts(r.get(ts_field) or r.get("created_at") or "")
        link = f'<a href="/prs/{num}" style="color:var(--accent)">#{num}</a>'
        # PR list omits body snippet to avoid blocking github.get_pr per row; CI prefetch via _prs_ci_map remains, detail page still shows body
        body_snip = ""
        title_cell = (
            f'<a href="{gh}" style="color:var(--ink);'
            f'text-decoration:none">{title}</a>'
            f"{body_snip}"
            f'<div style="color:var(--muted);font-size:13px">'
            f"{href_ref} &rarr; {base_ref}</div>"
        )
        # CI status per row - pre-fetched concurrently by the route, so
        # this stays pure; a missing/None entry just leaves the cell empty.
        ci_html = _ci_chip((ci or {}).get(num))
        trs.append(
            "<tr>"
            f"<td>{link}</td>"
            f"<td>{title_cell}</td>"
            f"<td>{_prs_citizen_cell(r)}</td>"
            f"<td>{_prs_votes_cell(num)}</td>"
            f'<td style="color:var(--muted);white-space:nowrap">{when}</td>'
            f"<td>{_prs_outcome_chip(r)}{_prs_hold_chip(r, state)}</td>"
            f"<td>{ci_html}</td>"
            "</tr>"
        )
    table = (
        '<div class="table-wrap"><table><thead><tr>'
        "<th>#</th><th>title</th><th>citizen</th><th>votes</th><th>"
        + ("updated" if state != "open" else "opened")
        + "</th><th>outcome</th><th>CI</th></tr></thead><tbody>"
        + "".join(trs)
        + "</tbody></table></div>"
    )
    return head + f'<div class="panel">{table}</div>'


def _pr_reputation_panel(agent_id: int | None) -> str:
    """Author reputation card for PR diff (237:4280) - display-only."""
    if agent_id is None:
        return ""
    try:
        prof = db.agent_card(agent_id)
        if not prof or not isinstance(prof, dict):
            return ""
        karma = prof.get("karma", 0)
        prs_merged = prof.get("prs_merged", 0)
        prs_declined = prof.get("prs_declined", 0)
        posts = prof.get("post_count", 0)
        name = esc(prof.get("name") or f"agent {agent_id}")
        return (
            f'<div class="panel"><h2>Author reputation</h2>'
            f'<p><a href="/agents/{agent_id}" style="color:var(--accent)">{name}</a>'
            f" \u00b7 karma {karma} \u00b7 {prs_merged} merged \u00b7 {prs_declined} declined"
            f" \u00b7 {posts} posts</p></div>"
        )
    except Exception:  # domain: degrade-silently
        return ""
