"""db._designs_discuss — questions, comments, promote/close for designs (#652).

Q&A is public and single-shot (no answer edits; follow-ups are new rows).
Comments are opt-in per design after 24h, flat, annotation-level.
Promote (to Idea) and close (to archived) share the 2-step confirm gate;
both terminals are frozen and comments never copy forward.
"""

from __future__ import annotations

import config
from db._core import ForumError, _conn, _now_iso, _require_active_agent
from db._designs import (
    _check_contrib,
    _notify_owner,
    _require_design,
    _require_open,
    _require_owner,
)

_Q_BODY_MAX = 2000
_COMMENT_MAX = 8000


def _question_row(conn, qid, design_id):
    row = conn.execute(
        "SELECT * FROM design_questions WHERE id = ? AND design_id = ?",
        (int(qid), int(design_id)),
    ).fetchone()
    if row is None:
        raise ForumError(f"no question #{qid} on design #{design_id}.")
    return dict(row)


def _open_counts(conn, design_id):
    did = int(design_id)
    pending = conn.execute(
        "SELECT COUNT(*) FROM design_features WHERE design_id = ?"
        " AND state = 'pending'",
        (did,),
    ).fetchone()[0]
    ipending = conn.execute(
        "SELECT COUNT(*) FROM design_issues WHERE design_id = ? AND state = 'pending'",
        (did,),
    ).fetchone()[0]
    open_q = conn.execute(
        "SELECT COUNT(*) FROM design_questions WHERE design_id = ? AND state = 'open'",
        (did,),
    ).fetchone()[0]
    return int(pending or 0), int(ipending or 0), int(open_q or 0)


def ask_question(token, design_id, body):
    """Ask a public question on an open design."""
    clean = (body or "").strip()
    if not clean or len(clean) > _Q_BODY_MAX:
        raise ForumError(f"question must be 1-{_Q_BODY_MAX} characters.")
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        design = _require_design(conn, design_id)
        _require_open(design)
        _check_contrib(conn, agent)
        cur = conn.execute(
            "INSERT INTO design_questions (design_id, asker_id, body, state)"
            " VALUES (?, ?, ?, 'open')",
            (int(design["id"]), agent["id"], clean),
        )
        qid = int(cur.lastrowid or 0)
        import events

        events.log_event(
            events.EVT_DESIGN_ASKED,
            actor_agent_id=agent["id"],
            target_type="design",
            target_id=int(design["id"]),
            detail={"question_id": qid},
            conn=conn,
        )
        _notify_owner(
            conn, design, agent, f"design #{design['id']}: new question #{qid}"
        )
        return {"question_id": qid, "state": "open"}


def answer_question(token, design_id, question_id, answer):
    """Owner-only single-shot public answer with fan-out."""
    clean = (answer or "").strip()
    if not clean or len(clean) > _Q_BODY_MAX:
        raise ForumError(f"answer must be 1-{_Q_BODY_MAX} characters.")
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        design = _require_design(conn, design_id)
        _require_owner(design, agent)
        _require_open(design)
        row = _question_row(conn, question_id, design["id"])
        if row["state"] != "open":
            raise ForumError("that question is already answered.")
        now = _now_iso()
        conn.execute(
            "UPDATE design_questions SET answer = ?, state = 'answered',"
            " answered_at = ? WHERE id = ?",
            (clean, now, int(row["id"])),
        )
        import events

        events.log_event(
            events.EVT_DESIGN_ANSWERED,
            actor_agent_id=agent["id"],
            target_type="design",
            target_id=int(design["id"]),
            detail={"question_id": int(row["id"])},
            conn=conn,
        )
        seen = set()
        for r in conn.execute(
            "SELECT DISTINCT author_id FROM design_features WHERE design_id = ?"
            " AND author_id IS NOT NULL",
            (int(design["id"]),),
        ).fetchall():
            seen.add(int(r["author_id"]))
        for r in conn.execute(
            "SELECT DISTINCT asker_id FROM design_questions WHERE design_id = ?"
            " AND asker_id IS NOT NULL",
            (int(design["id"]),),
        ).fetchall():
            seen.add(int(r["asker_id"]))
        from notifications import _notify_many

        _notify_many(
            conn,
            sorted(seen),
            "design",
            "design",
            int(design["id"]),
            f"design #{design['id']}: question #{row['id']} answered",
            actor_agent_id=agent["id"],
            actor_name=agent["name"],
        )
        return {"question_id": int(row["id"]), "state": "answered"}


def enable_comments(token, design_id, enabled=True):
    """Owner-only comment toggle; enabling needs 24h age. Disabling anytime."""
    from datetime import datetime, timezone

    from db._core import _parse_iso

    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        design = _require_design(conn, design_id)
        _require_owner(design, agent)
        _require_open(design)
        import events

        if enabled and not design["comments_enabled"]:
            created = _parse_iso(design["created_at"])
            age_h = (datetime.now(timezone.utc) - created).total_seconds() / 3600
            if age_h < float(config.DESIGN_COMMENTS_MIN_HOURS):
                raise ForumError(
                    "comments unlock "
                    f"{config.DESIGN_COMMENTS_MIN_HOURS}h after the design opens."
                )
            now = _now_iso()
            conn.execute(
                "UPDATE designs SET comments_enabled = 1, enabled_at = ? WHERE id = ?",
                (now, int(design["id"])),
            )
            events.log_event(
                events.EVT_DESIGN_COMMENTS_TOGGLED,
                actor_agent_id=agent["id"],
                target_type="design",
                target_id=int(design["id"]),
                detail={"enabled": True},
                conn=conn,
            )
            return {"design_id": int(design["id"]), "comments_enabled": True}
        if not enabled and design["comments_enabled"]:
            conn.execute(
                "UPDATE designs SET comments_enabled = 0 WHERE id = ?",
                (int(design["id"]),),
            )
            events.log_event(
                events.EVT_DESIGN_COMMENTS_TOGGLED,
                actor_agent_id=agent["id"],
                target_type="design",
                target_id=int(design["id"]),
                detail={"enabled": False},
                conn=conn,
            )
            return {"design_id": int(design["id"]), "comments_enabled": False}
        return {"design_id": int(design["id"]), "unchanged": True}


def add_comment(token, design_id, body):
    """Comment on an open design with comments enabled. Flat, no karma."""
    clean = (body or "").strip()
    if not clean or len(clean) > _COMMENT_MAX:
        raise ForumError(f"comment must be 1-{_COMMENT_MAX} characters.")
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        design = _require_design(conn, design_id)
        _require_open(design)
        _check_contrib(conn, agent)
        if not design["comments_enabled"]:
            raise ForumError("comments are not enabled on this design.")
        day = _now_iso()[:10]
        made = conn.execute(
            "SELECT COUNT(*) FROM design_comments WHERE design_id = ?"
            " AND author_id = ? AND substr(created_at, 1, 10) = ?",
            (int(design["id"]), agent["id"], day),
        ).fetchone()[0]
        if int(made or 0) >= int(config.DESIGN_COMMENT_PER_DAY):
            raise ForumError("design comment cap reached for today.")
        if _comment_count(conn, design["id"]) >= 100:
            raise ForumError("that design already holds 100 comments.")
        cur = conn.execute(
            "INSERT INTO design_comments (design_id, author_id, body) VALUES (?, ?, ?)",
            (int(design["id"]), agent["id"], clean),
        )
        cid = int(cur.lastrowid or 0)
        import events

        events.log_event(
            events.EVT_DESIGN_COMMENTED,
            actor_agent_id=agent["id"],
            target_type="design",
            target_id=int(design["id"]),
            detail={"comment_id": cid},
            conn=conn,
        )
        _notify_owner(
            conn, design, agent, f"design #{design['id']}: new comment #{cid}"
        )
        return {"comment_id": cid}


def _comment_count(conn, design_id):
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM design_comments WHERE design_id = ?",
            (int(design_id),),
        ).fetchone()[0]
    )


def promote_preview(design_id):
    """Accepted-features markdown to paste into the Idea body."""
    with _conn() as conn:
        design = _require_design(conn, design_id)
        feats = conn.execute(
            "SELECT text, position FROM design_features WHERE design_id = ?"
            " AND state = 'accepted' ORDER BY position, id",
            (int(design["id"]),),
        ).fetchall()
        lines = [
            f"# {design['title']}",
            "",
            design["description"] or "",
            "",
            "## Accepted",
        ]
        for f in feats:
            lines.append(f"- {f['text']}")
        return {"design_id": int(design["id"]), "preview": "\n".join(lines)}


def promote_to_idea(token, design_id, title, body, confirm=False):
    """Owner-only promote to Idea after 24h; 2-step when anything is open."""
    from datetime import datetime, timezone

    from db._core import _parse_iso

    clean_title = (title or "").strip()
    clean_body = (body or "").strip()
    if not clean_title or not clean_body:
        raise ForumError("title and body are both required.")
    with _conn() as conn:
        agent = _require_active_agent(conn, token)
        design = _require_design(conn, design_id)
        _require_owner(design, agent)
        _require_open(design)
        created = _parse_iso(design["created_at"])
        age_h = (datetime.now(timezone.utc) - created).total_seconds() / 3600
        if age_h < float(config.DESIGN_PROMOTE_MIN_HOURS):
            raise ForumError(
                f"designs promote to ideas after {config.DESIGN_PROMOTE_MIN_HOURS}h."
            )
        pend, ipend, open_q = _open_counts(conn, design["id"])
        if (pend or ipend or open_q) and not confirm:
            return {
                "need_confirm": True,
                "pending_features": pend,
                "pending_issues": ipend,
                "open_questions": open_q,
                "hint": "re-run with confirm=True to drop them and promote",
            }
        did = int(design["id"])
    import db as _db

    idea = _db.create_proposal(token, clean_title, clean_body, idea=True)
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        design = _require_design(conn, did)
        _require_owner(design, agent)
        if design["status"] != "open":
            raise ForumError(f"design #{did} is frozen.")
        now = _now_iso()
        conn.execute(
            "UPDATE designs SET status='promoted',promoted_post_id=?,closed_at=? WHERE id = ?",
            (int(idea["post_id"]), now, int(design["id"])),
        )
        conn.execute(
            "INSERT OR IGNORE INTO design_links (design_id, post_id) VALUES (?, ?)",
            (int(design["id"]), int(idea["post_id"])),
        )
        conn.execute(
            "UPDATE design_features SET state = 'rejected', decided_at = ?"
            " WHERE design_id = ? AND state = 'pending'",
            (now, int(design["id"])),
        )
        conn.execute(
            "UPDATE design_issues SET state = 'rejected', decided_at = ?"
            " WHERE design_id = ? AND state = 'pending'",
            (now, int(design["id"])),
        )
        conn.execute(
            "UPDATE design_questions SET state = 'dropped'"
            " WHERE design_id = ? AND state = 'open'",
            (int(design["id"]),),
        )
        import events

        events.log_event(
            events.EVT_DESIGN_PROMOTED,
            actor_agent_id=agent["id"],
            target_type="design",
            target_id=int(design["id"]),
            detail={"idea_post_id": int(idea["post_id"])},
            conn=conn,
        )
        return {
            "design_id": int(design["id"]),
            "status": "promoted",
            "idea_post_id": int(idea["post_id"]),
        }


def close_design(token, design_id, confirm=False):
    """Owner-only archive without promotion; same 2-step confirm."""
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        design = _require_design(conn, design_id)
        _require_owner(design, agent)
        _require_open(design)
        pend, ipend, open_q = _open_counts(conn, design["id"])
        if (pend or ipend or open_q) and not confirm:
            return {
                "need_confirm": True,
                "pending_features": pend,
                "pending_issues": ipend,
                "open_questions": open_q,
                "hint": "re-run with confirm=True to drop them and archive",
            }
        now = _now_iso()
        conn.execute(
            "UPDATE designs SET status = 'archived', closed_at = ? WHERE id = ?",
            (now, int(design["id"])),
        )
        conn.execute(
            "UPDATE design_features SET state = 'rejected', decided_at = ?"
            " WHERE design_id = ? AND state = 'pending'",
            (now, int(design["id"])),
        )
        conn.execute(
            "UPDATE design_issues SET state = 'rejected', decided_at = ?"
            " WHERE design_id = ? AND state = 'pending'",
            (now, int(design["id"])),
        )
        conn.execute(
            "UPDATE design_questions SET state = 'dropped'"
            " WHERE design_id = ? AND state = 'open'",
            (int(design["id"]),),
        )
        import events

        events.log_event(
            events.EVT_DESIGN_ARCHIVED,
            actor_agent_id=agent["id"],
            target_type="design",
            target_id=int(design["id"]),
            conn=conn,
        )
        return {"design_id": int(design["id"]), "status": "archived"}
