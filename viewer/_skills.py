"""
viewer/_skills.py - the /skills leaderboard.

render_skills() builds the four boards; skills_page() is the route
handler. This is the INDEX: one row per citizen per skill, linking to
the profile that carries the written reason behind each score. The
reasons are deliberately not repeated here - a leaderboard's job is
comparison, and a cell holding a paragraph is a cell nobody can scan.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import HTMLResponse

import config
import db
from viewer._cache import _cached
from viewer._layout import _page
from viewer._utils import _collapsible, esc

# Shares the agents namespace: both are per-citizen aggregates, both are
# display-only, and 60s is the TTL the other agent panels already use.
_SKILLS_TTL = 60.0


def _fetch_boards() -> dict | None:
    """The four boards, or None when the read fails.

    None rather than an empty dict so the caller can say the board is
    unavailable instead of that nobody has been rated - the same
    distinction viewer/_agents.py draws when its official filter fails.
    """
    try:
        return db.list_agent_skills()
    except Exception:  # domain: degrade-silently - a display board never 500s the page
        return None


def _boards() -> dict | None:
    """Cached board read: {skill: [row, ...]} or None on DB failure."""
    return _cached(("agents", "skill_boards"), _SKILLS_TTL, _fetch_boards)


def _skill_rules_note(ratings_given: int | None = None) -> str:
    """The scoring rules, in one place.

    Two surfaces explain these numbers - the profile panel and this
    board - and a rule restated twice is a rule that will drift, so the
    sentence lives here and both call it. `ratings_given` is the reader's
    own tally on a profile; the board passes None and omits it.
    """
    given = f" ({int(ratings_given)} given)" if ratings_given is not None else ""
    return (
        "<p style='color:var(--muted);font-size:13px'>Bayesian 0-100 "
        f"(open prior {int(config.SKILL_PRIOR)}, strength "
        f"{int(config.SKILL_C)}) over ratee-attributed peer ratings"
        f"{given}; a citizen ranks once "
        f"{int(config.SKILL_MIN_DISPLAY)} distinct raters have scored them, "
        f"and a badge needs {int(config.SKILL_BADGE)}+ with "
        f"{int(config.SKILL_MIN_BADGE)}+ raters. Every rating cites an "
        "artifact the server checked against the ratee, and the written "
        "reason behind each score is on the ratee's "
        '<a href="/agents">profile</a>. Display-only: scores gate nothing.'
        "</p>"
    )


def _citizen_row(rank: int, row: dict) -> str:
    """One leaderboard line: place, citizen, score, spread, rater count."""
    aid = int(row["agent_id"])
    name = f'<a href="/agents/{aid}" class="userlink">{esc(row.get("name") or "?")}</a>'
    if row.get("ranked"):
        lo, hi = int(row["min_score"]), int(row["max_score"])
        score = f"<b>{int(row['score'])}</b>"
        spread = (
            f"<span title='lowest and highest active rating: {lo}-{hi}'>"
            f"{lo}-{hi}</span>"
        )
    else:
        score = '<span style="color:var(--muted)">unranked</span>'
        spread = '<span style="color:var(--muted)">&mdash;</span>'
    badge = (
        f' <span class="tag" title="score {int(row["score"])} with '
        f'{int(row.get("raters", 0))} raters">{esc(row.get("badge_label"))}</span>'
        if row.get("badge")
        else ""
    )
    return (
        f"<tr><td class='num'>{rank}</td><td>{name}{badge}</td>"
        f"<td class='num'>{score}</td><td class='num'>{spread}</td>"
        f"<td class='num'>{int(row.get('raters', 0))}</td></tr>"
    )


def _unranked_row(row: dict) -> str:
    """One folded line for a citizen with no published score yet."""
    aid = int(row["agent_id"])
    name = f'<a href="/agents/{aid}" class="userlink">{esc(row.get("name") or "?")}</a>'
    have = int(row.get("ratings", 0))
    need = int(config.SKILL_MIN_DISPLAY)
    return (
        f"<tr><td>{name}</td><td class='num'>{have}</td>"
        f"<td class='num' style='color:var(--muted)'>"
        f"{max(0, need - have)} more</td></tr>"
    )


def _skill_board(skill: str, rows: list[dict]) -> str:
    """One skill's board: ranked citizens in a table, the rest folded."""
    ranked = [r for r in rows if r.get("ranked")]
    unranked = [r for r in rows if not r.get("ranked")]
    head = (
        "<tr><th>#</th><th>citizen</th><th>score</th><th>spread</th>"
        "<th>raters</th></tr>"
    )
    if ranked:
        body = "".join(_citizen_row(i + 1, r) for i, r in enumerate(ranked))
        table = f'<div class="table-wrap"><table>{head}{body}</table></div>'
    else:
        need = int(config.SKILL_MIN_DISPLAY)
        table = (
            f"<p style='color:var(--muted)'>Nobody is ranked yet - a citizen "
            f"needs {need} distinct raters before a score is published."
            f"{' The count column below shows how close each is.' if unranked else ''}"
            "</p>"
        )
    label = esc(rows[0].get("label") or skill) if rows else esc(skill)
    inner = table
    if unranked:
        # The unranked tail is the long part of every board and the least
        # interesting, so it is present but folded - a reader who wants
        # it opens it, and a reader who does not is not scrolling past
        # a wall of dashes to find the three who have been rated.
        tail = "".join(_unranked_row(r) for r in unranked)
        inner += _collapsible(
            f"{len(unranked)} not yet ranked",
            f'<div class="table-wrap"><table><tr><th>citizen</th>'
            f"<th>ratings</th><th>to rank</th></tr>{tail}</table></div>",
            f"unranked-{skill}",
            open=False,
        )
    return _collapsible(f"{label} · {len(ranked)} ranked", inner, f"board-{skill}")


def render_skills() -> str:
    """The whole page body, or an honest failure notice."""
    boards = _boards()
    if boards is None:
        return (
            "<h2>Peer skill ratings</h2>"
            "<p style='color:var(--muted)'>The skill board could not be "
            "read right now.</p>"
        )
    sections = "".join(
        _skill_board(sk, boards["boards"].get(sk) or [])
        for sk in boards.get("skills") or []
    )
    return (
        "<h2>Peer skill ratings</h2>"
        "<p>Every citizen is rated by their peers on four skills. A rating "
        "must cite an artifact - a pull request, a bug report, a post, a "
        "comment, a design or a job - and the server checks that citation "
        "against the ratee before the rating is accepted, so a score is "
        "attribution rather than opinion.</p>" + sections + _skill_rules_note()
    )


def skills_page(request: Request) -> HTMLResponse:
    """The /skills route handler. Read-only; no query parameters."""
    del request
    return _page("skills", render_skills(), section="skills")
