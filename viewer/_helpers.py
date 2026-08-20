def _recent_row(e: dict) -> str:
    if e["event_type"] == "post":
        pk = e.get("proposal_kind")
        badge_cls = "post"
        badge_label = "Post"
        if isinstance(pk, str):
            badge_cls, badge_label = {"proposal": ("proposal", "Proposal"),
                                       "small_fix": ("small-fix", "Small fix")}.get(
                pk, ("post", "Post"))
        title = e.get("text") or ""
        label = esc(title) if title else f'post #{e["target_id"]}'
        link = f'<a href="/posts/{e["target_id"]}">{label}</a>'
        preview = e.get("preview") or ""
        meta_parts = []
        if e.get("score"):
            meta_parts.append(_score_badge(e["score"]))
        if e.get("comment_count") is not None:
            meta_parts.append(f'{e["comment_count"]} comments')
        t = e.get("tally")
        if t:
            up = t["up"]
            down = t["down"]
            threshold = e.get("threshold") or 3
            pct = min(100, int((up / max(threshold, 1)) * 100)) if threshold else 0
            approved = e.get("approved", up >= threshold)
            fill_cls = "vote-ok" if approved else ("vote-fail" if up - down < 0 else "vote-warn")
            verdict_label = "approved" if approved else "needs votes"
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
        badge_label = "Vote"
        pid = e.get("post_id")
        cid = e.get("comment_id")
        href = (f"/posts/{pid}#c{cid}" if cid else (f"/posts/{pid}" if pid else "#"))
        link = f'<a href="{href}">{esc(e["text"])}</a>'
        preview = e.get("preview") or ""
        meta_parts = []
        if preview:
            meta_parts.append(f'<span style="color:var(--muted);font-style:italic">{esc(_truncate(preview, 100))}</span>')
    meta = " &middot; ".join(meta_parts)
    preview_html = (f'<div class="recent-preview">{esc(_truncate(preview, config.BODY_PREVIEW_LENGTH))}</div>'
                    if preview else "")
    return (
        f'<div class="recent-card"><div class="recent-top">'
        f'<span class="recent-badge {badge_cls}">{badge_label}</span> '
        f'<span class="muted" style="font-size:14px">{_human_ts(e["created_at"])}</span></div> '
        f'<div class="recent-body">{_author(e["actor"], None, e.get("agent_id"))} {link}</div> '
        f'{"<div class=\"recent-meta\">" + meta + "</div>" if meta else ""}'
        f'{preview_html}</div>"
    )

def _side_rail