"""viewer._feed_helpers - sidebar and feed generation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import config
from db import (
    _content,
    _cooldown,
    _karma,
)

if TYPE_CHECKING:
    pass

_KARMA_WEIGHT_VOTES = 2
_KARMA_WEIGHT_REPLIES = 3

_RECENT_WINDOW = timedelta(days=14)
_SCORE_HALF_LIFE = timedelta(hours=36)

_VISIBLE_THRESHOLD = 1

_POLL_FRACTION = 0.1
_POLL_MAX = 10
_POLL_MIN = 3


def _poll_count(total: int) -> int:
    n = max(_POLL_MIN, min(_POLL_MAX, int(total * _POLL_FRACTION)))
    return n


def _score(post: dict, now: datetime) -> float:
    age_hours = (now - post["created_at"]).total_seconds() / 3600
    karma = post["score"]
    return karma - 0.001 * age_hours


def _rank_posts(posts: list[dict], now: datetime) -> list[dict]:
    by_karma: dict[int, list[dict]] = {}
    for post in posts:
        k = post["score"]
        by_karma.setdefault(k, []).append(post)
    ranked = []
    for k in sorted(by_karma, reverse=True):
        bucket = by_karma[k]
        bucket.sort(key=lambda p: p["created_at"], reverse=True)
        ranked.extend(bucket)
    return ranked


def _side_rail(now: datetime, show_proposals: bool) -> str:
    """Generate sidebar HTML with recent + hot + proposals tabs."""
    recent = _recent_posts(now)
    hot = _hot_posts(now)
    proposals = _proposal_feed(now) if show_proposals else []
    recent_html = _render_rail_section("Recent", recent[:5], now)
    hot_html = _render_rail_section("Hot", hot[:5], now)
    proposals_html = (
        _render_rail_section("Proposals", proposals[:5], now)
        if show_proposals
        else ""
    )
    return f'<div class="side-rail">{recent_html}{hot_html}{proposals_html}</div>'


_post_id_cache = TTLCache(ttl_seconds=60.0)
_side_rail_cache: TTLCache[str] = TTLCache(ttl_seconds=60.0)


def side_rail_html(show_proposals: bool) -> str:
    """Return the sidebar HTML, cached for 60 seconds."""
    key = show_proposals
    return _side_rail_cache.get_or_compute(key, lambda: _build_side_rail(show_proposals))


def _build_side_rail(show_proposals: bool) -> str:
    now = datetime.now(timezone.utc)
    return _side_rail(now, show_proposals)


def _recent_posts(now: datetime) -> list[dict]:
    window = now - _RECENT_WINDOW
    rows = _content.list_posts(
        limit=30,
        sort="newest",
        since=window,
        proposal_kind="none",
    )
    return [r for r in rows if r["score"] >= _VISIBLE_THRESHOLD]


def _hot_posts(now: datetime) -> list[dict]:
    cutoff = now - timedelta(days=60)
    rows = _content.list_posts(
        limit=100,
        sort="newest",
        since=cutoff,
        proposal_kind="none",
    )
    scored = [_score(p, now) for p in rows]
    best = sorted(zip(scored, rows), reverse=True)
    return [p for _, p in best[:20] if _score(p, now) >= _VISIBLE_THRESHOLD]


def _proposal_feed(now: datetime) -> list[dict]:
    cutoff = now - timedelta(days=30)
    return _content.list_posts(
        limit=20,
        sort="newest",
        since=cutoff,
        proposal_kind="any",
    )


def _render_rail_section(
    title: str, posts: list[dict], now: datetime
) -> str:
    if not posts:
        return f'<div class="rail-section"><h4>{title}</h4><p class="muted">None yet.</p></div>'
    items = "\n".join(
        f'<li><a href="/posts/{p["id"]}">{html.escape(p["title"])}</a></li>'
        for p in posts
    )
    return (
        f'<div class="rail-section">'
        f'<h4>{title}</h4>'
        f'<ol>{items}</ol>'
        f'<p class="muted">{len(posts)} posts</p>'
        f"</div>"
    )


# -----------------------------------------------------------------------------
# Feed pagination
# -----------------------------------------------------------------------------

_PAGE_SIZE = 20
_PAGE_NEIGHBOURS = 2


def paginated_feed(
    posts: list[dict],
    page: int,
    total: int,
    base_url: str,
) -> str:
    """Return a pagination control HTML for the given page / total."""
    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    if total_pages <= 1:
        return ""
    pages = _page_window(page, total_pages)
    items = []
    if page > 1:
        items.append(
            f'<li><a href="{base_url}?page={page-1}" aria-label="prev">&laquo;</a></li>'
        )
    for p in pages:
        if p == page:
            items.append(f'<li class="active">{p}</li>')
        elif p == -1:
            items.append('<li class="disabled"><span>…</span></li>')
        else:
            items.append(f'<li><a href="{base_url}?page={p}">{p}</a></li>')
    if page < total_pages:
        items.append(
            f'<li><a href="{base_url}?page={page+1}" aria-label="next">&raquo;</a></li>'
        )
    return f'<ul class="pagination">{chr(10).join(items)}</ul>'


def _page_window(page: int, total: int) -> list[int]:
    window = []
    for p in range(
        max(1, page - _PAGE_NEIGHBOURS),
        min(total, page + _PAGE_NEIGHBOURS) + 1,
    ):
        if window and p - window[-1] > 1:
            window.append(-1)
        window.append(p)
    return window


def post_ids(page: int) -> tuple[list[int], int]:
    """Return (post_ids, total_count) for page, cached for 60 seconds."""
    key = page
    cached = _post_id_cache.get(key)
    if cached is not None:
        return cached
    total = _content.count_posts(proposal_kind="none")
    offset = (page - 1) * _PAGE_SIZE
    rows = _content.list_posts(
        limit=_PAGE_SIZE,
        offset=offset,
        sort="newest",
        proposal_kind="none",
    )
    ids = [r["id"] for r in rows]
    result = (ids, total)
    _post_id_cache.set(key, result)
    return result