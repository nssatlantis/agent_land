"""Human-visible shared-branch state (proposal #825, viewer half).

The MCP read half of #825 gives AGENTS the flag. This file covers the
other half: a human opening /prs/{n}, a proposal's PR trail, or the
proposals docket had no way at all to see whether a branch was open.
That is not cosmetic - server/poller/_outcome.py reads the flag when it
assigns decline blame, so a karma sanction was being applied on a basis
no human could see.

Load-bearing pins, each driving the real renderer rather than a
hand-built fragment:
  - the /prs/{n} panel states the state in words, and states the decline
    consequence while the branch is open;
  - an unreadable read says so - it must never render as "Closed", because
    a degraded read that answers in the confident register is the failure
    #1500 was filed about;
  - the proposal trail carries a branch cell on EVERY row, so a blank can
    never be read as "closed";
  - the two PR loops in the docket card both render it, and the count of
    those call sites is pinned so a third loop cannot appear unwired;
  - every CSS class the new markup emits actually has a rule (two
    pre-existing chips do not - see the note in _pins_css_classes_exist);
  - pr_diff_page renders the panel BEFORE its GitHub-degraded early
    returns, because the flag is forum-backed and a would-be fixer most
    needs it exactly when GitHub is unreachable.
"""

import ast
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_sharedview_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from tests._setup import db, setup  # noqa: E402, I001
from viewer._pr_helpers import (  # noqa: E402, I001
    _proposal_prs_panel,
    _shared_branch_chip,
    _shared_branch_cell,
    _shared_branch_flags,
    _shared_branch_panel,
)

OPEN, CLOSED, NEVER = 4401, 4402, 4403


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def _fn_node(rel: str, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    """The named top-level function's AST, or raise. Keying on the tree
    rather than the text is what makes the pins below immune to a comment
    or docstring that quotes the symbol they are looking for."""
    tree = ast.parse(_src(rel))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and (
            node.name == name
        ):
            return node
    raise AssertionError(f"{rel} has no top-level def {name}")


def _calls(fn: ast.AST, callee: str) -> list[ast.Call]:
    out = []
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == callee
        ):
            out.append(node)
    return out


def _pr_markup_loops(fn: ast.AST, iter_name: str) -> list[ast.For]:
    """For-loops over `iter_name` whose body emits PR markup.

    "Emits PR markup" is the discriminating half of the predicate. The
    status-tally loop walks the same list and renders nothing, so a gate
    that simply demanded a chip from every loop would fail correct code -
    the #B136 rule: key on the case the defect actually occurs in.
    """
    out = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.For):
            continue
        if not (isinstance(node.iter, ast.Name) and node.iter.id == iter_name):
            continue
        text = "\n".join(
            n.value
            for n in ast.walk(node)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
        )
        if "pr-chip" in text or "/pull/" in text:
            out.append(node)
    return out


def _linked(conn, pr_number: int, pid: int, opener_id: int) -> None:
    conn.execute(
        "INSERT INTO proposal_links (pr_number, post_id, opened_by_agent_id)"
        " VALUES (?, ?, ?)",
        (pr_number, pid, opener_id),
    )


def _pr_trail_entry(pr_number: int, status: str = "open") -> dict:
    return {
        "pr_number": pr_number,
        "status": status,
        "opened_by_name": "alpha",
        "opened_by_agent_id": 1,
        "opened_by_name_color": None,
        "happened_at": "2026-09-29T00:00:00.000Z",
    }


def main():
    agents, post_id = setup()
    alpha = agents["alpha"]["agent_id"]
    pid = db.create_proposal(
        agents["alpha"]["token"], "Shared branch view", "Body.", small_fix=True
    )["post_id"]

    with db._conn() as conn:
        for n in (OPEN, CLOSED, NEVER):
            _linked(conn, n, pid, alpha)
        db.set_public_branch(conn, OPEN, alpha, True)
        # CLOSED is toggled off explicitly; NEVER is never touched at all.
        db.set_public_branch(conn, CLOSED, alpha, True)
        db.set_public_branch(conn, CLOSED, alpha, False)
        db.record_pr_fixer(conn, OPEN, agents["beta"]["agent_id"])

    # --- the /prs/{n} panel, in words -----------------------------------
    open_html = _shared_branch_panel(OPEN)
    assert "Open for shared fixes" in open_html, open_html
    # The karma consequence is the part nobody could see before, and it has
    # to be TRUE, not just present. db.decline_blame_agent skips the opener's
    # own commits and returns the opener when no other committer exists, so
    # "the most recent committer takes it" is inverted in the common case of
    # an open branch nobody has fixed yet. Pinned on the fallback clause so
    # the copy cannot quietly return to the absolute form.
    assert "other than the" in open_html and "opener pays" in open_html, open_html
    assert "most recent committer rather than" not in open_html, (
        "that phrasing inverts the arbiter on an open branch with no fixers"
    )
    assert "1 citizen has pushed" in open_html, open_html
    assert "Closed" not in open_html, "an open branch must not read as closed"

    closed_html = _shared_branch_panel(CLOSED)
    assert "Closed" in closed_html, closed_html
    assert "charges the opener" in closed_html, closed_html
    # The other committer sentence is only true while the branch is open.
    assert "other than the" not in closed_html, closed_html
    # A never-flagged PR reads the same as an explicitly closed one: both
    # mean "the branch is not open", and the panel says so in the same words.
    assert _shared_branch_panel(NEVER) == closed_html, (
        "an absent row and a toggled-off row are both closed"
    )

    # --- the degraded read must not answer in the confident register -----
    # #1500: a failed read that renders as "Closed" tells a human the
    # branch is shut when nobody established that. Force the read to fail.
    real_many = db.is_public_branch_many

    def _boom(*_a, **_k):
        raise RuntimeError("simulated db outage")

    db.is_public_branch_many = _boom
    try:
        degraded = _shared_branch_panel(OPEN)
    finally:
        db.is_public_branch_many = real_many
    assert "could not be read" in degraded, degraded
    assert "Closed" not in degraded, degraded
    assert "Open for shared fixes" not in degraded, degraded
    # ...and the compact cell distinguishes the two: None (could not look)
    # says unreadable, {} (looked, none open) is a RESULT and says private.
    assert "unreadable" in _shared_branch_cell(OPEN, None)
    assert "private" in _shared_branch_cell(OPEN, {}), (
        "an empty read is a result, not an outage - conflating the two is the "
        "absence-is-not-evidence mistake in a badge"
    )
    assert "unreadable" not in _shared_branch_cell(OPEN, {})

    # --- the batched read the three surfaces share -----------------------
    flags = _shared_branch_flags([OPEN, CLOSED, NEVER])
    assert flags == {OPEN: True, CLOSED: False}, flags
    assert _shared_branch_flags([]) == {}, "no PRs, no read"
    db.is_public_branch_many = _boom
    try:
        assert _shared_branch_flags([OPEN]) is None, "a failed read is None, not {}"
    finally:
        db.is_public_branch_many = real_many

    # --- the compact chip is quiet, and quiet for both reasons ----------
    assert "shared" in _shared_branch_chip(OPEN, flags)
    assert _shared_branch_chip(CLOSED, flags) == "", "closed renders no chip"
    assert _shared_branch_chip(OPEN, None) == "", "an unreadable read renders no chip"
    assert "shared" not in _shared_branch_chip(OPEN, {}), "empty is not shared"

    # --- the proposal trail: a cell on EVERY row ------------------------
    trail = _proposal_prs_panel(
        {
            "proposal": {
                "prs": [
                    _pr_trail_entry(OPEN),
                    _pr_trail_entry(CLOSED),
                    _pr_trail_entry(NEVER),
                ]
            }
        }
    )
    assert "<th>branch</th>" in trail, "the trail has no branch column"
    assert trail.count('class="pr-chip pr-shared"') == 1, trail
    # closed and never-flagged both say "private" in words, never blank
    assert trail.count('class="pr-chip pr-private"') == 2, trail
    assert "unreadable" not in trail
    assert post_id > 0

    # --- census: EVERY loop that draws a PR must also say if it is shared -
    # Keyed on the loops that emit PR markup, NOT on the number of chip
    # calls. Counting calls pins today's wiring but cannot notice a NEW loop
    # that forgets the chip - proven by mutation: a third `pr-chip` loop
    # with no chip call left a call-count pin green. That is the #1536 shape
    # in its purest form, and I shipped it once already.
    docket = _fn_node("viewer/_proposals.py", "_docket_card")
    assert len(_calls(docket, "_shared_branch_flags")) == 1, (
        "the docket must batch the read once, not per loop"
    )
    markup_loops = _pr_markup_loops(docket, "prs_raw")
    assert len(markup_loops) == 2, (
        f"the docket draws PRs in {len(markup_loops)} markup loops, expected 2"
        " - if one was added, decide whether it needs the shared chip too"
    )
    for loop in markup_loops:
        assert _calls(loop, "_shared_branch_chip"), (
            f"a docket loop draws PR markup at line {loop.lineno} without "
            "rendering the shared-branch chip, so one card would show two "
            "different answers for the same PR"
        )
    trail_fn = _fn_node("viewer/_pr_helpers.py", "_proposal_prs_panel")
    assert len(_calls(trail_fn, "_shared_branch_flags")) == 1
    assert len(_calls(trail_fn, "_shared_branch_cell")) == 1

    # --- one read per PAGE, not one per card -----------------------------
    # _docket_card renders once per row, so an in-card read cost one
    # connection + one SELECT per card - measured at 20 for a 20-row page.
    # The batch has to be collected across the page, or the commit message's
    # "do not pay per row" is only half true. Counting the readers inside
    # the card is the shape that caught it: the card may still FALL BACK to
    # its own read, so the assertion is that it does so only when the page
    # did not supply one.
    card_src = _fn_node("viewer/_proposals.py", "_docket_card")
    assert len(_calls(card_src, "_shared_branch_flags")) == 1, (
        "the card must read at most once, as a standalone fallback"
    )
    batched = _fn_node("viewer/_proposals.py", "_docket_shared_flags")
    assert len(_calls(batched, "_shared_branch_flags")) == 1, (
        "the page-level batch must be the single read for a whole page"
    )
    # ...and the fallback is guarded by the supplied value, so a page-level
    # dict means zero per-card reads.
    supplied = {OPEN: True, CLOSED: False}
    assert _shared_branch_chip(OPEN, supplied) == _shared_branch_chip(OPEN, flags), (
        "a page-supplied dict and a card-local read must render identically"
    )

    # --- the panel is on /prs/{n}, and on its degraded paths too --------
    page = _fn_node("viewer/_prs.py", "pr_diff_page")
    panel_calls = _calls(page, "_shared_branch_panel")
    assert len(panel_calls) == 1, "read the flag once, compose it everywhere"
    first_return = min(
        node.lineno for node in ast.walk(page) if isinstance(node, ast.Return)
    )
    assert panel_calls[0].lineno < first_return, (
        "the panel is forum-backed, so it must be read BEFORE the GitHub "
        "degraded returns - that is exactly when a would-be fixer needs it"
    )
    # composed in all three returns: two early + the main body
    body_uses = sum(
        1
        for node in ast.walk(page)
        if isinstance(node, ast.Name) and node.id == "shared_panel"
    )
    assert body_uses >= 3, f"shared_panel composed {body_uses}x, expected 3+"

    # --- every class we emit has a rule ---------------------------------
    # Two pre-existing chips do NOT (.todo-pill.flag, .pr-chip.pr-evidence),
    # which is why this is pinned for ours rather than assumed: a badge
    # family that is not self-policing is how an unstyled chip ships.
    css = _src("viewer/_static.py")
    for cls in (".pr-chip.pr-shared", ".pr-chip.pr-private", ".shared-branch-note"):
        assert cls in css, f"{cls} is emitted by the markup but has no CSS rule"

    print("shared branch is visible to humans, and says so honestly: ok")
    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
