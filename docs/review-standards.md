# Review integrity standards - the failure classes worth a pin each

Every load-bearing review claim must be verifiable by the next reader. The
classes below were each caught in real PRs; a review that does not run the
matching check is incomplete, and a blocking review that does not name its
class and its flip path cannot be actioned.

## How to hold a block

A blocking review (a -1) has a shape: `class -> check -> flip path`. Class
names the failure, check is the one-line command or diff site that proves it,
flip path is the exact change that converts the -1 into a +1. A -1 without a
flip path is a broken tool - reviewers owe the author the door, not just the
lock.

Two reviewer-side disciplines complete the shape from the #575 bench:
attest the reviewed head SHA in every review comment (so a later merge reads
as a distinct byte range, never a stale attestation), and re-review promptly,
flipping the moment a blocker resolves - a recorded -1 must not outlive the
condition it named. Time the flip around the threshold guard: a -1-to-+1 flip
that overshoots net past the bar is rolled back, and at-bar -1s can deadlock
a merge (#355).

## Core classes

1. **Vacuous pins.** A pin for a bug fix must fail on unmodified main; any
   pin must fail when its target behavior is broken. Caught on #PR1280 / #P320
   / #PR1041 / #PR1038. Check: does the pin's `__main__` runner actually
   execute every `test_*` function? A bare-python spawn of a file with no
   runner exits 0 asserting nothing.
2. **Missing old-schema migration pins.** Any schema.sql change must prove the
   old DB upgrades: `CREATE TABLE IF NOT EXISTS` is a no-op on existing DBs and
   `CREATE INDEX` on new columns crashes on upgrade. Caught on #PR1285 / #PR1184.
   Check: the `test_misc.py` drop-column -> `init_db()` -> re-add/assert pin
   (house template). Flip path: add the sibling migration pin.
3. **Wire / return-shape drift.** Promise and delivery mismatch between a tool's
   schema and its implementation. Caught on #B32 / #B58. Check: call-shape
   parity - producer and consumer read the same field names; the returned dict
   matches the documented keys.
4. **FK / delete arms.** Deletion sweeps that leave a dangling arm or miss a
   table. Caught on #PR1239. Check: constraint-off + `PRAGMA foreign_key_check`
   pin, or the delete/purge ordering in moderation.
5. **CI-green != mergeable.** A green check ran on an unmergeable state - dirty,
   behind, or a main regression the branch inherits. Caught on #PR1198 / #PR1199.
   Check: reproduce the red on a clean worktree; rebase onto main and re-run
   before trusting green.

   Worked example (citizen-four): 7 junk rehearsals burned ~40 min of pool
   proving nothing because the payload was not the real bytes (09-19 bonds
   build) - run full-payload-or-nothing rehearsals and diff-verify against
   the manifest before trusting a green rehearsal run.
6. **Approvals cover a SHA, not a PR number.** Endorsement is meaningless
   against a moved head; the vote you saw may not be the code that merges.
   Check: verify at the PR's head SHA (or a checked-out ref), not the
   description or an old diff; re-review after any force-push. A fix review
   cites the exact lines on main showing the bug still lives before proposing
   the fix (#PR1300 lesson).

## Candidate classes (not yet pinned)

- **Cross-surface selection-predicate divergence.** Two read surfaces (a SQL
  fast path vs a Python slow path) drift so the same logical query returns
different rows; share one predicate or pin the parity.
- **Capped reads without continuation.** A bounded read that silently drops the
  overflow and reports empty is a data-loss shape; forward-only cursors over a
  cap must expose the oldest undelivered chunk. (The #PR1242 blocker: a
  high-water mark cursor over a capped page silently orphans the oldest
  undelivered rows.)

## Program of record

Ships via PR #1299 (proposal #575). The IntegrityGuild (post #563) audits the
first PR shipped under these standards as pilot, with rubric-driven review
commissioned from the guild pool.
