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


def _calls(fn: ast.FunctionDef | ast.AsyncFunctionDef, callee: str) -> list[ast.Call]:
    out = []
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == callee
        ):
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
    # The karma consequence is the part nobody could see before; it must be
    # stated while the branch is open, not just implied by the toggle.
    assert "most recent committer" in open_html, open_html
    assert "1 citizen has pushed" in open_html, open_html
    assert "Closed" not in open_html, "an open branch must not read as closed"

    closed_html = _shared_branch_panel(CLOSED)
    assert "Closed" in closed_html, closed_html
    assert "charges the opener" in closed_html, closed_html
    # The committer sentence is only true while open.
    assert "most recent committer" not in closed_html, closed_html
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

    # --- census: the docket's TWO PR loops both render the chip ----------
    # This is the check whose absence cost me a correction on #1536, where
    # a docstring and a commit message said "both surfaces" and there were
    # three. Pinned as a count so a third loop cannot appear unwired.
    docket = _fn_node("viewer/_proposals.py", "_docket_card")
    assert len(_calls(docket, "_shared_branch_flags")) == 1, (
        "the docket must batch the read once, not per loop"
    )
    assert len(_calls(docket, "_shared_branch_chip")) == 2, (
        "both docket PR loops (evidence chips and the main trail) must render "
        "the shared chip, or the same card shows two answers for one PR"
    )
    trail_fn = _fn_node("viewer/_pr_helpers.py", "_proposal_prs_panel")
    assert len(_calls(trail_fn, "_shared_branch_flags")) == 1
    assert len(_calls(trail_fn, "_shared_branch_cell")) == 1

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
