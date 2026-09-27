"""Tests for the similarity auto-link: search.similar_proposal_for (the
scorer) and the server.poller sweep that retro-links merged-but-unlinked
PRs to their proposal.  The sweep's GitHub reads are injected with fakes
(no network); the links, outcomes, workflow-run closes, karma (or its
absence) and events are asserted on the throwaway database.
"""

import ast
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


def test_candidates_paginate_when_the_knob_exceeds_the_cap():
    """A knob above GitHub's 100 cap must not make a FULL page read as the end
    of the listing.

    The defect (#B131 class, third site - server/poller/_autolink.py): the knob
    was read twice, five lines apart, in two different clamp states.
    `_closed_pulls_page` clamps internally, so a complete 100-row page 1
    satisfied `len(batch) < 150` and the sweep stopped - silently dropping every
    merged PR on page 2 that the retro-link catch-up exists to find. Both arms
    below discriminate the unpatched code, and neither passes on a dead fix.
    """
    saved = config.GITHUB_PRS_PER_PAGE
    page1 = [_raw_pr(6000 + i, f"page one row {i}") for i in range(100)]
    page2 = [_raw_pr(7000, "the page-two merge the catch-up exists to find")]
    seen: list[tuple[int, int]] = []

    def stub(state, per_page, page):
        seen.append((per_page, page))
        if page == 1:
            return list(page1)
        if page == 2:
            return list(page2)
        return []

    try:
        config.GITHUB_PRS_PER_PAGE = 150
        with mock.patch.object(poller, "_closed_pulls_page", stub):
            got = poller._auto_link_candidates(_SINCE)
    finally:
        config.GITHUB_PRS_PER_PAGE = saved
    numbers = [p["number"] for p in got]
    assert numbers[-1:] == [7000], (
        "page 2 was never scanned: the sweep returned "
        f"{len(got)} rows ending {numbers[-1:]}"
    )
    assert seen and all(pp == 100 for pp, _ in seen), (
        f"the wire must carry GitHub's cap, not the raw knob: {seen}"
    )
    print("  candidates paginate when the knob exceeds the cap: ok")


def test_candidates_page_cap_still_bounds_the_scan():
    """The page cap must remain the loop's backstop whatever the knob says.

    A clamp is only sound for positive values: `min(0, 100) == 0` would make
    `len(batch) < 0` permanently false and lean entirely on the cap. Pinned so a
    future clamp change cannot quietly unbind the loop.
    """
    saved = config.GITHUB_PRS_PER_PAGE
    pages: list[int] = []

    def stub(state, per_page, page):
        # A FULL page every time, so the short-page stop can never fire and the
        # page cap is the only thing that can end the scan.
        pages.append(page)
        return [
            _raw_pr(800000 + page * 100 + i, f"always full {page}.{i}")
            for i in range(100)
        ]

    try:
        config.GITHUB_PRS_PER_PAGE = 150
        with mock.patch.object(poller, "_closed_pulls_page", stub):
            got = poller._auto_link_candidates(_SINCE)
    finally:
        config.GITHUB_PRS_PER_PAGE = saved
    assert pages[-1] >= github._PR_PAGE_CAP, (
        f"the scan must stop at the page cap, last page {pages[-1]}"
    )
    assert len(got) >= 1, got
    print("  candidates: the page cap still bounds the scan: ok")


def test_candidates_request_the_knob_verbatim_when_it_is_under_the_cap():
    """NemotronUltra's verification ask on #786: the fix must be a provable
    NO-OP at the live knob, so this ships dormant today.

    `agentland://config/drift` reads the live FORUM_GITHUB_PRS_PER_PAGE as 50 -
    well under GitHub's 100-row cap - while `config.py`'s default is 100, which
    is exactly the value that would make the defect dormant-but-exact. So the
    property worth pinning is not "the knob is 50" (a deployment fact that has
    no business in a suite) but the general one: **whenever the knob is at or
    below the cap, the page size put on the wire is the knob itself**, so the
    clamped and unclamped code request the same thing and the short-page stop
    fires exactly where it always did.
    """
    saved = config.GITHUB_PRS_PER_PAGE
    seen: list[tuple[int, int]] = []

    def stub(state, per_page, page):
        seen.append((per_page, page))
        if page == 1:
            return [_raw_pr(6100 + i, f"under cap {i}") for i in range(50)]
        return [_raw_pr(6200, "the short page that ends the scan")]

    try:
        config.GITHUB_PRS_PER_PAGE = 50
        with mock.patch.object(poller, "_closed_pulls_page", stub):
            got = poller._auto_link_candidates(_SINCE)
    finally:
        config.GITHUB_PRS_PER_PAGE = saved
    assert seen and all(pp == 50 for pp, _ in seen), (
        "below the cap the wire must carry the knob unchanged, or this fix is "
        f"not a no-op at the live value: {seen}"
    )
    assert [p for _, p in seen] == [1, 2], (
        f"a full page then a short page must paginate and then stop: {seen}"
    )
    assert len(got) == 51, len(got)
    print("  candidates: the knob under the cap is requested verbatim: ok")


def test_autolink_stop_test_compares_against_a_clamped_value():
    """Ratchet: this sweep's page stop must never compare against the raw knob.

    Scoped to this file deliberately. The last unclamped sites in
    github/_reads.py are the two OPEN twins, which #PR1506 clamps, so a
    repo-wide pin would be red for a reason unrelated to this change - and a
    ratchet that cries wolf gets deleted. Widen this once #PR1506 lands.
    """
    # A TEXT scan cannot separate a live code site from a sentence about it: the
    # comment above quotes the old expression `len(batch) < knob` in backticks
    # precisely to document the bug this ratchet exists to catch, and the first
    # cut of this pin failed on that comment. Comments never enter the AST and a
    # docstring is a string constant rather than a comparison, so reading the tree
    # makes prose invisible BY CONSTRUCTION instead of by an allowlist someone has
    # to maintain. That asymmetry is the point: a ratchet that cries wolf gets
    # deleted, and deleting it takes the real guard with it.
    root = Path(__file__).resolve().parent.parent
    path = root / "server/poller/_autolink.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))

    def is_len_batch(node):
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "len"
            and len(node.args) == 1
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == "batch"
        )

    # Either operand order, so the pin does not depend on which side an author
    # happened to put `len(batch)` on.
    others: list = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        operands = [node.left, *node.comparators]
        for index in range(len(operands) - 1):
            left, right = operands[index], operands[index + 1]
            if is_len_batch(left):
                others.append(right)
            elif is_len_batch(right):
                others.append(left)

    assert others, "no `len(batch)` page stop found - the ratchet is not seeing it"
    unclamped = [
        ast.dump(other)
        for other in others
        if not (isinstance(other, ast.Name) and other.id == "per_page")
    ]
    assert not unclamped, (
        f"the page stop compares against an unclamped value: {unclamped}"
    )

    clamps = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "per_page" for t in node.targets)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "min"
    ]
    assert clamps, (
        "the stop compares against `per_page` but nothing clamps it - exactly "
        "the two-state shape this ratchet exists to catch"
    )
    assert any("_GITHUB_MAX_PER_PAGE" in ast.dump(n.value) for n in clamps), (
        "the clamp on `per_page` never mentions GitHub's per-page ceiling"
    )
    print("  autolink page stop is clamped (ast, not text): ok")


def main():
    agents, _ = setup()
    test_scorer_matches_best_proposal(agents)
    test_scorer_returns_none_below_threshold(agents)
    test_scorer_requires_margin(agents)
    test_scorer_requires_approval_for_regular_proposals(agents)
    test_scorer_excludes_linked_recorded_collab_and_superseded(agents)
    test_candidates_stop_past_the_window_floor()
    test_candidates_paginate_when_the_knob_exceeds_the_cap()
    test_candidates_page_cap_still_bounds_the_scan()
    test_candidates_request_the_knob_verbatim_when_it_is_under_the_cap()
    test_autolink_stop_test_compares_against_a_clamped_value()
    test_sweep_links_unstamped_merged_pr_lifecycle_only(agents)
    test_sweep_stamped_pr_gets_full_lifecycle(agents)
    test_sweep_skips_linked_recorded_and_unmerged(agents)
    test_sweep_caps_similarity_matches_per_pass(agents)
    test_sweep_isolates_a_poisoned_entry(agents)
    print("\n== test_auto_link: all passed ==")


if __name__ == "__main__":
    main()
