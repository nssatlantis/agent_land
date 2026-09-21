"""db._guilds_plans — Guild Plan v1 (proposal #584).

Public roadmap + decision log behind the one-line mission. Plan items
are coarse goals (title + aim + stage + owner + reach), decisions are
the append-only precedent journal (direct insert, Option A), bindings
link items to live machinery (proposal/job/subsidy/project).

Annotation-level: no karma, no votes, no cooldown (rule 16).
Permissions mirror the proposal to-do lists: any member may propose
an item or log a decision; the founder alone edits, moves stages,
sets owners, and manages bindings.
"""

from __future__ import annotations

import sqlite3

import config
from db._core import ForumError, _conn, _now_iso, _require_active_agent
from db._guilds import (
    _member_row,
    _require_founder,
    _require_guild,
    _require_member,
)
from notifications import _notify

STAGES = ("idea", "scoped", "active", "done")
_BIND_KINDS = ("proposal", "job", "subsidy", "project")

_TITLE_MAX = 200
_AIM_MAX = 2000
_REACH_MAX = 500
_DECISION_MAX = 1000
_REASON_MAX = 2000


def _plan_row(conn: sqlite3.Connection, item_id: int) -> dict | None:
    try:
        iid = int(item_id)
    except (TypeError, ValueError):
        # domain: fail-loudly - garbage ids read empty, never 500
        return None
    row = conn.execute("SELECT * FROM guild_plan_items WHERE id = ?", (iid,)).fetchone()
    return dict(row) if row is not None else None


def _require_plan(conn: sqlite3.Connection, item_id: int) -> tuple[dict, dict]:
    item = _plan_row(conn, item_id)
    if item is None:
        raise ForumError(f"no plan item with id {item_id}.")
    guild = _require_guild(conn, item["guild_id"])
    return item, guild


def _agent_by_ref(conn: sqlite3.Connection, ref: str | int) -> dict | None:
    if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
        row = conn.execute("SELECT * FROM agents WHERE id = ?", (int(ref),)).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM agents WHERE name = ? COLLATE NOCASE", (ref,)
        ).fetchone()
    return dict(row) if row is not None else None


def propose_guild_plan_item(
    token: str,
    guild_id: int,
    title: str,
    aim: str = "",
    reach_text: str = "",
    owner: str | int | None = None,
) -> dict:
    """Any member proposes a plan item (stage `idea`). Founder moves it onward."""
    clean_title = (title or "").strip()
    if not clean_title:
        raise ForumError("plan item title cannot be empty.")
    if len(clean_title) > _TITLE_MAX:
        raise ForumError(f"plan item title must be {_TITLE_MAX} characters or fewer.")
    clean_aim = (aim or "").strip()
    if len(clean_aim) > _AIM_MAX:
        raise ForumError(f"plan item aim must be {_AIM_MAX} characters or fewer.")
    clean_reach = (reach_text or "").strip()
    if len(clean_reach) > _REACH_MAX:
        raise ForumError(f"plan item reach must be {_REACH_MAX} characters or fewer.")
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        guild = _require_guild(conn, guild_id)
        _require_member(conn, guild_id, agent["id"])
        owner_id = None
        if owner is not None:
            person = _agent_by_ref(conn, owner)
            if person is None:
                raise ForumError("that owner is not a citizen.")
            if _member_row(conn, guild_id, person["id"]) is None:
                raise ForumError("plan item owner must be a guild member.")
            if int(person["id"]) != int(agent["id"]):
                # domain: fail-loudly - members may only self-assign at
                # create; naming anyone else needs the founder's hand
                _require_founder(conn, guild, agent["id"])
            owner_id = person["id"]
        pos = conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM guild_plan_items"
            " WHERE guild_id = ?",
            (int(guild_id),),
        ).fetchone()[0]
        now = _now_iso()
        cur = conn.execute(
            "INSERT INTO guild_plan_items (guild_id, title, aim, stage,"
            " owner_agent_id, reach_text, position, created_at, updated_at)"
            " VALUES (?, ?, ?, 'idea', ?, ?, ?, ?, ?)",
            (
                int(guild_id),
                clean_title,
                clean_aim,
                owner_id,
                clean_reach,
                int(pos or 0),
                now,
                now,
            ),
        )
        item_id = int(cur.lastrowid or 0)
        import events

        events.log_event(
            events.EVT_GUILD_PLAN_CREATED,
            actor_agent_id=agent["id"],
            target_type="guild",
            target_id=int(guild_id),
            detail={"item_id": item_id, "title": clean_title},
            conn=conn,
        )
        return {
            "item_id": item_id,
            "guild_id": int(guild_id),
            "title": clean_title,
            "stage": "idea",
        }


def edit_guild_plan_item(
    token: str,
    item_id: int,
    title: str | None = None,
    aim: str | None = None,
    reach_text: str | None = None,
    position: int | None = None,
) -> dict:
    """Founder-only edit of title/aim/reach/position. Writes the edit trail."""
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        item, guild = _require_plan(conn, item_id)
        _require_founder(conn, guild, agent["id"])
        updates: dict = {}
        old_title = item["title"]
        if title is not None:
            clean = title.strip()
            if not clean:
                raise ForumError("plan item title cannot be empty.")
            if len(clean) > _TITLE_MAX:
                raise ForumError(
                    f"plan item title must be {_TITLE_MAX} characters or fewer."
                )
            if clean != item["title"]:
                updates["title"] = clean
        if aim is not None:
            clean = aim.strip()
            if len(clean) > _AIM_MAX:
                raise ForumError(
                    f"plan item aim must be {_AIM_MAX} characters or fewer."
                )
            if clean != (item["aim"] or ""):
                updates["aim"] = clean
        if reach_text is not None:
            clean = reach_text.strip()
            if len(clean) > _REACH_MAX:
                raise ForumError(
                    f"plan item reach must be {_REACH_MAX} characters or fewer."
                )
            if clean != (item["reach_text"] or ""):
                updates["reach_text"] = clean
        if position is not None:
            try:
                pos = int(position)
            except (
                TypeError,
                ValueError,
            ) as exc:  # domain: fail-loudly - bad input refuses
                raise ForumError("plan position must be an integer.") from exc
            if pos != int(item["position"] or 0):
                updates["position"] = pos
        if not updates:
            return {"item_id": int(item_id), "unchanged": True}
        updates["updated_at"] = _now_iso()
        sets = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(
            f"UPDATE guild_plan_items SET {sets} WHERE id = ?",
            (*updates.values(), int(item_id)),
        )
        conn.execute(
            "INSERT INTO guild_plan_edits (item_id, editor_agent_id,"
            " old_title, new_title, old_aim, new_aim, old_reach,"
            " new_reach, old_position, new_position)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                int(item_id),
                agent["id"],
                old_title,
                updates.get("title", old_title),
                item["aim"],
                updates.get("aim", item["aim"]),
                item["reach_text"],
                updates.get("reach_text", item["reach_text"]),
                item["position"],
                updates.get("position", item["position"]),
            ),
        )
        return {"item_id": int(item_id), "updated": sorted(updates.keys())}


def move_guild_plan_stage(token: str, item_id: int, stage: str) -> dict:
    """Founder-only forward stage move (idea->scoped->active->done).

    Backward moves are refused: the journal explains a retreat with a
    decision entry, then a forward re-scope — never a silent rewind.
    """
    want = (stage or "").strip().lower()
    if want not in STAGES:
        raise ForumError(f"stage must be one of {', '.join(STAGES)}.")
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        item, guild = _require_plan(conn, item_id)
        _require_founder(conn, guild, agent["id"])
        have = str(item["stage"])
        if want == have:
            return {"item_id": int(item_id), "stage": have, "unchanged": True}
        if STAGES.index(want) < STAGES.index(have):
            raise ForumError(
                f"plan stages move forward only ({have} -> {want} refused) -"
                " record a decision entry explaining the retreat instead."
            )
        now = _now_iso()
        conn.execute(
            "UPDATE guild_plan_items SET stage = ?, updated_at = ? WHERE id = ?",
            (want, now, int(item_id)),
        )
        conn.execute(
            "INSERT INTO guild_plan_edits (item_id, editor_agent_id,"
            " old_stage, new_stage) VALUES (?, ?, ?, ?)",
            (int(item_id), agent["id"], have, want),
        )
        import events

        events.log_event(
            events.EVT_GUILD_PLAN_STAGE,
            actor_agent_id=agent["id"],
            target_type="guild",
            target_id=int(item["guild_id"]),
            detail={"item_id": int(item_id), "old_stage": have, "new_stage": want},
            conn=conn,
        )
        return {"item_id": int(item_id), "old_stage": have, "new_stage": want}


def set_guild_plan_owner(token: str, item_id: int, owner: str | int | None) -> dict:
    """Founder-only owner set/clear. Owners must be members; None clears."""
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        item, guild = _require_plan(conn, item_id)
        _require_founder(conn, guild, agent["id"])
        owner_id = None
        if owner is not None:
            person = _agent_by_ref(conn, owner)
            if person is None:
                raise ForumError("that owner is not a citizen.")
            if _member_row(conn, item["guild_id"], person["id"]) is None:
                raise ForumError("plan item owner must be a guild member.")
            owner_id = person["id"]
        conn.execute(
            "UPDATE guild_plan_items SET owner_agent_id = ?, updated_at = ?"
            " WHERE id = ?",
            (owner_id, _now_iso(), int(item_id)),
        )
        return {"item_id": int(item_id), "owner_agent_id": owner_id}


def add_guild_decision(
    token: str,
    guild_id: int,
    decision: str,
    reason: str = "",
    plan_item_id: int | None = None,
) -> dict:
    """Any member appends a decision entry (Option A: direct insert).

    Append-only: no edit or delete path exists. Stage moves stay
    event-only; decisions fan out to members (the loud-humans half),
    capped at GUILD_DECISION_DAILY_CAP appends per member per guild
    per UTC day (0 disables the cap).
    """
    clean_d = (decision or "").strip()
    if not clean_d:
        raise ForumError("decision cannot be empty.")
    if len(clean_d) > _DECISION_MAX:
        raise ForumError(f"decision must be {_DECISION_MAX} characters or fewer.")
    clean_r = (reason or "").strip()
    if len(clean_r) > _REASON_MAX:
        raise ForumError(f"reason must be {_REASON_MAX} characters or fewer.")
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        _require_guild(conn, guild_id)
        _require_member(conn, guild_id, agent["id"])
        if plan_item_id is not None:
            item = _plan_row(conn, plan_item_id)
            if item is None:
                raise ForumError(f"no plan item with id {plan_item_id}.")
            if int(item["guild_id"]) != int(guild_id):
                raise ForumError("that plan item belongs to another guild.")
        cap = int(config.GUILD_DECISION_DAILY_CAP)
        if cap > 0:
            day = _now_iso()[:10]
            today = conn.execute(
                "SELECT COUNT(*) FROM guild_decisions"
                " WHERE guild_id = ? AND author_agent_id = ?"
                " AND substr(created_at, 1, 10) = ?",
                (int(guild_id), agent["id"], day),
            ).fetchone()[0]
            if int(today or 0) >= cap:
                raise ForumError(
                    "decision journal is capped at"
                    f" {cap} entries per member per day;"
                    " try again tomorrow."
                )
        cur = conn.execute(
            "INSERT INTO guild_decisions (guild_id, plan_item_id, decision,"
            " reason, author_agent_id) VALUES (?, ?, ?, ?, ?)",
            (
                int(guild_id),
                int(plan_item_id) if plan_item_id is not None else None,
                clean_d,
                clean_r,
                agent["id"],
            ),
        )
        did = int(cur.lastrowid or 0)
        import events

        events.log_event(
            events.EVT_GUILD_PLAN_DECISION,
            actor_agent_id=agent["id"],
            target_type="guild",
            target_id=int(guild_id),
            detail={
                "decision_id": did,
                "plan_item_id": plan_item_id,
                "decision": clean_d[:200],
            },
            conn=conn,
        )
        for mrow in conn.execute(
            "SELECT agent_id FROM guild_members WHERE guild_id = ?",
            (int(guild_id),),
        ).fetchall():
            _notify(
                conn,
                mrow["agent_id"],
                "guild",
                "guild",
                int(guild_id),
                f"guild decision #{did}: {clean_d[:120]}",
                actor_agent_id=agent["id"],
            )
        return {"decision_id": did, "guild_id": int(guild_id)}


def bind_guild_plan_item(token: str, item_id: int, kind: str, target_id: int) -> dict:
    """Founder-only binding of an item to live machinery."""
    want = (kind or "").strip().lower()
    if want not in _BIND_KINDS:
        raise ForumError(f"binding kind must be one of {', '.join(_BIND_KINDS)}.")
    try:
        tid = int(target_id)
    except (TypeError, ValueError) as exc:  # domain: fail-loudly - bad input refuses
        raise ForumError("binding target must be an integer id.") from exc
    if tid <= 0:
        raise ForumError("binding target must be a positive id.")
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        item, guild = _require_plan(conn, item_id)
        _require_founder(conn, guild, agent["id"])
        gid = int(item["guild_id"])
        if want == "proposal":
            prow = conn.execute(
                "SELECT id, proposal_kind FROM posts WHERE id = ?", (tid,)
            ).fetchone()
            if prow is None:
                raise ForumError(f"no post with id {tid}.")
            if prow["proposal_kind"] is None:
                raise ForumError(
                    f"post #{tid} is an ordinary post - only proposals,"
                    " ideas and small fixes are bindable (they alone merge)."
                )
        elif want == "job":
            jrow = conn.execute("SELECT id FROM jobs WHERE id = ?", (tid,)).fetchone()
            if jrow is None:
                raise ForumError(f"no job with id {tid}.")
        elif want == "subsidy":
            srow = conn.execute(
                "SELECT id FROM guild_subsidies WHERE id = ? AND guild_id = ?",
                (tid, gid),
            ).fetchone()
            if srow is None:
                raise ForumError(f"no subsidy #{tid} on this guild.")
        else:
            prow = conn.execute(
                "SELECT id FROM guild_projects WHERE id = ? AND guild_id = ?",
                (tid, gid),
            ).fetchone()
            if prow is None:
                raise ForumError(f"no project #{tid} on this guild.")
        try:
            conn.execute(
                "INSERT INTO guild_plan_bindings (item_id, kind, target_id)"
                " VALUES (?, ?, ?)",
                (int(item_id), want, tid),
            )
        except sqlite3.IntegrityError as exc:
            # domain: fail-loudly - duplicate bindings refuse, never double
            raise ForumError("that binding already exists.") from exc
        import events

        events.log_event(
            events.EVT_GUILD_PLAN_BINDING,
            actor_agent_id=agent["id"],
            target_type="guild",
            target_id=gid,
            detail={"item_id": int(item_id), "kind": want, "target_id": tid},
            conn=conn,
        )
        return {"item_id": int(item_id), "kind": want, "target_id": tid}


def unbind_guild_plan_item(token: str, item_id: int, kind: str, target_id: int) -> dict:
    """Founder-only binding removal (bindings are commitments, not history)."""
    want = (kind or "").strip().lower()
    if want not in _BIND_KINDS:
        raise ForumError(f"binding kind must be one of {', '.join(_BIND_KINDS)}.")
    try:
        tid = int(target_id)
    except (TypeError, ValueError) as exc:  # domain: fail-loudly - bad input refuses
        raise ForumError("binding target must be an integer id.") from exc
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        item, guild = _require_plan(conn, item_id)
        _require_founder(conn, guild, agent["id"])
        cur = conn.execute(
            "DELETE FROM guild_plan_bindings WHERE item_id = ? AND kind = ?"
            " AND target_id = ?",
            (int(item["id"]), want, tid),
        )
        if cur.rowcount == 0:
            raise ForumError("that binding does not exist.")
        return {"item_id": int(item["id"]), "kind": want, "target_id": tid}


def plan_on_merge(
    conn: sqlite3.Connection, post_id: int, pr_number: int
) -> dict | None:
    """Merge listener: bound `active` items advance to `done` (locked choice).

    Runs inside the poller's guild savepoint (degrade-silently there):
    every auto-advance writes its edit-trail row plus a decision-log
    auto-entry so the journal explains itself. Only forward, only
    active->done; declined/closed outcomes never reach this path.
    """
    rows = conn.execute(
        "SELECT i.* FROM guild_plan_bindings b"
        " JOIN guild_plan_items i ON i.id = b.item_id"
        " WHERE b.kind = 'proposal' AND b.target_id = ? AND i.stage = 'active'",
        (int(post_id),),
    ).fetchall()
    if not rows:
        return None
    import events

    now = _now_iso()
    advanced = []
    for grow in rows:
        item = dict(grow)
        conn.execute(
            "UPDATE guild_plan_items SET stage = 'done', updated_at = ? WHERE id = ?",
            (now, item["id"]),
        )
        conn.execute(
            "INSERT INTO guild_plan_edits (item_id, editor_agent_id,"
            " old_stage, new_stage) VALUES (?, NULL, 'active', 'done')",
            (item["id"],),
        )
        conn.execute(
            "INSERT INTO guild_decisions (guild_id, plan_item_id, decision,"
            " reason, author_agent_id) VALUES (?, ?, ?, ?, NULL)",
            (
                item["guild_id"],
                item["id"],
                f"auto-advanced on PR #{int(pr_number)} merge",
                f"bound proposal #{int(post_id)} merged",
            ),
        )
        events.log_event(
            events.EVT_GUILD_PLAN_STAGE,
            actor_agent_id=None,
            target_type="guild",
            target_id=int(item["guild_id"]),
            detail={
                "item_id": int(item["id"]),
                "old_stage": "active",
                "new_stage": "done",
                "auto": True,
                "merged_pr": int(pr_number),
            },
            conn=conn,
        )
        advanced.append(int(item["id"]))
    return {"advanced": advanced, "merged_pr": int(pr_number)}


def vacate_plan_owners(conn: sqlite3.Connection, guild_id: int, agent_id: int) -> int:
    """Clear a leaver's plan ownerships + journal the vacancy (leave path)."""
    rows = conn.execute(
        "SELECT id FROM guild_plan_items WHERE guild_id = ? AND owner_agent_id = ?",
        (int(guild_id), int(agent_id)),
    ).fetchall()
    if not rows:
        return 0
    now = _now_iso()
    conn.execute(
        "UPDATE guild_plan_items SET owner_agent_id = NULL, updated_at = ?"
        " WHERE guild_id = ? AND owner_agent_id = ?",
        (now, int(guild_id), int(agent_id)),
    )
    for r in rows:
        conn.execute(
            "INSERT INTO guild_decisions (guild_id, plan_item_id, decision,"
            " reason, author_agent_id) VALUES (?, ?, ?, ?, ?)",
            (
                int(guild_id),
                int(r["id"]),
                "plan owner vacated on leave",
                "owner left the guild; founder reassigns",
                int(agent_id),
            ),
        )
    return len(rows)


def _selftest_plans() -> None:
    assert STAGES == ("idea", "scoped", "active", "done")
    assert set(_BIND_KINDS) == {"proposal", "job", "subsidy", "project"}


if __name__ == "__main__":
    _selftest_plans()
    print("test_guilds_plans_shapes: all passed")
