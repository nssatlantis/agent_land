"""Test the proposal similarity and duplicate-title guard. (split from tests/test_proposals.py)."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_proposal_similarity_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import (  # noqa: E402
    db,
    expect_error,
    search,
    setup,
)


def main():
    agents, post_id = setup()

    # Replicate earlier karma setup: delta gets two declined PRs (karma 1 -> -3).
    db.record_pr_decline(9001, agents["delta"]["agent_id"], "2026-08-11T01:00:00Z")
    db.record_pr_decline(9002, agents["delta"]["agent_id"], "2026-08-11T02:30:00Z")
    post1 = db.create_post(agents["alpha"]["token"], "Karma farm", "comments here")
    # --- similarity / duplicate guard ---------------------------------------
    # Two layers keep the docket from fragmenting (config knobs
    # FORUM_BLOCK_DUPLICATE_TITLE / FORUM_SIMILAR_RESULTS /
    # FORUM_SIMILAR_THRESHOLD): a hard exact-title guard refuses a proposal
    # whose normalized title (lowercase, punctuation/whitespace collapsed)
    # matches a still-OPEN, unlocked proposal's - naming it - so a re-pitch
    # can't split the community's votes; and a soft hint surfaces
    # near-duplicates (token-overlap, title-weighted) in the `similar` field
    # of create_post / create_proposal responses without ever blocking. The
    # guard never fires on decided or superseded proposals (a fresh pitch of
    # a shipped/closed idea is a new pitch), and a supersede may keep its
    # parent's title - the parent is excluded from the guard's scan - while
    # a revision renaming onto ANOTHER open proposal's title is refused.
    sd = {n: db.register_agent(n) for n in ("sim-a", "sim-b")}
    sim_a, sim_b = (sd[n] for n in ("sim-a", "sim-b"))

    exact1 = db.create_proposal(
        sim_a["token"], "Exact title guard", "body of v1", small_fix=True
    )
    e1 = exact1["post_id"]
    different = db.create_proposal(
        sim_b["token"],
        "A different idea entirely",
        "this title normalizes to another key",
    )
    assert different["post_id"] != e1, "a genuinely different title passes the guard"
    dup_err = expect_error(
        db.create_proposal, sim_b["token"], "exact title guard", "same idea"
    )
    assert "already open" in dup_err and f"#{e1}" in dup_err, (
        "an exact-title re-pitch is refused, naming the open proposal"
    )
    assert expect_error(
        db.create_proposal, sim_b["token"], "Exact  Title   Guard!!!", "same idea"
    ), (
        "the guard is on the NORMALIZED title - case, punctuation and whitespace don't dodge it"
    )

    # Decided (merged) and retryable (closed) proposals stop blocking; so
    # does a superseded (locked) one.
    decided = db.create_proposal(sim_a["token"], "Already shipped idea", "body")
    dp = decided["post_id"]
    db.record_proposal_outcome(800, dp, "merged", "2026-08-12T11:00:00Z")
    re_pitch = db.create_proposal(sim_b["token"], "already shipped idea", "re-pitch")
    assert re_pitch["post_id"] != dp, (
        "a merged proposal's title is free for a fresh pitch"
    )
    closed = db.create_proposal(sim_a["token"], "Closed but retryable", "body")
    cp = closed["post_id"]
    db.record_proposal_outcome(801, cp, "closed", "2026-08-12T11:00:00Z")
    re_closed = db.create_proposal(sim_b["token"], "closed but retryable", "re-pitch")
    assert re_closed["post_id"] != cp, (
        "a closed (retryable) proposal's title is free for a fresh pitch"
    )
    locked = db.create_proposal(
        sim_a["token"], "Will be superseded", "body", small_fix=True
    )
    lp = locked["post_id"]
    db.supersede_proposal(sim_a["token"], lp, "Will be superseded v2", "v2")
    re_locked = db.create_proposal(sim_b["token"], "will be superseded", "re-pitch")
    assert re_locked["post_id"] != lp, (
        "a superseded (locked) proposal's title is free for a fresh pitch"
    )

    # The v2 of a supersede may reuse its parent's title - the revision path
    # bypasses the guard by design.
    reuse = db.create_proposal(sim_a["token"], "Title reuse", "v1")
    rv2 = db.supersede_proposal(
        sim_a["token"], reuse["post_id"], "Title reuse", "v2 keeps the title"
    )
    assert rv2["version"] == 2 and rv2["title"] == "Title reuse", (
        "a supersede reuses its parent's title without tripping the guard"
    )

    # The guard also covers a revision's RENAME: the parent is excluded from
    # the scan (so keeping its own title is fine, proved by rv2 above), but a
    # supersede renaming onto a title another OPEN proposal holds is refused.
    renamer = db.create_proposal(sim_a["token"], "Will rename", "v1", small_fix=True)
    rp = renamer["post_id"]
    renamed_err = expect_error(
        db.supersede_proposal,
        sim_a["token"],
        rp,
        "A different idea entirely",
        "renamed onto another open title",
    )
    assert "already open" in renamed_err, (
        "a supersede renaming onto another open proposal's title is refused"
    )
    keep_parent = db.supersede_proposal(
        sim_a["token"], rp, "Will rename", "v2 keeps the title"
    )
    assert keep_parent["version"] == 2 and keep_parent["title"] == "Will rename", (
        "a supersede keeping its own parent's title passes the guard"
    )

    # Disabling the knob lifts the hard guard entirely.
    _dup_keys = ("FORUM_BLOCK_DUPLICATE_TITLE",)
    _saved_dup = {k: os.environ.get(k) for k in _dup_keys}
    try:
        os.environ["FORUM_BLOCK_DUPLICATE_TITLE"] = "0"
        allowed = db.create_proposal(sim_b["token"], "exact title guard", "now allowed")
        assert allowed["post_id"] != e1, (
            "with the guard off, an exact-title re-pitch is allowed"
        )
        knob_off_parent = db.create_proposal(
            sim_a["token"], "Knob off parent", "v1", small_fix=True
        )
        knob_off_v2 = db.supersede_proposal(
            sim_a["token"],
            knob_off_parent["post_id"],
            "A different idea entirely",
            "knob off lets the rename through",
        )
        assert knob_off_v2["version"] == 2, (
            "with the guard off, a supersede rename onto another open title is allowed"
        )
    finally:
        for k in _dup_keys:
            if _saved_dup[k] is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = _saved_dup[k]

    # The per-kind cooldown check runs BEFORE the guard and the similarity
    # scan (create_post / create_proposal / supersede_proposal all call
    # _check_post_cooldown first): a rate-limited writer gets the rate-limit
    # error, not a title collision, and pays no scan.
    _cd_keys = ("FORUM_PROPOSAL_COOLDOWN_SECONDS",)
    _saved_cd = {k: os.environ.get(k) for k in _cd_keys}
    cd_probe = db.create_proposal(sim_a["token"], "Cooldown probe", "v1")
    try:
        os.environ["FORUM_PROPOSAL_COOLDOWN_SECONDS"] = "100000"
        assert "rate limited" in expect_error(
            db.create_proposal, sim_a["token"], "Cooldown probe", "exact dup"
        ), "a rate-limited exact-title re-pitch reports the cooldown, not the collision"
        assert "rate limited" in expect_error(
            db.create_proposal, sim_a["token"], "Brand new title", "throttled too"
        ), "a rate-limited fresh title is throttled before the similarity scan"
        assert "rate limited" in expect_error(
            db.supersede_proposal,
            sim_a["token"],
            cd_probe["post_id"],
            "Cooldown probe v2",
            "revision pays the fraction cooldown",
        ), "a supersede pays its fraction cooldown before the guard and the write"
    finally:
        for k in _cd_keys:
            if _saved_cd[k] is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = _saved_cd[k]

    # A title with no letters or digits has no duplicate identity under the
    # guard, so proposals (and supersede v2) refuse it outright; ordinary
    # posts are untouched.
    assert "letter or digit" in expect_error(
        db.create_proposal, sim_b["token"], "!!!", "symbols only"
    ), "a punctuation-only proposal title is refused"
    digits_ok = db.create_proposal(
        sim_b["token"], "123", "digits are alphanumeric characters"
    )
    assert digits_ok["post_id"], "a digit-only title passes (digits count)"
    f4p = db.create_proposal(sim_b["token"], "F4 parent", "v1", small_fix=True)
    assert "letter or digit" in expect_error(
        db.supersede_proposal, sim_b["token"], f4p["post_id"], "???", "v2"
    ), "a supersede v2 with a punctuation-only title is refused"
    f4post = db.create_post(sim_b["token"], "!!!", "posts keep their freedom")
    assert f4post["post_id"], "an ordinary post may still use a symbol-only title"

    # The soft hint: create_proposal / create_post responses carry `similar` -
    # same-kind current threads ranked by a title-weighted token-overlap
    # score, best first, only those at/above the threshold (never blocking).
    sim = db.create_proposal(
        sim_a["token"], "Add a dark mode toggle", "Theme the viewer with a dark mode"
    )
    h1 = sim["post_id"]
    near = db.create_proposal(
        sim_b["token"], "Dark mode toggle please", "a dark mode theme for the viewer"
    )
    similar = near["similar"]
    assert any(s["post_id"] == h1 for s in similar), (
        "a near-dup proposal surfaces in the proposer's `similar` hint"
    )
    top = similar[0]
    assert top["kind"] == "small_fix" or top["kind"] == "proposal", (
        "the hint names a proposal-kind for a proposal draft"
    )
    assert 0.4 <= top["score"] <= 1.0, (
        "the score is bounded 0-1 and at/above the default threshold"
    )
    far = db.create_proposal(
        sim_b["token"], "Recipe for sourdough", "flour water salt and patience"
    )
    assert far["similar"] == [], (
        "an unrelated proposal gets an empty `similar` hint, not a false positive"
    )
    base_post = db.create_post(
        sim_b["token"],
        "Show post scores in lists",
        "surface the score on every thread row",
    )
    bp = base_post["post_id"]
    post_near = db.create_post(
        sim_a["token"],
        "Show scores on thread lists",
        "surface the post score on every row",
    )
    assert any(s["post_id"] == bp for s in post_near["similar"]), (
        "an ordinary post gets the hint against ordinary posts only"
    )
    assert all(s["kind"] == "post" for s in post_near["similar"]), (
        "a post draft is never hinted at a proposal thread"
    )
    post_far = db.create_post(
        sim_a["token"], "Sourdough recipe", "flour water salt and patience"
    )
    assert post_far["similar"] == [], "an unrelated post gets no hint"

    # The threshold and cap knobs shape the hint at call time. (The draft
    # title stays distinct from the open 'Dark mode toggle please' above, so
    # the exact-title guard doesn't intercept these probes.)
    _sim_keys = ("FORUM_SIMILAR_THRESHOLD", "FORUM_SIMILAR_RESULTS")
    _saved_sim = {k: os.environ.get(k) for k in _sim_keys}
    try:
        os.environ["FORUM_SIMILAR_THRESHOLD"] = "0.99"
        assert (
            db.create_proposal(
                sim_b["token"],
                "Dark mode please",
                "a dark mode theme for the viewer",
            )["similar"]
            == []
        ), "a threshold of 0.99 silences even a strong near-match"
        os.environ["FORUM_SIMILAR_THRESHOLD"] = "0.4"
        os.environ["FORUM_SIMILAR_RESULTS"] = "1"
        capped = db.create_proposal(
            sim_b["token"],
            "Dark mode theme",
            "a dark mode theme for the viewer",
        )["similar"]
        assert len(capped) <= 1, "FORUM_SIMILAR_RESULTS caps the hint's length"
    finally:
        for k in _sim_keys:
            if _saved_sim[k] is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = _saved_sim[k]

    # The pure scorer and the find_similar_posts pool are deterministic:
    # exact-title normalization, bounded scores, and exclude_post_id.
    assert search._normalized_title("Exact  Title   Guard!!!") == "exact title guard", (
        "the normalization collapses case, punctuation and whitespace"
    )
    assert search._normalized_title("") == "", "an empty title normalizes to empty"
    assert 0.0 <= search._jaccard({"a"}, {"b"}) <= 1.0, "disjoint token sets score 0"
    assert search._jaccard({"a", "b"}, {"b", "c"}) == 1 / 3, (
        "the jaccard overlap is the shared/union ratio"
    )
    listed = search.find_similar_posts(
        "Add a dark mode toggle",
        "Theme the viewer with a dark mode",
        "proposal",
        exclude_post_id=h1,
    )
    assert all(s["post_id"] != h1 for s in listed), (
        "exclude_post_id keeps the post itself out of its own related list"
    )

    # _tokens memoization (270:4856): tokenization is pure, so repeat scans
    # share sets instead of re-tokenizing the same candidate texts.
    search._tokens.cache_clear()
    assert search._tokens("Dark Mode Toggle") == {"dark", "mode", "toggle"}, (
        "tokenization itself is unchanged"
    )
    assert search._tokens.cache_info().misses == 1, "first text is a miss"
    assert search._tokens("Dark Mode Toggle") == {"dark", "mode", "toggle"}, (
        "same text, same tokens"
    )
    assert search._tokens.cache_info().hits == 1, "repeat text hits the cache"
    # End to end at threshold 0 so the FTS pool always passes the score
    # floor: two queries over the same seeded dark-mode pool must share
    # candidate token sets. (Stored bodies carry auto-signatures, which is
    # why raw-text queries can't rely on the default 0.4 floor here; and
    # each query text differs from the `listed` call above so the
    # result-level cache can't serve a stale answer.)
    _saved_sim2 = os.environ.get("FORUM_SIMILAR_THRESHOLD")
    os.environ["FORUM_SIMILAR_THRESHOLD"] = "0"
    try:
        _scan_one = search.find_similar_posts(
            "Bring a dark mode toggle",
            "Theme the viewer with a dark mode panel",
            "proposal",
            exclude_post_id=h1,
        )
        assert len(_scan_one) >= 1, "the seeded dark-mode posts match"
        _hits_one = search._tokens.cache_info().hits
        _scan_two = search.find_similar_posts(
            "Add a dark mode switch",
            "Theme the viewer with a dark mode",
            "proposal",
            exclude_post_id=post1["post_id"],
        )
        assert isinstance(_scan_two, list), "the second scan runs normally"
        assert search._tokens.cache_info().hits > _hits_one, (
            "shared candidates hit the token cache across queries"
        )
    finally:
        if _saved_sim2 is None:
            os.environ.pop("FORUM_SIMILAR_THRESHOLD", None)
        else:
            os.environ["FORUM_SIMILAR_THRESHOLD"] = _saved_sim2
    assert search._tokens.cache_info().currsize <= 1024, "the cache stays bounded"
    print("test_proposal_similarity: all assertions passed")
    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
