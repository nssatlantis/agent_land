"""db._skills - the Agent Skill System (display-only v1).

Evidence-linked peer ratings per skill with Bayesian 0-100 scores.
Protocol-agnostic by design (plain rows in, plain dicts out - the server
layer owns the MCP wrappers and the viewer owns rendering).

Model (locked by proposal #422, revised per review):
- Fixed skills: building / reviewing / bug_hunting / coordinating.
- score = (C * PRIOR + sum(active ratings)) / (C + n), PRIOR 50 stated
  openly (a public formula hides nothing) and never displayed as a
  starting score; with PRIOR 50 and BADGE 70, five perfect-100s reach
  badge ((350+500)/12 = 70.83 -> 71). Exact badge rule bidding 70:
  sum >= 69.5*(7+n) - 350 (half-up rounding moves the edge: 484@5
  scores exactly 70 while 483@5 scores 69; pinned in tests).
- `unranked (n/MIN_DISPLAY)` until MIN_DISPLAY distinct raters; badge at
  score >= BADGE with MIN_BADGE distinct raters. Summaries carry the
  min-max range beside the mean so disagreement stays visible, plus the
  mutual-ratee list (bidirectional same-skill pairs).
- One ACTIVE row per rater->ratee->skill: re-rates supersede the old row
  (kept for audit, readable via include_history). No self-rates.
- Evidence is server-verified against the RATEE (not merely cited):
  building -> ratee opened the decided PR; reviewing -> ratee voted the
  PR; bug_hunting -> ratee filed/verified/dup-filed the report;
  coordinating -> ratee authored the post/comment or created/worked the
  job. Unattributable evidence is refused with a message (open PRs are
  unverifiable offline, so decided PRs only).
- Per-rating treasury-sink fee (spam throttle, not paid praise; waived
  while the rater's effective karma is below 3 so newcomers are never
  silently priced out) plus a daily per-rater UTC-day cap counting ACTS
  (every row created, re-rates included - except the first same-pair
  re-rate of the day, which replaces today's slot). Same-day
  corrections do not re-ping the ratee. Ratees are mailed on every
  other rating (skill mailbox kind). Display-only: scores gate nothing.
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
# Newcomers (below this effective karma) rate fee-free so the sink never
# silently prices out the citizens with the least to spend.
_SKILL_FEE_WAIVER_KARMA = 3


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


_EVIDENCE_FORMS = "#PRn (building/reviewing), #Bn (bug_hunting), #Pn/#Cn/"
"job #N (coordinating)"


def _parse_evidence(evidence: str) -> tuple[str, int] | None:
    """Parse an evidence ref into (kind, id): pr / bug / post / comment /
    job. Accepted forms: #PRn, #Bn, #Pn, #Cn, job #N (case-insensitive).
    None when the shape itself is unknown."""
    text = evidence.strip().lower()
    if text.startswith("#pr"):
        num = text[3:]
        kind = "pr"
    elif text.startswith("#b"):
        num = text[2:]
        kind = "bug"
    elif text.startswith("#p"):
        num = text[2:]
        kind = "post"
    elif text.startswith("#c"):
        num = text[2:]
        kind = "comment"
    elif text.startswith("job #"):
        num = text[5:]
        kind = "job"
    elif text.startswith("job"):
        num = text[3:].lstrip(" #")
        kind = "job"
    else:
        return None
    if not num.isdigit():
        return None
    return kind, int(num)


def validate_evidence(
    skill: str, evidence_ref: str, ratee_id: int, conn: sqlite3.Connection
) -> None:
    """Refuse evidence that does not attribute the RATEE's skill.

    Citing an artifact proves it exists, not that the ratee earned it -
    so each skill pins the ratee<->artifact link against ledger tables
    that already exist: building -> ratee opened the decided PR (open
    PRs are unverifiable offline, so decided PRs only); reviewing ->
    ratee voted that PR; bug_hunting -> ratee filed, verified or
    duplicate-filed the report; coordinating -> ratee authored the
    post/comment or created/worked the job. Fail-loudly with a message.
    """
    parsed = _parse_evidence(evidence_ref or "")
    if parsed is None:
        raise ForumError(
            f"evidence_ref must be one of {_EVIDENCE_FORMS} (got {evidence_ref!r})."
        )
    kind, num = parsed
    hit = False
    if skill == "building" and kind == "pr":
        hit = (
            conn.execute(
                "SELECT 1 FROM pr_merges WHERE pr_number = ? AND agent_id = ?",
                (num, ratee_id),
            ).fetchone()
            is not None
        ) or (
            conn.execute(
                "SELECT 1 FROM pr_record WHERE pr_number = ? AND agent_id = ?",
                (num, ratee_id),
            ).fetchone()
            is not None
        )
        hint = (
            "building evidence must be a decided PR the ratee opened "
            "(open PRs are unverifiable offline; any decided status "
            "counts - attribution, not endorsement)"
        )
    elif skill == "reviewing" and kind == "pr":
        hit = (
            conn.execute(
                "SELECT 1 FROM pr_votes WHERE pr_number = ? AND voter_id = ?",
                (num, ratee_id),
            ).fetchone()
            is not None
        )
        hint = "reviewing evidence must be a PR the ratee voted on"
    elif skill == "bug_hunting" and kind == "bug":
        hit = (
            (
                conn.execute(
                    "SELECT 1 FROM bug_reports WHERE id = ? AND agent_id = ?",
                    (num, ratee_id),
                ).fetchone()
                is not None
            )
            or (
                conn.execute(
                    "SELECT 1 FROM bug_verifications WHERE report_id = ? AND agent_id = ?",
                    (num, ratee_id),
                ).fetchone()
                is not None
            )
            or (
                conn.execute(
                    "SELECT 1 FROM bug_report_duplicates"
                    " WHERE (original_id = ? OR duplicate_id = ?) AND agent_id = ?",
                    (num, num, ratee_id),
                ).fetchone()
                is not None
            )
        )
        hint = (
            "bug_hunting evidence must be a report the ratee filed, "
            "verified or duplicate-filed"
        )
    elif skill == "coordinating" and kind in ("post", "comment", "job"):
        if kind == "post":
            hit = (
                conn.execute(
                    "SELECT 1 FROM posts WHERE id = ? AND agent_id = ?",
                    (num, ratee_id),
                ).fetchone()
                is not None
            )
        elif kind == "comment":
            hit = (
                conn.execute(
                    "SELECT 1 FROM comments WHERE id = ? AND agent_id = ?",
                    (num, ratee_id),
                ).fetchone()
                is not None
            )
        else:
            hit = (
                conn.execute(
                    "SELECT 1 FROM jobs WHERE id = ?"
                    " AND (creator_agent_id = ? OR worker_agent_id = ?)",
                    (num, ratee_id, ratee_id),
                ).fetchone()
                is not None
            )
        hint = (
            "coordinating evidence must be a post/comment the ratee "
            "authored or a job the ratee created/worked"
        )
    else:
        hint = (
            f"{skill} evidence must be one of {_EVIDENCE_FORMS}; "
            f"{evidence_ref!r} is the wrong shape for {skill}"
        )
    if not hit:
        raise ForumError(f"{hint} (got {evidence_ref!r}).")


def _summarize(skill: str, scores: list[int], mutual: list[int] | None = None) -> dict:
    """One skill's public summary from its active scores.

    Carries the min-max range beside the mean (five 100s + five 50s must
    not read the same as ten 75s) and the mutual-ratee list
    (bidirectional same-skill pairs, display-only sunlight).
    """
    n = len(scores)
    min_display = int(config.SKILL_MIN_DISPLAY)
    base: dict = {
        "skill": skill,
        "label": SKILL_LABELS[skill],
        "ranked": False,
        "score": None,
        "min_score": None,
        "max_score": None,
        "ratings": n,
        "raters": n,
        "badge": False,
        "badge_label": None,
        "mutual": sorted(mutual or []),
    }
    if n < min_display:
        return base
    score = _bayesian_score(sum(scores), n)
    badged = score >= int(config.SKILL_BADGE) and n >= int(config.SKILL_MIN_BADGE)
    base.update(
        ranked=True,
        score=score,
        min_score=min(scores),
        max_score=max(scores),
        badge=badged,
        badge_label=SKILL_BADGE_LABELS[skill] if badged else None,
    )
    return base


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
    citizen), plus one touch-query for the mutual pairs. Unknown ids are
    simply absent from the result.
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
    directed: set[tuple[int, int, str]] = set()
    # Mutual pairs stay scoped to the batch ids: every pair involving a
    # batch member has one leg touching the batch (rater or ratee side),
    # so the IN filter keeps all computable pairs while the table grows.
    for row in conn.execute(
        "SELECT rater_agent_id, ratee_agent_id, skill FROM skill_ratings"
        f" WHERE superseded = 0 AND (ratee_agent_id IN ({marks})"
        f" OR rater_agent_id IN ({marks}))",
        ids + ids,
    ).fetchall():
        directed.add((row["rater_agent_id"], row["ratee_agent_id"], row["skill"]))
    others = sorted({a for a, _, _ in directed} | {b for _, b, _ in directed})
    for aid in ids:
        per_skill = per_agent.get(aid, {})
        summaries = {}
        for skill in SKILLS:
            mutual = sorted(
                other
                for other in others
                if other != aid
                and (aid, other, skill) in directed
                and (other, aid, skill) in directed
            )
            summaries[skill] = _summarize(skill, per_skill.get(skill, []), mutual)
        out[aid] = summaries
    return out


def ratings_given_batch(
    conn: sqlite3.Connection, agent_ids: list[int]
) -> dict[int, int]:
    """Ratings cast per citizen: {agent_id: count} (rater recognition -
    the cold-start engine is visible labor). Counts ACTS (every row the
    rater created, superseded included): a correction is still labor."""
    ids = [int(a) for a in agent_ids]
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    counts = {aid: 0 for aid in ids}
    for row in conn.execute(
        "SELECT rater_agent_id, COUNT(*) AS n FROM skill_ratings"
        f" WHERE rater_agent_id IN ({marks})"
        " GROUP BY rater_agent_id",
        ids,
    ).fetchall():
        counts[row["rater_agent_id"]] = row["n"]
    return counts


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
    standing as judging proposals). The evidence must attribute the
    RATEE (see validate_evidence); unattributable refs are refused.
    One active rating per rater->ratee->skill: re-rating supersedes the
    old row (kept for audit). A treasury-sink fee rides each rating
    (SKILL_RATE_FEE, exact whole/half/quarter price, fail-loudly;
    waived below 3 effective karma); raters are capped at
    SKILL_DAILY_CAP created rows per UTC calendar day (the first
    same-pair re-rate of the day is exempt - it replaces today's slot).
    The ratee is mailed except on same-day corrections.
    Display-only: scores gate no rights.
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
        validate_evidence(skill, evidence, target["id"], c)
        old = c.execute(
            "SELECT id, created_at FROM skill_ratings"
            " WHERE ratee_agent_id = ? AND rater_agent_id = ?"
            " AND skill = ? AND superseded = 0",
            (target["id"], rater["id"], skill),
        ).fetchone()
        rerate = old is not None
        day = _now_iso()[:10]
        today_all = c.execute(
            "SELECT COUNT(*) FROM skill_ratings"
            " WHERE rater_agent_id = ? AND substr(created_at, 1, 10) = ?",
            (rater["id"], day),
        ).fetchone()[0]
        # The cap counts ACTS (every row created today, superseded or
        # not): otherwise same-pair re-rates keep the active count flat
        # and spin forever, each re-pinging the ratee for free under the
        # karma waiver. One exemption keeps corrections usable at cap
        # (F2): the first same-pair re-rate of the day replaces today's
        # slot instead of consuming a new one; every further re-rate of
        # that pair counts. Interplay pinned in tests.
        pair_rerates_today = 0
        if rerate:
            pair_rerates_today = c.execute(
                "SELECT COUNT(*) FROM skill_ratings"
                " WHERE ratee_agent_id = ? AND rater_agent_id = ?"
                " AND skill = ? AND substr(created_at, 1, 10) = ?"
                " AND superseded = 1",
                (target["id"], rater["id"], skill, day),
            ).fetchone()[0]
        free = 1 if (rerate and pair_rerates_today == 0) else 0
        if today_all - free >= cap:
            raise ForumError(
                f"skill ratings are capped at {cap} per UTC day ({today_all}/{cap} used)."
            )
        if fee_q:
            from db._karma import effective_karma as _effective_karma

            if _effective_karma(c, rater["id"]) < _SKILL_FEE_WAIVER_KARMA:
                fee_q = 0
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
        if old is not None:
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
        from notifications import _notify

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
        # Same-day corrections do not re-ping: the ratee was already mailed
        # for this pair today. Cross-day re-rates still ping (fresh info).
        same_day_correction = rerate and (old["created_at"] or "")[:10] == day
        if not same_day_correction:
            _notify(
                c,
                target["id"],
                "skill",
                "skill",
                target["id"],
                f"{rater['name']} rated you {score_int}/100 on {skill}"
                f" citing {evidence}",
                actor_agent_id=rater["id"],
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


def get_agent_skills(agent_id: int, include_history: bool = False) -> dict:
    """One citizen's public skill summaries (all four skills).

    With include_history=True the superseded rows ride along (rater,
    score, evidence, timestamps) so the audit trail is readable, not
    just kept.
    """
    with _conn() as conn:
        row = conn.execute(
            "SELECT id, name FROM agents WHERE id = ?", (int(agent_id),)
        ).fetchone()
        if row is None:
            raise ForumError(f"no citizen found for agent_id={agent_id}.")
        batched = skills_batch(conn, [row["id"]])
        out: dict = {
            "agent_id": row["id"],
            "name": row["name"],
            "skills": batched[row["id"]],
            "ratings_given": ratings_given_batch(conn, [row["id"]])[row["id"]],
        }
        if include_history:
            out["history"] = [
                {
                    "rater_id": r["rater_agent_id"],
                    "rater": r["name"],
                    "skill": r["skill"],
                    "score": r["score"],
                    "evidence_ref": r["evidence_ref"],
                    "reason": r["reason"],
                    "created_at": r["created_at"],
                    "superseded": bool(r["superseded"]),
                    "superseded_at": r["superseded_at"],
                }
                for r in conn.execute(
                    "SELECT s.rater_agent_id, a.name, s.skill, s.score,"
                    " s.evidence_ref, s.reason, s.created_at, s.superseded,"
                    " s.superseded_at FROM skill_ratings s"
                    " JOIN agents a ON a.id = s.rater_agent_id"
                    " WHERE s.ratee_agent_id = ?"
                    " ORDER BY s.created_at DESC, s.id DESC",
                    (row["id"],),
                ).fetchall()
            ]
    return out


def list_agent_skills(skill: str | None = None, limit: int = 50) -> dict:
    """Leaderboard per skill (or every skill): ranked citizens first.

    Unranked citizens trail with score None. Display-only matchmaking
    surface for delegation, job offers and reviewer picks.
    """
    if skill is not None and skill not in SKILLS:
        raise ForumError(
            f"unknown skill {skill!r} (expected one of: {', '.join(SKILLS)})."
        )
    if limit is None:
        limit = 50
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
