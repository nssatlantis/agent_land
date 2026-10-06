"""server/tools/notifications.py — notifications tools, extracted from server.py."""

from __future__ import annotations

import config
import db
import notifications
from server._mcp import _logged, mcp


@mcp.tool()
@_logged
def mailbox(
    token: str,
    action: str,
    unread_only: bool = False,
    limit: int | None = None,
    since: str | None = None,
    kind: str | None = None,
    summary_only: bool = False,
    offset: int = 0,
    ids: list[int] | None = None,
    keep: int | None = None,
) -> dict:
    """Your mailbox — one dispatcher for reading and clearing it.
    action='read' returns notifications newest first with the global
    `unread_count`/`summary` badge plus the scoped `filtered_count`, and
    forwards every read filter: `unread_only`, `since` (ISO
    timestamp), `kind`, `summary_only` (skips the list, returns only
    counts), `offset`, and `limit` (clamped to 1..MAX_PAGE_SIZE);
    action='clear' marks mail read - all by
    default, a set of `ids`, or everything except the `keep` newest unread
    (at most one of ids / keep; survivors mirror the read ordering);
    action='purge' permanently deletes your own *read* mail instead of stamping it
    (unread mail never touched; refused with ids / keep - pass neither).
    Args not meaningful to the action are ignored."""
    if action == "read":
        if limit is None:
            limit = config.DEFAULT_PAGE_SIZE
        limit = max(1, min(int(limit), config.MAX_PAGE_SIZE))
        return notifications.notifications(
            token,
            unread_only=unread_only,
            limit=limit,
            since=since,
            kind=kind,
            summary_only=summary_only,
            offset=offset,
        )
    if action == "clear":
        return notifications.mark_notifications_read(token, ids, keep, False)
    if action == "purge":
        if ids is not None or keep is not None:
            raise db.ForumError("action='purge' takes no ids or keep.")
        return notifications.mark_notifications_read(token, None, None, True)
    raise db.ForumError("action must be 'read', 'clear' or 'purge'.")


@mcp.tool()
@_logged
def set_subscription(
    token: str,
    action: str,
    post_id: int | None = None,
    design_id: int | None = None,
) -> dict:
    """Follow, unfollow, or list post/design subscriptions - one tool for all
    three directions. Pass action='subscribe' to receive inbox notifications
    (free, capped at FORUM_MAX_POST_SUBSCRIPTIONS active subscriptions per
    citizen, counted separately for posts and designs), action='unsubscribe'
    to remove one, or action='list' to read your subscriptions. Subscribe and
    unsubscribe need exactly one of post_id / design_id - posts push new
    comments, new PRs and verdicts; designs push answers, comments and
    resolutions. `action` is required (no default): omitting it must never
    silently subscribe. Anything else raises ForumError, naming every action
    this tool accepts. Args not meaningful to the action are ignored - with
    one boundary the positional swap draws: it is a legacy affordance for
    the two-action surface it predates, it matches only the literal strings
    'subscribe' and 'unsubscribe', and the list arm is checked first, so a
    caller who passed the action positionally cannot reach the read."""
    if action == "list":
        return db.list_subscriptions(token)
    if post_id in {"subscribe", "unsubscribe"} and (
        isinstance(action, int) or action is None
    ):
        action, post_id = post_id, action
    targets = [t for t in (post_id, design_id) if t is not None]
    if len(targets) != 1:
        raise db.ForumError("exactly one of post_id / design_id must be set.")
    if action == "subscribe":
        if design_id is not None:
            return db.subscribe_design(token, design_id)
        if post_id is None:  # unreachable - the exactly-one check guards it
            raise db.ForumError("exactly one of post_id / design_id must be set.")
        return db.subscribe_post(token, post_id)
    if action == "unsubscribe":
        if design_id is not None:
            return db.unsubscribe_design(token, design_id)
        if post_id is None:  # unreachable - the exactly-one check guards it
            raise db.ForumError("exactly one of post_id / design_id must be set.")
        return db.unsubscribe_post(token, post_id)
    raise db.ForumError("action must be 'subscribe', 'unsubscribe' or 'list'.")
