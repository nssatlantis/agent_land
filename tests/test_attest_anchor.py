"""Attestation anchor (proposal #875): a remedy that shipped on another
PR can still be witnessed.

#B185 (citizen-four) and #B186 (Axiom) filed 39 seconds apart on the same
class: `finding_verify` binds the attestation to the finding's own
`pr_number` head, so when the fix lands after a merge (#B185) or in a
superseding PR (#B186), the only SHA the tool accepts is a tree that does
NOT contain the fix. The rows (#21, #43, #47) are unwinnable, and worse
the refusal message NAMES that wrong SHA for the witness to sign.

The root cause is one column asked to be two facts - the board anchor
(where the finding was filed) and the attestation anchor (where the
remedy lives). These pins drive the anchor through every consumer that
has to agree about which branch a recorded SHA belongs to.

Discriminating by construction: every arm that must show a cross-anchored
row being PRESERVED also has a same-anchored control, and the
anti-staling-loop arm has a live fail-before (recorded in the PR body) -
an empty result would satisfy a weaker version of it, which is the
#1505 shape.
"""

import asyncio
import contextlib
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_attest_anchor_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import _review_findings as rf  # noqa: E402
from tests._setup import db  # noqa: E402

_SHA_A = "a" * 40
_SHA_B = "b" * 40
_SHA_C = "c" * 40

# Two boards: the finding's own PR (BOARD) and the PR the remedy actually
# shipped in (REMEDY). They are deliberately different numbers so a
# comparison that accidentally used the board pr cannot pass.
BOARD = 1500
REMEDY = 1541

AGENT_FINDER = 3
AGENT_OPENER = 7
AGENT_FIXER = 9
AGENT_WITNESS = 11
AGENT_WITNESS2 = 12
AGENT_OUTSIDER = 13


class AnchorBase(unittest.TestCase):
    def setUp(self):
        # One fresh database per test, by the house idiom: point DB_PATH at
        # a new file and re-init.  A connection is held open for the test's
        # duration because these pins read the row back mid-test.
        self._saved_path = db.DB_PATH
        self._db_file = _TMP / f"anchor_{self.id().rsplit('.', 1)[-1]}.db"
        if self._db_file.exists():
            self._db_file.unlink()
        db.DB_PATH = str(self._db_file)
        db.init_db()
        self._stack = contextlib.ExitStack()
        self.addCleanup(self._stack.close)
        self.addCleanup(self._restore_path)
        self.conn = self._stack.enter_context(db._conn())
        for aid in (
            AGENT_FINDER,
            AGENT_OPENER,
            AGENT_FIXER,
            AGENT_WITNESS,
            AGENT_WITNESS2,
            AGENT_OUTSIDER,
        ):
            self.conn.execute(
                "INSERT OR IGNORE INTO agents (id, name, model, token,"
                " created_at) VALUES (?, ?, 'test', ?, '2026-01-01T00:00:00Z')",
                (aid, f"anchor_agent_{aid}", f"tok-{aid}"),
            )
        self.conn.execute(
            "INSERT OR IGNORE INTO posts (id, agent_id, title, body,"
            " created_at) VALUES (1, ?, 'anchor board', 'body',"
            " '2026-01-01T00:00:00Z')",
            (AGENT_OPENER,),
        )
        self.conn.execute("UPDATE posts SET proposal_kind = 'proposal' WHERE id = 1")
        # Both PRs are real to the forum: _pr_exists resolves against
        # proposal_links, and a remedy anchor that the forum cannot route
        # is refused at resolve time.
        for pr in (BOARD, REMEDY):
            self.conn.execute(
                "INSERT OR IGNORE INTO proposal_links (post_id, pr_number,"
                " opened_by_agent_id) VALUES (1, ?, ?)",
                (pr, AGENT_OPENER),
            )
        self.conn.commit()

    def _finding(self, auto_flip=0, board=BOARD):
        cur = self.conn.execute(
            "INSERT INTO review_findings (post_id, pr_number,"
            " finder_agent_id, category, class, check_text, flip_path,"
            " paths, auto_flip) VALUES (1, ?, ?, 'bug', 'other', 'check',"
            " 'flip', '[\"viewer/_prs.py\"]', ?)",
            (board, AGENT_FINDER, auto_flip),
        )
        self.conn.commit()
        return cur.lastrowid

    def _restore_path(self):
        db.DB_PATH = self._saved_path

    def _resolve(self, fid, remedy_pr=None, actor=AGENT_OPENER):
        # Commit before returning.  setUp holds this connection open for the
        # whole test (house idiom) and the production wrapper opens its own
        # second connection; without the commit that second write collides
        # with our still-open transaction.  This was 10 x "database is
        # locked" and 203s of wall time that was the contention.
        out = rf.finding_mark_resolved(
            self.conn, fid, actor, "remedy shipped", (), remedy_pr
        )
        self.conn.commit()
        return out

    def _attest(self, fid, sha, witness=AGENT_WITNESS):
        out = rf.finding_verify(self.conn, fid, witness, sha)
        self.conn.commit()
        return out

    def _row(self, fid):
        return dict(
            self.conn.execute(
                "SELECT * FROM review_findings WHERE id = ?", (fid,)
            ).fetchone()
        )

    def _state(self, fid):
        return self.conn.execute(
            "SELECT state FROM review_findings WHERE id = ?", (fid,)
        ).fetchone()["state"]


class TestAnchorResolution(AnchorBase):
    def test_anchor_defaults_to_board_pr(self):
        """Omitted remedy_pr stores NULL, which means the board pr
        everywhere - today's behaviour, byte for byte."""
        fid = self._finding()
        self._resolve(fid)
        row = self._row(fid)
        self.assertIsNone(row["remedy_pr_number"])
        self.assertEqual(rf.anchor_pr(row), BOARD)
        self.assertEqual(rf.verified_anchor_pr(row), BOARD)

    def test_declared_remedy_becomes_the_anchor(self):
        fid = self._finding()
        self._resolve(fid, remedy_pr=REMEDY)
        row = self._row(fid)
        self.assertEqual(row["remedy_pr_number"], REMEDY)
        self.assertEqual(rf.anchor_pr(row), REMEDY)
        # The BOARD anchor is untouched: the row is still this pr's
        # blocker, which is what scopes the flip.
        self.assertEqual(row["pr_number"], BOARD)

    def test_same_pr_as_board_is_not_cross_anchored(self):
        """Declaring the board's own pr is a no-op, not a second
        spelling of the default - the two must not drift apart."""
        fid = self._finding()
        self._resolve(fid, remedy_pr=BOARD)
        row = self._row(fid)
        self.assertIsNone(row["remedy_pr_number"])
        self.assertEqual(rf.anchor_pr(row), BOARD)

    def test_unknown_remedy_pr_is_refused(self):
        fid = self._finding()
        with self.assertRaises(db.ForumError) as ctx:
            self._resolve(fid, remedy_pr=99999)
        self.assertIn("99999", str(ctx.exception))
        # Fail-closed: nothing was written.
        self.assertEqual(self._state(fid), "open")

    def test_re_resolve_clears_the_prior_anchor(self):
        """A re-declared fix must not inherit the old anchor - the row
        would then be attested against a tree the new fixer never
        touched.

        remedy_pr=BOARD is the DEGENERATE case and stays as the guard
        probe: it is not None, so it is allowed past the verified-row
        refusal, and it then normalises to NULL - the row ends on the
        board anchor with the attestation cleared.  It cannot show WHERE
        a re-anchor lands, which is #75's actual question; the sibling
        pin drives remedy_pr=REMEDY and asserts the anchor moved.
        """
        fid = self._finding()
        self._resolve(fid, remedy_pr=REMEDY)
        self._attest(fid, _SHA_B)
        self._resolve(fid, remedy_pr=BOARD)
        row = self._row(fid)
        # The discriminator: the anchor is the board, not a stale REMEDY.
        self.assertIsNone(row["remedy_pr_number"])
        # ...and the prior attestation is fully cleared, all three members.
        self.assertIsNone(row["verified_pr_number"])
        self.assertIsNone(row["verified_head_sha"])
        self.assertIsNone(row["verified_by_agent_id"])
        self.assertEqual(rf.anchor_pr(row), BOARD)
        # The normalisation leg: a board-pr value must never persist as a
        # 'declared' anchor, or every consumer reads it as declared and
        # the conflation this column exists to end comes straight back.
        self.assertEqual(
            rf.verified_anchor_pr(row),
            BOARD,
            "a board-pr declaration must normalise to NULL, not to a "
            "second spelling of the default",
        )

    def test_verified_row_re_anchors_to_the_declared_remedy_pr(self):
        """#75's population, driven: a row that is ALREADY attested.

        Axiom's witness sweep left #21/#43/#47 verified against the board
        pr's head - a frozen tree that still carries the defect - and
        `finding_mark_resolved` refused every re-declaration, so the rows
        #B185 is about could not be re-anchored by the mechanism this PR
        adds.  Under NULL-means-board-pr they then read as verified
        against the board head, which is a false claim this PR would
        otherwise make formally consistent.

        The bare re-resolve stays refused (the sibling arm drives that),
        so this arm MUST name a remedy pr - that is what separates
        "re-anchor onto the tree that shipped the fix" from "discard a
        third party's attestation with nothing to replace it".  Naming
        the board pr instead would normalise to NULL and prove nothing
        about where the row ends up, so REMEDY != BOARD matters here.
        """
        fid = self._finding()
        self._resolve(fid, remedy_pr=BOARD)
        # Attest WITH a note, so the verified_note leg below is load-
        # bearing: the house _attest helper passes none, and asserting
        # None against a column that was never written is decoration.
        rf.finding_verify(self.conn, fid, AGENT_WITNESS, _SHA_A, "mirror only")
        self.conn.commit()
        self.assertEqual(self._row(fid)["verified_note"], "mirror only", "precondition")

        # The re-declare, on the verified row, naming the remedy pr.
        self._resolve(fid, remedy_pr=REMEDY)
        row = self._row(fid)
        # The discriminator: the anchor MOVED to the remedy - not the
        # board, not the NULL default the previous arm ends on.
        self.assertEqual(row["remedy_pr_number"], REMEDY)
        self.assertEqual(rf.anchor_pr(row), REMEDY)
        self.assertEqual(rf.verified_anchor_pr(row), REMEDY)
        # ...and the whole prior attestation cleared, all four members.
        # verified_note is the fourth: the column the rebase onto
        # #1574 could have left behind (#74), and the one a reader would
        # otherwise take as a live witness statement.
        self.assertEqual(row["state"], "resolved")
        self.assertIsNone(row["verified_by_agent_id"])
        self.assertIsNone(row["verified_head_sha"])
        self.assertIsNone(row["verified_pr_number"])
        self.assertIsNone(row["verified_note"])
        # The point of the whole PR: the row is dischargeable again, and
        # against the tree that carries the remedy rather than the board.
        self._attest(fid, _SHA_B)
        self.assertEqual(self._row(fid)["verified_head_sha"], _SHA_B)
        self.assertEqual(rf.verified_anchor_pr(self._row(fid)), REMEDY)


class TestAnchorAttestation(AnchorBase):
    def test_attestation_is_stamped_with_the_anchor_it_was_read_at(self):
        """The core of #B185/#B186: the sha is recorded BESIDE the pr it
        came from. Without this every consumer is guessing."""
        fid = self._finding()
        self._resolve(fid, remedy_pr=REMEDY)
        self._attest(fid, _SHA_B)
        row = self._row(fid)
        self.assertEqual(row["verified_head_sha"], _SHA_B)
        self.assertEqual(row["verified_pr_number"], REMEDY)
        self.assertEqual(rf.verified_anchor_pr(row), REMEDY)

    def test_default_path_stamps_the_board_pr(self):
        fid = self._finding()
        self._resolve(fid)
        self._attest(fid, _SHA_B)
        self.assertEqual(self._row(fid)["verified_pr_number"], BOARD)

    def test_cross_anchored_row_still_enforces_the_third_party_seats(self):
        """The anchor widens WHICH tree may be read; it must not widen
        WHO may read it. A resolver is still not a witness."""
        fid = self._finding()
        self._resolve(fid, remedy_pr=REMEDY, actor=AGENT_OPENER)
        with self.assertRaises(db.ForumError):
            self._attest(fid, _SHA_B, witness=AGENT_OPENER)
        with self.assertRaises(db.ForumError):
            self._attest(fid, _SHA_B, witness=AGENT_FINDER)

    def test_a_row_with_no_remedy_still_refuses_a_re_verify(self):
        fid = self._finding()
        self._resolve(fid)
        self._attest(fid, _SHA_B)
        with self.assertRaises(db.ForumError):
            self._resolve(fid)
        # unchanged, pre-existing behaviour
        self.assertEqual(self._state(fid), "resolved")


class TestAnchorStaling(AnchorBase):
    """The staling loop this change would otherwise introduce.

    A cross-anchored row pins REMEDY's sha. `finding_stale_on_push` is
    called with the BOARD pr's new head by the poller's reconcile sweep.
    Unscoped, `verified_head_sha != <board head>` is true forever and the
    row re-stales on every single sweep - a row that can never be
    witnessed, which is the exact bug being fixed, reintroduced one layer
    down.
    """

    def test_board_head_moving_does_not_stale_a_cross_anchored_row(self):
        fid = self._finding()
        self._resolve(fid, remedy_pr=REMEDY)
        self._attest(fid, _SHA_B)
        self.assertEqual(self._state(fid), "resolved")
        for _ in range(3):  # three sweeps - the loop is the point
            rf.finding_stale_on_push(self.conn, BOARD, _SHA_C)
            self.assertEqual(
                self._state(fid),
                "resolved",
                "a cross-anchored row must survive the board pr moving",
            )

    def test_remedy_head_moving_does_stale_it(self):
        """The control that makes the arm above discriminating: the
        anchor's OWN movement still invalidates. If this passed because
        staling was broken rather than because it is scoped, the previous
        test would be vacuous."""
        fid = self._finding()
        self._resolve(fid, remedy_pr=REMEDY)
        self._attest(fid, _SHA_B)
        rf.finding_stale_on_push(self.conn, REMEDY, _SHA_C)
        self.assertEqual(self._state(fid), "stale")

    def test_ANCHOR_head_moving_stales_through_the_real_sweep(self):
        """citizen-four's finding (b), pinned at the CALLER.

        `test_remedy_head_moving_does_stale_it` calls
        `finding_stale_on_push` directly, so it proves the function is
        scoped correctly and says nothing about whether any caller ever
        hands it the ANCHOR's head.  It did not: the sweep keyed its
        candidate lookup on the board pr, so a cross-anchored row was
        never found - the inverse of the loop I had just fixed.

        This drives the real entrypoint with the input production
        produces: both prs open, the ANCHOR's head advancing.
        """
        fid = self._finding()
        self._resolve(fid, remedy_pr=REMEDY)
        self._attest(fid, _SHA_B)
        self.assertEqual(self._state(fid), "resolved")
        out = rf.reconcile_boards_for_heads(self.conn, {REMEDY: _SHA_C})
        self.assertEqual(out.get(REMEDY, 0), 1, "the anchor pr was found")
        self.assertEqual(self._state(fid), "stale")

    def test_reconcile_sweep_does_not_loop(self):
        """Drive the real sweep entrypoint, which is fed the board pr -
        the shape the poller actually calls."""
        fid = self._finding()
        self._resolve(fid, remedy_pr=REMEDY)
        self._attest(fid, _SHA_B)
        for _ in range(3):
            out = rf.reconcile_boards_for_heads(self.conn, {BOARD: _SHA_C})
            self.assertEqual(out.get(BOARD, 0), 0)
            self.assertEqual(self._state(fid), "resolved")

    def test_stale_all_is_anchor_scoped_too(self):
        fid = self._finding()
        self._resolve(fid, remedy_pr=REMEDY)
        self._attest(fid, _SHA_B)
        rf.finding_stale_all(self.conn, BOARD)
        self.assertEqual(self._state(fid), "resolved")
        rf.finding_stale_all(self.conn, REMEDY)
        self.assertEqual(self._state(fid), "stale")

    def test_default_rows_stale_exactly_as_before(self):
        """No cross-anchor declared: today's behaviour, unchanged."""
        fid = self._finding()
        self._resolve(fid)
        self._attest(fid, _SHA_B)
        rf.finding_stale_on_push(self.conn, BOARD, _SHA_C)
        self.assertEqual(self._state(fid), "stale")
        fid2 = self._finding()
        self._resolve(fid2)
        self._attest(fid2, _SHA_B)
        rf.finding_stale_on_push(self.conn, BOARD, _SHA_B)
        self.assertEqual(self._state(fid2), "resolved")


class TestAnchorFlipGate(AnchorBase):
    """A remedy in PR Y is not a fix to PR X's tree.

    This is a deliberate TIGHTENING. Before #875 a resolver could point a
    board-pr blocker at a remedy shipped elsewhere and a witness could
    sign it, clearing X's blocker on the strength of Y's fix. X still
    needs the fix forward-ported; the gate must keep saying so.
    """

    def _seed_flip(self, auto_flip=1):
        fid = self._finding(auto_flip=auto_flip)
        self.conn.execute(
            "INSERT OR IGNORE INTO pr_votes (pr_number, voter_id, value,"
            " created_at) VALUES (?, ?, -1, '2026-01-01T00:00:00Z')",
            (BOARD, AGENT_FINDER),
        )
        self.conn.commit()
        return fid

    def test_same_anchored_attestation_clears_the_flip(self):
        fid = self._seed_flip()
        self._resolve(fid)
        self._attest(fid, _SHA_C)
        out = rf.flip_ready(self.conn, 1, BOARD, AGENT_FINDER, _SHA_C)
        self.assertTrue(out["ready"], out)

    def test_cross_anchored_attestation_does_not_clear_the_flip(self):
        fid = self._seed_flip()
        self._resolve(fid, remedy_pr=REMEDY)
        self._attest(fid, _SHA_C)  # attested at the remedy pr's head
        out = rf.flip_ready(self.conn, 1, BOARD, AGENT_FINDER, _SHA_C)
        self.assertFalse(out["ready"])
        # The row is resolved+verified, so it is NOT among the open
        # blockers - which is why this is assertNotIn. The comment that used
        # to sit here said the opposite ("still REPORTED"), documenting an
        # assertion the test does not make.
        blockers = rf.reviewer_blockers(self.conn, 1, BOARD, AGENT_FINDER)
        self.assertNotIn(fid, [b["id"] for b in blockers])

    def test_a_wrong_head_still_reports_a_blocker(self):
        """The arm #872 asks for, on the anchor-scoped predicate: a
        resolved+verified row whose sha is not this pr's head must read
        as an open blocker, so a third writer cannot desync the board and
        the gate."""
        fid = self._seed_flip()
        self._resolve(fid)
        self._attest(fid, _SHA_C)
        out = rf.flip_ready(self.conn, 1, BOARD, AGENT_FINDER, _SHA_A)
        self.assertFalse(out["ready"])

    def _vote_value(self):
        return self.conn.execute(
            "SELECT value FROM pr_votes WHERE pr_number = ? AND voter_id = ?",
            (BOARD, AGENT_FINDER),
        ).fetchone()["value"]

    def test_cross_anchored_attestation_does_not_flip_the_vote(self):
        """MiMo's fold-in: the OTHER consumer of the cleared-on-head
        predicate. flip_pr_vote_to_approve re-checks every consented row
        inside the write txn, so a gate that guarded only flip_ready would
        still let a cross-anchored attestation through the write path.
        Asserted on the vote row, not just the raise, because the refusal
        has to precede the write - an exception raised AFTER the UPDATE
        would leave the vote flipped."""
        fid = self._seed_flip()
        self._resolve(fid, remedy_pr=REMEDY)
        self._attest(fid, _SHA_C)
        with self.assertRaises(db.ForumError) as ctx:
            rf.flip_pr_vote_to_approve(self.conn, 1, BOARD, AGENT_FINDER, _SHA_C)
        self.assertIn("blockers reopened", str(ctx.exception))
        self.assertEqual(self._vote_value(), -1, "the vote must still be -1")

    def test_same_anchored_attestation_does_flip_the_vote(self):
        """The control. Without it the arm above could pass on a function
        that refuses everything - the shapes are identical, so an arm that
        cannot tell them apart is decoration."""
        fid = self._seed_flip()
        self._resolve(fid)
        self._attest(fid, _SHA_C)
        out = rf.flip_pr_vote_to_approve(self.conn, 1, BOARD, AGENT_FINDER, _SHA_C)
        self.assertEqual(out["net"], 1)
        self.assertEqual(self._vote_value(), 1)


class TestAnchorQueue(AnchorBase):
    def test_witness_queue_carries_both_anchors(self):
        """A witness reading the queue must be able to tell WHICH pr to
        read. Reporting only pr_number sends them to the frozen tree the
        bug is about."""
        fid = self._finding()
        self._resolve(fid, remedy_pr=REMEDY)
        rows = rf._witness_queue(self.conn)
        mine = [r for r in rows if r["id"] == fid]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["remedy_pr_number"], REMEDY)
        self.assertEqual(mine[0]["pr_number"], BOARD)
        self.assertEqual(rf.anchor_pr(mine[0]), REMEDY)

    def test_cross_anchored_row_is_still_advertised_as_witness_work(self):
        """It is winnable now, so it is honest backlog - but the seat is
        only takeable if the witness is not finder or fixer."""
        fid = self._finding()
        self._resolve(fid, remedy_pr=REMEDY)
        row = self._row(fid)
        self.assertTrue(rf.verifiable_by_me(row, AGENT_WITNESS, True))
        self.assertFalse(rf.verifiable_by_me(row, AGENT_OPENER, True))
        self.assertFalse(rf.verifiable_by_me(row, AGENT_FINDER, True))


class TestAnchorMigration(AnchorBase):
    def test_columns_exist_after_init(self):
        cols = {
            r["name"] for r in self.conn.execute("PRAGMA table_info(review_findings)")
        }
        self.assertIn("remedy_pr_number", cols)
        self.assertIn("verified_pr_number", cols)

    def test_legacy_row_without_anchors_behaves_as_the_board_pr(self):
        """A row written before the columns existed: NULL everywhere.
        This is the population #B185/#B186 are actually about, so it is
        the one that must not move.

        The -1 and the auto_flip consent are seeded HERE on purpose.
        flip_ready short-circuits with 'no-minus-one' before it ever reads
        the cleared-on-head predicate, so without them the
        COALESCE(verified_pr_number, pr_number) arm never executes - the
        original version of this test asserted False for that reason and
        never reached the SQL it exists for.
        """
        fid = self._finding(auto_flip=1)
        self._resolve(fid)
        self._attest(fid, _SHA_C)
        self.conn.execute(
            "UPDATE review_findings SET remedy_pr_number = NULL,"
            " verified_pr_number = NULL WHERE id = ?",
            (fid,),
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO pr_votes (pr_number, voter_id, value,"
            " created_at) VALUES (?, ?, -1, '2026-01-01T00:00:00Z')",
            (BOARD, AGENT_FINDER),
        )
        self.conn.commit()
        row = self._row(fid)
        self.assertEqual(rf.anchor_pr(row), BOARD)
        self.assertEqual(rf.verified_anchor_pr(row), BOARD)
        out = rf.flip_ready(self.conn, 1, BOARD, AGENT_FINDER, _SHA_C)
        self.assertTrue(
            out["ready"],
            "the NULL arm must CLEAR a legacy row verified on the board head",
        )
        # The half that gives the assertion above its meaning: same NULL
        # row, a different live head. Mirrors
        # test_a_wrong_head_still_reports_a_blocker, so if the arm above
        # were vacuous - or if everything cleared - this would not hold.
        stale = rf.flip_ready(self.conn, 1, BOARD, AGENT_FINDER, _SHA_A)
        self.assertFalse(stale["ready"], "a different live head must still block")

    def test_boot_migration_is_idempotent(self):
        db.init_db()
        cols = [
            r["name"] for r in self.conn.execute("PRAGMA table_info(review_findings)")
        ]
        self.assertEqual(cols.count("remedy_pr_number"), 1)
        self.assertEqual(cols.count("verified_pr_number"), 1)

    def test_migration_runs_on_a_database_that_already_has_the_table(self):
        """The CREATE TABLE IF NOT EXISTS path cannot add a column to a
        table that exists, so the ALTER has to be reachable on its own -
        this drives the real gap."""
        with db._conn() as c:
            c.execute("ALTER TABLE review_findings DROP COLUMN remedy_pr_number")
            c.execute("ALTER TABLE review_findings DROP COLUMN verified_pr_number")
        gone = {
            r["name"] for r in self.conn.execute("PRAGMA table_info(review_findings)")
        }
        self.assertNotIn("remedy_pr_number", gone)
        db.init_db()
        cols = {
            r["name"] for r in self.conn.execute("PRAGMA table_info(review_findings)")
        }
        self.assertIn("remedy_pr_number", cols)
        self.assertIn("verified_pr_number", cols)


class TestAnchorWrapper(AnchorBase):
    """The MCP wrapper is where the attestation actually happens (the db
    layer only stores), so the defect site is here - #B183's point."""

    def _finding_row(self, fid):
        return self._row(fid)

    def test_wrapper_reads_the_ANCHOR_pr_not_the_board_pr(self):
        import server.tools.repo._findings as wf

        fid = self._finding()
        self._resolve(fid, remedy_pr=REMEDY)
        seen = []

        def fake_raw(pr_number, *a, **k):
            seen.append(pr_number)
            return {"head": {"sha": _SHA_B}}

        with (
            mock.patch.object(wf.github, "_pr_raw", side_effect=fake_raw),
            mock.patch.object(wf.github, "_invalidate_pr"),
            mock.patch.object(wf, "_refresh_mirror", new=_noop),
        ):
            asyncio.run(wf.finding_verify("tok-" + str(AGENT_WITNESS), fid, _SHA_B))
        # Every live read named the REMEDY pr. If any read still used
        # BOARD, the pre-write check would have refused.
        self.assertTrue(seen, "no live head read happened")
        self.assertEqual(set(seen), {REMEDY})
        row = self._row(fid)
        self.assertEqual(row["verified_head_sha"], _SHA_B)
        self.assertEqual(row["verified_pr_number"], REMEDY)

    def test_wrapper_still_refuses_a_moved_anchor_head(self):
        import server.tools.repo._findings as wf

        fid = self._finding()
        self._resolve(fid, remedy_pr=REMEDY)
        calls = {"n": 0}

        def fake_raw(pr_number, *a, **k):
            calls["n"] += 1
            return {"head": {"sha": _SHA_C}}

        with (
            mock.patch.object(wf.github, "_pr_raw", side_effect=fake_raw),
            mock.patch.object(wf.github, "_invalidate_pr"),
            mock.patch.object(wf, "_refresh_mirror", new=_noop),
        ):
            with self.assertRaises(db.ForumError) as ctx:
                asyncio.run(wf.finding_verify("tok-" + str(AGENT_WITNESS), fid, _SHA_B))
        self.assertIn("head moved", str(ctx.exception))
        self.assertEqual(calls["n"], 1, "refused on the pre-write read")
        self.assertIsNone(self._row(fid)["verified_head_sha"])

    def test_wrapper_refusal_names_the_anchor_when_cross_anchored(self):
        """#B186's sharpest cost: the refusal used to hand the witness a
        SHA that is provably the wrong tree. When the anchor moved, the
        message has to say which pr it is talking about."""
        import server.tools.repo._findings as wf

        fid = self._finding()
        self._resolve(fid, remedy_pr=REMEDY)

        def fake_raw(pr_number, *a, **k):
            return {"head": {"sha": _SHA_C}}

        with mock.patch.object(wf.github, "_pr_raw", side_effect=fake_raw):
            with self.assertRaises(db.ForumError) as ctx:
                asyncio.run(wf.finding_verify("tok-" + str(AGENT_WITNESS), fid, _SHA_B))
        self.assertIn(f"#{REMEDY}", str(ctx.exception))

    def test_merged_anchor_with_no_declared_remedy_refuses(self):
        """citizen-four's finding (a), pinned.

        A merged anchor is a frozen head, and for a row whose remedy was
        never declared that head is the board pr's - provably the tree
        WITHOUT the fix.  Accepting it is what turned #B185/#B186's
        honestly-unwinnable rows into winnable-against-the-defect, so
        the tool must refuse and say how to fix it.
        """
        import server.tools.repo._findings as wf

        fid = self._finding()
        self._resolve(fid)  # no remedy_pr: the anchor is the board pr

        def fake_raw(pr_number, *a, **k):
            return {"head": {"sha": _SHA_B}, "state": "closed", "merged": True}

        with mock.patch.object(wf.github, "_pr_raw", side_effect=fake_raw):
            with self.assertRaises(db.ForumError) as ctx:
                asyncio.run(wf.finding_verify("tok-" + str(AGENT_WITNESS), fid, _SHA_B))
        msg = str(ctx.exception)
        self.assertIn("merged or", msg)
        self.assertIn("remedy_pr", msg)
        self.assertIsNone(self._row(fid)["verified_head_sha"])

    def test_declared_remedy_on_a_merged_pr_is_accepted(self):
        """The positive control, and the only route by which the stranded
        rows can ever discharge: a DECLARED remedy pr may be merged,
        because the resolver named the pr that shipped the fix.  Without
        this arm the refusal above would be a dead end rather than a
        redirect."""
        import server.tools.repo._findings as wf

        fid = self._finding()
        self._resolve(fid, remedy_pr=REMEDY)
        seen = []

        def fake_raw(pr_number, *a, **k):
            seen.append(pr_number)
            return {"head": {"sha": _SHA_B}, "state": "closed", "merged": True}

        with (
            mock.patch.object(wf.github, "_pr_raw", side_effect=fake_raw),
            mock.patch.object(wf.github, "_invalidate_pr"),
            mock.patch.object(wf, "_refresh_mirror", new=_noop),
        ):
            asyncio.run(wf.finding_verify("tok-" + str(AGENT_WITNESS), fid, _SHA_B))
        self.assertEqual(self._row(fid)["verified_head_sha"], _SHA_B)
        # MiMo's fold-in: fake_raw returns identical bytes for ANY pr, so
        # without this the arm cannot tell which pr was read and a
        # regression reading BOARD instead of REMEDY would pass.
        self.assertEqual(set(seen), {REMEDY}, "must read the DECLARED remedy pr")

    def test_default_path_never_mentions_an_anchor(self):
        """No behaviour change on the default path: same message shape,
        same pr read."""
        import server.tools.repo._findings as wf

        fid = self._finding()
        self._resolve(fid)
        seen = []

        def fake_raw(pr_number, *a, **k):
            seen.append(pr_number)
            return {"head": {"sha": _SHA_C}}

        with mock.patch.object(wf.github, "_pr_raw", side_effect=fake_raw):
            with self.assertRaises(db.ForumError) as ctx:
                asyncio.run(wf.finding_verify("tok-" + str(AGENT_WITNESS), fid, _SHA_B))
        self.assertEqual(set(seen), {BOARD})
        self.assertNotIn("anchored on", str(ctx.exception))

    def test_merged_flag_alone_also_refuses(self):
        """GitHub reports a merged pr as state='closed' AND merged=true;
        a stub or a partial payload may carry only the flag.  Asserting
        on the state string alone would let that shape through."""
        import server.tools.repo._findings as wf

        fid = self._finding()
        self._resolve(fid)

        def fake_raw(pr_number, *a, **k):
            return {"head": {"sha": _SHA_B}, "merged": True}

        with mock.patch.object(wf.github, "_pr_raw", side_effect=fake_raw):
            with self.assertRaises(db.ForumError) as ctx:
                asyncio.run(wf.finding_verify("tok-" + str(AGENT_WITNESS), fid, _SHA_B))
        self.assertIn("merged or", str(ctx.exception))

    def test_closed_state_without_the_merged_flag_also_refuses(self):
        """The other disjunct, alone. test_merged_flag_alone_also_refuses
        covers merged=true with no state; this covers state='closed' with
        no merged key. A partial payload must not slip through whichever
        half of the guard happens to be tested first."""
        import server.tools.repo._findings as wf

        fid = self._finding()
        self._resolve(fid)

        def fake_raw(pr_number, *a, **k):
            return {"head": {"sha": _SHA_B}, "state": "closed"}

        with mock.patch.object(wf.github, "_pr_raw", side_effect=fake_raw):
            with self.assertRaises(db.ForumError) as ctx:
                asyncio.run(wf.finding_verify("tok-" + str(AGENT_WITNESS), fid, _SHA_B))
        self.assertIn("merged or", str(ctx.exception))
        self.assertIsNone(self._row(fid)["verified_head_sha"])

    def test_post_write_recheck_stales_against_the_ANCHOR(self):
        """The fail-closed compensation must name the anchor too, or a
        post-write move on the remedy pr would leave an unattested row
        displayed as verified."""
        import server.tools.repo._findings as wf

        fid = self._finding()
        self._resolve(fid, remedy_pr=REMEDY)
        calls = {"n": 0}

        def fake_raw(pr_number, *a, **k):
            calls["n"] += 1
            return {"head": {"sha": _SHA_B if calls["n"] == 1 else _SHA_C}}

        with (
            mock.patch.object(wf.github, "_pr_raw", side_effect=fake_raw),
            mock.patch.object(wf.github, "_invalidate_pr"),
            mock.patch.object(wf, "_refresh_mirror", new=_noop),
        ):
            with self.assertRaises(db.ForumError) as ctx:
                asyncio.run(wf.finding_verify("tok-" + str(AGENT_WITNESS), fid, _SHA_B))
        self.assertIn("head moved during verification", str(ctx.exception))
        self.assertEqual(calls["n"], 2, "post-write recheck must have run")
        self.assertEqual(self._state(fid), "stale")

    def test_post_write_recheck_that_raises_stales_instead_of_propagating(self):
        """The other half of the fail-closed branch. The existing pin makes
        the second read return a MOVED head; this one makes it raise. If
        the exception escaped uncaught the row would sit `resolved` and
        read as verified with no attestation behind it - so the raise must
        be converted, not propagated."""
        import server.tools.repo._findings as wf

        fid = self._finding()
        self._resolve(fid, remedy_pr=REMEDY)
        calls = {"n": 0}

        def fake_raw(pr_number, *a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"head": {"sha": _SHA_B}}
            raise RuntimeError("github read exploded")

        with (
            mock.patch.object(wf.github, "_pr_raw", side_effect=fake_raw),
            mock.patch.object(wf.github, "_invalidate_pr"),
            mock.patch.object(wf, "_refresh_mirror", new=_noop),
        ):
            with self.assertRaises(db.ForumError) as ctx:
                asyncio.run(wf.finding_verify("tok-" + str(AGENT_WITNESS), fid, _SHA_B))
        self.assertEqual(calls["n"], 2, "post-write recheck must have run")
        self.assertIn("post-write", str(ctx.exception))
        self.assertEqual(self._state(fid), "stale")


async def _noop(*a, **k):
    return None


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
