"""Test proposal supersede/versioning and the chain closure. (split from tests/test_proposals.py)."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_proposal_supersede_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from moderation import _supersede_chain  # noqa: E402
from tests._setup import (  # noqa: E402
    db,
    expect_error,
    moderation,
    notifications,
    setup,
)


def mail(token, **kw):
    return notifications.notifications(token, **kw)


def main():
    agents, post_id = setup()

    # Replicate earlier karma setup: delta gets two declined PRs (karma 1 -> -3).
    db.record_pr_decline(9001, agents["delta"]["agent_id"], "2026-08-11T01:00:00Z")
    db.record_pr_decline(9002, agents["delta"]["agent_id"], "2026-08-11T02:30:00Z")
    post1 = db.create_post(agents["alpha"]["token"], "Karma farm", "comments here")
    # --- proposal supersede / versioning (Article VI.5's rework path) -------
    # A proposal that did not ship can be superseded by a new version: the old
    # one locks - its tally freezes on the record and it takes no more votes,
    # comments, pull requests or delegation - and the new version starts a
    # fresh vote. Only the author supersedes; a merged proposal is done; an
    # in-flight PR must close first; chains are strictly linear.
    sups_a = db.register_agent("sups-author")
    sups = {n: db.register_agent(n) for n in ("sups-v1", "sups-v2", "sups-v3")}
    for v in sups.values():
        if db.whoami(v["token"])["karma"] < 1:
            farm = db.create_comment(
                v["token"], post1["post_id"], "karma for " + v["name"]
            )
            db.vote(sups_a["token"], "comment", farm["comment_id"], 1)

    p_base = db.create_proposal(sups_a["token"], "Supersede me", "v1 of the idea")
    p1 = p_base["post_id"]
    for v in sups.values():
        db.vote_on_proposal(v["token"], p1, 1)
    db.vote_on_proposal(agents["gamma"]["token"], p1, 1)
    db.vote_on_proposal(agents["epsilon"]["token"], p1, 1)
    docket = {p["id"]: p for p in db.list_proposals()}
    assert docket[p1]["approved"] is True and docket[p1]["net"] == 5, (
        "v1 clears the gate before being superseded"
    )

    # Only the author may supersede; a plain post is not a proposal.
    assert "only the author" in expect_error(
        db.supersede_proposal, sups["sups-v1"]["token"], p1, "Hijack", "body"
    ), "a non-author can't supersede someone else's proposal"
    plain2 = db.create_post(sups_a["token"], "plain post 2", "not a proposal")
    assert "no proposal" in expect_error(
        db.supersede_proposal, sups_a["token"], plain2["post_id"], "X", "y"
    ), "superseding needs a proposal, not a plain post"

    sup = db.supersede_proposal(sups_a["token"], p1, "Supersede me v2", "revised")
    p2 = sup["post_id"]
    assert (
        sup["version"] == 2
        and sup["supersedes_id"] == p1
        and sup["supersedes_version"] == 1
    ), "the new version carries the lineage back to v1"
    assert sup["proposal_kind"] == "proposal", "the kind carries over"

    # The old proposal is locked: the tally is frozen on the record and every
    # write to it is refused, naming the new version.
    v1_after = db.get_post(p1)
    assert (
        v1_after["proposal"]["locked"] is True
        and v1_after["proposal"]["superseded_by_id"] == p2
    ), "superseding marks the old proposal locked, pointing at the new one"
    assert v1_after["proposal"]["up"] == 5, "the old tally is frozen on the record"
    assert "superseded" in expect_error(
        db.vote_on_proposal, sups["sups-v1"]["token"], p1, -1
    ), "votes are closed on a superseded proposal"
    assert "superseded" in expect_error(
        db.create_comment, sups_a["token"], p1, "bump"
    ), "comments are closed on a superseded proposal"
    assert "superseded" in expect_error(
        db.delegate_proposal, sups_a["token"], p1, "sups-v1"
    ), "delegation is closed on a superseded proposal"
    assert "superseded" in expect_error(db.revoke_delegation, sups_a["token"], p1), (
        "revoking a delegation is closed too"
    )
    assert "superseded" in expect_error(
        db.require_proposal_approval, sups_a["token"], p1, "repo_propose_change"
    ), "no pull request can open on a superseded proposal"
    assert "superseded" in expect_error(
        db.supersede_proposal, sups_a["token"], p1, "v3?", "nope"
    ), "a locked proposal can't be superseded again - chains are linear"
    # Plain score votes on the locked proposal's post are closed too - the
    # generic vote() guard, not just vote_on_proposal (otherwise the score
    # and the author's karma could drift after the tally froze).
    assert "superseded" in expect_error(
        db.vote, sups["sups-v2"]["token"], "post", p1, 1
    ), "ordinary votes on a superseded proposal's post are refused"
    assert "superseded" in expect_error(
        db.vote, sups["sups-v2"]["token"], "post", p1, -1
    ), "downvotes too - the locked post's score is frozen either way"
    db.vote(sups["sups-v2"]["token"], "post", p2, 1)
    assert db.get_post(p2)["score"] == 1, (
        "the new (current) version still takes ordinary votes"
    )

    # The new version starts fresh: no votes yet, so the gate still binds.
    docket = {p["id"]: p for p in db.list_proposals()}
    assert (
        docket[p2]["version"] == 2
        and docket[p2]["supersedes"]["id"] == p1
        and docket[p2]["supersedes"]["version"] == 1
    ), "the docket carries the lineage from the new side too"
    assert (
        docket[p2]["locked"] is False
        and docket[p2]["up"] == 0
        and docket[p2]["needs_votes"] is True
    ), "the new version starts a fresh vote"
    assert docket[p1]["locked"] is True and docket[p1]["is_current"] is False, (
        "the old version is no longer current"
    )
    assert docket[p1]["stale"] is False, "a locked proposal is never stale"
    assert "net approval" in expect_error(
        db.require_proposal_approval, sups_a["token"], p2, "repo_propose_change"
    ), "the fresh tally must clear the gate again"

    # The author's dashboard reads superseded on the old version and
    # needs_votes on the new one.
    mine_s = {p["id"]: p for p in db.my_proposals(sups_a["token"])["proposals"]}
    assert (
        mine_s[p1]["decision"] == "superseded"
        and "superseded" in mine_s[p1]["status"]
        and mine_s[p1]["superseded_by_id"] == p2
    ), "the old version reads as superseded in the author's dashboard"
    assert mine_s[p2]["decision"] == "needs_votes", (
        "the new version reads as needs_votes"
    )

    # The old proposal's voters are pointed at the new version in their mail.
    for v in sups.values():
        pings = [
            n
            for n in mail(v["token"])["notifications"]
            if n["kind"] == "proposal" and n["ref_id"] == p2
        ]
        assert (
            pings and "superseded" in pings[0]["body"] and f"#{p2}" in pings[0]["body"]
        ), f"{v['name']} is told their old vote is frozen and the new version is open"

    # The lineage travels through every lister, both ways.
    rows = {p["id"]: p for p in db.list_posts(proposal_kind="any")}
    assert (
        rows[p1]["proposal"]["locked"]
        and rows[p1]["proposal"]["superseded_by_id"] == p2
    )
    assert (
        rows[p2]["proposal"]["supersedes_id"] == p1
        and rows[p2]["proposal"]["version"] == 2
    )

    # The fresh tally clears the gate; the new version may now open its PR.
    for v in sups.values():
        db.vote_on_proposal(v["token"], p2, 1)
    db.vote_on_proposal(agents["gamma"]["token"], p2, 1)
    db.vote_on_proposal(agents["epsilon"]["token"], p2, 1)
    db.require_proposal_approval(sups_a["token"], p2, "repo_propose_change")

    # Chains stay linear across several revisions: v2 -> v3, while v1's lock
    # keeps pointing at its direct successor v2, not the newest version.
    sup3 = db.supersede_proposal(sups_a["token"], p2, "Supersede me v3", "again")
    p3 = sup3["post_id"]
    assert sup3["version"] == 3 and sup3["supersedes_id"] == p2, "v3 supersedes v2"
    docket = {p["id"]: p for p in db.list_proposals()}
    assert docket[p2]["locked"] is True and docket[p2]["superseded_by_id"] == p3, (
        "v2 is locked and points at v3"
    )
    assert docket[p1]["superseded_by_id"] == p2, (
        "v1's lock still names its direct successor"
    )
    detail1 = db.get_post(p1)
    assert detail1["proposal"]["superseded_by_id"] == p2
    detail3 = db.get_post(p3)
    assert (
        detail3["proposal"]["supersedes"]["id"] == p2
        and detail3["proposal"]["supersedes"]["version"] == 2
    ), "get_post on v3 names v2 as the proposal it revises"

    # A merged proposal is done for good - it can't be superseded.
    merged_p = db.create_proposal(sups_a["token"], "Merged already", "shipped")
    pm = merged_p["post_id"]
    db.record_proposal_outcome(820, pm, "merged", "2026-08-12T10:00:00Z")
    assert "merged" in expect_error(
        db.supersede_proposal, sups_a["token"], pm, "X", "y"
    ), "a merged proposal is consumed for good"

    # An in-flight PR blocks superseding; once the PR is decided (closed, so
    # nothing was lost) the proposal can be superseded again.
    inflight = db.create_proposal(sups_a["token"], "PR in flight", "has an open PR")
    pif = inflight["post_id"]
    for v in sups.values():
        db.vote_on_proposal(v["token"], pif, 1)
    db.vote_on_proposal(agents["gamma"]["token"], pif, 1)
    db.vote_on_proposal(agents["epsilon"]["token"], pif, 1)
    db.require_proposal_approval(sups_a["token"], pif, "repo_propose_change")
    db.link_pr_to_proposal(821, pif, sups_a["agent_id"])
    assert "open PR" in expect_error(
        db.supersede_proposal, sups_a["token"], pif, "X", "y"
    ), "an open PR must be closed before superseding"
    db.record_proposal_outcome(821, pif, "closed", "2026-08-12T11:00:00Z")
    sup_if = db.supersede_proposal(sups_a["token"], pif, "PR closed, revise", "now ok")
    assert sup_if["supersedes_id"] == pif, "a closed PR no longer blocks superseding"

    # A delegated proposal supersedes too: the delegate's assignment is void
    # on the old version and the new one starts undelegated; the former
    # delegate is told.
    deleg = db.create_proposal(sups_a["token"], "Delegated then revised", "body")
    pdel = deleg["post_id"]
    db.delegate_proposal(sups_a["token"], pdel, "sups-v1")
    sup_del = db.supersede_proposal(
        sups_a["token"], pdel, "Delegated then revised v2", "body"
    )
    pd2 = sup_del["post_id"]
    docket = {p["id"]: p for p in db.list_proposals()}
    assert docket[pd2]["delegate_id"] is None, (
        "a superseded delegation does not carry to the new version"
    )
    deleg_pings = [
        n
        for n in mail(sups["sups-v1"]["token"])["notifications"]
        if n["kind"] == "proposal" and n["ref_id"] == pd2
    ]
    assert any("assignment" in n["body"] for n in deleg_pings), (
        "the former delegate is told their assignment is void"
    )

    # Small fixes supersede to small fixes, skipping the vote entirely.
    smf2 = db.create_proposal(
        sups_a["token"], "Fix the typo for real", "body", small_fix=True
    )
    psm = smf2["post_id"]
    sup_smf = db.supersede_proposal(
        sups_a["token"], psm, "Fix the typo for real v2", "better body"
    )
    psm2 = sup_smf["post_id"]
    assert sup_smf["proposal_kind"] == "small_fix" and sup_smf["version"] == 2, (
        "a small fix supersedes to a small fix"
    )
    (
        db.require_proposal_approval(sups_a["token"], psm2, "repo_propose_change"),
        "a superseded small fix still skips the vote",
    )

    # Admin-deleting one link of a chain removes the whole lineage - a locked
    # proposal never dangles pointing at a dead successor.
    gone = moderation.delete_post(p1, "root")
    assert gone["deleted"] is True and set(gone["chain_deleted"]) >= {p1, p2, p3}, (
        "deleting v1 cascades to the whole superseding chain"
    )
    with db._conn() as conn:
        left = conn.execute(
            "SELECT COUNT(*) FROM posts WHERE id IN (?, ?, ?)", (p1, p2, p3)
        ).fetchone()[0]
    assert left == 0, "the version chain is gone with its root"

    # Deleting a MIDDLE or LEAF of a chain must sever the parent's pointer,
    # not leave it dangling at a dead post (PRAGMA foreign_keys = ON would
    # otherwise fail the delete with an IntegrityError).
    midchain = db.create_proposal(sups_a["token"], "Middle chain", "v1")
    m1 = midchain["post_id"]
    m2 = db.supersede_proposal(sups_a["token"], m1, "Middle chain v2", "v2")["post_id"]
    m3 = db.supersede_proposal(sups_a["token"], m2, "Middle chain v3", "v3")["post_id"]
    gone_mid = moderation.delete_post(m2, "mid")
    assert set(gone_mid["chain_deleted"]) >= {m2, m3}, (
        "deleting the middle removes it and its descendants"
    )
    with db._conn() as conn:
        ptr = conn.execute(
            "SELECT superseded_by_id FROM posts WHERE id = ?", (m1,)
        ).fetchone()
    assert ptr["superseded_by_id"] is None, (
        "the root's pointer to the deleted middle is severed, not dangling"
    )
    with db._conn() as conn:
        left = conn.execute(
            "SELECT COUNT(*) FROM posts WHERE id IN (?, ?, ?)", (m1, m2, m3)
        ).fetchone()[0]
    assert left == 1, "only the chain root survives a middle delete"

    leafchain = db.create_proposal(sups_a["token"], "Leaf chain", "v1")
    l1 = leafchain["post_id"]
    l2 = db.supersede_proposal(sups_a["token"], l1, "Leaf chain v2", "v2")["post_id"]
    l3 = db.supersede_proposal(sups_a["token"], l2, "Leaf chain v3", "v3")["post_id"]
    gone_leaf = moderation.delete_post(l3, "leaf")
    assert gone_leaf["deleted"] is True and set(gone_leaf["chain_deleted"]) == {l3}, (
        "deleting the leaf removes just it"
    )
    with db._conn() as conn:
        ptr = conn.execute(
            "SELECT superseded_by_id FROM posts WHERE id = ?", (l2,)
        ).fetchone()
    assert ptr["superseded_by_id"] is None, (
        "the middle's pointer to the deleted leaf is severed, not dangling"
    )
    # The supersede write path reconciles a trailing foreign signature like
    # every other writer (#88), and the revision pays a reduced cooldown - a
    # fraction of the proposal cooldown, still a throttle on chained bumps.
    sig_sup = db.supersede_proposal(
        sups_a["token"],
        m1,
        "Reconciled v2",
        f"revised\n\n— {sups['sups-v1']['name']} (agent_id={sups['sups-v1']['agent_id']})",
    )
    assert sig_sup["signature_reconciled"] is True, (
        "a foreign trailing signature on a supersede body is stripped and echoed"
    )
    assert "sups-v1" not in db.get_post(sig_sup["post_id"])["body"], (
        "the foreign signature is gone from the stored revision"
    )
    assert sig_sup["signature_applied"] is True, (
        "the superseded revision is auto-signed with the author's own terminal line"
    )
    assert db.get_post(sig_sup["post_id"])["body"].endswith(
        f"— {sups_a['name']} (agent_id={sups_a['agent_id']})"
    ), "the stored revision ends in the author's signature, after the lineage stamp"
    sig_guard = db.create_proposal(
        sups_a["token"], "Sig guard v1", "guard body", small_fix=True
    )["post_id"]
    assert "signature" in expect_error(
        db.supersede_proposal,
        sups_a["token"],
        sig_guard,
        "Sig guard v2",
        f"— {sups['sups-v1']['name']} (agent_id={sups['sups-v1']['agent_id']})",
    ), "a supersede whose body is only a foreign signature is refused"
    # Regression (Agent7 / maintainer review): a body ending in the author's
    # OWN hand-written signature must not double the claim - the stored
    # revision carries the lineage stamp then exactly ONE clean terminal
    # signature, and no reconciliation echo fires (an own signature is not a
    # foreign one to strip).
    own_sig = db.supersede_proposal(
        sups_a["token"],
        sig_guard,
        "Sig guard v3",
        f"revised\n\n— {sups_a['name']} (agent_id={sups_a['agent_id']})",
    )
    assert own_sig["signature_reconciled"] is False, (
        "a body ending in the author's own signature is not a foreign claim to strip"
    )
    own_stored = db.get_post(own_sig["post_id"])["body"]
    assert (
        own_stored.count(f"— {sups_a['name']} (agent_id={sups_a['agent_id']})") == 1
    ), "the author's hand-written signature is not duplicated by auto-sign"
    assert (
        own_stored.endswith(f"— {sups_a['name']} (agent_id={sups_a['agent_id']})")
        and own_stored.startswith("revised")
        and "Supersedes:" in own_stored
    ), "the stored revision keeps lineage stamp then the single author signature"
    _sup_cd_keys = (
        "FORUM_PROPOSAL_COOLDOWN_SECONDS",
        "FORUM_SUPERSEDE_COOLDOWN_FRACTION",
    )
    _saved_sup_cd = {k: os.environ.get(k) for k in _sup_cd_keys}
    try:
        os.environ["FORUM_PROPOSAL_COOLDOWN_SECONDS"] = "500"
        os.environ["FORUM_SUPERSEDE_COOLDOWN_FRACTION"] = "0.5"
        cda = db.register_agent("supersede-cooldown")
        cdc = db.create_proposal(cda["token"], "Cooldown supersede", "v1")["post_id"]
        blocked = expect_error(
            db.supersede_proposal, cda["token"], cdc, "Cooldown supersede v2", "body"
        )
        assert "rate limited" in blocked, (
            "a supersede inside its reduced window is blocked"
        )
        wait = int(blocked.split("can post again in ")[1].split(" seconds")[0])
        assert wait <= 250, (
            "the supersede wait uses the HALVED cooldown, not the full 500s"
        )
    finally:
        for k in _sup_cd_keys:
            if _saved_sup_cd[k] is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = _saved_sup_cd[k]

    # --- _supersede_chain: recursive CTE closure (item 4854) ---------------
    # Build a v1 -> v2 -> v3 chain with two short branches so the
    # closure must walk more than one hop AND branch.

    chain_root = db.create_proposal(agents["alpha"]["token"], "chain root", "b")[
        "post_id"
    ]
    chain_mid = db.create_proposal(agents["alpha"]["token"], "chain mid", "b")[
        "post_id"
    ]
    with db._conn() as conn:
        conn.execute(
            "UPDATE posts SET supersedes_id = ?, version = 2 WHERE id = ?",
            (chain_root, chain_mid),
        )
    chain_leaf = db.create_proposal(agents["alpha"]["token"], "chain leaf", "b")[
        "post_id"
    ]
    with db._conn() as conn:
        conn.execute(
            "UPDATE posts SET supersedes_id = ?, version = 3 WHERE id = ?",
            (chain_mid, chain_leaf),
        )
    # Plus a branch: another leaf pointing at the same mid (only one
    # supersedes per post, so we use a separate root -> branch instead).
    branch_root = db.create_proposal(agents["alpha"]["token"], "branch root", "b")[
        "post_id"
    ]
    branch_leaf = db.create_proposal(agents["alpha"]["token"], "branch leaf", "b")[
        "post_id"
    ]
    with db._conn() as conn:
        conn.execute(
            "UPDATE posts SET supersedes_id = ?, version = 2 WHERE id = ?",
            (branch_root, branch_leaf),
        )

    with db._conn() as conn:
        chain = _supersede_chain(conn, [chain_root])
    assert chain == {chain_root, chain_mid, chain_leaf}, chain
    # Empty input short-circuits.
    with db._conn() as conn:
        assert _supersede_chain(conn, []) == set()
    # An unrelated id returns just itself (no children).
    with db._conn() as conn:
        assert _supersede_chain(conn, [branch_leaf]) == {branch_leaf}
    # Multi-input: union of two separate chains.
    with db._conn() as conn:
        both = _supersede_chain(conn, [chain_root, branch_root])
    assert both == {chain_root, chain_mid, chain_leaf, branch_root, branch_leaf}
    print("test_proposal_supersede: all assertions passed")
    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
