"""db._pr_state — one live-PR predicate for every machine consumer.

Bug #B107 / proposal #725. "Is this PR still live?" had been reinvented as
"no ``proposal_outcomes`` row exists" in thirteen query copies across
``_nudges``, ``_agent``, ``_claiming``, ``_collaborative``, ``_proposal``
and ``_karma`` (plus three post-scoped "is this proposal decided" probes).
Outcome rows are written only when the outcome poller *observes* a
transition, so a PR merged while unobserved - a link that landed after the
merge, a stacked PR hand-merged bottom-up, a closed-feed page turn during a
poller outage - stayed merged-on-GitHub with no verdict row, and every
machine consumer read it as live: vote queues that nagged forever (10
numbers queued, 8 of them merged, observed 2026-09-25T12:26Z), ``unclaim``
and ``leave`` refusals against shipped work, and collaborator PR-cap slots
consumed by history in two independent copies of the same gate.

The predicate generalises the three-source decided-check that
``db/_pr_vote.py``'s vote gate already proved in production, plus the
closed-PR cache as a fourth source::

    decided(pr) = outcome_row OR pr_merges OR pr_record
                  OR (pr_rows.state = 'closed'
                      AND pr_rows.verified_at IS NOT NULL)
    live(pr)    = NOT decided(pr)

Both absence directions are load-bearing:

- **Absence of cache is not evidence of closure** (the #B79 direction):
  a PR with no ``pr_rows`` row is live unless a verdict row says otherwise.
  ``tests/test_perf_necessity_pins.py`` §9 pins a NULL-opener link with no
  cache row staying queued; ``tests/test_pr_state_predicate.py`` re-asserts
  it beside the new parity pins so the two directions cannot be traded off
  silently.
- **The cache arm requires a last-write stamp.** ``pr_rows.state`` carries
  ``DEFAULT 'closed'``, so ``verified_at IS NOT NULL`` is what separates
  "a writer observed this closure" from "a row nobody ever populated".
  ``verified_at`` refreshes on both write paths (``pr_rows_upsert`` and
  ``pr_rows_upsert_from_raw`` each carry ``verified_at=excluded.verified_at``),
  which makes it a genuine stamp rather than a set-once marker, and the
  boot migration (``db/_core/_boot_final.py``) adds the column on
  deployments whose ``pr_rows`` predates it, leaving legacy rows NULL: an
  honest "predates the writer", excluded by the guard until the <=6h
  backfill restamps them.

**No freshness knob, deliberately.** The proposal thread (#C1345, #C1350,
#C1354) converged on an age bound expressed in ``PR_MERGE_POLL_SECONDS``
intervals; the bytes retired it. ``PR_MERGE_POLL_SECONDS`` drives the
*outcome* poller (the ``proposal_outcomes`` writer); the ``pr_rows``
writers are the <=6h watermark-tracked backfill
(``server/poller/_outcome.py:_PR_ROWS_BACKFILL_MAX_AGE_SECONDS``) and
read-time revalidation (``server/pr_views.py``). A bound in merge-poll
intervals would have to be ~360x to cover the real writer - a multiplier
that large is a seconds literal in disguise, the exact misreading the
unitless form existed to prevent. The reopened-PR hazard the bound was
invented for is handled structurally instead: ``pr_rows_upsert_from_raw``
DELETES a row when revalidation observes a reopen, so the cache arm
self-heals at the next read, and the backfill restamps every still-closed
row within one cycle. The residual cohort (a stamped row whose PR was
reopened and that nobody ever reads again) is bounded, named here, and
made visible rather than silent by ``pr_state_as_of`` in ``check_in``.

**Named sibling, deliberately unchanged:** ``db/_pr_vote.py``'s vote gate
keeps its three verdict sources and gains no cache arm. The two consumer
classes have opposite fail-safes (#C1345's asymmetry): a queue reader that
acts on cache evidence costs one missed nudge when wrong, while a vote
gate that refuses a legitimate vote on cache evidence corrupts
participation - and the ``db/`` layer holds no live truth to bound that
risk with. The stale-vote window (a merged-unobserved PR can still accept
votes until a verdict row lands) is documented, deliberate, and narrower
than the queue defect this module fixes. ``tests/test_pr_state_predicate.py``
pins the sibling's three-source shape so it cannot absorb the cache arm
silently.
"""

from __future__ import annotations

import sqlite3


def pr_decided_sql(pr_expr: str) -> str:
    """SQL boolean expression (no bind params): the PR named by `pr_expr`
    is decided - a verdict row in any of the three verdict tables, or the
    stamped closed-PR cache says closed.

    `pr_expr` is a trusted SQL column reference chosen by the calling
    query (the same contract as ``_proposal_status_sql``'s alias): the
    fragment must stay joinable because two consumers are sub-selects
    inside ``check_in``'s single-statement mega-batch, which exists to
    avoid N+1 reads. Internal aliases are underscore-suffixed so the
    fragment nests inside any outer query without alias collisions.

    ``pr_expr`` MUST be alias-qualified (``pl.pr_number``, never a bare
    ``pr_number``): every arm is a correlated EXISTS over a table that
    has its own ``pr_number`` column, and an unqualified outer reference
    would resolve to the INNER table - a self-comparison that is true
    for every row, turning the arm into "table non-empty" (pinned by
    tests/test_pr_state_predicate.py's CI-nudge leg).
    """
    p = pr_expr
    return (
        "(EXISTS (SELECT 1 FROM proposal_outcomes _po_d"
        f" WHERE _po_d.pr_number = {p})"
        " OR EXISTS (SELECT 1 FROM pr_merges _pm_d"
        f" WHERE _pm_d.pr_number = {p})"
        " OR EXISTS (SELECT 1 FROM pr_record _pr_d"
        f" WHERE _pr_d.pr_number = {p})"
        " OR EXISTS (SELECT 1 FROM pr_rows _pw_d"
        f" WHERE _pw_d.pr_number = {p}"
        " AND _pw_d.state = 'closed'"
        " AND _pw_d.verified_at IS NOT NULL))"
    )


def pr_live_sql(pr_expr: str) -> str:
    """The negative form - reads as the question consumers ask."""
    return f"NOT {pr_decided_sql(pr_expr)}"


def pr_merged_sql(pr_expr: str) -> str:
    """SQL boolean expression (no bind params): the PR named by ``pr_expr``
    MERGED - the merged direction of the shared verdict (#831).  Arms
    mirror _proposal_pr_history's direction chain (#B141): outcome row,
    then pr_merges, then the stamped cache.  pr_record never appears here
    - its CHECK admits only 'declined'/'closed', so the negative ledger
    cannot attest a merge.  ``pr_expr`` MUST be alias-qualified (same
    contract as pr_decided_sql): a bare ``pr_number`` would resolve to
    the EXISTS subquery's own table.  An unstamped cache row attests
    nothing (the #B79 direction).  Joinable and parameter-free."""
    return (
        f"(EXISTS (SELECT 1 FROM proposal_outcomes po"
        f" WHERE po.pr_number = {pr_expr} AND po.status = 'merged')"
        f" OR EXISTS (SELECT 1 FROM pr_merges pm"
        f" WHERE pm.pr_number = {pr_expr})"
        f" OR EXISTS (SELECT 1 FROM pr_rows pw"
        f" WHERE pw.pr_number = {pr_expr} AND pw.state = 'closed'"
        " AND pw.verified_at IS NOT NULL AND pw.merged_at IS NOT NULL))"
    )


def pr_negative_before_sql(pr_expr: str, when_expr: str) -> str:
    """SQL boolean expression (no bind params): the PR named by ``pr_expr``
    reached a TERMINAL NEGATIVE outcome (declined/closed - never merged)
    strictly after the SQL expression ``when_expr``.  This is the temporal
    join findings_upheld (#831) is defined by: the row existed while the
    decision was live, so it could have shaped it - presence at the
    decision, which no instrument here can upgrade to proven influence.

    Each arm carries its own timestamp (outcome happened_at, pr_record
    closed_at, stamped-cache closed_at), so sources cannot disagree about
    which time is compared: any negative arm attesting "decided after
    ``when_expr``" satisfies the predicate.  The cache arm requires the
    writer's stamp AND ``merged_at IS NULL`` - a closed row carrying a
    merge time is the merged direction, never this one.  Both expressions
    MUST be alias-qualified (same contract as pr_decided_sql).  Joinable
    and parameter-free.

    BOTH SIDES ARE TRUNCATED TO SECOND PRECISION, and the truncation is
    load-bearing rather than tidying.  ``when_expr`` is normally
    review_findings.created_at, which carries the schema DEFAULT's
    millisecond form (strftime '%Y-%m-%dT%H:%M:%fZ', 24 chars, '.' at
    index 19), while every stamp compared against it - po.happened_at,
    prd.closed_at, pw.closed_at - is GitHub-sourced second precision
    ('YYYY-MM-DDTHH:MM:SSZ', 20 chars, 'Z' at index 19).  Unnormalised,
    '.' (0x2E) sorts BELOW 'Z' (0x5A), so a finding created in the same
    second as the decision reads as strictly before it and
    findings_upheld over-counts (#168).  The two formats are named in
    db/_core/_boot_foundation.py:178-180; do NOT read the substr() pairs
    as a simplification - removing them restores the inflation, and
    only the mixed-precision test arm can catch that.

    The missing-data sentinels are likewise stated per arm rather than
    left to SQLite's string ordering (#831 board finding #43).  The two
    NOT NULL stamp columns are written with an EMPTY-STRING sentinel
    (server/poller/_outcome.py's ``or ""`` at :349 and :577), so they
    guard ``<> ''``; the cache column is nullable, so it guards
    ``IS NOT NULL`` and ``<> ''`` in the same vocabulary.  A row whose
    decision time was never captured attests nothing, in every arm.
    The guards change no behaviour today - ``x < ''`` is already false
    for every non-empty x, and substr(NULL) comparisons are NULL - and
    that is precisely the point: the exclusion is a stated intent the
    sentinel pins lock, not an accident of collation that one future
    comparison edit could silently invert."""
    return (
        f"(EXISTS (SELECT 1 FROM proposal_outcomes po"
        f" WHERE po.pr_number = {pr_expr}"
        " AND po.status IN ('declined', 'closed')"
        " AND po.happened_at <> ''"
        f" AND substr({when_expr}, 1, 19)"
        " < substr(po.happened_at, 1, 19))"
        f" OR EXISTS (SELECT 1 FROM pr_record prd"
        f" WHERE prd.pr_number = {pr_expr}"
        " AND prd.closed_at <> ''"
        f" AND substr({when_expr}, 1, 19)"
        " < substr(prd.closed_at, 1, 19))"
        f" OR EXISTS (SELECT 1 FROM pr_rows pw"
        f" WHERE pw.pr_number = {pr_expr} AND pw.state = 'closed'"
        " AND pw.verified_at IS NOT NULL AND pw.merged_at IS NULL"
        " AND pw.closed_at IS NOT NULL AND pw.closed_at <> ''"
        f" AND substr({when_expr}, 1, 19)"
        " < substr(pw.closed_at, 1, 19)))"
    )


def proposal_decided_sql(post_expr: str) -> str:
    """SQL boolean expression (no bind params): the proposal named by
    `post_expr` is decided - an outcome row keyed to the post, or any
    linked PR decided by the shared fragment.

    The post-scoped form serves the notification-accuracy consumers whose
    old probe read "no outcome row for this post" ("a decided proposal has
    no discussion to notify about"): a proposal whose only PR merged
    unobserved has no post-keyed outcome row either, and used to keep
    pinging its voters forever (observed live: proposal #635 merged as
    PR #1440 and check_in kept reporting new discussion on it).
    """
    q = post_expr
    return (
        "(EXISTS (SELECT 1 FROM proposal_outcomes _po_p"
        f" WHERE _po_p.post_id = {q})"
        " OR EXISTS (SELECT 1 FROM proposal_links _pl_p"
        f" WHERE _pl_p.post_id = {q}"
        f" AND {pr_decided_sql('_pl_p.pr_number')}))"
    )


def pr_is_decided(conn: sqlite3.Connection, pr_number: int) -> bool:
    """Scalar wrapper - the fragment evaluated for one PR."""
    sql = "SELECT " + pr_decided_sql("?")
    row = conn.execute(sql, (pr_number,) * 4).fetchone()
    return bool(row is not None and row[0])


def pr_is_live(conn: sqlite3.Connection, pr_number: int) -> bool:
    """Scalar wrapper, negative form."""
    return not pr_is_decided(conn, pr_number)


def proposal_is_decided(conn: sqlite3.Connection, post_id: int) -> bool:
    """Scalar wrapper for the post-scoped predicate."""
    sql = "SELECT " + proposal_decided_sql("?")
    row = conn.execute(sql, (post_id,) * 2).fetchone()
    return bool(row is not None and row[0])


def pr_state_as_of(conn: sqlite3.Connection) -> str | None:
    """The newest cache-arm evidence stamp: MAX(``pr_rows.verified_at``),
    falling back to the backfill watermark when no row carries a stamp.

    Rendered in ``check_in`` so a poller outage is visible: one shared
    predicate removes the accidental surface disagreement (check_in saying
    7 while ``list_proposals(view='review')`` said 5) that was previously
    the only stale-cache detector, and this replaces the accident with an
    instrument. ``None`` distinguishes *never backfilled* (the cache is
    unpopulated; readers fall back to live GitHub) from a timestamp, the
    same two-directional absence rule ``pr_cache_meta`` already keeps.
    """
    try:
        row = conn.execute("SELECT MAX(verified_at) FROM pr_rows").fetchone()
        if row is not None and row[0]:
            return row[0]
        from db._pr_rows import pr_rows_watermark

        return pr_rows_watermark(conn)
    except sqlite3.OperationalError:
        # domain: degrade-silently - the render is an instrument, never a
        # dependency: a deployment whose pr_rows predates the verified_at
        # migration (boot heal failed) must still get a check_in.
        return None
