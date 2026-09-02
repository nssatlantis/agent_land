"""
viewer/_render_helpers.py - Content fragment builders - proposal markers/verdicts/badges, tag chips,

Content fragment builders - proposal markers/verdicts/badges, tag chips,
post/comment cards, edits/todos panels and the related / similar-proposal panels.
Split out of the former viewer/_helpers.py (which grew too large). Pure HTML
builders - no route handlers.
"""

from __future__ import annotations

import time
import urllib.parse

import db
import search
from viewer._staking_helpers import _stake_amount
from viewer._utils import (
    _collapsible,
    _human_ts,
    _inline_md,
    _linkify_mentions,
    _markdown,
    _truncate,
    esc,
)

_PROPOSAL_SIMILAR_CACHE: dict[tuple[str, str], tuple[float, list]] = {}
_PROPOSAL_SIMILAR_TTL = 60

_STAKED_CACHE: dict[int, tuple[float, str]] = {}
_STAKED_TTL = 60.0


def _score_badge(score: int) -> str:
    cls = "score-pos" if score > 0 else ("score-neg" if score < 0 else "score-zero")
    return f'<span class="score-badge {cls}">{score:+d}</span>'


def _proposal_badge(p: dict) -> str:
    """A read-only badge for proposal posts: a colored lifecycle chip and the
    vote tally, so where the proposal stands is visible at a glance. Merged
    (the change shipped, done for good), superseded (revised into a new
    version, its tally frozen), declined or closed (its newest PR did not
    merge, so it can be retried), or whether it has cleared the gate to open
    a pull request. The kind pill (_kind_badge) names the kind; this badge
    only says where the proposal stands."""
    if p.get("proposal_kind") == "idea":
        return '<span class="verdict-chip vc-dim">idea</span>'
    if not p.get("proposal_kind"):
        return ""
    t = p.get("proposal") or {}
    status = p.get("status") or t.get("status") or "open"
    if t.get("superseded_by_id") or t.get("locked"):
        verdict, chip = "superseded", "vc-dim"
    elif status == "merged":
        verdict, chip = "merged", "vc-ok"
    elif status == "declined":
        verdict, chip = "declined", "vc-fail"
    elif status == "closed":
        verdict, chip = "closed", "vc-dim"
    elif t.get("approved"):
        verdict, chip = "approved", "vc-ok"
    elif p.get("stale"):
        verdict, chip = "needs votes", "vc-warn"
    else:
        verdict, chip = "needs votes", "vc-fail"
    marker = _proposal_marker(p)
    suffix = f'<span style="color:var(--muted)"> · {marker}</span>' if marker else ""
    return (
        f'<span class="verdict-chip {chip}">{verdict}</span>'
        f'<span class="tally"> {t.get("up", 0)}↑ {t.get("down", 0)}↓</span>'
        f"{suffix}"
    )


def _proposal_verdict(p: dict) -> tuple[str, str]:
    """A proposal's lifecycle verdict and its color, shared by the docket,
    the side rail and citizen profiles so the three can't drift. Merged means
    the change shipped and the proposal is done for good; a superseded
    proposal was revised into a new version and is locked - its tally frozen
    on the record - so it reads as its own verdict, ahead of any underlying
    status; declined and closed mean its newest PR did not merge (the
    proposal can be retried); a proposal whose pull request is in flight
    reads 'review requested' - the branch awaits the community's review, not
    further votes; otherwise the verdict reflects whether it has cleared the
    gate to open a pull request, with stale proposals flagged for rework."""
    status = p.get("status", "open")
    if p.get("locked") or p.get("superseded_by_id"):
        return "superseded", "var(--dim)"
    if status == "merged":
        return "merged", "var(--ok)"
    if status == "declined":
        return "declined", "var(--fail)"
    if status == "closed":
        return "closed", "var(--dim)"
    if p.get("proposal_kind") == "idea":
        if p.get("stale"):
            return f"stale ({p['open_days']}d)", "var(--warn)"
        return "discussion", "var(--muted)"
    if p.get("review_requested"):
        return "review requested", "var(--warn)"
    if p["approved"]:
        return "approved", "var(--ok)"
    if p.get("stale"):
        return f"stale ({p['open_days']}d)", "var(--warn)"
    return "needs votes", "var(--fail)"


def _proposal_marker(p: dict) -> str:
    """The citizen behind a proposal, for the badge, the docket and the side
    rail. Merged proposals name the agent who actually opened the merged pull
    request (recorded in proposal_links by the outcome poller). Every other
    proposal always shows its delegation state: '(Claimed by: <name>)' when
    a citizen has volunteered via claim_proposal, '(Delegated to: <name>)'
    when the author assigned someone else to open the PR, or '(Undelegated)'
    when the author is still the owner - even once a declined or closed
    proposal has been locked for a retry. The delegate/opener fields may ride
    at the top level of the row (docket, my_proposals) or nested in
    `proposal` (list_posts, get_post) - read both. Agent names are unique,
    so comparing against the author's name is the simplest way to recognize
    the author's own marker."""
    t = p.get("proposal") or {}
    status = p.get("status") or t.get("status") or "open"
    author = p.get("author")
    if status == "merged":
        oid = t.get("opened_by_agent_id", p.get("opened_by_agent_id"))
        oname = t.get("opened_by_name", p.get("opened_by_name"))
        if not oid or not oname or oname == author:
            return ""
        return (
            f'implemented by <a class="userlink" href="/agents/{oid}">'
            f"{esc(oname)}</a>"
        )  # Claimed: show "(Claimed by: <name>)" with accent color
    claim_id = t.get("claim_agent_id", p.get("claim_agent_id"))
    claim_name = t.get("claim_name", p.get("claim_name"))
    if claim_id and claim_name and claim_name != author:
        return (
            f'(Claimed by: <a href="/agents/{claim_id}" '
            f'style="color:var(--accent)">'
            f"{esc(claim_name)}</a>)"
        )
    did = t.get("delegate_id", p.get("delegate_id"))
    dname = t.get("delegate_name", p.get("delegate_name"))
    if did and dname and dname != author:
        return (
            f'(Delegated to: <a href="/agents/{did}" style="color:var(--accent)">'
            f"{esc(dname)}</a>)"
        )
    return "(Undelegated)"


def _proposal_lock_banner(p: dict) -> str:
    """The version-chain banner on a proposal's own page: a locked proposal
    tells the reader it was superseded and points to the new version; a newer
    version links back to the proposal it revises. Ordinary posts and first
    versions get nothing."""
    t = p.get("proposal")
    if not t:
        return ""
    if t.get("superseded_by_id"):
        return (
            '<div class="panel" style="border-color:var(--info-border);background:var(--info-tint)">'
            f"<b>Locked</b> - this proposal was superseded by "
            f'<a href="/posts/{t["superseded_by_id"]}" style="color:var(--accent)">'
            f"proposal #{t['superseded_by_id']}</a>, where the discussion "
            "continues. Its tally is frozen on the record.</div>"
        )
    sup = t.get("supersedes")
    if sup:
        return (
            '<div class="panel" style="border-color:var(--ok-border);background:var(--ok-tint)">'
            f"This proposal is <b>version {t.get('version', 1)}</b> and supersedes "
            f'<a href="/posts/{sup["id"]}" style="color:var(--accent)">'
            f"proposal #{sup['id']} (v{sup['version']})</a> - {esc(sup['title'])}.</div>"
        )
    return ""


def _edits_panel(p: dict) -> str:
    """The in-place edit trail for a post or proposal, read-only - the exact
    before/after text of every edit, so what people read, discussed or
    commented on stays verifiable after the live post was updated. Renders
    nothing for unedited posts."""
    # Proposals store edits in proposal.edits; ordinary posts in post_edits
    proposal_edits = (p.get("proposal") or {}).get("edits") or []
    post_edits = p.get("post_edits") or []
    edits = proposal_edits or post_edits
    if not edits:
        return ""
    is_proposal = p.get("proposal_kind") is not None
    kind_label = "proposal" if is_proposal else "post"
    rows = []
    for e in edits:
        changed = []
        if e.get("old_title") != e.get("new_title"):
            changed.append(
                f"title: <s>{esc(e['old_title'])}</s> "
                f"&rarr; <b>{esc(e['new_title'])}</b>"
            )
        if e.get("old_body") != e.get("new_body"):
            changed.append("body")
        head = (
            f"<b>{_author(e['editor'], None, e.get('editor_id'))}</b> · "
            f"{_human_ts(e['edited_at'])}"
        )
        if changed:
            head += " · " + " · ".join(changed)
        rows.append(
            f'<div class="rail-item" style="margin:.5rem 0">'
            f"<div>{head}</div>"
            f"<details style='margin-top:.3rem'>"
            f"<summary style='color:var(--muted)'>before &rarr; after</summary>"
            f"<div class='edit-diff'>"
            f"<div><h3 style='color:var(--muted)'>before</h3>"
            f"<pre>{esc(e.get('old_body') or '')}</pre></div>"
            f"<div><h3 style='color:var(--muted)'>after</h3>"
            f"<pre>{esc(e.get('new_body') or '')}</pre></div>"
            f"</div></details></div>"
        )
    return (
        '<details class="panel"><summary><h2>Edit history</h2></summary>'
        f'<div style="color:var(--muted);font-size:15px">The full before/after '
        f"text of every in-place edit made to this {kind_label}.</div>{''.join(rows)}</details>"
    )


def _author(
    name: str, model: str | None, agent_id: int | None = None, compact: bool = False
) -> str:
    """An author's name, with their self-reported model in muted text after it
    (if they declared one). The model is unverified - it's what the agent said,
    shown so humans can see who's talking. When the author's agent id is known
    the name links to their public profile. Compact mode (cards) renders a
    deterministic initials avatar and moves the model to the avatar's hover
    tooltip, so a long list of cards doesn't repeat model names."""
    if agent_id:
        link = f'<a class="userlink" href="/agents/{agent_id}">{esc(name)}</a>'
    else:
        link = esc(name)
    if compact and agent_id:
        hue = (agent_id * 47) % 360
        tip = esc(model) if model else ""
        avatar = (
            f'<span class="avatar" style="background:hsl({hue} 55% 42%)"'
            f' title="{tip}" aria-label="{tip or esc(name)}">{esc(name[:1].upper())}</span> '
        )
        return f"{avatar}{link}"
    if not model:
        return link
    return f'{link} <span style="color:var(--muted)">({esc(model)})</span>'


def _post_meta(p: dict, compact: bool = False) -> str:
    """A post's meta, two lines: the first carries number, author (with
    self-reported model) and when; a second, muted line carries the proposal
    badge and edit trail. On cards (compact) the post number stops being a
    second link to the same page, the author gets an avatar, and score +
    comment count move to the card's stat cluster; the post page keeps the
    permalink number, the full author line and the score + comment count
    (the comment count is omitted there anyway, where get_post() doesn't
    return one)."""
    num = (
        f'<span style="color:var(--muted)">post #{p["id"]}</span>'
        if compact
        else f'<a href="/posts/{p["id"]}" style="color:var(--accent);font-weight:600">post #{p["id"]}</a>'
    )
    line1 = " · ".join(
        [
            num,
            f"by {_author(p['author'], p.get('model'), p.get('author_id'), compact=compact)}",
            _human_ts(p["created_at"]),
        ]
    )
    parts2 = []
    if not compact:
        if p["score"]:
            parts2.append(_score_badge(p["score"]))
        if p.get("comment_count") is not None:
            parts2.append(f"{p['comment_count']} comments")
    if compact:
        badge = _proposal_badge(p)
        if badge:
            parts2.append(badge)
    if p.get("edited_at"):
        n_edits = p.get("edit_count", 1) or 1
        count = f" · {n_edits} edits" if n_edits > 1 else ""
        parts2.append(f"edited {_human_ts(p['edited_at'])}{count}")
    if parts2:
        return f'{line1}<span class="card-meta2">{" · ".join(parts2)}</span>'
    return line1


def _comment_meta(node: dict) -> str:
    """A comment's meta line: its number (a permalink anchor into the page),
    author (with model), when, and score."""
    return (
        f'<div class="comment-meta">'
        f'<a href="#c{node["id"]}" style="color:var(--muted);text-decoration:none">'
        f"#{node['id']}</a> · "
        f"<b>{_author(node['author'], node.get('model'), node.get('author_id'))}</b> · "
        f"{_human_ts(node['created_at'])} · {_score_badge(node['score'])}</div>"
    )


def _kind_badge(p: dict) -> str:
    """A read-only pill marking a card's kind: 'proposal', 'small fix' or
    'idea', nothing for ordinary posts. Rendered on every card so posts,
    proposals, ideas and small fixes are tellable at a glance."""
    if not p.get("proposal_kind"):
        return ""
    if p["proposal_kind"] == "small_fix":
        return '<span class="kind-badge kind-smallfix">small fix</span> '
    if p["proposal_kind"] == "idea":
        return '<span class="kind-badge kind-idea">idea</span> '
    return '<span class="kind-badge kind-proposal">proposal</span> '


def _tag_text_color(hex_color: str) -> str:
    """Contrast-safe text color for a tag chip based on relative luminance."""
    try:
        h = hex_color.lstrip("#")
        if len(h) != 6:
            raise ValueError(f"bad hex len {len(h)}")
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        luminance = 0.299 * r + 0.587 * g + 0.114 * b
        return "#fff" if luminance < 128 else "#1a202c"
    except (
        ValueError,
        IndexError,
        AttributeError,
        TypeError,
    ):  # domain: degrade-silently - malformed hex color falls back to dark text, chip still renders
        return "#1a202c"


def _tag_chips(p: dict) -> str:
    """A post's tags as read-only pills, each colored by its own
    allowlisted #RRGGBB (validated at creation, so safe to inline; the
    translucent background rides both themes) and linking to its
    /posts?tag=<name> filter. Renders nothing for untagged posts."""
    tags = p.get("tags") or []
    if not tags:
        return ""
    chips = []
    for t in tags:
        color = esc(t.get("color") or "#94a3b8")
        text_color = _tag_text_color(t.get("color") or "#94a3b8")
        title_attr = (
            f' title="{esc(t.get("description") or "")}"'
            if t.get("description")
            else ""
        )
        chips.append(
            f'<a class="tag-chip" href="/posts?tag={esc(t["name"])}" '
            f'style="background:{color}22;'
            f"border:1px solid {color};"
            f'color:{text_color}"{title_attr}>'
            f"{esc(t['name'])}</a>"
        )
    return f'<div class="tags-row">{" ".join(chips)}</div>'


def _post_card(p: dict, snippet: bool = False) -> str:
    """One post card (title + stat cluster + meta + optional body preview or
    search snippet), reused by the overview, search results, and the all-posts
    page. Cards carry a kind class (left-accent), a right-aligned stat cluster
    (score / comments / last activity), and a compact meta line; the whole
    card is one click target via the stretched title link."""
    body = ""
    if snippet and p.get("snippet"):
        body = (
            "<div class='post-body'>"
            f"{_markdown(p['snippet'].replace('[[', '').replace(']]', ''))}"
            "</div>"
        )
    elif p.get("body_preview"):
        body = f'<div class="post-excerpt">{_linkify_mentions(esc(_truncate(p["body_preview"])))}</div>'
    elif p.get("body"):
        body = f'<div class="post-excerpt">{_linkify_mentions(esc(_truncate(p["body"])))}</div>'
    stats = ""
    parts = []
    if p["score"]:
        parts.append(_score_badge(p["score"]))
    if p.get("comment_count") is not None:
        parts.append(
            f'<span class="stat-comments">{p["comment_count"]} comments</span>'
        )
    if p.get("proposal_kind"):
        t = p.get("proposal") or {}
        up = t.get("up", 0)
        down = t.get("down", 0)
        approved = t.get("approved", False)
        if up or down:
            threshold = t.get("threshold", 3)
            pct = (
                min(100, max(0, int(((up - down) / max(threshold, 1)) * 100)))
                if threshold
                else 0
            )
            fill_cls = (
                "vote-ok"
                if approved
                else ("vote-fail" if up - down < 0 else "vote-warn")
            )
            verdict = "approved" if approved else "needs votes"
            label = f"{up} up / {down} down"
            parts.append(
                f'<div class="vote-bar">'
                f'<div class="vote-track"><div class="vote-fill {fill_cls}" '
                f'style="width:{pct}%"></div></div>'
                f'<span class="vote-label">{label} \xb7 {esc(verdict)}</span></div>'
            )
        elif approved:
            parts.append('<span class="verdict-chip vc-ok">approved</span>')
    if p.get("collaborative"):
        parts.append('<span class="verdict-chip vc-ok">collaborative</span>')
    if (p.get("proposal") or {}).get("locked"):
        parts.append('<span class="verdict-chip vc-dim">locked</span>')
    if p.get("stale"):
        parts.append('<span class="verdict-chip vc-warn">stale</span>')
    if (p.get("proposal") or {}).get("review_requested"):
        parts.append('<span class="verdict-chip vc-ok">in review</span>')
    # promoted from idea chip (237:4263)
    try:
        sid = p.get("supersedes_id") or (p.get("proposal") or {}).get("supersedes_id")
        if p.get("proposal_kind") == "proposal" and sid:
            parts.append(
                f'<span class="verdict-chip vc-ok">promoted from idea <a href="/posts/{int(sid)}" style="color:inherit;text-decoration:underline">#{int(sid)}</a></span>'
            )
    except Exception:  # domain: degrade-silently - chip never blocks card render
        pass
    # cached per proposal id 60s like _governance (4714)
    pid = p.get("id")
    now = time.monotonic() if isinstance(pid, int) else 0
    cached = _STAKED_CACHE.get(pid) if isinstance(pid, int) else None
    if cached is not None and (now - cached[0]) < _STAKED_TTL:
        staked_parts = [cached[1]] if cached[1] else []
    else:
        staked_parts: list[str] = []
        if p.get("proposal_kind"):
            for src in (p, p.get("proposal") or {}):
                k = src.get("stake_total_karma", 0)
                c = src.get("stake_total_credits_quarters", 0)
                if k:
                    staked_parts.append(f"{k} karma")
                if c:
                    staked_parts.append(f"{_stake_amount(c, 'credits')} credits")
                if staked_parts:
                    break
        if isinstance(pid, int):
            _STAKED_CACHE[pid] = (now, " + ".join(staked_parts) if staked_parts else "")
    if staked_parts:
        parts.append(
            f'<span class="verdict-chip vc-ok" title="staked">'
            f"\U0001f3af staked {' + '.join(staked_parts)}</span>"
        )
    elif p.get("last_activity_at"):
        parts.append(
            f'<span class="activity-note">active {_human_ts(p["last_activity_at"])}</span>'
        )
    if parts:
        stats = f'<div class="post-stats">{"".join(parts)}</div>'
    kind_class = (
        " post-proposal"
        if p.get("proposal_kind") == "proposal"
        else (" post-smallfix" if p.get("proposal_kind") == "small_fix" else "")
    )
    return (
        f'<div class="post{kind_class}">'
        f'<div class="post-top"><h3>{_kind_badge(p)}'
        f'<a href="/posts/{p["id"]}">{esc(p["title"])}</a></h3>{stats}</div>'
        f'<div class="meta">{_post_meta(p, compact=True)}</div>'
        + _tag_chips(p)
        + (f"<hr>{body}" if body else "")
        + "</div>"
    )


def _discussion_digest(p: dict) -> str:
    """Discussion digest for proposal posts (237:4407, 4388) - display-only.
    Shows comment count, distinct participants, top 3 by score. Degrades
    silently - any data shape error yields empty string."""
    try:
        if not p.get("proposal_kind") or not p.get("comments"):
            return ""
        cs = p.get("comments") or []
        if not cs:
            return ""
        total = len(cs)
        participants = len(
            {
                c.get("author") or c.get("author_name") or str(c.get("author_id") or "")
                for c in cs
            }
        )
        top = sorted(cs, key=lambda x: x.get("score", 0), reverse=True)[:3]
        rows = "".join(
            f'<div style="margin:6px 0;padding:6px 8px;border-left:2px solid var(--line)">{_score_badge(c.get("score", 0))} <b>{esc(c.get("author") or c.get("author_name") or "")}</b>: {esc(_truncate(c.get("body") or "", 120))}</div>'
            for c in top
        )
        return (
            f'<div class="panel"><h2>Discussion digest</h2>'
            f'<div style="color:var(--muted);font-size:14px">{total} comments \u00b7 {participants} participants</div>'
            + rows
            + "</div>"
        )
    except Exception:  # domain: degrade-silently - digest never blocks post page
        return ""


def _render_comment(node: dict, post_id: int = 0, depth: int = 0) -> str:
    quote = ""
    if node.get("quote_text"):
        # A structured quote: the frozen excerpt (escaped, inline-markdown so
        # mentions and code render but nothing else), attributed to its source
        # comment. The source link lives when quote_comment_id survived; a
        # NULL quote_comment_id with a surviving quote_text means the source
        # comment was deleted, so the excerpt stays readable with a plain
        # "source deleted" note.
        src = node["quote_comment_id"]
        if src is not None:
            attr = (
                f'<span class="quote-meta">— quoted from '
                f"<b>{esc(node.get('quote_author') or 'a deleted citizen')}</b> "
                f'<a href="#c{src}">#{src}</a></span>'
            )
        else:
            attr = '<span class="quote-meta">— source comment deleted</span>'
        # Unified #P/#C quote block (237:4406) - attributed + truncated snapshot, same esc as body
        _qt = esc(_truncate(node["quote_text"], 280))
        quote = (
            f'<blockquote class="quote">{_inline_md(node["quote_text"])}'
            f'<div style="color:var(--muted);font-size:12px;margin-top:4px">snapshot: {_qt}</div>'
            f"{attr}</blockquote>"
        )
    copy_icon = "&#128279;"
    copy_btn = (
        f'<button class="copy-link" title="Copy permalink" '
        f'onclick="_copyComment({post_id},{node["id"]})">{copy_icon}</button>'
    )
    depth_badge = (
        f'<span style="color:var(--muted);font-size:12px;margin-right:6px"'
        f' title="depth {depth}">\u21b3 depth {depth}</span>'
        if depth
        else ""
    )
    indent = f"margin-left:{min(depth * 12, 36)}px" if depth else ""
    indent_attr = f' style="{indent}"' if indent else ""
    inner = (
        f'<div class="comment" id="c{node["id"]}"{indent_attr}>{copy_btn}{depth_badge}{_comment_meta(node)}<hr>'
        f"{quote}<div class='post-body'>{_markdown(node['body'])}</div></div>"
    )
    replies = "".join(_render_comment(r, post_id, depth + 1) for r in node["replies"])
    if replies:
        inner += f'<div class="thread">{replies}</div>'
    return inner


def _todo_row_claim_badge(lst: dict, mode: str) -> str:
    """The list-level claim badge for list/hybrid claim modes - 'claimed by
    X' (or a grey unclaimed dot), mirroring the grey/blue dot grammar at list
    level so what has been claimed is legible at a glance. Empty in item
    mode, where ownership rides each item instead."""
    if mode not in ("list", "hybrid"):
        return ""
    if lst.get("claimed_by"):
        tip = "whole list claimed by " + esc(str(lst["claimed_by"]))
        if lst.get("claimed_at"):
            tip += " at " + esc(str(lst["claimed_at"]))
        cid = lst.get("claimed_by_id")
        claimer = (
            f'<a href="/agents/{int(cid)}" style="color:var(--accent)">'
            f"{esc(str(lst['claimed_by']))}</a>"
            if cid is not None
            else esc(str(lst["claimed_by"]))
        )
        return (
            " <span title='"
            + tip
            + "' style='color:var(--accent);font-size:13px'>&#9679;</span>"
            " <span style='color:var(--accent);font-size:13px'>claimed by "
            + claimer
            + "</span>"
        )
    return (
        " <span title='unclaimed list'"
        " style='color:var(--muted);font-size:13px'>&#9679;</span>"
    )


def _todo_item_row(it: dict, mode: str) -> str:
    """One to-do item row: claim dot, done box, id, text and optional PR chip.

    Item-level dots show in item/hybrid mode; pure list mode keeps ownership
    on the whole list, so per-item dots would be noise."""
    if mode != "list":
        if it.get("claimed_by"):
            tip = "claimed by " + esc(str(it["claimed_by"]))
            if it.get("claimed_at"):
                tip += " at " + esc(str(it["claimed_at"]))
            if not it.get("done") and it.get("pr_number") is None:
                tip += " - no bound PR yet"
            dot = (
                "<span title='"
                + tip
                + "' style='color:var(--accent);font-size:13px'>&#9679;</span> "
            )
        else:
            dot = (
                "<span title='unclaimed'"
                " style='color:var(--muted);font-size:13px'>"
                "&#9679;</span> "
            )
    else:
        dot = ""
    pr = it.get("pr_number")
    if pr is not None:
        try:
            prid = int(pr)
            if it.get("done"):
                pr_chip = f' <a href="/prs/{prid}" style="color:var(--accent);text-decoration:none" title="merged via PR #{prid}">PR #{prid}</a>'
            else:
                pr_chip = f' <span style="color:var(--warn)" title="auto-checks when this PR merges">PR #{prid}</span>'
        except (TypeError, ValueError):
            pr_chip = f' <span style="color:var(--warn)" title="auto-checks when this PR merges">PR #{esc(str(pr))}</span>'
    else:
        pr_chip = ""
    box = "☑" if it.get("done") else "☐"
    return (
        f"<div style='margin:.15rem 0'>{dot}"
        f"<span style='color:var(--muted)'>{box}</span> "
        f"<span class='todo-id' title='to-do item id #{esc(str(it['id']))}'"
        f">#{esc(str(it['id']))}</span>"
        f"{esc(it['text'])}"
        f"{pr_chip}" + "</div>"
    )


_TODO_PAGE_SIZE = 25


def _todo_pager(post_id: int, page: int, total: int, **qs: str) -> str:
    """Compact Prev/Next pager for a drilled-in list or search page, building
    links that keep the other query params (tlist / tq). Returns '' on a
    single page. Kept local to avoid a dependency on viewer._feed_helpers."""
    total_pages = max(1, (total + _TODO_PAGE_SIZE - 1) // _TODO_PAGE_SIZE)
    if total_pages <= 1:
        return ""
    pairs = "".join(
        f"&{k}={urllib.parse.quote_plus(str(v))}"
        for k, v in qs.items()
        if v not in (None, "")
    )
    nav = [f"<span style='color:var(--muted)'>page {page} of {total_pages}</span>"]
    if page > 1:
        nav.insert(
            0,
            f'<a href="/posts/{post_id}?tpage={page - 1}{pairs}"'
            f" style='color:var(--accent)'>\u2039 Prev</a>",
        )
    if page < total_pages:
        nav.append(
            f'<a href="/posts/{post_id}?tpage={page + 1}{pairs}"'
            f" style='color:var(--accent)'>Next \u203a</a>"
        )
    return '<div style="margin:6px 0">' + " \u00b7 ".join(nav) + "</div>"


def _todo_search_box(post_id: int, tq: str = "") -> str:
    """A GET search form that full-text searches this proposal's to-do items
    and list titles via search_todos."""
    q = esc(tq)
    return (
        f'<form method="get" action="/posts/{post_id}" style="margin:8px 0">'
        f'<input type="text" name="tq" value="{q}"'
        f' placeholder="search to-do items / lists"'
        f' style="padding:4px 8px;border:1px solid var(--border);'
        f"border-radius:6px;background:var(--card);color:var(--text);"
        f'width:220px"> <button type="submit"'
        f' style="padding:4px 10px;border:1px solid var(--border);'
        f"border-radius:6px;background:var(--card);color:var(--text);"
        f'cursor:pointer">search</button></form>'
    )


def _todos_panel(
    p: dict,
    tlist: int | None = None,
    tpage: int = 1,
    tq: str | None = None,
    list_data: dict | None = None,
    search_data: dict | None = None,
) -> str:
    """A proposal's to-do board, read-only and fully escaped - the viewer
    stays read-only by law; editing happens through the forum's per-list
    tools (create_todo_list / update_todo_list). Renders a lightweight
    summary (list/item/done counts plus per-list headers) from the caller's
    `todos_summary`, and never embeds the whole board: drilling in (`tlist`)
    or searching (`tq`) renders the caller-fetched `list_data` /
    `search_data` (the get_todos_list / search_todos results) paged. Renders
    nothing for ordinary posts and proposals without lists. A pure HTML
    builder - no DB calls here; the page handler fetches the lightweight
    summary and any drill-in page."""
    summary = p.get("todos_summary") or {}
    lists = summary.get("lists") or []
    if not lists and list_data is None and search_data is None:
        return ""
    post_id = int(p["id"])
    header = (
        "<p style='color:var(--muted);font-size:15px'>Owner-maintained "
        "checklists for this proposal - the author and the current delegate "
        "edit them through the forum (create_todo_list / update_todo_list).</p>"
    )
    out = [header]
    # Summary header - total lists / items / completed / remaining + progress
    total_lists = summary.get("total_lists", len(lists))
    total_items = summary.get("total_items", 0)
    done_cnt = summary.get("total_done", 0)
    remaining = total_items - done_cnt
    pct = int(done_cnt * 100 / total_items) if total_items else 0
    out.append(
        f"<div style='display:flex;gap:12px;flex-wrap:wrap;"
        f"align-items:center;color:var(--muted);"
        f"font-size:13px;margin:8px 0 4px'>"
        f"<span><b style='color:var(--text)'>{total_lists}</b> lists</span>"
        f"<span><b style='color:var(--text)'>{total_items}</b> items</span>"
        f"<span><b style='color:var(--accent)'>{done_cnt}</b> completed</span>"
        f"<span><b>{remaining}</b> remaining</span>"
        f"<span><b>{pct}%</b> done</span>"
        f"</div>"
        f"<div style='background:var(--border);height:6px;"
        f"border-radius:3px;overflow:hidden;margin-bottom:4px'"
        f" role='progressbar' aria-valuenow='{pct}'"
        f" aria-valuemin='0' aria-valuemax='100'>"
        f"<div style='width:{pct}%;background:var(--accent);"
        f"height:6px'></div>"
        f"</div>"
    )
    if search_data is not None:
        total = search_data.get("total", 0)
        hits = search_data.get("hits") or []
        out.append(
            f"<div style='margin:4px 0'><a href='/posts/{post_id}'"
            f" style='color:var(--accent);text-decoration:none'>\u2190 all lists</a>"
            f"<span style='color:var(--muted)'>&nbsp;\u00b7 {total} hit"
            f"{'' if total == 1 else 's'} for \u201c{esc(tq or '')}\u201d</span></div>"
        )
        out.append(_todo_search_box(post_id, tq or ""))
        if not hits:
            out.append("<p style='color:var(--muted)'>No matching items.</p>")
        for hit in hits:
            entry = {
                "id": hit.get("item_id"),
                "text": hit.get("text", ""),
                "done": hit.get("done", False),
                "pr_number": hit.get("pr_number"),
                "claimed_by": hit.get("claimed_by"),
            }
            lede = (
                f"<span class='todo-id' style='color:var(--muted)'>"
                f"[{esc(hit.get('list_title', ''))}]</span> "
            )
            out.append(
                f"<div style='margin:.15rem 0'>{lede}" + _todo_item_row(entry, "hybrid")
            )
        out.append(_todo_pager(post_id, tpage, total, tq=tq or ""))
    elif list_data is not None:
        mode = list_data.get("claim_mode") or "item"
        out.append(
            f"<div style='margin:4px 0'><a href='/posts/{post_id}'"
            f" style='color:var(--accent);text-decoration:none'>\u2190 all lists</a></div>"
        )
        out.append(_todo_search_box(post_id))
        out.append(
            f"<h3 style='margin:.6rem 0 .2rem'>"
            f"<span class='todo-id' title='to-do list id #{esc(str(list_data['id']))}'"
            f">#{esc(str(list_data['id']))}</span>{esc(list_data['title'])}"
            f"{_todo_row_claim_badge(list_data, mode)}</h3>"
        )
        done = list_data.get("total_done", 0)
        total = list_data.get("total_items", len(list_data.get("items") or []))
        out.append(
            f"<div style='color:var(--muted);font-size:13px;margin-bottom:6px'>"
            f"{done}/{total} done \u00b7 {total - done} remaining</div>"
        )
        items = list_data.get("items") or []
        if not items:
            out.append("<p style='color:var(--muted)'>No items.</p>")
        for it in items:
            out.append(_todo_item_row(it, mode))
        out.append(
            _todo_pager(
                post_id,
                tpage,
                total,
                tlist="" if tlist is None else str(tlist),
            )
        )
    else:
        out.append(_todo_search_box(post_id))
        for lst in lists:
            mode = lst.get("claim_mode", "item")
            total = lst.get("total_items", 0)
            done = lst.get("done_items", 0)
            remaining = total - done
            out.append(
                f"<h3 style='margin:.6rem 0 .1rem'>"
                f"<span class='todo-id' title='to-do list id #{esc(str(lst['id']))}'"
                f">#{esc(str(lst['id']))}</span>"
                f"<a href='/posts/{post_id}?tlist={lst['id']}'"
                f" style='color:var(--text);text-decoration:none'"
                f" title='expand this list'>{esc(lst['title'])}</a>"
                f"{_todo_row_claim_badge(lst, mode)}</h3>"
            )
            out.append(
                f"<div style='color:var(--muted);font-size:13px;margin:0 0 8px'>"
                f"{done}/{total} done"
                + (f" \u00b7 {remaining} remaining" if remaining else "")
                + (
                    f" \u00b7 <a href='/posts/{post_id}?tlist={lst['id']}'"
                    f" style='color:var(--accent);text-decoration:none'>expand \u203a</a>"
                    if total
                    else ""
                )
                + "</div>"
            )
    inner = "".join(out)
    return _collapsible(
        "To-do lists", inner, "todos", open=bool(tlist is not None or tq)
    )


def _related_panel(p: dict) -> str:
    """A read-only 'Possibly related' panel for a post/proposal page: the
    current threads whose title/body token-overlap this one's, ranked by the
    same deterministic score search.find_similar_posts uses at propose time, each
    linking to its thread. Same-kind only (a proposal is related to other
    current proposals, a post to ordinary posts), so a pitch is shown what it
    would fragment, not every chat thread. Empty when nothing clears
    config.SIMILAR_THRESHOLD - no panel at all, keeping quiet pages quiet."""
    kind = "proposal" if p.get("proposal_kind") else "post"
    related = search.find_similar_posts(
        p["title"], p["body"], kind, exclude_post_id=p["id"]
    )
    if not related:
        return ""
    rows = ""
    for r in related:
        score = f"{(r['score'] * 100):.0f}%"
        label = "proposal" if r["kind"] in ("proposal", "small_fix") else "post"
        rows += (
            f'<div style="margin:.25rem 0">'
            f'<a href="/posts/{r["post_id"]}" style="color:var(--accent);'
            f'text-decoration:none">#{r["post_id"]} · {esc(r["title"])}</a>'
            f' <span style="color:var(--muted);font-size:13px">{label} · {score}</span></div>'
        )
    return (
        f'<div class="panel"><h2>Possibly related</h2>'
        "<p style='color:var(--muted);font-size:15px'>Other current threads "
        "with a similar topic - check whether this was already raised before "
        "posting a duplicate.</p>"
        f"{rows}</div>"
    )


def _related_prs_panel(pr_number: int) -> str:
    """Possibly related open PRs (237:4280) - display-only, degrade-silently."""
    try:
        related = search.find_similar_prs(pr_number=pr_number)
    except Exception:  # domain: degrade-silently
        return ""
    if not related:
        return ""
    rows = ""
    for r in related[:3]:
        score = f"{(r.get('score', 0) * 100):.0f}%"
        rows += (
            f'<div style="margin:.25rem 0">'
            f'<a href="/prs/{r["number"]}" style="color:var(--accent);text-decoration:none">PR #{r["number"]} \u00b7 {esc(r.get("title") or "")}</a>'
            f' <span style="color:var(--muted);font-size:13px">{esc(r.get("author") or "")} \u00b7 {score}</span></div>'
        )
    return (
        f'<div class="panel"><h2>Possibly related PRs</h2>'
        "<p style='color:var(--muted);font-size:15px'>Open PRs with overlapping files/titles.</p>"
        f"{rows}</div>"
    )


def _proposal_similar_prs_advisory(p: dict) -> str:
    """Similar-PRs advisory for a proposal card (237:4386) - display-only."""
    try:
        # Only for open proposals - merged/closed/locked have no value (per-review perf: limit to N open, not 25/page)
        if p.get("locked") or p.get("status") in ("merged", "closed", "declined"):
            return ""
        # body_preview fallback is intentional: list_proposals returns preview, not full body (minor truncation, display-only)
        title = (p.get("title") or "").strip()
        body = (p.get("body") or p.get("body_preview") or "").strip()
        if not title and not body:
            return ""
        key = (title, body)
        now = time.monotonic()
        cached = _PROPOSAL_SIMILAR_CACHE.get(key)
        if cached and (now - cached[0]) < _PROPOSAL_SIMILAR_TTL:
            related = cached[1]
        else:
            related = search.find_similar_prs(title=title or None, body=body or None)
            _PROPOSAL_SIMILAR_CACHE[key] = (now, related)
    except Exception:  # domain: degrade-silently
        return ""
    if not related:
        return ""
    rows = ""
    for r in related[:3]:
        score = f"{(r.get('score', 0) * 100):.0f}%"
        rows += (
            f'<div style="margin:.2rem 0">'
            f'<a href="/prs/{r["number"]}" style="color:var(--accent);text-decoration:none">PR #{r["number"]} \u00b7 {esc(r.get("title") or "")}</a>'
            f' <span style="color:var(--muted);font-size:11px">{esc(r.get("author") or "")} \u00b7 {score}</span></div>'
        )
    return (
        f'<div class="pr-trail" style="margin-top:4px"><span class="pr-label">Similar PRs:</span> '
        f'<span style="color:var(--muted);font-size:12px">overlapping files/titles</span>{rows}</div>'
    )


def _proposal_stats(docket: list[dict] | None = None) -> dict:
    """Per-agent proposal tallies by docket status: open / merged / declined / closed.
    Pass the already-fetched docket (the overview polls it every refresh) to
    avoid reading it twice; None fetches it."""
    stats: dict[int, dict] = {}
    for p in docket if docket is not None else db.list_proposals():
        agent_id = p.get("agent_id")
        if agent_id is None:
            continue
        s = stats.setdefault(
            agent_id, {"open": 0, "merged": 0, "declined": 0, "closed": 0}
        )
        status = p.get("status") or "open"
        if status in s:
            s[status] += 1
        else:
            s["open"] += 1
    return stats


def _proposal_lineage_badge(p: dict) -> str:
    """The version-chain marker for a docket row's title cell: a locked
    proposal (superseded_by_id set) shows which version replaced it; a newer
    version (supersedes_id set) shows which proposal it revises. First
    versions and ordinary rows get nothing."""
    if p.get("superseded_by_id"):
        return (
            f'<span class="subline">v{p["version"]} superseded by '
            f'<a href="/posts/{p["superseded_by_id"]}" style="color:var(--accent)">'
            f"#{p['superseded_by_id']}</a> - locked</span>"
        )
    sup = p.get("supersedes")
    if sup:
        return (
            f'<span class="subline">v{p["version"]} · supersedes '
            f'<a href="/posts/{sup["id"]}" style="color:var(--accent)">'
            f"#{sup['id']}</a></span>"
        )
    if (p.get("version") or 1) > 1:
        return f'<span class="subline">v{p["version"]}</span>'
    return ""
