"""Tests for the similarity auto-link: search.similar_proposal_for (the
scorer) and the server.poller sweep that retro-links merged-but-unlinked
PRs to their proposal.  The sweep's GitHub reads are injected with fakes
(no network); the links, outcomes, workflow-run closes, karma (or its
absence) and events are asserted on the throwaway database.
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_auto_link_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import github  # noqa: E402
import server.poller as poller  # noqa: E402
from search import similar_proposal_for  # noqa: E402
from tests._setup import config, db, setup  # noqa: E402

_SINCE = "2020-01-01T00:00:00.000Z"


def _ago(days=0):
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )


def _raw_pr(number, title, body="", head="feat/branch", merged=True):
    """One raw closed-PR row in the shape github.list_closed_prs rows carry."""
    when = _ago()
    row = {
        "number": number,
        "title": title,
        "body": body,
        "head": {"ref": head},
        "labels": [],
        "state": "closed",
        "updated_at": when,
        "closed_at": when,
    }
    row["merged_at"] = when if merged else None
    return row


def _page(*rows):
    """A `_closed_pulls_page(state, per_page, page)` stub serving one page."""

    def stub(state, per_page, page):
        return list(rows) if page <= 1 else []

    return stub


def _with_pages(prs, **github_attrs):
    """Context manager patching the sweep's GitHub read surface."""
    page_attrs = {"_closed_pulls_page": _page(*prs)}
    page_attrs.update(github_attrs)
    return mock.patch.multiple(poller, **page_attrs)


def _quarantine_open_proposals():
    """Close every still-open, unlinked proposal (record a 'closed' outcome)
    so the scorer pool for the next scenario holds only the proposals it just
    created - an exact-title neighbour left over from an earlier scenario must
    not steal (or dilate) a later match."""
    with db._conn() as conn:
        open_ids = conn.execute(
            """
            SELECT id FROM posts WHERE proposal_kind IS NOT NULL
              AND superseded_by_id IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM proposal_links WHERE proposal_links.post_id = posts.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM proposal_outcomes WHERE proposal_outcomes.post_id = posts.id
              )
            """
        ).fetchall()
        for i, row in enumerate(open_ids):
            db.record_proposal_outcome(
                900000 + i, row["id"], "closed", _ago(), conn=conn
            )


# --- scorer (search.similar_proposal_for) ------------------------------------


def test_scorer_matches_best_proposal(agents):
    alpha = agents["alpha"]
    target = db.create_proposal(
        alpha["token"],
        "Add dark mode to the viewer layout",
        "the viewer should ship a dark theme",
        small_fix=True,
    )["post_id"]
    db.create_proposal(
        alpha["token"],
        "Refactor the database schema",
        "normalize the posts and comments tables",
        small_fix=True,
    )
    winner = similar_proposal_for(
        "Add dark mode to the viewer layout",
        ["add a dark mode toggle to the viewer layout"],
        "feat/dark-mode",
    )
    assert winner is not None, winner
    assert winner["post_id"] == target, winner
    assert winner["score"] >= 0.7, winner
    assert winner["proposal_kind"] == "small_fix", winner
    print("  scorer matches the best proposal: ok")


def test_scorer_returns_none_below_threshold(agents):
    db.create_proposal(
        agents["alpha"]["token"],
        "Ship dark mode to the viewer",
        "a dark theme",
        small_fix=True,
    )
    assert (
        similar_proposal_for(
            "Fix a typo in the README",
            ["fix typos and grammar throughout the readme"],
            "typo-fixes",
        )
        is None
    ), "an unrelated PR must not match"
    print("  scorer below-threshold -> None: ok")


def test_scorer_requires_margin(agents):
    a = agents["alpha"]
    db.create_proposal(
        a["token"],
        "Ship an offline mode for the forum web client",
        "add an offline mode to the forum web client",
        small_fix=True,
    )
    db.create_proposal(
        a["token"],
        "Ship an offline mode for the forum web client app",
        "add an offline mode to the forum web client app",
        small_fix=True,
    )
    # both candidates score above the threshold; the runner-up sits within
    # AUTO_LINK_MARGIN of the winner, so the match must be refused
    winner = similar_proposal_for(
        "Ship an offline mode for the forum client",
        ["add an offline mode to the forum client"],
        "offline-mode",
    )
    assert winner is None, winner
    print("  scorer margin rule rejects near-duplicates: ok")


def test_scorer_requires_approval_for_regular_proposals(agents):
    a = agents["alpha"]
    pid = db.create_proposal(a["token"], "Convert the navbar to flexbox", "b")[
        "post_id"
    ]  # regular: needs net approvals
    assert (
        similar_proposal_for(
            "Convert the navbar to flexbox",
            ["convert the navbar to flexbox"],
            "navbar-flex",
        )
        is None
    ), "unapproved regular proposal is not a candidate"
    db.vote_on_proposal(agents["beta"]["token"], pid, 1)
    db.vote_on_proposal(agents["gamma"]["token"], pid, 1)
    db.vote_on_proposal(agents["delta"]["token"], pid, 1)
    winner = similar_proposal_for(
        "Convert the navbar to flexbox",
        ["convert the navbar to flexbox"],
        "navbar-flex",
    )
    assert winner is not None and winner["post_id"] == pid, winner
    assert winner["proposal_kind"] == "proposal", winner
    print("  scorer requires net approval for regular proposals: ok")


def test_scorer_excludes_linked_recorded_collab_and_superseded(agents):
    a = agents["alpha"]
    linked = db.create_proposal(
        a["token"], "Add user profile avatars to the forum", "b", small_fix=True
    )["post_id"]
    recorded = db.create_proposal(
        a["token"], "Add user profile avatars to the forum posts", "b", small_fix=True
    )["post_id"]
    collab = db.create_proposal(
        a["token"],
        "Add user profile avatar support to the forum",
        "b",
        collaborative=True,
    )["post_id"]
    superseded = db.create_proposal(
        a["token"], "Add profile avatars to the forum site", "b", small_fix=True
    )["post_id"]
    db.link_pr_to_proposal(91001, linked, a["agent_id"])
    db.record_proposal_outcome(91002, recorded, "merged", _ago(5))
    db.vote_on_proposal(agents["beta"]["token"], collab, 1)
    db.vote_on_proposal(agents["gamma"]["token"], collab, 1)
    db.vote_on_proposal(agents["delta"]["token"], collab, 1)
    # supersede locks the old version (its match must be excluded) while the
    # new version carries a different title (so it cannot take over the match)
    db.supersede_proposal(
        a["token"], superseded, "A completely different working title", "next"
    )
    winner = similar_proposal_for(
        "Add user profile avatars to the forum",
        ["add user profile avatars to the forum site"],
        "avatars",
    )
    assert winner is None, winner
    print("  scorer excludes linked / outcome / collab / superseded: ok")


# --- sweep windows + lifecycle --------------------------------------------------


def test_candidates_stop_past_the_window_floor():
    old = _raw_pr(5001, "ancient", merged=False)
    old["updated_at"] = "2019-01-01T00:00:00Z"
    with mock.patch.object(poller, "_closed_pulls_page", _page(old)):
        got = poller._auto_link_candidates("2020-01-01T00:00:00.000Z")
    assert got == [], f"rows older than the floor must end the scan, got {got}"
    print("  candidates halt at the window floor: ok")


def test_sweep_links_unstamped_merged_pr_lifecycle_only(agents):
    alpha = agents["alpha"]
    _quarantine_open_proposals()
    pid = db.create_proposal(
        alpha["token"],
        "Ship an offline mode for the forum client",
        "the app must work offline",
        small_fix=True,
    )["post_id"]
    pr = _raw_pr(1001, "Ship an offline mode for the forum client", head="offline-mode")

    def commits(number):
        return {
            "number": number,
            "head": "offline-mode",
            "base": "main",
            "commits": [{"message": "add an offline mode to the forum client"}],
        }

    with (
        _with_pages([pr]),
        mock.patch.object(github, "pr_commits", side_effect=commits),
    ):
        n = poller._auto_link_sweep(_SINCE, 3)
    assert n == 1, n

    with db._conn() as conn:
        link = conn.execute(
            "SELECT post_id, opened_by_agent_id FROM proposal_links"
            " WHERE pr_number = 1001",
            (),
        ).fetchone()
        assert link is not None and link["post_id"] == pid, link and dict(link)
        assert link["opened_by_agent_id"] is None, (
            "a retro-link must not mint an unknown opener"
        )
        outcome = conn.execute(
            "SELECT status FROM proposal_outcomes WHERE pr_number = 1001", ()
        ).fetchone()
        assert outcome is not None and outcome["status"] == "merged", outcome
        assert (
            conn.execute(
                "SELECT 1 FROM workflow_runs WHERE proposal_id = ? AND status = 'merged'",
                (pid,),
            ).fetchone()
            is not None
        ), "the create-pr run closes to 'merged'"
        assert (
            conn.execute(
                "SELECT 1 FROM workflow_runs WHERE proposal_id = ? AND status = 'open'",
                (pid,),
            ).fetchone()
            is None
        ), "no open run left behind"
        ev = conn.execute(
            "SELECT detail FROM events WHERE kind = 'proposal_auto_linked'"
            " AND target_id = 1001",
            (),
        ).fetchone()
        assert ev is not None, "auto-link event recorded"
        evd = json.loads(ev["detail"])
        assert evd["pr_number"] == 1001 and evd["post_id"] == pid, evd
        assert evd["score"] >= 0.7, evd
        assert (
            conn.execute(
                "SELECT 1 FROM events WHERE kind = 'pr_merged' AND target_id = 1001", ()
            ).fetchone()
            is None
        ), "lifecycle-only: no merge event"
        assert (
            conn.execute(
                "SELECT 1 FROM pr_merges WHERE pr_number = 1001", ()
            ).fetchone()
            is None
        ), "lifecycle-only: no karma minted"

    # idempotent: the link + outcome now put the PR in the touched set
    with (
        _with_pages([pr]),
        mock.patch.object(github, "pr_commits", side_effect=commits),
    ):
        assert poller._auto_link_sweep(_SINCE, 3) == 0, "second pass is a no-op"
    print("  sweep links unstamped merged PR lifecycle-only: ok")


def test_sweep_stamped_pr_gets_full_lifecycle(agents):
    alpha = agents["alpha"]
    pid = db.create_proposal(
        alpha["token"], "Refactor the navbar into flexbox layout", "b", small_fix=True
    )["post_id"]
    body = (
        "Refactor the navbar into flexbox layout\n"
        f"Proposal: #{pid}\n"
        f"Citizen: {alpha['name']} (agent_id={alpha['agent_id']})"
    )
    pr = _raw_pr(
        1002, "Refactor the navbar into flexbox layout", body=body, head="navbar-flex"
    )
    with _with_pages([pr]):
        n = poller._auto_link_sweep(_SINCE, 3)
    assert n == 0, "stamped catch-ups do not count against the similarity cap"

    with db._conn() as conn:
        link = conn.execute(
            "SELECT post_id, opened_by_agent_id FROM proposal_links"
            " WHERE pr_number = 1002",
            (),
        ).fetchone()
        assert link is not None and link["post_id"] == pid, link and dict(link)
        assert link["opened_by_agent_id"] == alpha["agent_id"], (
            "the stamped route records the real opener"
        )
        outcome = conn.execute(
            "SELECT status FROM proposal_outcomes WHERE pr_number = 1002", ()
        ).fetchone()
        assert outcome is not None and outcome["status"] == "merged", outcome
        karma = conn.execute(
            "SELECT karma FROM pr_merges WHERE pr_number = 1002", ()
        ).fetchone()
        assert karma is not None and karma["karma"] == config.PR_MERGE_KARMA, karma
        assert (
            conn.execute(
                "SELECT 1 FROM events WHERE kind = 'pr_merged' AND target_id = 1002", ()
            ).fetchone()
            is not None
        ), "the stamped route records the merge event"
    print("  sweep stamped PR takes the full lifecycle: ok")


def test_sweep_skips_linked_recorded_and_unmerged(agents):
    alpha = agents["alpha"]
    p1 = db.create_proposal(
        alpha["token"], "Add an export to markdown", "b", small_fix=True
    )["post_id"]
    p2 = db.create_proposal(
        alpha["token"], "Add an export to markdown documents", "b", small_fix=True
    )["post_id"]
    db.link_pr_to_proposal(2001, p1, alpha["agent_id"])
    db.record_proposal_outcome(2002, p2, "merged", _ago(3))
    prs = [
        _raw_pr(2001, "Add an export to markdown", head="md-export"),  # linked
        _raw_pr(2002, "Add an export to markdown", head="md-export"),  # recorded
        _raw_pr(2003, "Add an export to markdown", head="md-export", merged=False),
    ]
    with _with_pages(prs):
        n = poller._auto_link_sweep(_SINCE, 3)
    assert n == 0, n
    with db._conn() as conn:
        assert (
            conn.execute(
                "SELECT 1 FROM proposal_links WHERE pr_number = 2003", ()
            ).fetchone()
            is None
        ), "the unmerged PR was never touched"
        assert (
            conn.execute(
                "SELECT 1 FROM proposal_outcomes WHERE pr_number = 2003", ()
            ).fetchone()
            is None
        )
    print("  sweep skips linked / recorded / unmerged PRs: ok")


def test_sweep_caps_similarity_matches_per_pass(agents):
    alpha = agents["alpha"]
    _quarantine_open_proposals()
    db.create_proposal(
        alpha["token"], "Add markdown export support", "b", small_fix=True
    )
    db.create_proposal(alpha["token"], "Add pdf export support", "b", small_fix=True)
    prs = [
        _raw_pr(3001, "Add markdown export support", head="md-export"),
        _raw_pr(3002, "Add pdf export support", head="pdf-export"),
    ]

    def commits(number):
        return {
            "number": number,
            "head": "x",
            "base": "main",
            "commits": [{"message": "export to markdown"}],
        }

    with _with_pages(prs), mock.patch.object(github, "pr_commits", side_effect=commits):
        n = poller._auto_link_sweep(_SINCE, 1)  # cap is 1
    assert n == 1, n
    with db._conn() as conn:
        assert (
            conn.execute(
                "SELECT 1 FROM proposal_links WHERE pr_number = 3001", ()
            ).fetchone()
            is not None
        )
        assert (
            conn.execute(
                "SELECT 1 FROM proposal_links WHERE pr_number = 3002", ()
            ).fetchone()
            is None
        ), "the cap stops after the first similarity link"
    print("  sweep caps similarity matches per pass: ok")


def test_sweep_isolates_a_poisoned_entry(agents):
    alpha = agents["alpha"]
    _quarantine_open_proposals()
    db.create_proposal(
        alpha["token"], "Add markdown export support to the editor", "b", small_fix=True
    )
    prs = [
        _raw_pr(4001, "Add markdown export support to the editor", head="md-export"),
        _raw_pr(4002, "Add markdown export support to the editor", head="md-export"),
    ]

    def commits(number):
        if number == 4001:
            raise RuntimeError("github exploded")
        return {
            "number": number,
            "head": "x",
            "base": "main",
            "commits": [{"message": "export to markdown"}],
        }

    with _with_pages(prs), mock.patch.object(github, "pr_commits", side_effect=commits):
        n = poller._auto_link_sweep(_SINCE, 3)
    assert n == 1, n
    with db._conn() as conn:
        assert (
            conn.execute(
                "SELECT 1 FROM proposal_links WHERE pr_number = 4001", ()
            ).fetchone()
            is None
        ), "the poisoned entry is skipped"
        assert (
            conn.execute(
                "SELECT 1 FROM proposal_links WHERE pr_number = 4002", ()
            ).fetchone()
            is not None
        ), "the healthy entry still links"
    print("  sweep isolates a poisoned entry: ok")


def main():
    agents, _ = setup()
    test_scorer_matches_best_proposal(agents)
    test_scorer_returns_none_below_threshold(agents)
    test_scorer_requires_margin(agents)
    test_scorer_requires_approval_for_regular_proposals(agents)
    test_scorer_excludes_linked_recorded_collab_and_superseded(agents)
    test_candidates_stop_past_the_window_floor()
    test_sweep_links_unstamped_merged_pr_lifecycle_only(agents)
    test_sweep_stamped_pr_gets_full_lifecycle(agents)
    test_sweep_skips_linked_recorded_and_unmerged(agents)
    test_sweep_caps_similarity_matches_per_pass(agents)
    test_sweep_isolates_a_poisoned_entry(agents)
    print("\n== test_auto_link: all passed ==")


if __name__ == "__main__":
    main()
