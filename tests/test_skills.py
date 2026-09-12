"""Tests for the Agent Skill System v1 (db._skills + discovery tools).

Bayesian scoring (open prior 50, strength 7, badge 70/5), unranked-until-
3-raters, ratee-attributed evidence enforcement, one active rating per
rater->ratee->skill, treasury-sink fee (karma<3 waiver), daily UTC cap,
ratee mailbox ping, mutual pairs, min-max range, history reads, and the
profile/leaderboard payloads.
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
import notifications  # noqa: E402

agents, post_id = setup()
# alpha posts but earns no karma in setup - one upvote gives alpha the
# proposal-vote floor so alpha can serve as a distinct rater below.
db.vote(agents["beta"]["token"], "post", post_id, 1)
# Rating fees (0.25cr each) would exhaust setup's 0.5cr seeds mid-file and
# couple every test to execution order - fund all raters generously once.
# The fee test below still pins the exact per-rating delta.
with db._conn() as _seed_conn:
    for _name in ("alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta"):
        _credits.grant(
            agents[_name]["agent_id"], 40, "skill_test_seed", conn=_seed_conn
        )


def _aid(name):
    return agents[name]["agent_id"]


def _seed_pr_merge(pr_number, opener):
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO pr_merges (pr_number, agent_id, merged_at) VALUES (?, ?, ?)",
            (pr_number, _aid(opener), "2026-09-12T00:00:00.000Z"),
        )


def _seed_pr_vote(pr_number, voter, value=1):
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO pr_votes (pr_number, voter_id, value) VALUES (?, ?, ?)",
            (pr_number, _aid(voter), value),
        )


# Attributable-evidence fixtures (one PR per ratee/skill under test).
_seed_pr_merge(1, "theta")
_seed_pr_merge(101, "gamma")
_seed_pr_merge(102, "zeta")
_seed_pr_merge(9, "alpha")
_seed_pr_merge(10, "beta")
_seed_pr_merge(9001, "eta")
_seed_pr_vote(2, "eta")
_seed_pr_vote(9, "alpha")
_seed_pr_vote(10, "beta")


def _rate(rater, ratee, skill="building", score=100, ref="#PR1", why="solid work"):
    return db.rate_skill(
        agents[rater]["token"], agents[ratee]["agent_id"], skill, score, ref, why
    )


def test_bayesian_math_pins_prior_and_strength():
    assert skills._bayesian_score(0, 0) == 50, "empty board reads the open prior"
    assert skills._bayesian_score(500, 5) == 71, "(350+500)/12 = 70.83 -> 71"
    assert skills._bayesian_score(100, 1) == 56, "(350+100)/8 = 56.25 -> 56"
    assert skills._bayesian_score(0, 1) == 44, "(350+0)/8 = 43.75 -> 44"
    assert skills._bayesian_score(300, 3) == 65, "(350+300)/10 = 65"


def test_badge_inequality_exact_shape():
    # Exact rule S >= 175 + 75n (not the "n>=C perfects" prose): seven
    # perfect-100s still hit exactly 75 at n=7, and at n=8 seven 100s +
    # one 75 hit exactly 75 - so all-perfect is sufficient, not necessary.
    assert skills._bayesian_score(700, 7) == 75
    assert skills._bayesian_score(775, 8) == 75, "7x100 + 75 at n=8 is exact"
    assert skills._bayesian_score(768, 8) == 75, "S>=768 still rounds to 75"
    assert skills._bayesian_score(767, 8) == 74, "S<=767 misses"
    assert skills._bayesian_score(500, 5) == 71, "five perfects clear 70"


def test_unranked_until_three_distinct_raters():
    _rate("beta", "theta", score=100)
    _rate("gamma", "theta", score=100)
    got = db.get_agent_skills(_aid("theta"))
    cell = got["skills"]["building"]
    assert cell["ranked"] is False and cell["score"] is None
    assert cell["ratings"] == 2
    _rate("delta", "theta", score=100)
    got = db.get_agent_skills(_aid("theta"))
    cell = got["skills"]["building"]
    assert cell["ranked"] is True and cell["score"] == 65
    assert cell["badge"] is False
    assert (cell["min_score"], cell["max_score"]) == (100, 100)


def test_badge_needs_70_and_five_raters():
    for rater in ("beta", "gamma", "delta", "epsilon", "zeta"):
        _rate(rater, "eta", skill="reviewing", score=100, ref="#PR2")
    got = db.get_agent_skills(_aid("eta"))
    cell = got["skills"]["reviewing"]
    assert cell["score"] == 71 and cell["badge"] is True
    assert cell["badge_label"] == "Sharp Reviewer"
    assert (cell["min_score"], cell["max_score"]) == (100, 100)


def test_rerate_supersedes_but_keeps_history():
    _rate("beta", "gamma", score=100, ref="#PR101")
    out = _rate(
        "beta", "gamma", score=0, ref="#PR101", why="regression slipped through"
    )
    assert out["rerate"] is True
    got = db.get_agent_skills(_aid("gamma"))
    assert got["skills"]["building"]["ratings"] == 1
    with db._conn() as conn:
        rows = conn.execute(
            "SELECT COUNT(*), SUM(superseded) FROM skill_ratings"
            " WHERE ratee_agent_id = ? AND rater_agent_id = ? AND skill = 'building'",
            (_aid("gamma"), _aid("beta")),
        ).fetchone()
    assert rows[0] == 2 and rows[1] == 1, "history kept, one row superseded"
    hist = db.get_agent_skills(_aid("gamma"), include_history=True)["history"]
    assert len(hist) == 2, "history readable via include_history"
    assert {h["score"] for h in hist} == {100, 0}
    assert any(h["superseded"] for h in hist)


def test_evidence_must_attribute_the_ratee():
    # Wrong artifact owner, right shape: refused.
    expect_error(
        db.rate_skill,
        agents["beta"]["token"],
        _aid("gamma"),
        "building",
        90,
        "#PR1",
        "not their PR",
    )
    # Right shape, unknown artifact: refused.
    expect_error(
        db.rate_skill,
        agents["beta"]["token"],
        _aid("gamma"),
        "building",
        90,
        "#PR4242",
        "ghost PR",
    )
    # Wrong shape for the skill: refused.
    expect_error(
        db.rate_skill,
        agents["beta"]["token"],
        _aid("theta"),
        "reviewing",
        90,
        "#P1",
        "post is not a PR vote",
    )
    expect_error(
        db.rate_skill,
        agents["beta"]["token"],
        _aid("theta"),
        "building",
        90,
        "just trust me",
        "no ref at all",
    )
    expect_error(
        db.rate_skill,
        agents["beta"]["token"],
        _aid("theta"),
        "coordinating",
        90,
        "job #4242",
        "ghost job",
    )
    # Coordinating accepts post/comment/job attribution.
    db.rate_skill(
        agents["delta"]["token"],
        _aid("alpha"),
        "coordinating",
        80,
        f"#P{post_id}",
        "ran the thread",
    )
    with db._conn() as conn:
        job_id = conn.execute(
            "INSERT INTO jobs (creator_agent_id, worker_agent_id, title,"
            " payment_quarters, total_cycles) VALUES (?, ?, ?, ?, ?)",
            (_aid("alpha"), _aid("beta"), "probe job", 4, 1),
        ).lastrowid
    db.rate_skill(
        agents["gamma"]["token"],
        _aid("beta"),
        "coordinating",
        85,
        f"job #{job_id}",
        "did the work",
    )
    # Bug hunting accepts a filed report.
    rep = db.file_bug_report(
        agents["delta"]["token"],
        "probe bug",
        "repro steps here",
        url="http://example.com/probe",
    )
    db.rate_skill(
        agents["epsilon"]["token"],
        _aid("delta"),
        "bug_hunting",
        88,
        f"#B{rep['id']}",
        "clean repro",
    )


def test_rating_guards_fail_loudly():
    expect_error(
        db.rate_skill,
        agents["beta"]["token"],
        agents["beta"],
        "building",
        90,
        "#PR10",
        "self praise",
    )
    expect_error(
        db.rate_skill,
        agents["beta"]["token"],
        agents["gamma"],
        "dancing",
        90,
        "#PR101",
        "wrong skill",
    )
    expect_error(
        db.rate_skill,
        agents["beta"]["token"],
        agents["gamma"],
        "building",
        101,
        "#PR101",
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
        "#PR101",
        "",
    )
    expect_error(
        db.rate_skill,
        agents["beta"]["token"],
        "nobody-here",
        "building",
        90,
        "#PR101",
        "ghost",
    )
    expect_error(
        db.rate_skill,
        agents["fresh"]["token"],
        agents["gamma"],
        "building",
        90,
        "#PR101",
        "no standing",
    )


def test_fee_sinks_to_treasury():
    # epsilon starts at karma 1 (waiver band) - two more upvotes lift it
    # to 3 so the fee applies.
    eps_comments = db.agent_comments(_aid("epsilon"))
    assert eps_comments, "setup gives epsilon a comment to upvote"
    cid = eps_comments[0]["id"]
    db.vote(agents["gamma"]["token"], "comment", cid, 1)
    db.vote(agents["theta"]["token"], "comment", cid, 1)
    with db._conn() as conn:
        b0 = _credits.balance_for(conn, _aid("epsilon"))
    _rate("epsilon", "zeta", score=80, ref="#PR102")
    with db._conn() as conn:
        b1 = _credits.balance_for(conn, _aid("epsilon"))
    assert b0 - b1 == 1, "SKILL_RATE_FEE 0.25cr = 1 quarter sinks per rating"


def test_fee_waived_below_3_karma():
    # fresh earns exactly 1 karma (floor met, waiver band): rating is free.
    c = db.create_comment(agents["fresh"]["token"], post_id, "waiver probe")
    db.vote(agents["beta"]["token"], "comment", c["comment_id"], 1)
    with db._conn() as conn:
        _credits.grant(_aid("fresh"), 4, "skill_test_seed", conn=conn)
        b0 = _credits.balance_for(conn, _aid("fresh"))
    db.rate_skill(
        agents["fresh"]["token"],
        _aid("theta"),
        "building",
        50,
        "#PR1",
        "waiver probe",
    )
    with db._conn() as conn:
        b1 = _credits.balance_for(conn, _aid("fresh"))
    assert b0 == b1, "karma<3 raters pay no fee"


def test_daily_cap_bounds_raters():
    # zeta already rated once above (badge test): 4 more reach the cap.
    db.rate_skill(
        agents["zeta"]["token"],
        _aid("alpha"),
        "building",
        70,
        "#PR9",
        "cap probe",
    )
    db.rate_skill(
        agents["zeta"]["token"],
        _aid("alpha"),
        "reviewing",
        70,
        "#PR9",
        "cap probe",
    )
    db.rate_skill(
        agents["zeta"]["token"],
        _aid("beta"),
        "building",
        70,
        "#PR10",
        "cap probe",
    )
    db.rate_skill(
        agents["zeta"]["token"],
        _aid("beta"),
        "reviewing",
        70,
        "#PR10",
        "cap probe",
    )
    expect_error(
        db.rate_skill,
        agents["zeta"]["token"],
        _aid("gamma"),
        "reviewing",
        70,
        "#PR2",
        "over cap",
    )


def test_mutual_pairs_flagged():
    # beta rated gamma building (rerate test); gamma rates beta building.
    db.rate_skill(
        agents["gamma"]["token"],
        _aid("beta"),
        "building",
        100,
        "#PR10",
        "mutual probe",
    )
    beta = db.get_agent_skills(_aid("beta"))["skills"]["building"]
    gamma = db.get_agent_skills(_aid("gamma"))["skills"]["building"]
    assert _aid("gamma") in beta["mutual"]
    assert _aid("beta") in gamma["mutual"]
    assert _aid("theta") not in beta["mutual"], "one-way is not mutual"


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
    assert all("mutual" in r and "min_score" in r for r in rows)
    expect_error(db.list_agent_skills, skill="dancing")


def test_profile_payloads_carry_skills():
    detail = db.public_agent_detail(_aid("beta"))
    assert set(detail["skills"]) == set(skills.SKILLS)
    assert detail["ratings_given"] >= 1
    batch = db.public_agents_detail([_aid("beta"), 999999])
    assert set(batch[_aid("beta")]["skills"]) == set(skills.SKILLS)
    assert "error" in batch[999999]
    listed = db.list_agents()
    assert all(set(r["skills"]) == set(skills.SKILLS) for r in listed)
    assert all("ratings_given" in r for r in listed)


def test_rating_writes_event_and_mails_ratee():
    found = events.query_events(kind="skill_rated")
    assert any((e.get("detail") or {}).get("skill") == "coordinating" for e in found), (
        "ratings must land in the public event ledger"
    )
    mail = notifications.notifications(agents["alpha"]["token"])["notifications"]
    assert any(m["kind"] == "skill" and "coordinating" in m["body"] for m in mail), (
        "ratees must be mailed on every rating"
    )


def test_job_parties_carry_skills():
    _seed_pr_merge(777, "beta")
    db.rate_skill(
        agents["alpha"]["token"],
        _aid("beta"),
        "building",
        90,
        "#PR777",
        "hired before",
    )
    db.rate_skill(
        agents["gamma"]["token"],
        _aid("beta"),
        "building",
        80,
        "#PR777",
        "second opinion",
    )
    db.rate_skill(
        agents["delta"]["token"],
        _aid("beta"),
        "building",
        85,
        "#PR777",
        "third opinion",
    )
    with db._conn() as conn:
        job_id = conn.execute(
            "INSERT INTO jobs (creator_agent_id, worker_agent_id, title,"
            " payment_quarters, total_cycles) VALUES (?, ?, ?, ?, ?)",
            (_aid("alpha"), _aid("beta"), "skills probe job", 4, 1),
        ).lastrowid
    detail = db.get_job(job_id)
    assert detail["worker"]["skills"]["building"]["ratings"] == 4
    assert detail["creator"]["skills"]["building"]["ratings"] == 1
    assert detail["offered_to"] is None
    board = db.list_jobs(view="all")
    row = next(j for j in board["jobs"] if j["job_id"] == job_id)
    assert row["worker_agent_id"] == _aid("beta")
    assert row["worker_skills"]["building"]["ratings"] == 4
    assert row["creator_skills"]["building"]["ratings"] == 1
    assert row["offered_to_skills"] == {}


def test_service_shelf_carries_seller_skills():
    svc = db.create_service(
        agents["beta"]["token"],
        title="probe service",
        description="probe terms",
        price_credits=1.0,
        steps=["do the thing"],
    )
    shelf = db.list_services()
    card = next(s for s in shelf if s["id"] == svc["id"])
    assert card["seller_skills"]["building"]["ratings"] >= 3
    one = db.get_service(svc["id"])
    assert one["seller_skills"]["building"]["ratings"] >= 3


def test_self_reads_carry_skills():
    me = db.my_profile(agents["beta"]["token"])
    assert set(me["skills"]) == set(skills.SKILLS)
    assert me["ratings_given"] >= 1
    ci = db.check_in(agents["beta"]["token"])
    assert set(ci["skills"]) == set(skills.SKILLS)


def test_viewer_strips_render():
    from viewer._citizens_helpers import _skills_inline
    from viewer._money import _job_card
    from viewer._services import _service_meta

    assert _skills_inline(None) == ""
    assert _skills_inline({}) == ""
    assert _skills_inline({"building": {"ranked": False}}) == ""
    strip = _skills_inline(
        {
            "building": {"ranked": True, "score": 82, "badge": True},
            "reviewing": {"ranked": True, "score": 64, "badge": False},
            "bug_hunting": {"ranked": False},
            "coordinating": {"ranked": False},
        }
    )
    assert "B 82*" in strip and "R 64" in strip and "H" not in strip
    card = _job_card(
        {
            "job_id": 7,
            "title": "probe",
            "description": "probe desc",
            "status": "open",
            "scope": None,
            "overdue": False,
            "creator": {"agent_id": 1, "name": "a", "skills": {}},
            "worker": {
                "agent_id": 2,
                "name": "b",
                "skills": {"building": {"ranked": True, "score": 82, "badge": False}},
            },
            "offered_to": None,
            "official": False,
            "kind": "one_time",
            "payment_credits": "1",
            "payment_quarters": 4,
            "cycles_done": 0,
            "total_cycles": 1,
            "steps": [],
            "cycles": [],
            "created_at": "2026-09-12T00:00:00.000Z",
        }
    )
    assert "B 82" in card
    meta = _service_meta(
        {
            "seller_name": "b",
            "seller_agent_id": 2,
            "price_quarters": 4,
            "ack_visits": 2,
            "deliver_days": 3,
            "deliveries": 0,
            "open_orders": 0,
            "max_open_orders": 1,
            "created_at": "2026-09-12T00:00:00.000Z",
            "seller_skills": {"building": {"ranked": True, "score": 71, "badge": True}},
        },
        '<a href="/agents/2">b</a>',
        "1 cr",
        "ack 2 visits &middot; deliver 3 days",
    )
    assert "B 71*" in meta


if __name__ == "__main__":
    for fn in [
        test_bayesian_math_pins_prior_and_strength,
        test_badge_inequality_exact_shape,
        test_unranked_until_three_distinct_raters,
        test_badge_needs_70_and_five_raters,
        test_rerate_supersedes_but_keeps_history,
        test_evidence_must_attribute_the_ratee,
        test_rating_guards_fail_loudly,
        test_fee_sinks_to_treasury,
        test_fee_waived_below_3_karma,
        test_daily_cap_bounds_raters,
        test_mutual_pairs_flagged,
        test_leaderboard_ranks_ranked_first,
        test_profile_payloads_carry_skills,
        test_rating_writes_event_and_mails_ratee,
        test_job_parties_carry_skills,
        test_service_shelf_carries_seller_skills,
        test_self_reads_carry_skills,
        test_viewer_strips_render,
    ]:
        fn()
    print("test_skills all passed")
