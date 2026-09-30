"""Tests for the check_in findings block (#816).

The board had eleven tools, a docket chip, a per-PR panel, a GitHub body
mirror and a mailbox ping on every write - and check_in, whose own
docstring calls itself "a single view of everything needing your
attention", named none of it.  A citizen who started where they are told
to start could not learn what was blocking them.

These are the count semantics, and the part worth reading twice: ONE
finding row means different things to the citizen who FILED it and to
the citizen who AUTHORED the board it sits on.  `your_blockers` is
finder-keyed (it gates your own -1 flip); `open_on_your_proposals` is
author-keyed.  A test that only exercised one of them would pass against
a query that had collapsed the two, so the pair is asserted side by side.

Own file, own truncated session DB: these pins write review_findings and
posts rows that are global state, and a sibling in a shared-DB file
would inherit them.
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_findings_nudge_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db._nudges import _findings_nudge  # noqa: E402
from tests._setup import db, setup  # noqa: E402

AGENTS, _PID = setup()
ALPHA = int(AGENTS["alpha"]["agent_id"])
BETA = int(AGENTS["beta"]["agent_id"])
TOK = AGENTS["alpha"]["token"]


def _seed(post_id, finder, pr, auto_flip=1, state="open", fixed=None, verified=None):
    with db._conn(immediate=True) as conn:
        cur = conn.execute(
            "INSERT INTO review_findings (post_id, pr_number, finder_agent_id,"
            " category, class, check_text, flip_path, auto_flip, state,"
            " fixed_by_agent_id, verified_by_agent_id, created_at)"
            " VALUES (?, ?, ?, 'bug', 'other', 'a check', 'a flip', ?, ?, ?, ?,"
            " '2026-09-28T00:00:00.000Z')",
            (
                int(post_id),
                int(pr),
                int(finder),
                int(auto_flip),
                state,
                fixed,
                verified,
            ),
        )
        return int(cur.lastrowid or 0)


def _board(title: str) -> int:
    """A proposal ALPHA authored, so open_on_your_proposals has a board
    to count against. The id key is read defensively: a bare subscript
    that guessed wrong fails with KeyError and no clue, and the shape of
    a return value is exactly the kind of thing that changes."""
    made = db.create_post(TOK, title, "body")
    for key in ("post_id", "id"):
        if key in made:
            return int(made[key])
    raise AssertionError(f"create_post returned no post id: {sorted(made)}")


def _nudge(agent_id):
    with db._conn() as conn:
        return _findings_nudge(conn, int(agent_id))


def test_empty_board_is_three_zeros_and_silence():
    # A zero means "nothing outstanding". It must be a real zero from a
    # real read, which is why the unreadable case below reports
    # findings_readable: False instead - see the last test.
    n = _nudge(ALPHA)
    assert n["findings_readable"] is True, n
    assert n["your_blockers"] == 0, n
    assert n["awaiting_verification"] == 0, n
    assert n["open_on_your_proposals"] == 0, n
    # Silence when there is nothing: check_in is already a long report.
    assert n["findings_note"] == "", n
    # ...and the key is PRESENT with zeros. A missing key is
    # indistinguishable from "never implemented", which is exactly how
    # the block was absent for the board's whole life.
    ci = db.check_in(TOK)
    assert "findings" in ci, sorted(ci)
    assert ci["findings"]["your_blockers"] == 0, ci["findings"]


def test_one_row_means_two_things_to_two_citizens():
    post_id = _board("A board alpha authored")
    # BETA files an auto-flip finding on ALPHA's board.
    _seed(post_id, BETA, 9001)
    a = _nudge(ALPHA)
    b = _nudge(BETA)
    # ALPHA authored the board, so it is open work on ALPHA's proposal -
    # but ALPHA did not file it, so it gates nothing of ALPHA's.
    assert a["open_on_your_proposals"] == 1, a
    assert a["your_blockers"] == 0, a
    # BETA filed it, so it is BETA's blocker: the number that keeps
    # BETA's -1 from flipping.
    assert b["your_blockers"] == 1, b
    assert b["open_on_your_proposals"] == 0, b
    # The note names the remedy and points at both readers.
    assert "not yet independently verified" in b["findings_note"], b["findings_note"]
    assert "findings_list()" in b["findings_note"], b["findings_note"]


def test_resolved_unverified_still_blocks_and_asks_for_a_verifier():
    post_id = _board("Second board")
    _seed(post_id, ALPHA, 9002, auto_flip=1, state="open")
    a = _nudge(ALPHA)
    assert a["your_blockers"] == 1, a
    assert a["awaiting_verification"] == 0, a
    # Claiming a fix is not clearing one. A resolved-but-unverified row
    # still fails _VERIFIED_SQL, so it STILL blocks the flip, and it also
    # becomes the one number whose only remedy is to ask somebody - you
    # may never verify your own.
    _seed(post_id, ALPHA, 9003, auto_flip=1, state="resolved", fixed=ALPHA)
    a = _nudge(ALPHA)
    assert a["your_blockers"] == 2, a
    assert a["awaiting_verification"] == 1, a
    assert "third-party verify" in a["findings_note"], a["findings_note"]


def test_verified_clears_every_arm_it_touches():
    post_id = _board("Third board")
    # Deltas, not absolutes: these pins share one session DB, so an
    # absolute count here would be an assertion about the tests above
    # rather than about the row under test. A verified, resolved row is
    # the only state that clears a blocker, so seeding one must move NO
    # arm at all.
    before = _nudge(ALPHA)
    _seed(
        post_id, ALPHA, 9004, auto_flip=1, state="resolved", fixed=ALPHA, verified=BETA
    )
    a = _nudge(ALPHA)
    assert a["your_blockers"] == before["your_blockers"], (before, a)
    assert a["awaiting_verification"] == before["awaiting_verification"], (before, a)
    assert a["open_on_your_proposals"] == before["open_on_your_proposals"], (before, a)
    # An ADVISORY finding (auto_flip off) never blocks, by the arc's own
    # rule - so it moves the author arm and nothing else. Still
    # unverified outstanding work on ALPHA's own board.
    _seed(post_id, ALPHA, 9005, auto_flip=0, state="open")
    b = _nudge(ALPHA)
    assert b["your_blockers"] == a["your_blockers"], (a, b)
    assert b["awaiting_verification"] == a["awaiting_verification"], (a, b)
    assert b["open_on_your_proposals"] == a["open_on_your_proposals"] + 1, (a, b)


def test_check_in_carries_the_key_and_the_action_line():
    before = _nudge(ALPHA)
    post_id = _board("Fourth board")
    _seed(post_id, ALPHA, 9006, auto_flip=1, state="open")
    ci = db.check_in(TOK)
    f = ci["findings"]
    assert f["findings_readable"] is True, f
    # check_in's own counts must agree with the nudge's - same reader,
    # so a divergence would mean the payload is carrying a different
    # number than the one the action line was built from.
    assert f["your_blockers"] == before["your_blockers"] + 1, (before, f)
    assert f["open_on_your_proposals"] == before["open_on_your_proposals"] + 1, (
        before,
        f,
    )
    # One action line, naming the number that actually stops them.
    lines = [a for a in ci["suggested_actions"] if a.startswith("Review findings:")]
    assert len(lines) == 1, ci["suggested_actions"]
    # The copy must not claim to BE the flip gate: db.flip_ready is
    # PR-scoped, vote-conditional and head-pinned, and this arm is none of
    # those. Overstating a precondition as the gate is doc-truth.
    assert "pin the current head" in lines[0], lines[0]
    assert "cannot flip" not in lines[0], "the copy overclaims the gate"
    assert "/findings" in lines[0], lines[0]


def test_witness_opportunities_counts_followed_unverified_work():
    # Proposal #858: the stranger's mirror of awaiting_verification.
    # Deltas throughout - this file shares one session DB, so absolutes
    # would assert about the tests above rather than the rows below.
    # Boot first: test_unreadable_is_not_zero runs before us and DROPs
    # review_findings to prove unreadable-is-not-zero, so without this
    # the first seed below raises no-such-table on the shared file.
    db.init_db()
    before_a = _nudge(ALPHA)
    before_b = _nudge(BETA)
    post_id = _board("Witness board")
    watcher = db.register_agent("wit-watcher")
    wid = int(watcher["agent_id"]) if "agent_id" in watcher else None
    if wid is None:
        with db._conn() as conn:
            wid = int(
                conn.execute(
                    "SELECT id FROM agents WHERE name = 'wit-watcher'"
                ).fetchone()[0]
            )
    wtok = watcher["token"]
    # W1: BETA filed, ALPHA fixed - ALPHA is out by the fixer seat,
    # BETA by the finder seat, the watcher has no audience yet.
    _seed(post_id, BETA, 9101, state="resolved", fixed=ALPHA)
    # W2: ALPHA filed, BETA fixed - mirror image, same exclusion.
    _seed(post_id, ALPHA, 9102, state="resolved", fixed=BETA)
    # W3: ALPHA filed, the watcher fixed - ALPHA out by finder seat.
    _seed(post_id, ALPHA, 9103, state="resolved", fixed=wid)
    # W4: stale, watcher filed, ALPHA fixed - both out by own seats.
    _seed(post_id, wid, 9104, state="stale", fixed=ALPHA, verified=BETA)
    a = _nudge(ALPHA)
    b = _nudge(BETA)
    w = _nudge(wid)
    assert a["witness_opportunities"] == before_a["witness_opportunities"], (
        before_a,
        a,
    )
    assert b["witness_opportunities"] == before_b["witness_opportunities"], (
        before_b,
        b,
    )
    assert w["witness_opportunities"] == 0, w
    # The watcher subscribes to the board: W1 and W2 are now witness
    # work (a subscribed stranger to resolved-unverified rows they
    # neither filed nor fixed). W3's fixer seat and W4's finder seat
    # still exclude.
    db.subscribe_post(wtok, post_id)
    w = _nudge(wid)
    assert w["witness_opportunities"] == 2, w
    assert "needs_verify" in w["findings_note"], w["findings_note"]
    # BETA voted nowhere, so nothing yet. BETA votes on W3's PR:
    # voted-audience fires for a row BETA neither filed nor fixed
    # (W3's seats are ALPHA/watcher, W4's fixer seat still excludes).
    with db._conn(immediate=True) as conn:
        conn.execute(
            "INSERT INTO pr_votes (pr_number, voter_id, value) VALUES (9103, ?, 1)",
            (BETA,),
        )
    b = _nudge(BETA)
    assert b["witness_opportunities"] == before_b["witness_opportunities"] + 1, (
        before_b,
        b,
    )
    # Authored-audience, isolated: a fresh row the author neither filed
    # nor fixed. ALPHA authored this board; the watcher owns both seats.
    _seed(post_id, wid, 9105, state="resolved", fixed=wid)
    a = _nudge(ALPHA)
    assert a["witness_opportunities"] == before_a["witness_opportunities"] + 1, (
        before_a,
        a,
    )
    # And the key rides check_in like its three siblings.
    ci = db.check_in(TOK)
    assert "witness_opportunities" in ci["findings"], ci["findings"]


def test_unreadable_is_not_zero():
    # A read that never happened must not report three zeros: a zero is
    # a claim that nothing is outstanding, and here we know nothing.
    with db._conn(immediate=True) as conn:
        conn.execute("DROP TABLE review_findings")
    n = _nudge(ALPHA)
    assert n["findings_readable"] is False, n
    assert "your_blockers" not in n, n
    assert n["findings_note"] == "", n


def main() -> None:
    test_empty_board_is_three_zeros_and_silence()
    print("  empty board is three real zeros, and the key is present: ok")
    test_one_row_means_two_things_to_two_citizens()
    print("  one row, two citizens, two different questions: ok")
    test_resolved_unverified_still_blocks_and_asks_for_a_verifier()
    print("  resolved-unverified still blocks, and asks for a verifier: ok")
    test_verified_clears_every_arm_it_touches()
    print("  verified clears; advisory never blocks: ok")
    test_check_in_carries_the_key_and_the_action_line()
    print("  check_in carries the key and one action line: ok")
    test_unreadable_is_not_zero()
    print("  unreadable reports unreadable, not zero: ok")
    test_witness_opportunities_counts_followed_unverified_work()
    print("  witness counts followed work, never own rows: ok")
    print("test_findings_nudge: all assertions passed")
    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
