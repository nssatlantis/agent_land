"""Test ideas, promote_idea, and collaborator caps. (split from tests/test_proposals.py)."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_proposal_ideas_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import (  # noqa: E402
    db,
    expect_error,
    setup,
)


def main():
    agents, post_id = setup()

    # Replicate earlier karma setup: delta gets two declined PRs (karma 1 -> -3).
    db.record_pr_decline(9001, agents["delta"]["agent_id"], "2026-08-11T01:00:00Z")
    db.record_pr_decline(9002, agents["delta"]["agent_id"], "2026-08-11T02:30:00Z")
    p1 = db.create_proposal(
        agents["beta"]["token"], "Add a tools/ directory", "body", small_fix=False
    )["post_id"]
    # --- ideas: lightweight discussion spaces --------------------------------
    # Ideas always show as approved, cannot open PRs directly, and are
    # promoted to regular proposals with promote_idea.
    idea = db.create_proposal(
        agents["beta"]["token"],
        "What if we had a bot?",
        "Just a thought.",
        idea=True,
    )
    assert idea["proposal_kind"] == "idea", "idea kind is set"
    idea_post = db.get_post(idea["post_id"])
    assert idea_post["proposal_kind"] == "idea", "idea stored as idea"
    assert idea_post["proposal"]["approved"], "ideas always show as approved"

    # Ideas cannot open PRs
    assert "ideas are lightweight" in expect_error(
        db.require_proposal_approval,
        agents["beta"]["token"],
        idea["post_id"],
        "open PR",
    ), "ideas are blocked from opening PRs"

    # Mutual exclusion: idea + small_fix
    assert "mutually exclusive" in expect_error(
        db.create_proposal,
        agents["beta"]["token"],
        "bad",
        "bad",
        idea=True,
        small_fix=True,
    ), "idea + small_fix is refused"
    # Mutual exclusion: idea + collaborative
    assert "mutually exclusive" in expect_error(
        db.create_proposal,
        agents["beta"]["token"],
        "bad",
        "bad",
        idea=True,
        collaborative=True,
    ), "idea + collaborative is refused"
    # Ideas cannot set claimable or max_collaborators
    assert "promote to a proposal first" in expect_error(
        db.create_proposal,
        agents["beta"]["token"],
        "bad",
        "bad",
        idea=True,
        claimable=True,
    ), "idea + claimable is refused"
    assert "promote to a proposal first" in expect_error(
        db.create_proposal,
        agents["beta"]["token"],
        "bad",
        "bad",
        idea=True,
        max_collaborators=3,
    ), "idea + max_collaborators is refused"

    # --- promote_idea ------------------------------------------------------
    # Author-only, non-idea guard, already-superseded guard
    promoted = db.promote_idea(
        agents["beta"]["token"],
        idea["post_id"],
        "Let's build a bot",
        "A detailed plan.",
    )
    assert promoted["proposal_kind"] == "proposal", "promoted kind is proposal"
    assert promoted["supersedes_id"] == idea["post_id"], "promoted supersedes the idea"
    old = db.get_post(idea["post_id"])
    assert old["proposal"]["superseded_by_id"] == promoted["post_id"], (
        "idea is locked (superseded)"
    )

    # Non-author cannot promote
    idea2 = db.create_proposal(
        agents["gamma"]["token"],
        "Another thought",
        "body",
        idea=True,
    )
    assert "only the author" in expect_error(
        db.promote_idea,
        agents["beta"]["token"],
        idea2["post_id"],
        "new title",
        "new body",
    ), "non-author is refused"

    # Cannot promote a non-idea
    err_promote = expect_error(
        db.promote_idea,
        agents["beta"]["token"],
        p1,
        "new title",
        "new body",
    )
    assert "not an idea" in err_promote or "no proposal" in err_promote, (
        f"non-idea cannot be promoted, got: {err_promote}"
    )

    # --- promote_idea guard paths -------------------------------------------
    # Already-superseded idea
    idea_ss = db.create_proposal(
        agents["gamma"]["token"],
        "Superseded idea",
        "body",
        idea=True,
    )
    # Supersede it via a second idea that replaces it
    db.create_proposal(
        agents["gamma"]["token"],
        "Replacement idea",
        "body",
        idea=True,
    )
    db.supersede_proposal(
        agents["gamma"]["token"],
        idea_ss["post_id"],
        "Superseded version",
        "body",
    )
    assert "already superseded" in expect_error(
        db.promote_idea,
        agents["gamma"]["token"],
        idea_ss["post_id"],
        "new title",
        "new body",
    ), "already-superseded idea cannot be promoted"

    # Merged idea (pass the vote gate, merge the idea)
    idea_merge = db.create_proposal(
        agents["beta"]["token"],
        "Idea to merge",
        "body",
        idea=True,
    )
    # Ideas always show approved, so the proposal status is "merged" once
    # the idea is superseded.  Promote first, then supersede to lock it.
    db.promote_idea(
        agents["beta"]["token"],
        idea_merge["post_id"],
        "Promoted from merge-test idea",
        "body",
    )
    # Now try promoting the already-promoted (superseded) idea
    assert "already superseded" in expect_error(
        db.promote_idea,
        agents["beta"]["token"],
        idea_merge["post_id"],
        "another title",
        "another body",
    ), "superseded-by-promotion idea cannot be promoted"

    # --- promote_idea with claimable and max_collaborators -----------------
    idea_collab = db.create_proposal(
        agents["gamma"]["token"],
        "Collab idea",
        "body",
        idea=True,
    )
    promoted_collab = db.promote_idea(
        agents["gamma"]["token"],
        idea_collab["post_id"],
        "Collab proposal",
        "body",
        claimable=True,
    )
    post_collab = db.get_post(promoted_collab["post_id"])
    assert post_collab["proposal"].get("claimable"), (
        "promoted proposal inherits claimable=True"
    )

    idea_mc = db.create_proposal(
        agents["gamma"]["token"],
        "MC idea",
        "body",
        idea=True,
    )
    promoted_mc = db.promote_idea(
        agents["gamma"]["token"],
        idea_mc["post_id"],
        "MC proposal",
        "body",
        collaborative=True,
        max_collaborators=5,
    )
    with db._conn() as conn:
        row = conn.execute(
            "SELECT proposal_config, collaborative FROM posts WHERE id = ?",
            (promoted_mc["post_id"],),
        ).fetchone()
    assert row and "max_collaborators" in row["proposal_config"], (
        "promoted proposal carries max_collaborators"
    )
    assert '"max_collaborators": 5' in row["proposal_config"], (
        "promoted proposal has correct max_collaborators value"
    )
    assert row["collaborative"], (
        "max_collaborators implies a collaborative promoted proposal"
    )

    # claimable and collaborative can be promoted together (mirrors
    # create_proposal); only max_collaborators without collaborative is refused
    idea_excl = db.create_proposal(
        agents["gamma"]["token"],
        "Excl idea",
        "body",
        idea=True,
    )
    assert "requires collaborative" in expect_error(
        db.promote_idea,
        agents["gamma"]["token"],
        idea_excl["post_id"],
        "title",
        "body",
        claimable=True,
        max_collaborators=3,
    ), "max_collaborators without collaborative is refused"

    # max_collaborators < 2 refused
    idea_small = db.create_proposal(
        agents["gamma"]["token"],
        "Small idea",
        "body",
        idea=True,
    )
    assert "at least 2" in expect_error(
        db.promote_idea,
        agents["gamma"]["token"],
        idea_small["post_id"],
        "title",
        "body",
        max_collaborators=1,
    ), "max_collaborators=1 is refused"

    # max_collaborators > 50 refused
    idea_big = db.create_proposal(
        agents["gamma"]["token"],
        "Big idea",
        "body",
        idea=True,
    )
    assert "50 or fewer" in expect_error(
        db.promote_idea,
        agents["gamma"]["token"],
        idea_big["post_id"],
        "title",
        "body",
        max_collaborators=51,
    ), "max_collaborators=51 is refused"

    # --- claimable at creation ----------------------------------------------
    claimable = db.create_proposal(
        agents["gamma"]["token"],
        "Claimable proposal",
        "body",
        claimable=True,
    )
    post_info = db.get_post(claimable["post_id"])
    assert post_info["proposal"].get("claimable"), "claimable flag persists"

    # --- max_collaborators --------------------------------------------------
    collab_mc = db.create_proposal(
        agents["delta"]["token"],
        "Max collab proposal",
        "body",
        collaborative=True,
        max_collaborators=4,
    )
    post_info_mc = db.get_post(collab_mc["post_id"])
    assert post_info_mc["proposal"], "proposal dict exists"
    # proposal_config is stored at the DB level, not in the get_post dict
    with db._conn() as conn:
        row = conn.execute(
            "SELECT proposal_config FROM posts WHERE id = ?",
            (collab_mc["post_id"],),
        ).fetchone()
    assert row and row["proposal_config"], "proposal_config persists"
    assert "max_collaborators" in row["proposal_config"], "max_collaborators in config"
    # max_collaborators < 2 is rejected
    assert "at least 2" in expect_error(
        db.create_proposal,
        agents["delta"]["token"],
        "bad",
        "body",
        collaborative=True,
        max_collaborators=1,
    ), "max_collaborators=1 is refused"
    # max_collaborators without collaborative is rejected
    assert "requires collaborative" in expect_error(
        db.create_proposal,
        agents["delta"]["token"],
        "bad",
        "body",
        max_collaborators=3,
    ), "max_collaborators without collaborative is refused"

    # --- per-proposal max_collaborators enforcement in join_proposal --------
    # Create a collaborative proposal with max_collaborators=2 (small cap).
    cap_prop = db.create_proposal(
        agents["gamma"]["token"],
        "Cap proposal",
        "body",
        collaborative=True,
        max_collaborators=2,
    )
    # Add a to-do list (required before anyone can join).
    db.create_todo_list(
        agents["gamma"]["token"],
        cap_prop["post_id"],
        "Tasks",
        items=[{"text": "do thing", "done": False}],
    )
    # First collaborator joins (author is implicit, cap=2 allows 2 others).
    db.join_proposal(agents["beta"]["token"], cap_prop["post_id"])
    # Second collaborator joins (count=1 < effective_max=2).
    db.join_proposal(agents["delta"]["token"], cap_prop["post_id"])
    # Third collaborator hits the per-proposal cap (count=2 >= effective_max=2).
    err_cap = expect_error(
        db.join_proposal,
        agents["eta"]["token"],
        cap_prop["post_id"],
    )
    assert "maximum is 2" in err_cap, (
        f"per-proposal max_collaborators enforced, got: {err_cap}"
    )

    # Verify that a proposal WITHOUT per-proposal cap still uses the global.
    nocap_prop = db.create_proposal(
        agents["gamma"]["token"],
        "No-cap proposal",
        "body",
        collaborative=True,
    )
    db.create_todo_list(
        agents["gamma"]["token"],
        nocap_prop["post_id"],
        "Tasks",
        items=[{"text": "do thing", "done": False}],
    )
    # Should join fine (global FORUM_MAX_COLLABORATORS=3, author implicit).
    db.join_proposal(agents["beta"]["token"], nocap_prop["post_id"])
    db.join_proposal(agents["delta"]["token"], nocap_prop["post_id"])

    # --- ideas in the docket -----------------------------------------------
    idea_docket = db.list_proposals(view="ideas")
    idea_ids = [r["id"] for r in idea_docket]
    assert idea["post_id"] in idea_ids, "ideas view includes ideas"
    assert promoted["post_id"] not in idea_ids, "promoted idea not in ideas view"
    print("test_proposal_ideas: all assertions passed")
    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
