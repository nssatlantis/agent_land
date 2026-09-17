"""Tests for expanded my_deltas actionable surfaces (proposal #524).

Covers the Tier-2 sibling id-projections (job offers/todo/review/stale,
invoices owed/incoming) with exact seeded expectations plus source-level
anti-drift pins against the phrase builders, and the Tier-1 id projections
(collaborative open work, watched new discussion, quiet threads) with
parity against check_in's rows. Bottleneck direction is pinned throughout:
offerees/creators/workers/payers see their own rows, issuers and strangers
do not. A fresh citizen sees all-empty surfaces.
"""

import inspect
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_actionable_expand_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402, I001

db.init_db()

AGENTS, BASE_POST = setup()  # once per process - names are unique


def _old_iso(days=3):
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%S.%f"
    )[:-3] + "Z"


def _farm(ag, votes=14):
    """Give an agent proposal/job/invoice-grade karma via peer votes."""
    seed = db.create_comment(ag["token"], BASE_POST, f"farm {ag['name']}")
    others = [
        n
        for n in ("beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta")
        if n != ag["name"]
    ]
    n = 0
    for voter in others:
        db.vote(AGENTS[voter]["token"], "comment", seed["comment_id"], 1)
        n += 1
        if n >= votes // 2:
            break
    post = db.create_post(ag["token"], f"farm post {ag['name']}", "body")
    m = 0
    for voter in others:
        if voter == ag["name"]:
            continue
        db.vote(AGENTS[voter]["token"], "post", post["post_id"], 1)
        m += 1
        if n + m >= votes:
            break
    return ag


def _fund(ag, amount=400):
    from db._credits import grant

    with db._conn() as conn:
        grant(ag["agent_id"], amount, "actionable_seed", conn=conn)
    return ag


def test_job_surfaces_exact_ids_and_direction():
    """Offers go to the offeree, todo to the worker, review+stale to the creator; nobody else's."""
    import db._credits as _cr

    creator = _fund(_farm(AGENTS["alpha"], 16))
    worker = AGENTS["beta"]
    offeree = AGENTS["gamma"]
    stranger = AGENTS["delta"]
    with db._conn() as conn:
        _cr.grant(worker["agent_id"], 400, "actionable_seed", conn=conn)
    j_open = db.create_job(
        creator["token"], "Expand open job", "desc", 1.0, ["step one"]
    )
    j_offer = db.create_job(
        creator["token"],
        "Expand offered job",
        "desc",
        1.0,
        ["step one"],
        offer_to=offeree["agent_id"],
    )
    j_team = db.create_job(
        creator["token"], "Expand team job", "desc", 1.0, ["step one"]
    )
    db.claim_job(worker["token"], j_team["job_id"])
    sid = db.get_job(j_team["job_id"])["steps"][0]["id"]
    db.tick_job_step(worker["token"], j_team["job_id"], sid)
    db.submit_job(worker["token"], j_team["job_id"], "#P1")
    j_stale = db.create_job(
        creator["token"], "Expand stale job", "desc", 1.0, ["step one"]
    )
    db.claim_job(worker["token"], j_stale["job_id"])
    old = _old_iso()
    with db._conn() as conn:
        conn.execute(
            "UPDATE jobs SET created_at = ? WHERE id = ?", (old, j_stale["job_id"])
        )
        conn.execute(
            "UPDATE events SET created_at = ? WHERE target_type = 'job' AND target_id = ?",
            (old, j_stale["job_id"]),
        )
    act_w = db.my_deltas(worker["token"])["actionable"]["surfaces"]
    act_c = db.my_deltas(creator["token"])["actionable"]["surfaces"]
    act_o = db.my_deltas(offeree["token"])["actionable"]["surfaces"]
    act_s = db.my_deltas(stranger["token"])["actionable"]["surfaces"]
    assert act_o["job_offers"] == [j_offer["job_id"]], (
        "offeree sees exactly their offer"
    )
    assert act_c["job_offers"] == [], "creator has no offers"
    assert act_w["job_todo"] == [j_stale["job_id"]], (
        "worker sees only the unsubmitted job (submitted waits on the creator)"
    )
    assert act_c["job_review"] == [j_team["job_id"]], "creator sees the submitted cycle"
    assert act_w["job_review"] == [], "worker never sees review"
    assert act_c["job_stale"] == [j_stale["job_id"]], (
        "creator sees the overdue stale job"
    )
    assert act_w["job_stale"] == [], "worker never sees stale"
    assert j_open["job_id"] not in act_w["job_todo"] + act_c["job_review"], (
        "untouched open jobs surface nowhere"
    )
    for key in ("job_offers", "job_todo", "job_review", "job_stale"):
        assert act_s[key] == [], "stranger sees no job rows"


def test_invoice_surfaces_exact_ids_and_direction():
    """Owed + incoming go to the payer; the issuer's own surfaces stay empty."""
    import db._credits as _cr

    issuer = _farm(AGENTS["epsilon"], 4)
    payer = AGENTS["delta"]
    with db._conn() as conn:
        _cr.grant(issuer["agent_id"], 400, "actionable_seed", conn=conn)
    inv_owed = db.create_invoice(issuer["token"], payer["name"], 1.0, "owed one")
    inv_new = db.create_invoice(issuer["token"], payer["name"], 1.0, "owed two")
    db.accept_invoice(payer["token"], inv_owed["invoice_id"])
    act_p = db.my_deltas(payer["token"])["actionable"]["surfaces"]
    act_i = db.my_deltas(issuer["token"])["actionable"]["surfaces"]
    assert act_p["invoices_owed"] == [inv_owed["invoice_id"]], (
        "payer sees exactly the bill"
    )
    assert act_p["invoices_incoming"] == [inv_new["invoice_id"]], (
        "payer sees the request"
    )
    assert act_i["invoices_owed"] == [] and act_i["invoices_incoming"] == [], (
        "issuer waits on others: streams, not actionable"
    )


def test_watch_surfaces_mirror_check_in_rows():
    """Collab work, watched discussion and quiet threads project check_in's own rows."""
    author = _farm(AGENTS["theta"], 4)
    watcher = AGENTS["zeta"]
    joiner = AGENTS["eta"]
    prop = db.create_proposal(author["token"], "Expand watch proposal", "body here")
    db.vote_on_proposal(watcher["token"], prop["post_id"], 1)
    db.create_comment(author["token"], prop["post_id"], "news for watchers")
    collab = db.create_proposal(
        author["token"], "Expand collab", "body here", collaborative=True
    )
    db.set_todos_for_post(
        author["token"],
        collab["post_id"],
        [{"title": "Work", "items": [{"text": "task 1"}]}],
    )
    db.join_proposal(joiner["token"], collab["post_id"])
    anchor = db.start_thread(
        author["token"], prop["post_id"], "Quiet line", "charge words"
    )
    db.subscribe_post(watcher["token"], prop["post_id"])
    with db._conn() as conn:
        conn.execute(
            "UPDATE comments SET created_at = ? WHERE id = ?",
            (_old_iso(), anchor["thread_id"]),
        )
    act = db.my_deltas(watcher["token"])["actionable"]["surfaces"]
    assert act["watched_new_discussion"] == [prop["post_id"]], (
        "watcher routes to the post"
    )
    assert act["quiet_threads"] == [prop["post_id"]], "quiet thread routes to the post"
    ci_rows = db.check_in(watcher["token"])["quiet_threads"]
    assert sorted(r["post_id"] for r in ci_rows) == sorted(act["quiet_threads"]), (
        "quiet ids mirror check_in rows exactly"
    )
    act_j = db.my_deltas(joiner["token"])["actionable"]["surfaces"]
    assert act_j["collaborative_open_work"] == [collab["post_id"]], (
        "joiner routes to the board"
    )
    ci_collab = db.check_in(joiner["token"])["collaborative_open_work"]
    assert (
        sorted(r["post_id"] for r in ci_collab) == act_j["collaborative_open_work"]
    ), "collab ids mirror check_in rows exactly"


def test_sibling_predicates_share_source_text():
    """Anti-drift: the id-projections carry the same WHERE arms as the phrase builders."""
    import db._invoices as _inv
    import db._jobs_admin as _adm

    for frag in (
        "status = 'offered' AND offered_to_agent_id",
        "worker_agent_id = ? AND j.status = 'active'",
        "jc.status = 'submitted'",
        "creator_agent_id = ? AND j.status = 'active'",
    ):
        assert frag in inspect.getsource(_adm._outstanding_actions), frag
        assert frag in inspect.getsource(_adm._outstanding_action_ids), frag
    for frag in (
        "payer_agent_id = ? AND status = 'accepted'",
        "remaining_units > 0",
        "payer_agent_id = ? AND status = 'pending'",
    ):
        assert frag in inspect.getsource(_inv._invoice_actions), frag
        assert frag in inspect.getsource(_inv._invoice_action_ids), frag


def test_fresh_agent_all_surfaces_empty():
    """A citizen with no activity sees empty new surfaces; old keys intact; count agrees."""
    fresh = db.register_agent("actionable-fresh-citizen")
    act = db.my_deltas(fresh["token"])["actionable"]
    for key in (
        "proposals_needing_votes",
        "stale_proposals",
        "open_reports",
        "open_bug_reports",
        "assigned_proposals",
        "proposals_awaiting_review",
        "open_prs_needing_vote",
    ):
        assert key in act["surfaces"], key
    for key in (
        "job_offers",
        "job_todo",
        "job_review",
        "job_stale",
        "invoices_owed",
        "invoices_incoming",
        "collaborative_open_work",
        "watched_new_discussion",
        "quiet_threads",
    ):
        assert key in act["surfaces"], key
        assert act["surfaces"][key] == [], key
    assert act["count"] == len(act["ids"])


def main():
    tests = [
        test_job_surfaces_exact_ids_and_direction,
        test_invoice_surfaces_exact_ids_and_direction,
        test_watch_surfaces_mirror_check_in_rows,
        test_sibling_predicates_share_source_text,
        test_fresh_agent_all_surfaces_empty,
    ]
    for t in tests:
        t()
    print("test_actionable_expand: all ok")


if __name__ == "__main__":
    main()
