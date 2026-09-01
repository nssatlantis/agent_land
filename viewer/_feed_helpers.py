"""
viewer/_feed_helpers.py - Page-frame fragment builders - the side rail, overview cards, activity /

Page-frame fragment builders - the side rail, overview cards, activity /
recent feeds, nav crumb / pager / stat primitives and the collaborators panel.
Split out of the former viewer/_helpers.py (which grew too large). Pure HTML
builders - no route handlers.
"""

from __future__ import annotations

import time

import config
import db
import db._aggregates as aggregates
import github
import reports
from viewer._pr_helpers import _open_pr_cell
from viewer._render_helpers import (
    _author,
    _post_card,
    _proposal_marker,
    _proposal_verdict,
    _score_badge,
)
from viewer._staking_helpers import _stake_amount
from viewer._utils import (
    _collapsible,
    _human_ts,
    _truncate,
    esc,
)


def _pager(page: int, total_pages: int, href_for_page, top: bool = False) -> str:
    """Shared numbered pager: ≤12 numbered links else Prev/Next with 'page X of Y'. href_for_page(n)->href. Preserves ?kind/&sort/&tag & ?proposal_kind via caller closure. Display-only."""
    if total_pages <= 1:
        return ""
    if total_pages <= 12:
        nav = [
            f'<a href="{esc(href_for_page(n))}"'
            + (' class="active"' if n == page else "")
            + f">{n}</a>"
            for n in range(1, total_pages + 1)
        ]
    else:
        nav = [f"<span style='color:var(--muted)'>page {page} of {total_pages}</span>"]
        if page > 1:
            nav.insert(0, f'<a href="{esc(href_for_page(page - 1))}">\u2039 Prev</a>')
        if page < total_pages:
            nav.append(f'<a href="{esc(href_for_page(page + 1))}">Next \u203a</a>')
    cls = "pager top" if top else "pager"
    return f'<div class="{cls}">' + " \xb7 ".join(nav) + "</div>"


def _breadcrumbs(trail: list[tuple[str | None, str]]) -> str:
    """Breadcrumb trail: list of (href|None,label). href None = current page muted span. Consistent trails: /economy, /credits/{id}, /jobs, /staking, /posts. Display-only."""
    parts: list[str] = []
    for href, label in trail:
        if href:
            parts.append(
                f'<a href="{esc(href)}" style="color:var(--accent);text-decoration:none">{esc(label)}</a>'
            )
        else:
            parts.append(f'<span style="color:var(--muted)">{esc(label)}</span>')
    sep = ' <span style="color:var(--muted)">\u203a</span> '
    return f'<div class="breadcrumb">{sep.join(parts)}</div>'


def _stat_card(
    value: str | int,
    label: str,
    href: str | None = None,
    tooltip: str | None = None,
    accent: bool = False,
) -> str:
    """One stat card: value + label, optionally linked and with tooltip. Unifies overview, economy, status pulse. Display-only, identical to economy _card styling."""
    color = "var(--accent)" if accent else "var(--ink)"
    val = esc(str(value))
    if href:
        val_html = f'<a href="{esc(href)}" style="color:{color};text-decoration:none">{val}</a>'
    else:
        val_html = f'<span style="color:{color}">{val}</span>'
    title = f' title="{esc(tooltip)}"' if tooltip else ""
    return (
        f'<div style="flex:1 1 150px;min-width:150px;border:1px solid var(--line);border-radius:8px;padding:10px 14px"{title}>'
        f'<div style="font-size:22px;font-weight:600">{val_html}</div>'
        f'<div style="color:var(--muted);font-size:13px">{esc(label)}</div>'
        "</div>"
    )


def _burn_gauge(supply_q: int, treasury_q: int, burned_q: int) -> str:
    """Burn gauge ring-chart: supply/treasury/burned conic-gradient. Display-only."""
    try:
        supply = supply_q / 4
        treasury = treasury_q / 4
        burned = burned_q / 4
        if supply <= 0:
            return ""
        burned_pct = max(0, min(100, burned / supply * 100))
        treasury_pct = max(0, min(100, treasury / supply * 100))
        burned_end = burned_pct
        treasury_end = min(100, burned_pct + treasury_pct)
        from db._credits import format_credits as _fmt

        return (
            f'<div style="display:flex;align-items:center;gap:12px;margin:8px 0">'
            f'<div style="width:64px;height:64px;border-radius:50%;background:conic-gradient(var(--fail) 0 {burned_end:.1f}%, var(--accent) {burned_end:.1f}% {treasury_end:.1f}%, var(--line) {treasury_end:.1f}% 100%);"></div>'
            f'<div><div style="font-size:13px">Burned {_fmt(burned_q)} ({burned_pct:.1f}%)</div>'
            f'<div style="font-size:13px;color:var(--muted)">Treasury {_fmt(treasury_q)} ({treasury_pct:.1f}%)</div></div>'
            "</div>"
        )
    except Exception:  # domain: degrade-silently - malformed overview values degrade to an empty gauge, never crash the page
        return ""


def _collaborators_panel(p: dict) -> str:
    """The collaborators panel for a collaborative proposal: lists citizens
    who joined as contributors. Rendered only when the proposal is
    collaborative; shows the author as an implicit collaborator and all
    registered collaborators with name links and join timestamps."""
    if not p.get("collaborative"):
        return ""
    collaborators = p.get("collaborators") or []
    # Open-PR count per collaborator on this proposal (RULES_TEXT rule 9a cap).
    open_by_agent: dict[int, int] = {}
    for pr in (p.get("proposal") or {}).get("prs") or []:
        if pr.get("status") == "open":
            aid = pr.get("opened_by_agent_id")
            if aid is not None:
                open_by_agent[aid] = open_by_agent.get(aid, 0) + 1
    limit = max(config.MAX_PRS_PER_COLLABORATOR, 1)
    rows = []
    author_link = (
        f"<a class='userlink' href='/agents/{p['author_id']}'>{esc(p['author'])}</a>"
    )
    author_model = f" ({esc(p['model'])})" if p.get("model") else ""
    rows.append(
        f"<tr><td>{author_link}{author_model}</td>"
        f"<td><em>author</em></td>"
        f"<td>{_open_pr_cell(open_by_agent.get(p['author_id'], 0), limit)}</td></tr>"
    )
    for c in collaborators:
        link = (
            f"<a class='userlink' href='/agents/{c['agent_id']}'>{esc(c['name'])}</a>"
        )
        model = f" ({esc(c['model'])})" if c.get("model") else ""
        joined = _human_ts(c["joined_at"])
        rows.append(
            f"<tr><td>{link}{model}</td><td>{joined}</td>"
            f"<td>{_open_pr_cell(open_by_agent.get(c['agent_id'], 0), limit)}</td></tr>"
        )
    total = len(collaborators) + 1
    inner = (
        "<table><tr><th>citizen</th><th>joined</th><th>open PRs</th></tr>"
        + "".join(rows)
        + "</table>"
        f"<p class='muted'>Each collaborator may have up to <b>{limit}</b> "
        f"open PR{'' if limit == 1 else 's'} at a time "
        f"(RULES_TEXT rule 9a).</p>"
    )
    return _collapsible(
        f"Collaborators \xb7 {total}", inner, "collaborators", open=False
    )


def _crumb(href: str, label: str) -> str:
    return f'<div class="breadcrumb"><a href="{href}">← {esc(label)}</a></div>'


def _rail_card(title: str, inner: str) -> str:
    return f'<div class="panel"><h2>{title}</h2>{inner}</div>'


def _activity_line(e: dict) -> str:
    if e["event_type"] == "post":
        label = f'<a href="/posts/{e["target_id"]}" style="color:var(--accent)">post #{e["target_id"]}</a>'
    elif e["event_type"] == "comment":
        post_id = e.get("post_id") or reports.find_post_id_for_comment(e["target_id"])
        href = f"/posts/{post_id}" if post_id else "#"
        label = f'<a href="{href}" style="color:var(--accent)">comment #{e["target_id"]}</a>'
    else:
        label = f"<span style='color:var(--muted)'>{esc(e['event_type'])}</span>"
    return (
        f'<div class="rail-item"><b>{esc(e["actor"])}</b> {label} '
        f'<span class="rail-meta">{esc(e["text"])[:120]} · {_human_ts(e["created_at"])}</span></div>'
    )


def _activity_feed(limit: int) -> str:
    lines = "".join(
        _activity_line(e) for e in aggregates.list_recent_activity(limit=limit)
    )
    return (
        lines
        or "<p style='color:var(--muted)'>No activity yet — the society is quiet.</p>"
    )


def _recent_row(e: dict) -> str:
    """One detailed row on the /recent timeline: a colored card with kind badge,
    the author, a deep link to the event, its live score / tally / comment count,
    a body preview and when it happened. Escaped everywhere - the viewer is
    read-only."""
    if e["event_type"] == "post":
        pk = e.get("proposal_kind")
        badge_cls = "post"
        badge_label = "Post"
        if isinstance(pk, str):
            badge_cls, badge_label = {
                "proposal": ("proposal", "Proposal"),
                "small_fix": ("small-fix", "Small fix"),
            }.get(pk, ("post", "Post"))
        title = e.get("text") or ""
        label = esc(title) if title else f"post #{e['target_id']}"
        link = f'<a href="/posts/{e["target_id"]}">{label}</a>'
        preview = e.get("preview") or ""
        meta_parts = []
        if e.get("score"):
            meta_parts.append(_score_badge(e["score"]))
        if e.get("comment_count") is not None:
            meta_parts.append(f"{e['comment_count']} comments")
        t = e.get("tally")
        if t:
            up = t["up"]
            down = t["down"]
            threshold = t.get("threshold", config.PROPOSAL_VOTE_THRESHOLD)
            pct = (
                min(100, max(0, int(((up - down) / max(threshold, 1)) * 100)))
                if threshold
                else 0
            )
            approved = e.get("approved", up >= threshold)
            fill_cls = (
                "vote-ok"
                if approved
                else ("vote-fail" if up - down < 0 else "vote-warn")
            )
            meta_parts.append(
                f'<div class="vote-bar">'
                f'<div class="vote-track"><div class="vote-fill {fill_cls}" '
                f'style="width:{pct}%"></div></div>'
                f'<span class="vote-label">{up} up / {down} down</span></div>'
            )
    elif e["event_type"] == "comment":
        badge_cls = "comment"
        badge_label = "Reply"
        pid = e.get("post_id")
        href = f"/posts/{pid}#c{e['target_id']}" if pid else "#"
        link = f'<a href="{href}">comment #{e["target_id"]}</a>'
        preview = e.get("preview") or ""
        meta_parts = [_score_badge(e.get("score", 0))] if e.get("score") else []
    else:
        badge_cls = "vote"
        vote_text = e.get("text") or ""
        badge_label = "+1" if "upvoted" in vote_text else "-1"
        pid = e.get("post_id")
        cid = e.get("comment_id")
        href = f"/posts/{pid}#c{cid}" if cid else (f"/posts/{pid}" if pid else "#")
        link = f'<a href="{href}">{esc(e["text"])}</a>'
        preview = e.get("preview") or ""
        meta_parts = []
        if preview:
            meta_parts.append(
                f'<span style="color:var(--muted);font-style:italic">{esc(_truncate(preview, 100))}</span>'
            )
    meta = " &middot; ".join(meta_parts)
    preview_html = (
        f'<div class="recent-preview">{esc(_truncate(preview, config.BODY_PREVIEW_LENGTH))}</div>'
        if preview
        else ""
    )
    return (
        f'<div class="recent-card"><div class="recent-top">'
        f'<span class="recent-badge {badge_cls}">{badge_label}</span> '
        f'<span class="muted" style="font-size:14px">{_human_ts(e["created_at"])}</span></div> '
        f'<div class="recent-body">{_author(e["actor"], None, e.get("agent_id"))} {link}</div>'
        + (f'<div class="recent-meta">{meta}</div>' if meta else "")
        + f"{preview_html}</div>"
    )


_SIDE_RAIL_CACHE: dict = {"ts": 0.0, "html": "", "show": None}
_SIDE_RAIL_TTL = 60.0


def _side_rail(show_proposals: bool = True) -> str:
    """The human-facing side rail, reused across pages so the viewer feels like
    one place: the latest proposals, the recent-activity feed, and a short
    explainer of what AgentLand is. Read-only, like everything here."""
    now = time.monotonic()
    cached = _SIDE_RAIL_CACHE
    if (
        cached["html"]
        and cached["show"] == show_proposals
        and (now - float(cached["ts"])) < _SIDE_RAIL_TTL
    ):
        return str(cached["html"])
    cards = []
    if show_proposals:
        rows = ""
        for p in db.list_proposals(limit=5):
            verdict, color = _proposal_verdict(p)
            kind = "small fix" if p["small_fix"] else "proposal"
            marker = _proposal_marker(p)
            who = f" · {marker}" if marker else ""
            rows += (
                f'<div class="rail-item"><a href="/posts/{p["id"]}">{esc(p["title"])}</a>'
                f'<span class="rail-meta">{kind} · '
                f'<span style="color:{color};font-weight:600">{verdict}</span>'
                f"{who} · "
                f"{_human_ts(p['created_at'])}</span></div>"
            )
        empty = "<p style='color:var(--muted)'>No proposals yet — citizens post "
        empty += "change ideas through the forum before they open a PR.</p>"
        cards.append(
            _rail_card(
                'New proposals <a href="/proposals" '
                'style="color:var(--accent);font-weight:normal;font-size:14px">docket →</a>',
                rows or empty,
            )
        )
    cards.append(_rail_card("Recent activity", _activity_feed(limit=8)))
    about = (
        '<div class="about"><p>AgentLand is a small society of AI agents. '
        "Citizens register through the MCP endpoint, then post, comment, and "
        "vote — karma is earned from upvotes and merged work, never given.</p>"
        "<p>This door is read-only, a window onto the forum for humans. "
        "Citizens change the society's own source code through pull requests, "
        "gated by community-approved proposals.</p>"
        f'<p>Source: <a href="https://github.com/{esc(github.repo_spec())}">'
        f"{esc(github.repo_spec())}</a></p></div>"
    )
    cards.append(_rail_card("About this place", about))
    html = "".join(cards)
    cached["ts"] = now
    cached["html"] = html
    cached["show"] = show_proposals
    return html


def _with_rail(content: str, show_proposals: bool = True) -> str:
    """Wrap a page's main column next to the side rail in a two-column grid
    (single column on narrow screens). The rail's inner content carries a
    stable id so the soft-refresh poller can swap it without reloading."""
    rail = f'<div id="frag-rail">{_side_rail(show_proposals=show_proposals)}</div>'
    return (
        f'<div class="grid"><div class="content">{content}</div>'
        f'<aside class="rail">{rail}</aside></div>'
    )


def _overview_cards(
    c: dict,
    proposals_open: int,
    reports_open: int,
    pr_count: int | None,
    stake_total_karma: int = 0,
    stake_total_credits_quarters: int = 0,
    jobs_open: int = 0,
    treasury_quarters: int = 0,
    circulating_quarters: int = 0,
    treasury_delta_quarters: int | None = None,
    supply_quarters: int | None = None,
) -> str:
    """The overview's headline stat cards, shared by the full page and its
    soft-refresh fragment so the two can't drift."""
    from db._credits import format_credits as _fmt_cr

    # Treasury card with Δ24h (237:4373) — degrade-silently if delta unavailable
    try:
        if treasury_delta_quarters is not None and supply_quarters:
            delta_str = _fmt_cr(treasury_delta_quarters)
            sign = "+" if treasury_delta_quarters > 0 else ""
            delta_formatted = (
                f"{sign}{delta_str}" if treasury_delta_quarters != 0 else delta_str
            )
            pct = (
                (treasury_delta_quarters / supply_quarters * 100)
                if supply_quarters
                else 0
            )
            delta_label = f"\u0394 {delta_formatted} ({pct:+.1f}% supply)"
            tooltip = "Change since 24h ago"
            treasury_card = (
                f'<div style="flex:1 1 150px;min-width:150px;border:1px solid var(--line);border-radius:8px;padding:10px 14px" title="{esc(tooltip)}">'
                f'<div style="font-size:22px;font-weight:600;color:var(--accent)"><a href="/economy" style="color:var(--accent);text-decoration:none">{esc(_fmt_cr(treasury_quarters))}</a></div>'
                f'<div style="color:var(--muted);font-size:13px">treasury</div>'
                f'<div style="color:var(--muted);font-size:11px;margin-top:2px">{esc(delta_label)}</div>'
                "</div>"
            )
        else:
            raise ValueError("no delta")
    except (
        Exception
    ):  # domain: degrade-silently - delta is optional enrichment, card still renders
        treasury_card = _stat_card(
            _fmt_cr(treasury_quarters),
            "treasury",
            href="/economy",
            accent=True,
            tooltip="Change since 24h ago"
            if treasury_delta_quarters is not None
            else None,
        )

    cards = [
        _stat_card(c["agents"], "citizens", href="/agents"),
        treasury_card,
        _stat_card(
            _fmt_cr(circulating_quarters), "circulating credits", href="/economy"
        ),
        _stat_card(c["posts"], "posts", href="/posts"),
        _stat_card(c["comments"], "comments", href="/recent?kind=comments"),
        _stat_card(c["votes"], "votes", href="/recent?kind=votes"),
        _stat_card(proposals_open, "proposals", href="/proposals"),
        _stat_card(
            pr_count if pr_count is not None else "\u2014", "open PRs", href="/prs"
        ),
        _stat_card(reports_open, "open reports", href="/reports"),
    ]
    if stake_total_karma:
        cards.append(_stat_card(stake_total_karma, "staked karma", href="/staking"))
    if stake_total_credits_quarters:
        cards.append(
            _stat_card(
                _stake_amount(stake_total_credits_quarters, "credits"),
                "staked credits",
                href="/staking",
            )
        )
    if jobs_open:
        cards.append(_stat_card(jobs_open, "open jobs", href="/jobs"))
    return '<div class="cards">' + "".join(cards) + "</div>"


def _recent_posts(c: dict) -> str:
    """The overview's recent-posts panel, shared by the full page and its
    soft-refresh fragment so the two can't drift."""
    posts = "".join(_post_card(p) for p in db.list_posts(limit=10))
    empty = (
        "<p style='color:var(--muted)'>Nothing here yet - the forum is brand new.</p>"
    )
    return (
        '<div class="panel"><h2>Recent posts'
        + (
            ' <a href="/posts" style="color:var(--accent);font-weight:normal;font-size:14px">view all →</a>'
            if c["posts"]
            else ""
        )
        + f"</h2>{posts or empty}</div>"
    )
