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


_PER_PAGE_CAP_NAME = "_GITHUB_MAX_PER_PAGE"

# Every `len(batch) <` page stop in production code, by file. This is the
# COMPLETENESS half of the ratchet, and it is asserted in both directions: a
# site added in a new file fails the equality, and so does a site DELETED -
# so coverage cannot quietly shrink while the pin stays green. A one-way
# check passes on the failure it was built to catch and is silent on the
# other, which is the direction that actually rots.
EXPECTED_PAGE_STOP_FILES = {
    "github/_reads.py": 8,
    "server/poller/_autolink.py": 1,
}

_SKIP_DIRS = {".git", "__pycache__", "tests", "node_modules"}


def _is_len_batch(node) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "len"
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == "batch"
    )


def _page_stop_sites(tree) -> list:
    """Every page-stop comparator in a module, with its line.

    Either operand order, so the pin does not depend on which side an author
    happened to put `len(batch)` on.
    """
    found: list = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        operands = [node.left, *node.comparators]
        for index in range(len(operands) - 1):
            left, right = operands[index], operands[index + 1]
            if _is_len_batch(left):
                found.append((node.lineno, right))
            elif _is_len_batch(right):
                found.append((node.lineno, left))
    return found


def _clamped_locals(tree) -> set:
    """Names in this module bound by a `min(...)` call - i.e. hoisted clamps."""
    names: set = set()
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "min"
        ):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                names.add(target.id)
    return names


def test_no_page_stop_compares_against_an_unclamped_value():
    """Ratchet over the CLASS, not the instance (#B131 / #786).

    The defect shape: a pagination loop stops on `len(batch) < X` where X is
    not the value that went on the wire. A FULL page then satisfies the test
    and reads as the end of the listing, so the sweep returns less than its
    window *while believing it exhausted it* - and for the autolink catch-up
    that means a merged PR on page 2 is never retro-linked, permanently,
    because the next sweep agrees with itself.

    Nine sites, eight already correct: four compare a hoisted `min`-clamped
    local, four compare GitHub's ceiling constant directly (and interpolate
    that same constant into the URL one line above, so the two provably
    agree), and `server/poller/_autolink.py` was the only raw-knob member -
    which this same PR fixes. This is the deliverable #786 named in its own
    title, and the previous cut of this pin deferred it to #PR1506. That PR
    has since merged, clamp-only, and a merged PR takes no further commits -
    so the deferral named a vehicle that had already departed. The condition
    in the old docstring ("widen this once #PR1506 lands") was checkable, and
    this is me noticing it had been met.

    Three properties, each present because its absence is a known failure:

    1. AST, not text. A text scan cannot tell a live code site from a
       sentence about one: the fix's own comment quotes the old expression
       `len(batch) < knob` in backticks to document the very bug this ratchet
       exists to catch, and the first cut FAILED on that comment. Comments
       never enter the tree and a docstring is a string constant rather than
       a Compare, so prose is invisible by construction rather than by an
       allowlist someone has to maintain. A ratchet's false positive is more
       dangerous than a test's: a test that cries wolf reds CI (loud, safe),
       while a ratchet cries wolf until somebody deletes it - and deleting it
       returns the real defect unguarded.
    2. A POSITIVE whitelist of correct operand shapes, not a blacklist. The
       defect at this PR's own site was `config.GITHUB_PRS_PER_PAGE`, an
       `ast.Attribute`; a rule of the form "reject Names that are not
       `per_page`" would have waved it straight through. Naming the two
       shapes that are actually right is what makes the pin discriminate.
    3. Bi-directional completeness (the EXPECTED_PAGE_STOP_FILES equality
       above), so a new site and a deleted site both redden.

    Scoped honestly: this reads production `.py` and skips `tests/`. It says
    nothing about whether each site is reached at runtime, and the pin's
    value is that it needs no maintenance to keep covering new modules - a
    new file with a page stop shows up in the walk and fails the equality.
    """
    root = Path(__file__).resolve().parent.parent
    found_counts: dict = {}
    offenders: list = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if rel.split("/")[0] in _SKIP_DIRS or rel == "server.py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        sites = _page_stop_sites(tree)
        if not sites:
            continue
        found_counts[rel] = len(sites)
        clamped = _clamped_locals(tree)
        for lineno, operand in sites:
            if isinstance(operand, ast.Name):
                ok = operand.id in clamped or operand.id == _PER_PAGE_CAP_NAME
            elif isinstance(operand, ast.Attribute):
                ok = operand.attr == _PER_PAGE_CAP_NAME
            else:
                ok = False
            if not ok:
                offenders.append(f"{rel}:{lineno} compares against {ast.dump(operand)}")

    assert found_counts, (
        "no `len(batch)` page stop found anywhere - either the class is gone "
        "(delete this ratchet deliberately) or the walk is not seeing it"
    )
    assert not offenders, (
        f"a page stop compares against a value that did not go on the wire: {offenders}"
    )
    assert found_counts == EXPECTED_PAGE_STOP_FILES, (
        "the page-stop inventory moved. A site added or removed is a real "
        "change and the two directions fail differently: a NEW site is a new "
        "unguarded comparison, a REMOVED one is coverage that quietly "
        f"disappeared. found={found_counts} expected={EXPECTED_PAGE_STOP_FILES}"
    )
    total = sum(found_counts.values())
    print(f"  all {total} page stops are clamped (ast, not text): ok")


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
    test_no_page_stop_compares_against_an_unclamped_value()
    test_sweep_links_unstamped_merged_pr_lifecycle_only(agents)
    test_sweep_stamped_pr_gets_full_lifecycle(agents)
    test_sweep_skips_linked_recorded_and_unmerged(agents)
    test_sweep_caps_similarity_matches_per_pass(agents)
    test_sweep_isolates_a_poisoned_entry(agents)
    print("\n== test_auto_link: all passed ==")


if __name__ == "__main__":
    main()
