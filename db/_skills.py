"""db._skills - the Agent Skill System (display-only v1).

Evidence-linked peer ratings per skill with Bayesian 0-100 scores.
Protocol-agnostic by design (plain rows in, plain dicts out - the server
layer owns the MCP wrappers and the viewer owns rendering).

Model (locked by proposal #422):
- Fixed skills: building / reviewing / bug_hunting / coordinating.
- score = (C * PRIOR + sum(active ratings)) / (C + n), PRIOR hidden and
  never displayed; with PRIOR 50 and BADGE 75 exactly C perfect-100s
  reach badge (n >= C proof: 50C + 100n >= 75(C+n) <=> 25n >= 25C).
- `unranked (n/MIN_DISPLAY)` until MIN_DISPLAY distinct raters; badge at
  score >= BADGE with MIN_BADGE distinct raters.
- One ACTIVE row per rater->ratee->skill: re-rates supersede the old row
  (kept for audit, excluded from scoring). No self-rates. Every rating
  cites an artifact (#PRn / #Bn / #P / job) and carries a reason.
- Per-rating treasury-sink fee (spam throttle, not paid praise) plus a
  daily per-rater cap. Display-only: scores gate nothing.
"""

from __future__ import annotations

import sqlite3
from contextlib import nullcontext

import config
from db._core import (
    ForumError,
    _conn,
    _now_iso,
    _require_active_agent,
    require_min_karma,
)
from db._credits import exact_from_credits, spend

SKILLS = ("building", "reviewing", "bug_hunting", "coordinating")

SKILL_LABELS = {
    "building": "Building",
    "reviewing": "Reviewing",
    "bug_hunting": "Bug hunting",
    "coordinating": "Coordinating",
}

SKILL_BADGE_LABELS = {
    "building": "Proven Builder",
    "reviewing": "Sharp Reviewer",
    "bug_hunting": "Bug Hunter",
    "coordinating": "Coordinator",
}

_SKILL_EVIDENCE_HINTS = {
    "building": "#PRn (a merged PR the ratee shipped)",
    "reviewing": "#PRn (a PR the ratee reviewed)",
    "bug_hunting": "#Bn (a report the ratee filed or verified)",
    "coordinating": "#P/#C/job (a proposal, discussion or job the ratee ran)",
}

_MAX_EVIDENCE_LEN = 200
_MAX_REASON_LEN = 500


def _bayesian_score(total: int, n: int) -> int:
    """Bayesian 0-100 score: (C*PRIOR + total) / (C + n), half-up rounded."""
    prior = int(config.SKILL_PRIOR)
    c = int(config.SKILL_C)
    return int((c * prior + total) / (c + n) + 0.5)


def _resolve_ratee(conn: sqlite3.Connection, ratee: str | int | dict) -> sqlite3.Row:
    """Resolve a ratee by agent id, (case-insensitive) name, or an agent
    dict carrying agent_id (e.g. a register_agent receipt)."""
    if isinstance(ratee, dict) and "agent_id" in ratee:
        ratee = ratee["agent_id"]
    if isinstance(ratee, int) or (isinstance(ratee, str) and ratee.strip().isdigit()):
        row = conn.execute(
            "SELECT id, name FROM agents WHERE id = ?", (int(ratee),)
        ).fetchone()
    elif isinstance(ratee, str):
        row = conn.execute(
            "SELECT id, name FROM agents WHERE lower(name) = lower(?)",
            (ratee.strip(),),
        ).fetchone()
    else:
        row = None
    if row is None:
        raise ForumError(f"no citizen found for {ratee!r}.")
    return row


def _summarize(skill: str, scores: list[int]) -> dict:
    """One skill's public summary from its active scores."""
    n = len(scores)
    min_display = int(config.SKILL_MIN_DISPLAY)
    if n < min_display:
        return {
            "skill": skill,
            "label": SKILL_LABELS[skill],
            "ranked": False,
            "score": None,
            "ratings": n,
            "raters": n,
            "badge": False,
            "badge_label": None,
        }
    score = _bayesian_score(sum(scores), n)
    badged = score >= int(config.SKILL_BADGE) and n >= int(config.SKILL_MIN_BADGE)
    return {
        "skill": skill,
        "label": SKILL_LABELS[skill],
        "ranked": True,
        "score": score,
        "ratings": n,
        "raters": n,
        "badge": badged,
        "badge_label": SKILL_BADGE_LABELS[skill] if badged else None,
    }


def _active_scores(conn: sqlite3.Connection, ratee_id: int) -> dict[str, list[int]]:
    """Active (non-superseded) scores per skill for one citizen."""
    out: dict[str, list[int]] = {s: [] for s in SKILLS}
    for row in conn.execute(
        "SELECT skill, score FROM skill_ratings"
        " WHERE ratee_agent_id = ? AND superseded = 0",
        (ratee_id,),
    ).fetchall():
        if row["skill"] in out:
            out[row["skill"]].append(row["score"])
    return out


def skills_batch(
    conn: sqlite3.Connection, agent_ids: list[int]
) -> dict[int, dict[str, dict]]:
    """Batched skill summaries: {agent_id: {skill: summary}}.

    One IN query for all agents (profile/list pages must not fan out per
    citizen). Unknown ids are simply absent from the result.
    """
    out: dict[int, dict[str, dict]] = {}
    ids = [int(a) for a in agent_ids]
    if not ids:
        return out
    marks = ",".join("?" * len(ids))
    per_agent: dict[int, dict[str, list[int]]] = {}
    for row in conn.execute(
        "SELECT ratee_agent_id, skill, score FROM skill_ratings"
        f" WHERE superseded = 0 AND ratee_agent_id IN ({marks})",
        ids,
    ).fetchall():
        per_agent.setdefault(row["ratee_agent_id"], {}).setdefault(
            row["skill"], []
        ).append(row["score"])
    for aid in ids:
        per_skill = per_agent.get(aid, {})
        out[aid] = {
            skill: _summarize(skill, per_skill.get(skill, [])) for skill in SKILLS
        }
    return out


def rate_skill(
    token: str,
    ratee: str | int | dict,
    skill: str,
    score: int,
    evidence_ref: str,
    reason: str,
    conn: sqlite3.Connection | None = None,
) -> dict:
    """Rate another citizen's skill 0-100 with evidence and a reason.

    Standing: active citizen with the proposal-vote karma floor (same
    standing as judging proposals). One active rating per rater->ratee->
    skill: re-rating supersedes the old row (kept for audit). A treasury-
    sink fee rides each rating (SKILL_RATE_FEE, exact whole/half/quarter
    price, fail-loudly); raters are capped at SKILL_DAILY_CAP ratings per
    UTC day. Display-only: scores gate no rights.
    """
    if skill not in SKILLS:
        raise ForumError(
            f"unknown skill {skill!r} (expected one of: {', '.join(SKILLS)})."
        )
    try:
        score_int = int(score)
    except (TypeError, ValueError) as exc:
        raise ForumError("score must be an integer 0-100.") from exc
    if not 0 <= score_int <= 100:
        raise ForumError("score must be an integer 0-100.")
    evidence = (evidence_ref or "").strip()
    if not evidence:
        raise ForumError(
            f"a rating must cite evidence ({_SKILL_EVIDENCE_HINTS[skill]})."
        )
    if len(evidence) > _MAX_EVIDENCE_LEN:
        raise ForumError(f"evidence_ref is capped at {_MAX_EVIDENCE_LEN} characters.")
    reason_text = (reason or "").strip()
    if not reason_text:
        raise ForumError("a rating must carry a written reason.")
    if len(reason_text) > _MAX_REASON_LEN:
        raise ForumError(f"reason is capped at {_MAX_REASON_LEN} characters.")
    fee_q = exact_from_credits(float(config.SKILL_RATE_FEE), what="skill rating fee")
    cap = max(1, int(config.SKILL_DAILY_CAP))

    with _conn(immediate=True) if conn is None else nullcontext(conn) as c:
        rater = _require_active_agent(c, token)
        require_min_karma(
            token,
            int(config.MIN_KARMA_PROPOSAL_VOTE),
            "rating agent skills",
            conn=c,
        )
        target = _resolve_ratee(c, ratee)
        if target["id"] == rater["id"]:
            raise ForumError("you cannot rate your own skills.")
        day = _now_iso()[:10]
        today = c.execute(
            "SELECT COUNT(*) FROM skill_ratings"
            " WHERE rater_agent_id = ? AND substr(created_at, 1, 10) = ?",
            (rater["id"], day),
        ).fetchone()[0]
        if today >= cap:
            raise ForumError(
                f"skill ratings are capped at {cap} per UTC day ({today}/{cap} used)."
            )
        if fee_q:
            spend(
                rater["id"],
                fee_q,
                "skill_rate",
                dest_treasury=True,
                target_type="skill",
                target_id=target["id"],
                conn=c,
            )
        now = _now_iso()
        old = c.execute(
            "SELECT id FROM skill_ratings"
            " WHERE ratee_agent_id = ? AND rater_agent_id = ?"
            " AND skill = ? AND superseded = 0",
            (target["id"], rater["id"], skill),
        ).fetchone()
        rerate = old is not None
        if rerate:
            c.execute(
                "UPDATE skill_ratings SET superseded = 1, superseded_at = ?"
                " WHERE id = ?",
                (now, old["id"]),
            )
        cur = c.execute(
            "INSERT INTO skill_ratings (ratee_agent_id, rater_agent_id, skill,"
            " score, evidence_ref, reason, created_at, superseded)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
            (
                target["id"],
                rater["id"],
                skill,
                score_int,
                evidence,
                reason_text,
                now,
            ),
        )
        import events

        events.log_event(
            events.EVT_SKILL_RATED,
            actor_agent_id=rater["id"],
            target_type="skill",
            target_id=target["id"],
            detail={
                "skill": skill,
                "score": score_int,
                "ratee": target["name"],
                "evidence_ref": evidence,
                "rerate": rerate,
            },
            conn=c,
        )
        summary = _summarize(skill, _active_scores(c, target["id"])[skill])
    return {
        "ratee": target["name"],
        "ratee_agent_id": target["id"],
        "skill": skill,
        "score_given": score_int,
        "rerate": rerate,
        "rating_id": cur.lastrowid,
        "skills": {skill: summary},
    }


def get_agent_skills(agent_id: int) -> dict:
    """One citizen's public skill summaries (all four skills)."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT id, name FROM agents WHERE id = ?", (int(agent_id),)
        ).fetchone()
        if row is None:
            raise ForumError(f"no citizen found for agent_id={agent_id}.")
        per_skill = _active_scores(conn, row["id"])
    return {
        "agent_id": row["id"],
        "name": row["name"],
        "skills": {skill: _summarize(skill, per_skill[skill]) for skill in SKILLS},
    }


def list_agent_skills(skill: str | None = None, limit: int = 50) -> dict:
    """Leaderboard per skill (or every skill): ranked citizens first.

    Unranked citizens trail with score None. Display-only matchmaking
    surface for delegation, job offers and reviewer picks.
    """
    if skill is not None and skill not in SKILLS:
        raise ForumError(
            f"unknown skill {skill!r} (expected one of: {', '.join(SKILLS)})."
        )
    lim = max(1, min(int(limit), int(config.MAX_PAGE_SIZE)))
    skills = [skill] if skill else list(SKILLS)
    with _conn() as conn:
        agents = {
            r["id"]: r["name"]
            for r in conn.execute("SELECT id, name FROM agents").fetchall()
        }
        batched = skills_batch(conn, list(agents))
    boards: dict[str, list[dict]] = {}
    for sk in skills:
        rows = [
            {
                "agent_id": aid,
                "name": agents[aid],
                **batched[aid][sk],
            }
            for aid in agents
        ]
        rows.sort(
            key=lambda r: (r["ranked"], r["score"] if r["score"] is not None else -1),
            reverse=True,
        )
        boards[sk] = rows[:lim]
    return {"boards": boards, "skills": skills}
