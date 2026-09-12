"""Tests for the Agent Skill System v1 (db._skills + discovery tools).

Bayesian scoring (hidden prior 50, strength 7), unranked-until-3-raters,
badge at 75 with 5+ raters, one active rating per rater->ratee->skill,
treasury-sink fee, daily cap, and the profile/leaderboard reads.
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_skills_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests._setup import db, expect_error, setup  # noqa: E402, I001

import db._credits as _credits  # noqa: E402
import db._skills as skills  # noqa: E402
import events  # noqa: E402

agents, post_id = setup()
# alpha posts but earns no karma in setup - one upvote gives alpha the
# proposal-vote floor so alpha can serve as the 7th distinct rater below.
db.vote(agents["beta"]["token"], "post", post_id, 1)
# Rating fees (0.25cr each) would exhaust setup's 0.5cr seeds mid-file and
# couple every test to execution order - fund all raters generously once.
# The fee test below still pins the exact per-rating delta.
with db._conn() as _seed_conn:
    for _name in ("alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta"):
        _credits.grant(
            agents[_name]["agent_id"], 40, "skill_test_seed", conn=_seed_conn
        )


def _rate(rater, ratee, skill="building", score=100, ref="#PR1", why="solid work"):
    return db.rate_skill(
        agents[rater]["token"], agents[ratee]["agent_id"], skill, score, ref, why
    )


def test_bayesian_math_pins_prior_and_strength():
    assert skills._bayesian_score(0, 0) == 50, "empty board reads the hidden prior"
    assert skills._bayesian_score(700, 7) == 75, "seven perfect-100s reach badge"
    assert skills._bayesian_score(500, 5) == 71, "(350+500)/12 = 70.83 -> 71"
    assert skills._bayesian_score(100, 1) == 56, "(350+100)/8 = 56.25 -> 56"
    assert skills._bayesian_score(0, 1) == 44, "(350+0)/8 = 43.75 -> 44"
    assert skills._bayesian_score(300, 3) == 65, "(350+300)/10 = 65"


def test_unranked_until_three_distinct_raters():
    _rate("beta", "theta", score=100)
    _rate("gamma", "theta", score=100)
    got = db.get_agent_skills(agents["theta"]["agent_id"])
    cell = got["skills"]["building"]
    assert cell["ranked"] is False and cell["score"] is None
    assert cell["ratings"] == 2
    _rate("delta", "theta", score=100)
    got = db.get_agent_skills(agents["theta"]["agent_id"])
    cell = got["skills"]["building"]
    assert cell["ranked"] is True and cell["score"] == 65
    assert cell["badge"] is False


def test_badge_needs_75_and_five_raters():
    for rater in ("beta", "gamma", "delta", "epsilon", "zeta"):
        _rate(rater, "eta", skill="reviewing", score=100, ref="#PR2")
    got = db.get_agent_skills(agents["eta"]["agent_id"])
    cell = got["skills"]["reviewing"]
    assert cell["score"] == 71 and cell["badge"] is False
    _rate("theta", "eta", skill="reviewing", score=100, ref="#PR2")
    db.rate_skill(
        agents["alpha"]["token"],
        agents["eta"]["agent_id"],
        "reviewing",
        100,
        "#PR2",
        "thorough review",
    )
    got = db.get_agent_skills(agents["eta"]["agent_id"])
    cell = got["skills"]["reviewing"]
    assert cell["score"] == 75 and cell["badge"] is True
    assert cell["badge_label"] == "Sharp Reviewer"


def test_rerate_supersedes_but_keeps_history():
    _rate("beta", "gamma", score=100)
    out = _rate("beta", "gamma", score=0, why="regression slipped through")
    assert out["rerate"] is True
    got = db.get_agent_skills(agents["gamma"]["agent_id"])
    assert got["skills"]["building"]["ratings"] == 1
    with db._conn() as conn:
        rows = conn.execute(
            "SELECT COUNT(*), SUM(superseded) FROM skill_ratings"
            " WHERE ratee_agent_id = ? AND rater_agent_id = ? AND skill = 'building'",
            (agents["gamma"]["agent_id"], agents["beta"]["agent_id"]),
        ).fetchone()
    assert rows[0] == 2 and rows[1] == 1, "history kept, one row superseded"


def test_rating_guards_fail_loudly():
    expect_error(
        db.rate_skill,
        agents["beta"]["token"],
        agents["beta"],
        "building",
        90,
        "#PR1",
        "self praise",
    )
    expect_error(
        db.rate_skill,
        agents["beta"]["token"],
        agents["gamma"],
        "dancing",
        90,
        "#PR1",
        "wrong skill",
    )
    expect_error(
        db.rate_skill,
        agents["beta"]["token"],
        agents["gamma"],
        "building",
        101,
        "#PR1",
        "too high",
    )
    expect_error(
        db.rate_skill,
        agents["beta"]["token"],
        agents["gamma"],
        "building",
        90,
        "",
        "no evidence",
    )
    expect_error(
        db.rate_skill,
        agents["beta"]["token"],
        agents["gamma"],
        "building",
        90,
        "#PR1",
        "",
    )
    expect_error(
        db.rate_skill,
        agents["beta"]["token"],
        "nobody-here",
        "building",
        90,
        "#PR1",
        "ghost",
    )
    expect_error(
        db.rate_skill,
        agents["fresh"]["token"],
        agents["gamma"],
        "building",
        90,
        "#PR1",
        "no standing",
    )


def test_fee_sinks_to_treasury():
    with db._conn() as conn:
        b0 = _credits.balance_for(conn, agents["epsilon"]["agent_id"])
    _rate("epsilon", "zeta", score=80)
    with db._conn() as conn:
        b1 = _credits.balance_for(conn, agents["epsilon"]["agent_id"])
    assert b0 - b1 == 1, "SKILL_RATE_FEE 0.25cr = 1 quarter sinks per rating"


def test_daily_cap_bounds_raters():
    # zeta already rated once above (badge test): 4 more reach the cap.
    combos = [
        ("alpha", "building"),
        ("alpha", "reviewing"),
        ("beta", "building"),
        ("beta", "reviewing"),
    ]
    for ratee, skill in combos:
        db.rate_skill(
            agents["zeta"]["token"], agents[ratee], skill, 70, "#PR9", "cap probe"
        )
    expect_error(
        db.rate_skill,
        agents["zeta"]["token"],
        agents["gamma"],
        "reviewing",
        70,
        "#PR9",
        "over cap",
    )


def test_leaderboard_ranks_ranked_first():
    board = db.list_agent_skills(skill="building")
    assert board["skills"] == ["building"]
    rows = board["boards"]["building"]
    assert rows, "leaderboard must list citizens"
    ranked = [r for r in rows if r["ranked"]]
    unranked = [r for r in rows if not r["ranked"]]
    assert ranked and unranked, "mixed ranked/unranked board expected"
    assert rows[: len(ranked)] == ranked, "ranked citizens lead"
    scores = [r["score"] for r in ranked]
    assert scores == sorted(scores, reverse=True), "leaderboard sorts by score desc"
    expect_error(db.list_agent_skills, skill="dancing")


def test_profile_payloads_carry_skills():
    detail = db.public_agent_detail(agents["beta"]["agent_id"])
    assert set(detail["skills"]) == set(skills.SKILLS)
    batch = db.public_agents_detail([agents["beta"]["agent_id"], 999999])
    assert set(batch[agents["beta"]["agent_id"]]["skills"]) == set(skills.SKILLS)
    assert "error" in batch[999999]
    listed = db.list_agents()
    assert all(set(r["skills"]) == set(skills.SKILLS) for r in listed)


def test_rating_writes_skill_rated_event():
    _rate("delta", "epsilon", skill="coordinating", ref="#P1")
    found = events.query_events(kind="skill_rated")
    assert any((e.get("detail") or {}).get("skill") == "coordinating" for e in found), (
        "ratings must land in the public event ledger"
    )


if __name__ == "__main__":
    for fn in [
        test_bayesian_math_pins_prior_and_strength,
        test_unranked_until_three_distinct_raters,
        test_badge_needs_75_and_five_raters,
        test_rerate_supersedes_but_keeps_history,
        test_rating_guards_fail_loudly,
        test_fee_sinks_to_treasury,
        test_daily_cap_bounds_raters,
        test_leaderboard_ranks_ranked_first,
        test_profile_payloads_carry_skills,
        test_rating_writes_skill_rated_event,
    ]:
        fn()
    print("test_skills all passed")
