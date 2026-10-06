"""Auto-merge must not fire on a PR carrying unresolved findings (#915).

The gate under test is `db.unresolved_findings_for_pr` and the poller
condition that reads it.  A pin on the READER alone would not be
evidence: `server/_merge_gate.py` in this same subsystem is a predicate
whose docstring claims "the poller calls it as defense-in-depth" and
`merge_eligible` has zero production callers outside its own test.  So
the load-bearing arms here DRIVE `_pr_vote_sweep` and assert on whether
`github.merge_pr` was called.

State census, not spot check: every member of `FINDING_STATES` is seeded
and given a declared verdict, and the expectation map's key set is
asserted EQUAL to `FINDING_STATES`, so a fifth value added to the schema
CHECK reds this file rather than being silently exempt.

Own session DB: this file registers agents, creates proposals and drives
the vote sweep, so it must not share a database with its siblings.
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_findings_gate_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402, I001
import github  # noqa: E402, I001
import events  # noqa: E402, I001
from db._review_findings import FINDING_STATES  # noqa: E402, I001
from server.poller import _pr_vote_sweep  # noqa: E402, I001

AGENTS, _ = setup()

_SHA = "a" * 40
_OTHER_SHA = "b" * 40
_counter = [0]


# -- fixtures ------------------------------------------------------------


def _linked_pr():
    """A small-fix proposal with a linked PR and a recorded opener."""
    _counter[0] += 1
    pid = db.create_proposal(
        AGENTS["alpha"]["token"], f"Gate test {_counter[0]}", "Body.", small_fix=True
    )["post_id"]
    pr_number = 8100 + pid
    db.link_pr_to_proposal(pr_number, pid, AGENTS["alpha"]["agent_id"])
    return pid, pr_number


def _at_bar(pr_number):
    for name in ("beta", "gamma", "delta"):
        db.vote_on_pr(AGENTS[name]["token"], pr_number, 1)


def _file_finding(pid, pr_number, finder="beta", **kw):
    """Seed through the REAL writer, so no row here is a shape no writer
    produces.  `auto_flip` defaults to the tool's own default (False) when
    not passed, which is the population this gate exists to catch."""
    args = {
        "category": "bug",
        "finding_class": "wire-shape",
        "check_text": "db/_x.py:1 reads field y, server sends z",
        "flip_path": "rename z to y",
        "paths": ["db/_x.py"],
    }
    args.update(kw)
    with db._conn() as conn:
        return db.finding_add(
            conn,
            pid,
            pr_number,
            AGENTS[finder]["agent_id"],
            args["category"],
            args["finding_class"],
            args["check_text"],
            args["flip_path"],
            args["paths"],
            args.get("auto_flip", False),
        )


def _unresolved(pr_number):
    with db._conn() as conn:
        return db.unresolved_findings_for_pr(conn, pr_number)


def _pr_dict(number):
    # head_sha is _SHA on purpose, and a test that clears a finding MUST
    # attest at that same sha.  The sweep stales a verification whose
    # verified_head_sha does not match the PR's live head, and it does so
    # in a sweep-wide reconcile that runs BEFORE the gate reads - so a
    # fixture that attested at some other sha would see its row flipped to
    # 'stale' first and the gate would then hold it, which is correct
    # production behaviour and a false red here.  Aligning the fixture
    # keeps the assertion honest instead of weakening it.
    return {
        "number": number,
        "title": "test",
        "head": "branch",
        "base": "main",
        "author": "nobody",
        "created_at": "",
        "html_url": "",
        "mergeable_state": "clean",
        "body": "",
        "head_sha": _SHA,
        "citizen": {"name": "alpha", "agent_id": AGENTS["alpha"]["agent_id"]},
    }


class _CallLog:
    def __init__(self, prs):
        self.calls = []
        self._prs = list(prs)

    def merge(self, number, **kw):
        self.calls.append(("merge", number))
        return {"pr_number": number, "merged": True, "sha": ""}

    def decline(self, number, **kw):
        self.calls.append(("decline", number))
        return {"pr_number": number}

    def rebase(self, number, **kw):
        self.calls.append(("rebase", number))
        return {"status": "ok", "new_sha": "rebased_sha"}

    def wait_ci(self, number, **kw):
        self.calls.append(("wait_ci", number))
        return "success"

    def open_prs(self):
        return [dict(p) for p in self._prs]

    def has_label(self, number, label, **kw):
        return False

    def checks(self, number, *, _head_sha=None):
        return {"state": "success"}


def _sweep(pr_number, reader=None):
    """Run one merge-eligible sweep over a single PR, with GitHub stubbed.
    `reader` optionally replaces db.unresolved_findings_by_pr so the
    fail-closed arm can make the board unreadable."""
    log = _CallLog([_pr_dict(pr_number)])
    saved = {
        "open_prs": github.open_prs,
        "pr_has_label": github.pr_has_label,
        "pr_checks": github.pr_checks,
        "merge_pr": github.merge_pr,
        "decline_pr": github.decline_pr,
        "rebase_pr_onto_main": github.rebase_pr_onto_main,
        "wait_for_ci": github.wait_for_ci,
        "unresolved": db.unresolved_findings_by_pr,
    }
    try:
        github.open_prs = log.open_prs
        github.pr_has_label = log.has_label
        github.pr_checks = log.checks
        github.merge_pr = log.merge
        github.decline_pr = log.decline
        github.rebase_pr_onto_main = log.rebase
        github.wait_for_ci = log.wait_ci
        if reader is not None:
            db.unresolved_findings_by_pr = reader
        # Deliberately NOT wrapped: a sweep that raises is a red pin, and
        # suppressing it here would convert a real failure into a
        # confusing unbound-local instead of the traceback that explains
        # it.  The sweep's own per-candidate handlers absorb what they
        # are meant to absorb.
        actions = _pr_vote_sweep()
        return log, actions
    finally:
        github.open_prs = saved["open_prs"]
        github.pr_has_label = saved["pr_has_label"]
        github.pr_checks = saved["pr_checks"]
        github.merge_pr = saved["merge_pr"]
        github.decline_pr = saved["decline_pr"]
        github.rebase_pr_onto_main = saved["rebase_pr_onto_main"]
        github.wait_for_ci = saved["wait_for_ci"]
        db.unresolved_findings_by_pr = saved["unresolved"]


# -- the gate ------------------------------------------------------------


def test_no_findings_still_merges():
    """Control: the gate must not touch a PR that carries no findings."""
    pid, pr_number = _linked_pr()
    _at_bar(pr_number)
    log, _ = _sweep(pr_number)
    assert _unresolved(pr_number) == [], _unresolved(pr_number)
    assert ("merge", pr_number) in log.calls, log.calls
    evts = events.query_events(kind="pr_auto_merged", target_id=pr_number)
    assert len(evts) == 1, "EVT_PR_AUTO_MERGED not logged on a clean PR"
    print("  clean PR still merges: ok")


def test_advisory_bug_blocks_the_merge():
    """The load-bearing arm.  auto_flip DEFAULTS TO 0, so this is the
    board's ordinary filing shape - and before #915 it could not affect a
    merge at all.  If this arm passes on unpatched bytes the change does
    nothing."""
    pid, pr_number = _linked_pr()
    _at_bar(pr_number)
    fid = _file_finding(pid, pr_number, category="bug")
    with db._conn() as conn:
        row = conn.execute(
            "SELECT auto_flip, state FROM review_findings WHERE id = ?", (fid,)
        ).fetchone()
    assert row["auto_flip"] == 0, "fixture drifted: this arm is about the DEFAULT"
    log, _ = _sweep(pr_number)
    assert _unresolved(pr_number), "an open bug must be outstanding"
    assert ("merge", pr_number) not in log.calls, (
        f"auto-merged with a finding: {log.calls}"
    )
    assert ("rebase", pr_number) not in log.calls, "must not even rebase"
    print("  advisory (auto_flip=0) bug blocks the merge: ok")


def test_consented_bug_blocks_the_merge():
    pid, pr_number = _linked_pr()
    _at_bar(pr_number)
    _file_finding(pid, pr_number, category="bug", auto_flip=True)
    log, _ = _sweep(pr_number)
    assert ("merge", pr_number) not in log.calls, log.calls
    print("  consented (auto_flip=1) bug blocks the merge: ok")


def test_improvement_blocks_the_merge():
    """Operator decision: category is not an input to the gate."""
    pid, pr_number = _linked_pr()
    _at_bar(pr_number)
    _file_finding(pid, pr_number, category="improvement")
    log, _ = _sweep(pr_number)
    assert ("merge", pr_number) not in log.calls, log.calls
    print("  improvement blocks the merge: ok")


def test_resolved_blocks_until_a_third_party_verifies():
    """Cleared means resolved AND independently verified.  The positive
    control sits directly beneath the clearing arm on purpose: a bare
    `assert verified_by_agent_id is None` is equally satisfied by a tree
    where the verification UPDATE never ran, so the unverified half is
    asserted FIRST and in the same test."""
    pid, pr_number = _linked_pr()
    _at_bar(pr_number)
    fid = _file_finding(pid, pr_number, category="bug")
    alpha, gamma = AGENTS["alpha"]["agent_id"], AGENTS["gamma"]["agent_id"]

    with db._conn() as conn:
        db.finding_mark_resolved(conn, fid, alpha, "fixed")
    # The fixer's own resolution is NOT clearance.
    assert _unresolved(pr_number), "a resolved-but-unverified finding must still block"
    log, _ = _sweep(pr_number)
    assert ("merge", pr_number) not in log.calls, log.calls

    with db._conn() as conn:
        db.finding_verify(conn, fid, gamma, _SHA, "checked at the head")
    assert _unresolved(pr_number) == [], _unresolved(pr_number)
    log, _ = _sweep(pr_number)
    assert ("merge", pr_number) in log.calls, log.calls
    print("  resolved blocks, verified clears: ok")


def test_state_census_covers_the_whole_closed_vocabulary():
    """Every state FINDING_STATES can hold, with a declared verdict - and
    the expectation map is asserted EQUAL to the vocabulary, so a fifth
    value added to the schema CHECK reds here instead of being silently
    exempt."""
    pid, pr_number = _linked_pr()
    ids = {}
    for state in sorted(FINDING_STATES):
        ids[state] = _file_finding(pid, pr_number, category="bug")
    # Drive four of the five through real writers; `disputed` has no
    # on-demand writer in a test, so it is set directly and named.
    with db._conn() as conn:
        db.finding_mark_resolved(
            conn, ids["resolved"], AGENTS["alpha"]["agent_id"], "f"
        )
        # Verified AT THE PUSH HEAD, so the push below leaves it resolved.
        # This is the head-pinning interaction the gate's own docstring
        # names, and the census has to respect it: the staler only
        # touches VERIFIED rows and flips one whose verified_head_sha
        # differs from the new head, so a row attested anywhere else
        # would silently leave the census's expected verdict for
        # 'resolved' as a false red rather than a real finding.
        db.finding_verify(
            conn, ids["resolved"], AGENTS["gamma"]["agent_id"], _OTHER_SHA, "checked"
        )
        db.finding_mark_resolved(conn, ids["stale"], AGENTS["alpha"]["agent_id"], "f")
        # Verified at the OLD head, so the push stales it - which is how
        # 'stale' is produced by a real writer rather than hand-set.
        db.finding_verify(
            conn, ids["stale"], AGENTS["gamma"]["agent_id"], _SHA, "checked"
        )
        db.finding_stale_on_push(conn, pr_number, _OTHER_SHA)
        db.finding_withdraw(conn, ids["withdrawn"], AGENTS["beta"]["agent_id"])
        conn.execute(
            "UPDATE review_findings SET state = 'disputed' WHERE id = ?",
            (ids["disputed"],),
        )
    expected = {
        "open": True,
        "resolved": False,
        "disputed": True,
        "stale": True,
        "withdrawn": False,
    }
    assert set(expected) == set(FINDING_STATES), (
        f"census is not exhaustive: {set(expected) ^ set(FINDING_STATES)}"
    )
    with db._conn() as conn:
        live = {
            r["state"]: r["id"]
            for r in conn.execute(
                "SELECT state, id FROM review_findings WHERE pr_number = ?",
                (pr_number,),
            )
        }
    assert set(live) == set(FINDING_STATES), live
    held = {f["id"] for f in _unresolved(pr_number)}
    for state, blocks in expected.items():
        assert (ids[state] in held) is blocks, (
            f"state {state!r}: expected blocks={blocks}, id={ids[state]} in={held}"
        )
    print("  state census covers FINDING_STATES: ok")


def test_a_failed_board_read_blocks_every_candidate():
    """Fail-closed.  A board we cannot read is not a board we may treat as
    clear, and the safe direction for a gate is to refuse."""
    pid, pr_number = _linked_pr()
    _at_bar(pr_number)

    def _boom(*a, **kw):
        raise RuntimeError("board unreadable")

    log, _ = _sweep(pr_number, reader=_boom)
    assert ("merge", pr_number) not in log.calls, (
        f"unreadable board unlocked a merge: {log.calls}"
    )
    print("  failed board read is fail-closed: ok")


def test_a_sibling_prs_findings_do_not_contaminate():
    """The per-PR scoping the README claims: PR A's open finding must not
    appear in PR B's read, and must not hold B."""
    pid_a, pr_a = _linked_pr()
    pid_b, pr_b = _linked_pr()
    _at_bar(pr_a)
    _at_bar(pr_b)
    _file_finding(pid_a, pr_a, category="bug")
    assert _unresolved(pr_a), "the filed finding must be outstanding on its own PR"
    assert _unresolved(pr_b) == [], "a sibling PR's finding leaked across"
    log, _ = _sweep(pr_b)
    assert ("merge", pr_b) in log.calls, log.calls
    print("  sibling PR findings stay scoped: ok")


def main():
    test_no_findings_still_merges()
    test_advisory_bug_blocks_the_merge()
    test_consented_bug_blocks_the_merge()
    test_improvement_blocks_the_merge()
    test_resolved_blocks_until_a_third_party_verifies()
    test_state_census_covers_the_whole_closed_vocabulary()
    test_a_failed_board_read_blocks_every_candidate()
    test_a_sibling_prs_findings_do_not_contaminate()
    print("All findings merge-gate tests passed.")


if __name__ == "__main__":
    main()
