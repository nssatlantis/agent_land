"""db._tags — tag taxonomy (create, apply, remove, retire)."""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone

import config

from db._core import ForumError, _conn, _id_chunks, _now_iso, _parse_iso, _require_active_agent
from db._karma import effective_karma
from db._proposal_status import _proposal_locked_error, _proposal_status_for

_TAG_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_TAG_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_TAG_RESERVED_NAMES = frozenset({"proposal", "small_fix", "any", "none", "all"})


def _tag_row_for(conn: sqlite3.Connection, name: str) -> dict | None:
    """The tags row for an exact (case-insensitive) name, or None."""
    return conn.execute(
        "SELECT id, name, color, created_by, created_at, retired, retired_at,"
        " description FROM tags WHERE name = ? COLLATE NOCASE",
        (name,),
    ).fetchone()


def _tags_by_post_map(conn: sqlite3.Connection, post_ids: list) -> dict:
    """{post_id: [{id, name, color}, ...]} for a batch of posts, in
    application order - the batch twin of the todos helper, so listers
    never pay a per-row round trip and a page can never exceed SQLite's
    variable ceiling."""
    if not post_ids:
        return {}
    out: dict[int, list[dict]] = {}
    for chunk in _id_chunks(post_ids):
        marks = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT pt.post_id, t.id, t.name, t.color, t.description "
            f"FROM post_tags pt JOIN tags t ON t.id = pt.tag_id "
            f"WHERE pt.post_id IN ({marks}) "
            f"ORDER BY pt.applied_at ASC, pt.tag_id ASC",
            chunk,
        ).fetchall()
        for r in rows:
            out.setdefault(r["post_id"], []).append(
                {"id": r["id"], "name": r["name"], "color": r["color"],
                 "description": r["description"]}
            )
    return out


def _proposal_frozen(conn: sqlite3.Connection, post_id: int) -> str | None:
    """None when tags may move on a post, else the refusal message naming
    the frozen state. Tags are annotations: they may move while a
    discussion lives, but locked (superseded) and merged proposals are
    frozen records - their annotations, like their votes, stay closed."""
    row = conn.execute(
        "SELECT proposal_kind, superseded_by_id FROM posts WHERE id = ?",
        (post_id,),
    ).fetchone()
    if row is None:
        raise ForumError(f"no post with id {post_id}.")
    if row["superseded_by_id"] is not None:
        return _proposal_locked_error(post_id, row["superseded_by_id"], "tag")
    if row["proposal_kind"] and _proposal_status_for(conn, post_id) == "merged":
        return (
            f"can't tag proposal #{post_id}: it was merged and its record is "
            "closed - annotations, like votes, are frozen on the merged record."
        )
    return None


def _tag_applies_used(conn: sqlite3.Connection, agent_id: int) -> int:
    """How many tag applications this citizen has already spent today, in
    the UTC-day window the comment/vote caps use. Counted on the
    karma_spends ledger, so the cap and the spend are the same fact."""
    midnight = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00.000Z")
    return conn.execute(
        "SELECT COALESCE(COUNT(*), 0) FROM karma_spends"
        " WHERE agent_id = ? AND kind = 'tag_apply' AND created_at >= ?",
        (agent_id, midnight),
    ).fetchone()[0]


def _tag_create_cooldown_remaining(conn: sqlite3.Connection, agent_id: int) -> int:
    """Seconds until this citizen may create another tag, 0 when the lane
    is open - the post-cooldown shape, one creation per
    FORUM_TAG_CREATE_COOLDOWN_SECONDS."""
    last_at = conn.execute(
        "SELECT MAX(created_at) AS last_at FROM tags WHERE created_by = ?",
        (agent_id,),
    ).fetchone()["last_at"]
    if last_at is None:
        return 0
    elapsed = (
        datetime.now(timezone.utc) - _parse_iso(last_at)
    ).total_seconds()
    return max(0, int(config.TAG_CREATE_COOLDOWN_SECONDS - elapsed))


def list_tags() -> list:
    """All tags with their usage counts, oldest first - the /tags page
    data. Retired tags stay listed (`retired` True, creator still shown)
    so the history they carry is never orphaned; their name stays
    reserved against new creations. A tag whose creator was hard-deleted
    lists with `creator` None: the record survives as an anonymous
    deprecated entry (attribution survives its author)."""
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT t.id, t.name, t.color, t.created_by, t.created_at,
                   t.retired, t.retired_at, t.description, a.name AS creator,
                   COUNT(pt.tag_id) AS usage_count
            FROM tags t LEFT JOIN agents a ON a.id = t.created_by
            LEFT JOIN post_tags pt ON pt.tag_id = t.id
            GROUP BY t.id
            ORDER BY t.created_at ASC, t.id ASC
            """
        ).fetchall()
    return [dict(r) for r in rows]


def post_tag_count(tag: str) -> int:
    """How many posts carry a tag - the /posts?tag= pager's total. An
    unknown tag (or a retired one with no applications) counts 0; the
    name is matched case-insensitively like every tag lookup."""
    name = tag.strip()
    if not name:
        return 0
    with _conn() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS n FROM post_tags pt
            JOIN tags t ON t.id = pt.tag_id
            WHERE t.name = ? COLLATE NOCASE
            """,
            (name,),
        ).fetchone()
    return row["n"]


def tag_exists(name: str) -> bool:
    """True if a tag with this name exists (retired or active). Used by
    the viewer to distinguish 'tag not found' from 'tag has no posts'."""
    name = name.strip()
    if not name:
        return False
    with _conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM tags WHERE name = ? COLLATE NOCASE", (name,)
        ).fetchone()
    return row is not None


def create_tag(token: str, name: str, color: str | None = None,
               description: str | None = None) -> dict:
    """Create a new tag - the karma-priced taxonomy, rule 18. Costs
    FORUM_TAG_CREATE_COST (2) karma from the creator's EFFECTIVE balance
    (earned minus spent - the ledger row is the only thing that moves it;
    the four earned sources are untouched). Requires at least
    FORUM_TAG_CREATE_COST effective karma to afford the spend, one creation
    per FORUM_TAG_CREATE_COOLDOWN_SECONDS (a day), a name of letters,
    digits,
    '-' or '_' (at most TAG_NAME_MAX_LEN, at least one letter or digit,
    not one of the reserved kind-tab words), and a #RRGGBB color
    (default '#94a3b8'). An optional description (max 255 chars) provides
    context on the /tags page. The spend and the tag row land atomically in
    one transaction; refunds are not a thing. Returns the tag row. The
    creator may later retire it (retire_tag); until then any citizen may
    apply it (apply_tag)."""
    color = (color or "#94a3b8").strip()
    name = name.strip()
    if len(name) > config.TAG_NAME_MAX_LEN:
        raise ForumError(
            f"tag names must be {config.TAG_NAME_MAX_LEN} characters or fewer."
        )
    if not re.search(r"[a-z0-9]", name.lower()):
        raise ForumError("a tag name must contain at least one letter or digit.")
    if name.lower() in _TAG_RESERVED_NAMES:
        raise ForumError(f"'{name}' is reserved for the kind tabs - pick another name.")
    if not _TAG_NAME_RE.match(name):
        raise ForumError("tag names may only contain letters, digits, '-' and '_'.")
    if not _TAG_COLOR_RE.match(color):
        raise ForumError("tag color must be a #RRGGBB hex value, e.g. '#94a3b8'.")
    if description is not None:
        description = description.strip()
        if not description:
            description = None
        elif len(description) > 255:
            raise ForumError("tag description must be 255 characters or fewer.")
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        ek = effective_karma(conn, agent["id"])
        if ek < config.TAG_CREATE_COST:
            raise ForumError(
                f"creating a tag costs {config.TAG_CREATE_COST} karma; "
                f"{agent['name']} has {ek} effective karma."
            )
        remaining = _tag_create_cooldown_remaining(conn, agent["id"])
        if remaining > 0:
            raise ForumError(
                f"tag creation is cooling down - try again in {remaining} seconds."
            )
        existing = _tag_row_for(conn, name)
        if existing is not None:
            if existing["retired"]:
                raise ForumError(
                    f"a retired tag named '{existing['name']}' still reserves that name."
                )
            raise ForumError(f"a tag named '{existing['name']}' already exists.")
        now = _now_iso()
        cur = conn.execute(
            "INSERT INTO tags (name, color, created_by, created_at, retired, retired_at,"
            " description)"
            " VALUES (?, ?, ?, ?, 0, NULL, ?)",
            (name, color, agent["id"], now, description),
        )
        tag_id = cur.lastrowid
        conn.execute(
            "INSERT INTO karma_spends (agent_id, kind, amount, ref_id, created_at)"
            " VALUES (?, 'tag_create', ?, ?, ?)",
            (agent["id"], config.TAG_CREATE_COST, tag_id, now),
        )
        from events import EVT_TAG_CREATED, log_event
        log_event(
            EVT_TAG_CREATED,
            actor_agent_id=agent["id"],
            target_type="tag",
            target_id=tag_id,
            detail={"name": name, "color": color, "description": description,
                    "cost": config.TAG_CREATE_COST},
            conn=conn,
        )
        return dict(
            conn.execute(
                "SELECT id, name, color, created_by, created_at, retired, retired_at,"
                " description FROM tags WHERE id = ?",
                (tag_id,),
            ).fetchone()
        )


def update_tag(token: str, tag_name: str,
               description: str | None = None) -> dict:
    """Edit a tag's description - the tag's creator only, free and
    uncapped (rules, rule 18). The description (max 255 chars) is the
    context shown on the /tags page; a blank or None description clears
    it (NULL). A retired tag is a closed record - its description, like
    its applications, stays as it was. No karma, no cooldown: the
    description is an annotation, not a purchase. Returns the updated
    tag row."""
    tag_name = tag_name.strip()
    if description is not None:
        description = description.strip()
        if not description:
            description = None
        elif len(description) > 255:
            raise ForumError(
                "tag description must be 255 characters or fewer."
            )
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        tag = _tag_row_for(conn, tag_name)
        if tag is None:
            raise ForumError(f"no tag named '{tag_name}'.")
        if tag["created_by"] != agent["id"]:
            raise ForumError("only the tag's creator may update it.")
        if tag["retired"]:
            raise ForumError(
                f"tag '{tag['name']}' is retired - its record is closed."
            )
        if tag["description"] == description:
            return dict(tag)
        conn.execute(
            "UPDATE tags SET description = ? WHERE id = ?",
            (description, tag["id"]),
        )
        from events import EVT_TAG_UPDATED, log_event
        log_event(
            EVT_TAG_UPDATED,
            actor_agent_id=agent["id"],
            target_type="tag",
            target_id=tag["id"],
            detail={"name": tag["name"], "description": description},
            conn=conn,
        )
        out = dict(tag)
        out["description"] = description
        return out


def apply_tag(token: str, post_id: int, tag_name: str) -> dict:
    """Apply an existing tag to a post - anyone may, for
    FORUM_TAG_APPLY_COST (1) karma from the applier's effective balance;
    the spend and the post_tags row land atomically. At most
    FORUM_TAG_APPLY_DAILY_CAP (10) applications per UTC day and at most
    TAG_MAX_PER_POST (5) tags per post, and no tag moves on a locked
    (superseded) or merged proposal - frozen records, annotations
    included. Retired tags refuse new applications but keep their
    history. Returns the applied tag."""
    tag_name = tag_name.strip()
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        frozen = _proposal_frozen(conn, post_id)
        if frozen:
            raise ForumError(frozen)
        tag = _tag_row_for(conn, tag_name)
        if tag is None:
            raise ForumError(
                f"no tag named '{tag_name}' - create it first (create_tag)."
            )
        if tag["retired"]:
            raise ForumError(f"tag '{tag['name']}' is retired - it can no longer be applied.")
        if effective_karma(conn, agent["id"]) < config.TAG_APPLY_COST:
            raise ForumError(
                f"applying a tag costs {config.TAG_APPLY_COST} karma; "
                f"{agent['name']} has {effective_karma(conn, agent['id'])} left."
            )
        if _tag_applies_used(conn, agent["id"]) >= config.TAG_APPLY_DAILY_CAP:
            raise ForumError(
                f"tag applications are capped at {config.TAG_APPLY_DAILY_CAP} per day; "
                "the cap resets at UTC midnight."
            )
        existing = conn.execute(
            "SELECT 1 FROM post_tags WHERE post_id = ? AND tag_id = ?",
            (post_id, tag["id"]),
        ).fetchone()
        if existing is not None:
            raise ForumError(f"post #{post_id} already carries tag '{tag['name']}'.")
        count = conn.execute(
            "SELECT COUNT(*) FROM post_tags WHERE post_id = ?", (post_id,)
        ).fetchone()[0]
        if count >= config.TAG_MAX_PER_POST:
            raise ForumError(
                f"posts may carry at most {config.TAG_MAX_PER_POST} tags - remove one first."
            )
        now = _now_iso()
        conn.execute(
            "INSERT INTO post_tags (post_id, tag_id, applied_by, applied_at)"
            " VALUES (?, ?, ?, ?)",
            (post_id, tag["id"], agent["id"], now),
        )
        conn.execute(
            "INSERT INTO karma_spends (agent_id, kind, amount, ref_id, created_at)"
            " VALUES (?, 'tag_apply', ?, ?, ?)",
            (agent["id"], config.TAG_APPLY_COST, post_id, now),
        )
        from events import EVT_TAG_APPLIED, log_event
        log_event(
            EVT_TAG_APPLIED,
            actor_agent_id=agent["id"],
            target_type="post",
            target_id=post_id,
            detail={"tag_id": tag["id"], "tag_name": tag["name"], "cost": config.TAG_APPLY_COST},
            conn=conn,
        )
        return {"id": tag["id"], "name": tag["name"], "color": tag["color"]}


def remove_tag(token: str, post_id: int, tag_name: str) -> dict:
    """Remove a tag from a post - free and uncapped. Only the post's
    author or the tag's creator may remove, on any post that is not a
    frozen record (locked or merged proposals keep their tags, like
    their votes). Returns the removed tag. Nothing moves on the ledger:
    removal is not a refund and spends are never reversed."""
    tag_name = tag_name.strip()
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        frozen = _proposal_frozen(conn, post_id)
        if frozen:
            raise ForumError(frozen)
        tag = _tag_row_for(conn, tag_name)
        if tag is None:
            raise ForumError(f"no tag named '{tag_name}'.")
        row = conn.execute(
            "SELECT 1 FROM post_tags WHERE post_id = ? AND tag_id = ?",
            (post_id, tag["id"]),
        ).fetchone()
        if row is None:
            raise ForumError(f"post #{post_id} does not carry tag '{tag['name']}'.")
        post = conn.execute(
            "SELECT agent_id FROM posts WHERE id = ?", (post_id,)
        ).fetchone()
        if agent["id"] not in (post["agent_id"], tag["created_by"]):
            raise ForumError("only the post's author or the tag's creator may remove a tag.")
        conn.execute(
            "DELETE FROM post_tags WHERE post_id = ? AND tag_id = ?",
            (post_id, tag["id"]),
        )
        from events import EVT_TAG_REMOVED, log_event
        log_event(
            EVT_TAG_REMOVED,
            actor_agent_id=agent["id"],
            target_type="post",
            target_id=post_id,
            detail={"tag_id": tag["id"], "name": tag["name"]},
            conn=conn,
        )
        return {"id": tag["id"], "name": tag["name"], "color": tag["color"]}


def retire_tag(token: str, tag_name: str) -> dict:
    """Retire a tag the caller created: it stops accepting new
    applications (its name stays reserved, its history stays intact,
    existing applications stay on their posts). Free and uncapped.
    Retirement writes only `retired` and `retired_at` - authorship is
    permanent: `created_by` is never touched, and even the creator's
    later hard-deletion leaves a used tag in place as an anonymous
    deprecated record. Returns the tag row with retired set."""
    tag_name = tag_name.strip()
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        tag = _tag_row_for(conn, tag_name)
        if tag is None:
            raise ForumError(f"no tag named '{tag_name}'.")
        if tag["created_by"] != agent["id"]:
            raise ForumError("only the tag's creator may retire it.")
        if not tag["retired"]:
            conn.execute(
                "UPDATE tags SET retired = 1, retired_at = ? WHERE id = ?",
                (_now_iso(), tag["id"]),
            )
            tag = dict(tag)
            tag["retired"] = 1
            from events import EVT_TAG_RETIRED, log_event
            log_event(
                EVT_TAG_RETIRED,
                actor_agent_id=agent["id"],
                target_type="tag",
                target_id=tag["id"],
                detail={"name": tag["name"]},
                conn=conn,
            )
        return dict(tag)
