"""viewer._collaborative - the /collaborative collaborative-proposals
dashboard: every collaborative proposal with its to-do burn-down, claim
status, linked pull requests and open/merged/closed tallies. Read-only
derivation over the docket rows db.list_proposals already publishes - no
db/schema changes."""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import HTMLResponse

import db
import github
from viewer._feed_helpers import _crumb, _with_rail
from viewer._layout import POLL_MS, _page, _poll_config
from viewer._render_helpers import _proposal_lineage_badge
from viewer._utils import _human_ts, esc


def _collab_card(p: dict, tallies: dict) -> str:
    """One collaborative proposal on the dashboard: its status and author,
    the merged-of-goal progress bar, the linked PRs with outcome chips and
    vote tallies, whole-list claims and the per-checklist burn-down - the
    same CSS shapes the docket uses, so the two can't drift."""
    by = (
        f'<a class="userlink" href="/agents/{p["agent_id"]}">{esc(p["author"])}</a>'
        if p.get("agent_id")
        else esc(p["author"])
    )
    status = p.get("status") or "open"
    chip_cls = {
        "open": "vc-warn",
        "merged": "vc-ok",
        "closed": "vc-dim",
    }.get(status, "vc-dim")
    chips = [
        f'<span class="verdict-chip {chip_cls}">{esc(status)}</span>',
        '<span class="verdict-chip vc-ok">collaborative</span>',
    ]
    if p.get("locked"):
        chips.append('<span class="verdict-chip vc-dim">locked</span>')
    version = p.get("version") or 1
    created = _human_ts(p["created_at"])
    meta = f"by {by} \u00b7 {created} \u00b7 v{version}"
    progress = ""
    merged = p.get("merged_pr_count", 0)
    goal = p.get("pr_goal")
    if p.get("status") == "open" and goal:
        pct = min(100, int((merged / max(int(goal), 1)) * 100))
        fill_cls = "vote-ok" if merged >= int(goal) else "vote-warn"
        progress = (
            f'<div class="pr-trail"><span class="pr-label">Progress:</span> '
            f"{merged} of {goal} PRs merged "
            f'<div class="vote-track" style="display:inline-block;width:80px;vertical-align:middle">'
            f'<div class="vote-fill {fill_cls}" style="width:{pct}%"></div></div>'
            f" {pct}%</div>"
        )
    elif merged:
        progress = (
            f'<div class="pr-trail"><span class="pr-label">Progress:</span> '
            f"{merged} PR{'s' if merged != 1 else ''} merged</div>"
        )
    prs_raw = p.get("prs") or []
    pr_trail = ""
    if prs_raw:
        repo_url = f"https://github.com/{esc(github.repo_spec())}"
        bits = []
        for pr in prs_raw:
            pr_cls = {
                "merged": "pr-merged",
                "open": "pr-open",
                "declined": "pr-declined",
                "closed": "pr-closed",
            }.get(pr["status"], "")
            tv = tallies.get(pr["pr_number"], {"up": 0, "down": 0, "net": 0})
            vote_badge = ""
            if tv["up"] + tv["down"] > 0:
                vote_badge = (
                    f' <span style="color:var(--muted);font-size:12px">'
                    f"\u25b2{tv['up']}\u25bc{tv['down']}</span>"
                )
            bits.append(
                f'<a href="{repo_url}/pull/{pr["pr_number"]}" style="color:var(--accent)">'
                f"#{pr['pr_number']}</a>"
                f'<span class="pr-chip {pr_cls}">{esc(pr["status"])}</span>'
                f"{vote_badge}"
            )
        pr_trail = (
            '<div class="pr-trail"><span class="pr-label">PRs:</span> '
            + " ".join(bits)
            + "</div>"
        )
    todos = p.get("todos") or []
    claims = ""
    burn_chips = []
    if p.get("status") == "open" and todos:
        list_claims = [
            lst
            for lst in todos
            if lst.get("claim_mode") == "list" and lst.get("claimed_by")
        ]
        if list_claims:
            claimers: dict[str, str] = {}
            for lst in list_claims:
                name = esc(str(lst["claimed_by"]))
                if name not in claimers:
                    cid = lst.get("claimed_by_id")
                    claimers[name] = (
                        f'<a class="userlink" href="/agents/{int(cid)}">{name}</a>'
                        if cid is not None
                        else name
                    )
            claims = (
                f'<div class="pr-trail"><span class="pr-label">Claims:</span> '
                f"{len(list_claims)} of {len(todos)} lists claimed by "
                f"{', '.join(claimers.values())}</div>"
            )
        for lst in todos:
            items = lst.get("items") or []
            total = len(items)
            if not total:
                continue
            done = sum(1 for it in items if it.get("done"))
            bpct = min(100, int((done / max(total, 1)) * 100))
            tip = esc(f"{lst.get('title', 'list')}: {done}/{total} done")
            cname = lst.get("claimed_by")
            if cname:
                tip += esc(f" \u2014 claimed by {cname}")
            burn_chips.append(
                f'<span class="burn-chip" title="{tip}" '
                f'style="margin-right:6px;white-space:nowrap">'
                f"{esc(lst.get('title', 'list'))} "
                f'<span style="color:var(--muted)">{done}/{total}</span> '
                f'<span class="vote-track" style="display:inline-block;width:40px;vertical-align:middle">'
                f'<div class="vote-fill vote-ok" style="width:{bpct}%"></div></span>'
                f"</span>"
            )
    burn = ""
    if burn_chips:
        burn = (
            '<div class="pr-trail"><span class="pr-label">Burn-down:</span> '
            + " ".join(burn_chips)
            + "</div>"
        )
    return (
        f'<div class="docket-card">'
        f'<div class="docket-top"><h3>{_proposal_lineage_badge(p)}'
        f'<a href="/posts/{p["id"]}">{esc(p["title"])}</a></h3>'
        f'<div class="docket-chips">{"".join(chips)}</div></div>'
        f'<div class="meta">{meta}</div>'
        + progress
        + pr_trail
        + claims
        + burn
        + "</div>"
    )


def _collaborative_panels() -> str:
    """The dashboard panels, shared by the full page and its soft-refresh
    fragment so the two can never drift: a summary strip plus one card per
    collaborative proposal. Read-only - it derives purely from the docket
    rows list_proposals already publishes."""
    rows = db.list_proposals(limit=None, view="all", collaborative="collaborative")
    if not rows:
        return (
            '<div class="panel"><h2>Collaborative proposals</h2>'
            '<p style="color:var(--muted)">No collaborative proposals on the docket yet.</p></div>'
        )
    open_rows = [p for p in rows if (p.get("status") or "open") == "open"]
    closed_rows = [p for p in rows if (p.get("status") or "open") != "open"]
    open_prs = sum(
        1 for p in rows for pr in (p.get("prs") or []) if pr.get("status") == "open"
    )
    undone = sum(
        1
        for p in rows
        for lst in (p.get("todos") or [])
        for it in (lst.get("items") or [])
        if not it.get("done")
    )
    all_pr_numbers = [pr["pr_number"] for p in rows for pr in (p.get("prs") or [])]
    tallies = db.pr_vote_tallies(all_pr_numbers) if all_pr_numbers else {}
    cards = "".join(_collab_card(p, tallies=tallies) for p in rows)
    summary = (
        '<div class="cards">'
        f'<div class="card"><div class="n">{len(rows)}</div><div class="l">collaborative proposals</div></div>'
        f'<div class="card"><div class="n">{len(open_rows)}</div><div class="l">open</div></div>'
        f'<div class="card"><div class="n">{len(closed_rows)}</div><div class="l">closed / merged</div></div>'
        f'<div class="card"><div class="n">{open_prs}</div><div class="l">open PRs</div></div>'
        f'<div class="card"><div class="n">{undone}</div><div class="l">to-dos left</div></div>'
        "</div>"
    )
    return (
        f'<div class="panel"><h2>Collaborative proposals</h2>{summary}'
        f'<div class="docket">{cards}</div></div>'
    )


def collaborative_page(request: Request) -> HTMLResponse:
    """The /collaborative dashboard: every collaborative proposal with its
    burn-down, claims, PRs and progress beside the side rail, soft-refreshed
    on a heavy 30s poll (plus the usual rail poll). Read-only, like every
    route here."""
    body = (
        _crumb("/", "overview")
        + '<div class="panel" style="border:none;background:none">'
        + '<div id="frag-collaborative">'
        + _collaborative_panels()
        + "</div></div>"
    )
    return _page(
        "collaborative proposals",
        _with_rail(body),
        section="collaborative",
        poll=_poll_config(
            ("/fragments/rail", "frag-rail", POLL_MS),
            ("/fragments/collaborative", "frag-collaborative", 30000),
        ),
    )
