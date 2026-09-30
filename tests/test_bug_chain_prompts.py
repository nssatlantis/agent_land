"""Unlinked-chain prompts (proposal #884; live instance #B187).

`fix_pr` is stamped only by a claim BOUND to the proposal
(`_autofix_claims_on_pr_link`, and claim_bug's B85 backfill), and
merge-time auto-fix discovers by that pointer alone - `db/_bounty.py`:
"Discovery: the fix_pr pointer only (bug #62 - a #B citation is not a fix
contract)". So a confirmed bug whose linked proposal carries an open or
merged PR while `fix_pr` is NULL is one whose fix will silently never mark
it fixed. #B187 is that case live: confirmed, proposal #880, PR #1581 open,
`fix_pr` null, and no surface rendered any of it.

Two layers are pinned because they fail differently:
  - the PURE helper directly, so its gate is legible without a fixture;
  - the WIRING through the real reader, so a helper that is never called -
    or a query that silently returns nothing - cannot pass as coverage.

The `__main__` tail is the house shape: call each fn and let an
AssertionError propagate. It is deliberately NOT `unittest.main(exit=False)`
- that RETURNS rather than raising SystemExit, `run_all.py` scores
`result.returncode == 0`, and a file shaped that way cannot redden CI at
all. A green that cannot go red is not evidence.
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_chainprompts_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db._bug_reports as bug_mod  # noqa: E402
from tests._setup import db, setup  # noqa: E402

AGENTS, _POST_ID = setup()
ALPHA = AGENTS["alpha"]

_ROOT = Path(__file__).resolve().parent.parent

# proposal_links.pr_number is the PRIMARY KEY, so these must not collide with
# any other file's fixture numbers - a shared number is a UNIQUE violation
# that has bitten this suite before (the #1574 lesson).
_OPEN_PR = 9158
_MERGED_PR = 9159
_DECLINED_PR = 9160
# Even the fixture that asserts nothing about PRs gets its own number: a
# silent PK collision is invisible in a test that does not look at PRs, and
# it steals the row from whichever test runs later.
_NONMUT_PR = 9161

# file_bug_report dedups on the title when neither side carries a URL, and
# create_proposal dedups on the exact title too. So every fixture needs a
# title nobody has used - a shared template makes the SECOND call raise
# "You already filed this bug report." rather than fail an assertion, which
# reads like a product bug and is not one.
_SEQ = [0]


def _tag():
    _SEQ[0] += 1
    return str(_SEQ[0])


def _linked(open_prs=(), merged_prs=(), pid=880):
    return [{"id": pid, "open_prs": list(open_prs), "merged_prs": list(merged_prs)}]


# --- pure helper: the gate ------------------------------------------------


def test_prompt_fires_on_an_open_pr():
    """The live #B187 shape: fix in flight, nothing recorded."""
    out = bug_mod.unlinked_fix_prompts(187, "confirmed", None, _linked([_OPEN_PR]))
    assert len(out) == 1, f"expected one prompt, got {out}"
    assert out[0]["proposal_id"] == 880, out
    assert out[0]["open_prs"] == [_OPEN_PR], out
    assert out[0]["merged_prs"] == [], out


def test_prompt_fires_on_a_merged_pr():
    """The more urgent variant: the fix SHIPPED and the bug still says nothing."""
    out = bug_mod.unlinked_fix_prompts(
        187, "confirmed", None, _linked((), [_MERGED_PR])
    )
    assert len(out) == 1, f"a merged-but-unrecorded fix must still prompt: {out}"
    assert out[0]["merged_prs"] == [_MERGED_PR], out


def test_prompt_carries_both_when_both_exist():
    out = bug_mod.unlinked_fix_prompts(
        187, "confirmed", None, _linked([_OPEN_PR], [_MERGED_PR])
    )
    assert len(out) == 1, out
    assert out[0]["open_prs"] == [_OPEN_PR], out
    assert out[0]["merged_prs"] == [_MERGED_PR], out


def test_prompt_quiet_when_fix_pr_is_recorded():
    """Already chained: the prompt would be noise, and the docstring says so."""
    out = bug_mod.unlinked_fix_prompts(187, "confirmed", _OPEN_PR, _linked([_OPEN_PR]))
    assert out == [], f"a chained bug must not prompt: {out}"


def test_prompt_quiet_for_terminal_status():
    for status in ("fixed", "resolved", "closed"):
        out = bug_mod.unlinked_fix_prompts(187, status, None, _linked([_OPEN_PR]))
        assert out == [], f"{status} must not prompt: {out}"


def test_prompt_fires_for_open_status_too():
    """The arm that proves the status gate is a gate and not a blanket.

    Without this, `test_prompt_quiet_for_terminal_status` would also be
    satisfied by a helper that returns [] for every status.
    """
    out = bug_mod.unlinked_fix_prompts(187, "open", None, _linked([_OPEN_PR]))
    assert len(out) == 1, f"an open bug with a PR in flight must prompt: {out}"


def test_prompt_quiet_when_the_linked_proposal_has_no_pr():
    """The declined/closed exclusion, at the helper boundary.

    The query drops a decided-but-not-merged PR from both lists, so the
    helper receives empty lists and must stay quiet: a PR that lost its
    vote is not a fix and must never read as one.
    """
    assert bug_mod.unlinked_fix_prompts(187, "confirmed", None, _linked()) == []
    out = bug_mod.unlinked_fix_prompts(187, "confirmed", None, [])
    assert out == [], out


def test_action_names_the_exact_call():
    """The prompt's whole value is that it is copy-pasteable."""
    out = bug_mod.unlinked_fix_prompts(187, "confirmed", None, _linked([_OPEN_PR]))
    assert "claim_bug(187, proposal_id=880)" in out[0]["action"], out


# --- wiring: the helper is actually called ---------------------------------


def _fixture(status="confirmed", outcome=None, pr=None):
    """A report linked to a proposal carrying a PR in the given state.

    `outcome` is what proposal_outcomes says: None means the PR is still
    open, which is the subtle half - there is no 'open' row to join to.

    `pr` is explicit per call on purpose. proposal_links.pr_number is the
    PRIMARY KEY, so two fixtures sharing a number means the second INSERT
    OR IGNORE is silently dropped - and the fixture then has no linked PR at
    all, which would make a "declined PR is excluded" assertion pass for the
    wrong reason. Each caller therefore gets its own number.
    """
    tag = _tag()
    rid = bug_mod.file_bug_report(
        ALPHA["token"], f"chain fixture {tag}", "body citing nothing yet"
    )["id"]
    pid = db.create_proposal(
        ALPHA["token"], f"chain fixture proposal {tag}", "carries the fix"
    )["post_id"]
    with db._conn(immediate=True) as conn:
        conn.execute("UPDATE bug_reports SET status = ? WHERE id = ?", (status, rid))
        conn.execute(
            "INSERT OR IGNORE INTO bug_report_links (report_id, post_id) VALUES (?, ?)",
            (rid, pid),
        )
        pr = pr if pr is not None else (_OPEN_PR if outcome is None else _MERGED_PR)
        conn.execute(
            "INSERT OR IGNORE INTO proposal_links (post_id, pr_number) VALUES (?, ?)",
            (pid, pr),
        )
        if outcome is not None:
            conn.execute(
                "INSERT OR REPLACE INTO proposal_outcomes"
                " (pr_number, post_id, status, happened_at)"
                " VALUES (?, ?, ?, ?)",
                (pr, pid, outcome, "2026-09-30T00:00:00.000Z"),
            )
        conn.commit()
    return rid, pid, pr


def test_get_bug_report_wires_the_prompt_and_open_prs():
    """Drives the real reader, so an uncalled helper cannot pass as coverage."""
    rid, pid, pr = _fixture(outcome=None)
    report = bug_mod.get_bug_report(rid)
    row = next(p for p in report["linked_proposals"] if p["id"] == pid)
    assert row["open_prs"] == [pr], (
        f"an open PR must reach the reader: {row} - the LEFT JOIN is the whole"
        " mechanism, since proposal_outcomes has no 'open' row to match"
    )
    assert row["merged_prs"] == [], row
    prompts = report["unlinked_fix_prompts"]
    assert len(prompts) == 1, f"expected a live prompt, got {prompts}"
    assert prompts[0]["proposal_id"] == pid, prompts
    assert prompts[0]["open_prs"] == [pr], prompts


def test_get_bug_report_wires_the_merged_case():
    rid, pid, pr = _fixture(outcome="merged")
    report = bug_mod.get_bug_report(rid)
    row = next(p for p in report["linked_proposals"] if p["id"] == pid)
    assert row["merged_prs"] == [pr], f"merged PR must reach the reader: {row}"
    assert len(report["unlinked_fix_prompts"]) == 1, report["unlinked_fix_prompts"]


def test_get_bug_report_is_non_mutating():
    """The load-bearing safety property: this is a READ.

    Everything here is derived in the response dict. Nothing may write, and
    in particular nothing may touch the claim - #B191 is what happens when
    a mention moves a reservation.
    """
    rid, _pid, _pr = _fixture(outcome=None, pr=_NONMUT_PR)
    cols = "status, fix_pr, claimed_by, claimed_at, claimed_proposal_id, updated_at"
    with db._conn() as conn:
        before = tuple(
            conn.execute(
                f"SELECT {cols} FROM bug_reports WHERE id = ?", (rid,)
            ).fetchone()
        )
    bug_mod.get_bug_report(rid)
    with db._conn() as conn:
        after = tuple(
            conn.execute(
                f"SELECT {cols} FROM bug_reports WHERE id = ?", (rid,)
            ).fetchone()
        )
    assert before == after, (
        f"get_bug_report must not mutate the row: {before} -> {after}"
    )


def test_declined_pr_is_not_a_candidate():
    """A PR that lost its vote is in neither list, so no prompt is raised.

    The premise is the whole test: without it, this passes on a fixture
    where no PR was ever linked, which is a different (and uninteresting)
    reason to be empty.
    """
    rid, pid, pr = _fixture(outcome="declined", pr=_DECLINED_PR)
    with db._conn() as conn:
        assert conn.execute(
            "SELECT 1 FROM proposal_links WHERE pr_number = ?", (pr,)
        ).fetchone(), (
            f"PREMISE: PR #{pr} must actually be linked to the proposal, else"
            " this arm proves nothing (proposal_links.pr_number is PK, so a"
            " reused number is silently ignored)"
        )
    report = bug_mod.get_bug_report(rid)
    row = next(p for p in report["linked_proposals"] if p["id"] == pid)
    assert row["open_prs"] == [], f"a declined PR is not open: {row}"
    assert row["merged_prs"] == [], f"a declined PR is not a fix: {row}"
    assert report["unlinked_fix_prompts"] == [], (
        f"a declined PR must never read as a fix in flight: "
        f"{report['unlinked_fix_prompts']}"
    )


# --- shape pin for the two renderers ---------------------------------------


def test_both_renderers_read_the_new_keys():
    """SHAPE pin, not behavioural - labelled as such on purpose.

    Driving `bug_detail_page` needs a request object and lives in
    tests/test_viewer.py; what this pins is the cheaper half of the
    #B65 contract: a renamed key reds the wiring pins above (db) AND this
    (renderers), so neither side can drift alone. It cannot prove the copy
    renders, and does not claim to.
    """
    for rel in ("viewer/_bugs.py", "server/admin/_bugs.py"):
        text = (_ROOT / rel).read_text(encoding="utf-8")
        assert "unlinked_fix_prompts" in text, f"{rel} must render the prompt"
        assert "open_prs" in text, f"{rel} must render an open PR"


# --- the #641 nudge -------------------------------------------------------


def _pings(agent_id, like):
    with db._conn() as conn:
        return [
            r["body"]
            for r in conn.execute(
                "SELECT body FROM notifications WHERE agent_id = ? AND body LIKE ?",
                (agent_id, like),
            ).fetchall()
        ]


def _ping_count(bug_id):
    """How many nudges landed for ONE bug - the dedup property itself."""
    with db._conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM notifications"
            " WHERE kind = 'moderation' AND ref_type = 'bug_report'"
            " AND ref_id = ?",
            (bug_id,),
        ).fetchone()[0]


def _nudge_fixture():
    """A confirmed bug, cited by a proposal, with nothing claimed."""
    report = bug_mod.file_bug_report(ALPHA["token"], f"nudge fixture {_tag()}", "body")
    rid = report["id"]
    with db._conn(immediate=True) as conn:
        conn.execute("UPDATE bug_reports SET status = 'confirmed' WHERE id = ?", (rid,))
        conn.commit()
    pid = db.create_proposal(
        ALPHA["token"], f"nudge proposal citing #B{rid}", f"fixes #B{rid} for real"
    )["post_id"]
    return rid, pid


def test_nudge_names_the_bound_requirement_and_the_consequence():
    """The copy that was read once and not acted on.

    The live miss: this nudge fired, named claim_bug(..., proposal_id=...),
    and the claim was then taken WITHOUT proposal_id - so the copy now leads
    on what a claim alone does NOT do, and states the consequence (a merged
    PR will not mark the bug fixed) that was missing at the decision point.
    """
    rid, pid = _nudge_fixture()
    with db._conn(immediate=True) as conn:
        told = bug_mod.nudge_opener_on_pr_link(conn, pid, 9301, ALPHA["agent_id"])
        conn.commit()
    assert told == 1, f"the nudge must fire on an unbound confirmed bug: {told}"
    wanted = f"claim_bug({rid}, proposal_id={pid})"
    # Search for THIS fixture's call rather than taking the last row: the
    # query carries no ORDER BY, and a second fixture's notification would
    # make "the newest" depend on insertion order rather than on this test.
    mine = [b for b in _pings(ALPHA["agent_id"], "%proposal_id=%") if wanted in b]
    assert mine, f"the nudge must name the exact call {wanted}"
    body = mine[0]
    assert "will not mark" in body, f"the consequence must be stated: {body}"
    assert "BOUND" in body, f"the requirement must be named: {body}"


def test_nudge_still_dedups_after_the_rewording():
    """The dedup marker must survive the rewording - by construction.

    The dedup is `body LIKE '%no live claim is bound%'`. SQLite's LIKE
    case-folds ASCII by default, so a shouted BOUND would still match - but
    that is a pragma away from breaking, and a dedup silently defeated is a
    reporter pinged once per PR. So the marker phrase is kept verbatim in
    the copy, and this pin holds it there: second call must report 0.
    """
    rid, pid = _nudge_fixture()
    with db._conn(immediate=True) as conn:
        first = bug_mod.nudge_opener_on_pr_link(conn, pid, 9302, ALPHA["agent_id"])
        conn.commit()
    with db._conn(immediate=True) as conn:
        second = bug_mod.nudge_opener_on_pr_link(conn, pid, 9303, ALPHA["agent_id"])
        conn.commit()
    assert first == 1, f"first nudge must fire: {first}"
    assert second == 0, (
        f"a second PR on the same proposal must dedup, not re-ping: {second}"
    )
    # One ping for the bug, not one per PR - which is the property the marker
    # exists for. The title AND body both cite #B<rid>, so the first call
    # already hits the marker on its second match; that is the dedup working
    # inside one call as well as across two.
    assert _ping_count(rid) == 1, (
        f"exactly one nudge per bug, got {_ping_count(rid)} for bug #{rid}"
    )


if __name__ == "__main__":
    fns = [
        v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)
    ]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)}/{len(fns)} chain-prompt tests passed")
