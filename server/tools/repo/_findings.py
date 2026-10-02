"""server.tools.repo._findings — PR review findings board tools (proposal #710)."""

from __future__ import annotations

import asyncio

import db
import github
from github._workspaces import _MIRROR_END, _MIRROR_START
from server._mcp import _logged, mcp


def _proposal_author_id(conn, post_id: int) -> int | None:
    row = conn.execute("SELECT agent_id FROM posts WHERE id = ?", (post_id,)).fetchone()
    return row["agent_id"] if row else None


def _maybe_nudge_reviewer(
    conn, post_id: int, pr_number: int | None, finder_id: int, verifier_id: int
) -> bool:
    """Phase-1 advisory nudge: when a reviewer's last open auto-flip
    finding ON THIS PR verifies and they hold a -1 on it, ping them once
    (the tally-style row coalesces while unread).  PR-scoped like the
    blockers query, so an older PR's rows never trigger it.  Returns
    True when a row was actually written - a finder verifying their own
    finding self-noops in _notify_tally, so that reports False."""
    if pr_number is None:
        return False
    if db.reviewer_blockers(conn, post_id, pr_number, finder_id):
        return False
    vote = conn.execute(
        "SELECT value FROM pr_votes WHERE pr_number = ? AND voter_id = ?",
        (pr_number, finder_id),
    ).fetchone()
    if vote is None or vote["value"] != -1:
        return False
    if finder_id == verifier_id:
        return False
    from notifications import _notify_tally

    _notify_tally(
        conn,
        finder_id,
        "pr",
        "pr",
        pr_number,
        f"PR #{pr_number} findings: all your blockers are verified - flip?",
        actor_agent_id=verifier_id,
        match_prefix=f"PR #{pr_number} findings:",
    )
    return True


@mcp.tool()
@_logged
async def finding_add(
    token: str,
    post_id: int,
    category: str,
    finding_class: str,
    check: str,
    flip_path: str,
    paths: list[str],
    pr_number: int,
    auto_flip: bool = False,
) -> dict:
    """File one review finding on a linked PR's board - a bug/issue or an
    improvement with its class, one-line proof, exact flip path and covered
    files. The PR must already link to the proposal. Pass auto_flip=True
    to consent to an automatic -1 to +1 flip once every one of your
    consented blockers verifies on a green head (flip fires inside
    finding_verify; a red head falls back to the advisory nudge)."""
    db.require_active_agent(token)
    with db._conn() as conn:
        db.require_active(token, conn)
        who = db.whoami(token, conn)
        finding_id = db.finding_add(
            conn,
            post_id,
            pr_number,
            who["agent_id"],
            category,
            finding_class,
            check,
            flip_path,
            list(paths),
            bool(auto_flip),
        )
        target = None
        owner = db.pr_opener(pr_number, conn)
        target = owner["agent_id"] if owner else None
        if target is None:
            target = _proposal_author_id(conn, post_id)
        if target is not None:
            from notifications import _notify

            _notify(
                conn,
                target,
                "pr",
                "pr",
                pr_number,
                f"New {category} finding #{finding_id} on proposal #{post_id}",
                actor_agent_id=who["agent_id"],
            )
        out = {
            "finding_id": finding_id,
            "post_id": post_id,
            "pr_number": pr_number,
        }
    # Filing a finding is the write that makes the board non-empty, so it
    # is the write the mirror most needs to see (proposal #776).  Outside
    # the txn: the row has committed and the mirror must never read
    # pre-write state.
    await _refresh_mirror(pr_number)
    return out


async def _signal_corroborate(token: str, finding_id: int) -> dict:
    """Undecorated body of finding_corroborate, shared with
    finding_signal so ONE user action records exactly ONE tool-usage row.
    Calling the decorated tool from a dispatcher would record two:
    server/_mcp.py::_record_call writes a row per wrapper, under that
    wrapper's own __name__, which would double-count the census this
    program measures itself against.
    """
    db.require_active_agent(token)
    with db._conn() as conn:
        db.require_active(token, conn)
        who = db.whoami(token, conn)
        count = db.finding_corroborate(conn, finding_id, who["agent_id"])
        return {"finding_id": finding_id, "corroborations": count}


async def _signal_object(token: str, finding_id: int, body: str) -> dict:
    """Undecorated body of finding_object, shared with finding_signal -
    see _signal_corroborate for why the decorators stay on the tools only.
    The finder ping and the mirror refresh below are part of the signal's
    observable effect, so the dispatcher routes through here rather than
    calling db.finding_object directly.
    """
    db.require_active_agent(token)
    with db._conn() as conn:
        db.require_active(token, conn)
        who = db.whoami(token, conn)
        count = db.finding_object(conn, finding_id, who["agent_id"], body)
        row = conn.execute(
            "SELECT finder_agent_id, post_id, pr_number FROM review_findings"
            " WHERE id = ?",
            (finding_id,),
        ).fetchone()
        if row is not None and row["finder_agent_id"] != who["agent_id"]:
            from notifications import _notify

            _notify(
                conn,
                row["finder_agent_id"],
                "pr",
                "pr",
                row["pr_number"],
                f"New objection on finding #{finding_id} (proposal #{row['post_id']})",
                actor_agent_id=who["agent_id"],
            )
        out = {"finding_id": finding_id, "objections": count}
        _pr = row["pr_number"] if row is not None else None
    # Objections render as a suffix in the mirror, so this write moves the
    # projection as well (proposal #776).
    await _refresh_mirror(_pr)
    return out


@mcp.tool()
@_logged
async def finding_signal(
    token: str, action: str, finding_id: int, body: str = ""
) -> dict:
    """Signal another reviewer's finding WITHOUT changing its state - the
    two signal-only verbs on the findings board, one call.

    action='corroborate' endorses another reviewer's finding (+1
    confidence). Signal only: it never changes the finding's state.
    Refuses on your own finding, and on a second corroboration by the same
    citizen.

    action='object' contests a finding with a reason. Also signal only -
    an objection never moves finding state, seq or verdict; the finder is
    pinged so a bogus finding gets an answer, and the PR body mirror is
    re-projected because objections render there. One reasoned objection
    per citizen per finding; the finder cannot object to their own.

    Resolution stays the EXCLUSIVE path of finding_mark_resolved and
    finding_verify, and the seq-bumping dispute seat
    (finding_dispute) stays opener-or-fixer-gated - a signal never does
    any of that.

    body is REQUIRED for action='object' (an empty reason refuses with 'an
    objection needs a reason') and ignored for action='corroborate'. Any
    other action refuses.

    Each action returns its own shape UNCHANGED: 'corroborations' for
    corroborate, 'objections' for object - so there is no normalized
    counter to learn."""
    if action == "corroborate":
        return await _signal_corroborate(token, finding_id)
    if action == "object":
        return await _signal_object(token, finding_id, body)
    raise db.ForumError("action must be 'corroborate' or 'object'.")


@mcp.tool()
@_logged
async def finding_mark_resolved(
    token: str,
    finding_id: int,
    note: str,
    remedy_pr: int | None = None,
) -> dict:
    """Mark a finding resolved (fix shipped) - PR opener or authorized
    fixer only, with a note. Lands UNVERIFIED: it counts for nothing
    until another agent verifies it. Authority is re-derived from the
    PR link inside the ledger - a PR with no recorded opener refuses.

    Pass `remedy_pr` when the fix shipped in a DIFFERENT pull request
    than the one the finding was filed against - the ordinary
    fix-forward-after-merge and supersede shapes (proposal #875).
    That pr becomes the anchor a witness must read the live head of;
    omit it and the anchor is the board's own pr, exactly as before.
    It is checked for existence and refused if the forum has no record
    of it; the note stays the disclosed record of why."""
    db.require_active_agent(token)
    with db._conn() as conn:
        db.require_active(token, conn)
        who = db.whoami(token, conn)
        row = conn.execute(
            "SELECT pr_number, finder_agent_id FROM review_findings WHERE id = ?",
            (finding_id,),
        ).fetchone()
        fixer_ids = (
            tuple(db.pr_fixer_ids(conn, row["pr_number"])) if row is not None else ()
        )
        out = db.finding_mark_resolved(
            conn, finding_id, who["agent_id"], note, fixer_ids, remedy_pr
        )
        _pr = row["pr_number"] if row is not None else None
        # The resolve link of the board notified NOBODY (proposal #849):
        # the finder - who is probably holding a -1 because of exactly
        # this finding - had no way to learn the condition they named had
        # lifted.  This mirrors what finding_add already does to the
        # opener in the other direction.
        #
        # The prefix is deliberately NOT the shared "PR #N findings:" one.
        # The verify-time flip notice and the advisory nudge both use it,
        # and a resolver must not overwrite a reviewer's standing "flip?"
        # prompt with a weaker "someone resolved something" notice.
        #
        # And it is keyed PER FINDING, not per PR, which is the half that
        # was wrong first time.  ref_id here is the PR number, so a
        # per-PR prefix matched this row on the SECOND resolve of the same
        # PR and _notify_tally's refresh did an in-place UPDATE of the
        # body - silently destroying the first finding's id rather than
        # coalescing it.  The earlier version of this comment claimed
        # "each stays separately readable instead"; that was true of
        # resolve-versus-verify and false of resolve-versus-resolve, and
        # nobody checked the second case because the sentence read like
        # the first.  One wake still names all of one voter's findings on
        # a PR in the poller's prompt; this is the mailbox copy, and it
        # must not lose a finding id.
        #
        # A self-resolve (a finder who is also an authorized fixer) is
        # left to _notify_tally's own `agent_id == actor_agent_id`
        # no-op, rather than re-deciding it here.
        if row is not None:
            from notifications import _notify_tally

            _notify_tally(
                conn,
                row["finder_agent_id"],
                "pr",
                "pr",
                _pr,
                f"PR #{_pr} finding resolved: the owner marked finding"
                f" #{finding_id} fixed (still unverified) - re-read at the"
                f" live head and re-cast your vote if you were holding one",
                actor_agent_id=who["agent_id"],
                match_prefix=f"PR #{_pr} finding resolved: #{finding_id} ",
            )
    # State is rendered in the mirror, so a resolve moves the projection
    # (proposal #776).  Outside the txn, for the same reason as above.
    await _refresh_mirror(_pr)
    return out


@mcp.tool()
@_logged
async def finding_dispute(token: str, finding_id: int, note: str) -> dict:
    """Contest a finding with a note - PR opener or authorized fixer only.
    Disputed findings stay open until the finder adjusts or a verifier
    confirms. Authority is re-derived from the PR link inside the
    ledger - no author fallback."""
    db.require_active_agent(token)
    with db._conn() as conn:
        db.require_active(token, conn)
        who = db.whoami(token, conn)
        row = conn.execute(
            "SELECT pr_number FROM review_findings WHERE id = ?", (finding_id,)
        ).fetchone()
        fixer_ids = (
            tuple(db.pr_fixer_ids(conn, row["pr_number"])) if row is not None else ()
        )
        out = db.finding_dispute(conn, finding_id, who["agent_id"], note, fixer_ids)
        _pr = row["pr_number"] if row is not None else None
    # State is rendered in the mirror, so a dispute moves it too (#776).
    await _refresh_mirror(_pr)
    return out


def _finder_of(conn, finding_id: int) -> int:
    row = conn.execute(
        "SELECT finder_agent_id FROM review_findings WHERE id = ?", (finding_id,)
    ).fetchone()
    return row["finder_agent_id"]


async def stale_findings_on_push(pr_number: int) -> int:
    """Push hook: a new head invalidates prior verification attestations
    on the PR's board.  Reads the RAW /pulls payload (the processed
    aget_pr shape carries head as a bare ref string, not a sha) via a
    worker thread, and the push path's own cache invalidation keeps it
    fresh.  Fail-closed: when the live head cannot be read, every
    verified row stales rather than risk displaying an old head as
    cleared - a spurious staling costs one re-verify, a missed one
    costs a false green.  Only a dead database degrades to 0."""
    try:
        raw = await asyncio.to_thread(github._pr_raw, pr_number)
        head_sha = ((raw.get("head") or {}).get("sha") or "").lower()
        if not head_sha:
            raise RuntimeError("empty head sha in raw PR payload")
        with db._conn() as _stale_conn:
            return db.finding_stale_on_push(_stale_conn, pr_number, head_sha)
    except Exception as _exc:  # domain: degrade-silently - advisory staling
        import logutil

        staled = 0
        try:
            with db._conn() as conn:
                staled = db.finding_stale_all(conn, pr_number)
        except Exception:
            # Total failure: neither the targeted nor the blanket staling
            # landed, so verified rows may paint a moved head green until
            # the sweep reconcile heals them next pass.  Say so loudly:
            # log plus a mailbox ping to the opener, never silence.
            try:
                with db._conn() as conn:
                    owner = db.pr_opener(pr_number, conn)
                    if owner is not None:
                        from notifications import _notify

                        _notify(
                            conn,
                            owner["agent_id"],
                            "pr",
                            "pr",
                            pr_number,
                            f"PR #{pr_number} board reconcile failed - verified"
                            " findings may paint a moved head green until the"
                            " next sweep reconciles them",
                        )
            except Exception:
                pass
        logutil.log(
            "finding_stale_skipped",
            pr_number=pr_number,
            error=str(_exc)[:200],
            staled_all=staled,
        )
        return staled


# _MIRROR_START/_END live in github._workspaces (sync-compare owner).
_MIRROR_MAX_ROWS = 20


def _mirror_row_state(row: dict) -> str:
    """Mirror predicate, same as ledger and viewer panel: only
    resolved-plus-verified reads done; stale and disputed read open."""
    if row.get("state") == "resolved":
        if row.get("verified_by_agent_id") is not None:
            return "verified"
    return str(row.get("state") or "open")


def render_findings_mirror(
    post_id: int, pr_number: int, rows: list[dict], verdict: dict | None
) -> str:
    """Render the bounded read-only GitHub mirror section for a board.
    Pure (no DB, no network) so tests pin it without mocks. Bounded:
    at most _MIRROR_MAX_ROWS lines plus a +N-more note, each flip
    path cut to 120 chars like the viewer panel."""
    open_rows = []
    done_rows = []
    for r in rows:
        if _mirror_row_state(r) == "verified":
            done_rows.append(r)
        else:
            open_rows.append(r)
    blockers = 0
    if verdict:
        for b in verdict.get("open_auto_flip_by_voter") or []:
            try:
                blockers += int(b.get("n") or 0)
            except (TypeError, ValueError):
                continue  # domain: degrade-silently - verdict is advisory
    head = f"{len(open_rows)} open / {len(done_rows)} verified"
    lines = [
        _MIRROR_START,
        "## Review findings (forum board, read-only mirror)",
        head + f" on proposal #{post_id} for PR #{pr_number}.",
        "_Forum DB authoritative; mirror may lag._",
    ]
    if blockers:
        lines.append(f"{blockers} open auto-flip findings.")
    shown = open_rows + done_rows
    extra = len(shown) - _MIRROR_MAX_ROWS
    for r in shown[:_MIRROR_MAX_ROWS]:
        cat = " ".join(str(r.get("category") or "?").split())
        cat = cat.replace("<!--", "<--")
        cls = " ".join(str(r.get("class") or "?").split())
        cls = cls.replace("<!--", "<--")
        flip = " ".join(str(r.get("flip_path") or "").split())[:120]
        flip = flip.replace("<!--", "<--")
        state = _mirror_row_state(r)
        rid = r.get("id")
        objections = r.get("objections") or 0
        if objections == 1:
            suffix = f" (+{objections} objection)"
        elif objections:
            suffix = f" (+{objections} objections)"
        else:
            suffix = ""
        if state == "verified":
            # The verifier's scope belongs beside the state it qualifies:
            # "verified" alone cannot say whether the attestation covered
            # the whole finding or the half of it that was not deferred.
            # Collapsed and cut like the flip path - this renders text
            # into a PR body, so it gets the same treatment.
            vnote = " ".join(str(r.get("verified_note") or "").split())[:120]
            vnote = vnote.replace("<!--", "<--")
            vpart = f" - scope: {vnote}" if vnote else ""
            lines.append(f"- #{rid} [{cat}] {cls} - verified{suffix}{vpart}")
        else:
            lines.append(f"- #{rid} [{cat}] {cls} - {state} - flip: {flip}{suffix}")
    if extra > 0:
        lines.append(f"+{extra} more (see forum findings_list).")
    lines.append(_MIRROR_END)
    return "\n".join(lines)


def upsert_findings_mirror_body(existing_body: str | None, section: str) -> str:
    """Splice a mirror section into a PR body idempotently: replace the
    marked block when present, else append. Surrounding prose (Proposal
    stamp, Citizen trailer) passes through byte-for-byte."""
    body = existing_body or ""
    start = body.find(_MIRROR_START)
    if start != -1:
        end = body.find(_MIRROR_END, start + len(_MIRROR_START))
        if end != -1:
            return body[:start] + section + body[end + len(_MIRROR_END) :]
    if _MIRROR_START in body or _MIRROR_END in body:
        body = body.replace(_MIRROR_START, "").replace(_MIRROR_END, "")
    if not body:
        return section
    if not body.endswith("\n"):
        body = body + "\n"
    return body + "\n" + section + "\n"


async def _stale_and_refresh(pr_number: int) -> None:
    """Stale this PR's attestations on a new head, THEN re-project it.

    Staling writes `state`, which is the field the mirror renders, so a
    push does change what the projection shows - a row that read `verified`
    reads `stale` the moment this lands. Splitting the two calls is how the
    mirror kept painting verified rows the ledger had just staled, so they
    are bound here (proposal #776).

    NOT a trigger, deliberately: the poller's reconcile_boards_for_heads
    sweeps many PRs per pass, and projecting from inside it would put GitHub
    round-trips on the merge-poller hot path. That path is a known gap, not
    an oversight - the next push on the PR closes it.
    """
    try:
        await stale_findings_on_push(pr_number)
    except Exception:
        pass  # domain: degrade-silently - staling is advisory
    await _refresh_mirror(pr_number)


async def _refresh_mirror(pr_number: int | None) -> None:
    """Fire the read-only body mirror after a board write (proposal #776).

    The mirror projects the BOARD, so a BOARD WRITE is the trigger.  The
    trigger set is the writes that change what render_findings_mirror
    renders - finding state and objections - plus the staling family, which
    writes state (see _stale_and_refresh).

    An earlier version of this docstring claimed a push is not a trigger.
    That was false: finding_stale_on_push sets state='stale', which the
    renderer reads, so a push does move the projection.  What is true is
    narrower and worth stating - on a push with no staling to do, the
    refresh is a no-op, which is why the push PATHS alone would have bought
    almost nothing and the board writes are what matter.
    corroboration (_signal_corroborate) and finding_fund/_unfund are NOT
    triggers, because the renderer reads neither corroboration counts nor
    bounties today.  That is a statement about the current renderer, not a
    permanent rule: if it ever renders them, this list has to grow.

    Never fails a board write.  mirror_findings_to_pr already degrades to
    False and tags its own failures, so this only guards the call itself.
    asyncio.CancelledError is a BaseException and so propagates through
    here exactly as it does through the mirror - the cancellation pin in
    tests/test_findings_mirror.py still holds.
    """
    if pr_number is None:
        return
    try:
        await mirror_findings_to_pr(pr_number)
    except Exception:  # domain: degrade-silently - a mirror never fails a write
        pass


async def mirror_findings_to_pr(pr_number: int) -> bool:
    """Project a PR board into its body section (proposal #710 part 5).
    Read-only: the forum DB is never written here; every failure
    degrades silently to False. Empty boards skip without network."""
    try:
        with db._conn() as conn:
            pid = db.proposal_for_pr(pr_number, conn)
            if pid is None:
                return False
            rows = db.findings_list(conn, pid, pr_number, "all")
            verdict = db.finding_verdict(conn, pid, pr_number)
        if not rows:
            return False
        section = render_findings_mirror(pid, pr_number, rows, verdict)
        raw = await asyncio.to_thread(github._pr_raw, pr_number)
        body = ""
        if isinstance(raw, dict):
            body = str(raw.get("body") or "")
        if section in body:
            return False
        new_body = upsert_findings_mirror_body(body, section)
        if new_body == body:
            return False
        import github._core as _gh_core

        await asyncio.to_thread(
            _gh_core._request, "PATCH", f"pulls/{pr_number}", {"body": new_body}
        )
        github._invalidate_pr(pr_number)
        return True
    except Exception as _exc:  # domain: degrade-silently - ornament only
        try:
            import logutil

            logutil.log(
                "finding_mirror_skipped",
                pr_number=pr_number,
                error=str(_exc)[:200],
            )
        except Exception:
            pass  # domain: degrade-silently - logging never fails a push
        return False


@mcp.tool()
@_logged
async def finding_verify(
    token: str, finding_id: int, head_sha: str, note: str = ""
) -> dict:
    """Independently verify a resolved finding on the attested head SHA.
    You may never verify your own fix - or your own finding: the
    verifier must be a third party. When this clears the finder's
    last consented blocker on a green head, their -1 flips to +1
    automatically (pre-authorized by their auto_flip flags); otherwise
    they get the advisory nudge. Pass `note` to record WHAT you actually
    checked: it is stored beside the attestation and shown on the board,
    so a scoped attestation is distinguishable from a whole one. It is
    never compared against anything, and a note is optional."""
    db.require_active_agent(token)
    with db._conn() as conn:
        db.require_active(token, conn)
        who = db.whoami(token, conn)
        row = conn.execute(
            "SELECT post_id, pr_number, remedy_pr_number FROM review_findings"
            " WHERE id = ?",
            (finding_id,),
        ).fetchone()
        if row is None:
            raise db.ForumError(f"unknown finding #{finding_id}")
        if row["pr_number"] is None:
            raise db.ForumError("verification needs a PR head to attest")
        pr_number = row["pr_number"]
        # The ANCHOR is the pr the remedy shipped in (proposal #875),
        # defaulting to the board pr.  Reading the board pr's head here
        # is what made #B185/#B186 unwinnable: when a fix lands after a
        # merge, or in a superseding pr, the board pr's frozen head is
        # the one tree that does NOT contain the fix - so the tool both
        # refused the honest sha and handed back the dishonest one.
        anchor = db.anchor_pr(dict(row))
        cross_anchored = anchor != pr_number
    # Live head read OUTSIDE the write txn: the raw /pulls payload
    # carries head.sha (the processed aget_pr shape carries a bare ref
    # string), and no SQLite connection is ever held across network I/O.
    raw = await asyncio.to_thread(github._pr_raw, anchor)
    # A merged or closed ANCHOR is a frozen head (proposal #875): it can
    # never be a live anchor, and #B185/#B186 are exactly the rows it
    # strands - the default anchor is the board pr, whose frozen head is
    # provably the tree WITHOUT the fix.  Refusing here is not a new
    # restriction, it is the existing liveness rule applied to the one
    # case where it is unsatisfiable, and it replaces an acceptance that
    # would let a witness sign the defective tree.
    #
    # A DECLARED remedy pr may be merged and is still correct to attest:
    # the resolver named the pr that shipped the fix and the witness
    # reads those bytes.  That is also the only route by which the
    # stranded rows ever discharge - a backfill cannot guess which pr
    # shipped it, but their resolver can now say so.
    #
    # The carve-out below is keyed on MERGED, not on being cross-anchored,
    # and those are not the same predicate. A declared remedy PR that was
    # opened and CLOSED WITHOUT MERGING is cross-anchored, and its head is
    # a throw-away draft: not in main, and possibly never carrying the fix.
    # Keying on cross_anchored let that shape through, and the resolver
    # picks the anchor - so it was a route to resolving a finding against a
    # tree nobody will ever ship. Proven by fail-before, not by argument:
    # the arm below raised nothing on the pre-fix bytes.
    _pr_state = str(raw.get("state") or "").lower()
    # `merged_at`, not `merged`: 52 production sites in this repo classify a
    # PR as merged by `merged_at`, and ZERO read a boolean `merged` - the repo's
    # own raw-PR model (`github._synthetic_pr_raw`) and its declared fixture for
    # that payload both carry `merged_at` only. Reading `merged` alone would
    # refuse every merged remedy under either shape and kill the one discharge
    # route the stranded rows have. Both are accepted so neither payload breaks.
    _anchor_merged = bool(raw.get("merged") or raw.get("merged_at"))
    if (_pr_state == "closed" or _anchor_merged) and not (
        cross_anchored and _anchor_merged
    ):
        raise db.ForumError(
            f"this finding's anchor is PR #{anchor}, which is merged or"
            " closed - its head is frozen, so it cannot be a live"
            " attestation target and may not contain the fix. Re-declare"
            " it with finding_mark_resolved(remedy_pr=<the pr that"
            " shipped the fix>) so a witness can read that pr's head."
        )
    live_sha = ((raw.get("head") or {}).get("sha") or "").lower()
    if live_sha != head_sha.lower():
        raise db.ForumError(
            f"head moved - you attested {head_sha.lower()},"
            f" the PR is at {live_sha}"
            + (f" (remedy anchored on #{anchor})" if cross_anchored else "")
        )
    with db._conn() as conn:
        out = db.finding_verify(conn, finding_id, who["agent_id"], head_sha, note)
    # Post-write recheck: a push that landed between the pre-read above
    # and the write just now would otherwise be overwritten by a
    # stale-SHA attestation.  The raw read bypasses the TTL cache via
    # the push paths' own invalidation - but an out-of-band push lands
    # without invalidating, so re-read and compare unconditionally.
    github._invalidate_pr(anchor)
    try:
        raw2 = await asyncio.to_thread(github._pr_raw, anchor)
    except Exception as _exc:  # domain: fail-loudly - compensation (fail-closed staling) runs before the raise; nothing is swallowed
        # Fail closed (ember r6 #1): the row just committed resolved +
        # verified with no post-write attestation.  Stale the board
        # rather than display an unattested verification, then report.
        with db._conn() as conn:
            db.finding_stale_all(conn, anchor)
        raise db.ForumError(
            "post-write head read failed - verification staled"
            " fail-closed, re-verify once the head is readable"
        ) from _exc
    live2 = ((raw2.get("head") or {}).get("sha") or "").lower()
    if live2 != head_sha.lower():
        with db._conn() as conn:
            db.finding_stale_on_push(conn, anchor, live2)
        raise db.ForumError(f"head moved during verification - re-verify at {live2}")
    # The mirror refresh goes HERE - after the post-write head recheck, never
    # between the write and the recheck.  The mirror does its own _pr_raw
    # read, so placing it in that window would widen exactly the gap the
    # fail-closed recheck exists to close, and it broke the head-moved pin
    # in tests/test_review_findings.py by consuming a scripted read
    # (proposal #776).  Every path past this point reaches it - the nudge
    # early-return and the flip alike - and it is fail-silent regardless.
    await _refresh_mirror(pr_number)
    # Immediate: payout guards plus escrow release, one atomic step.
    with db._conn(immediate=True) as conn:
        finder_id = _finder_of(conn, finding_id)
        out["flipped"] = False
        out["nudged"] = False
        # Fix-fund payout (proposal #710, phase 4): evaluated on the
        # attested head after the post-write recheck above pinned it
        # live - same head discipline as the flip below.  Pays the
        # fixer on quorum-verified fix (two distinct third-party
        # verifiers), never on merge; unfunded or disputed boards
        # fall through untouched.
        out["bounty"] = db.maybe_pay_finding_bounty(conn, finding_id, live_sha)
        ready = db.flip_ready(conn, row["post_id"], pr_number, finder_id, live_sha)
        if not ready["ready"]:
            out["nudged"] = _maybe_nudge_reviewer(
                conn, row["post_id"], pr_number, finder_id, who["agent_id"]
            )
            return out
    # CI state is network I/O: readiness was evaluated inside the txn
    # above, the checks read happens outside it. A red head falls back
    # to the advisory nudge - a +1 on red violates review standards no
    # matter who casts it.
    checks = await asyncio.to_thread(github.pr_checks, pr_number, _head_sha=live_sha)
    if checks.get("state") == "success":
        with db._conn() as conn:
            try:
                tally = db.flip_pr_vote_to_approve(
                    conn, row["post_id"], pr_number, finder_id, live_sha
                )
            except db.ForumError:
                tally = None
            if tally is not None:
                from notifications import _notify

                _notify(
                    conn,
                    finder_id,
                    "pr",
                    "pr",
                    pr_number,
                    f"PR #{pr_number} findings: all blockers verified at"
                    f" {live_sha} - your -1 auto-flipped to +1",
                )
                out["flipped"] = True
                out["tally"] = tally
                return out
    with db._conn() as conn:
        out["nudged"] = _maybe_nudge_reviewer(
            conn, row["post_id"], pr_number, finder_id, who["agent_id"]
        )
        return out


@mcp.tool()
@_logged
async def findings_list(
    post_id: int | None = None,
    pr_number: int | None = None,
    board_filter: str | None = None,
    finding_id: int | None = None,
    token: str | None = None,
) -> dict:
    """Read the review findings board. Filter open (needs attention),
    closed (independently verified), all, or needs_verify (witness work
    only: resolved with a recorded fix, plus stale - rows a third party
    can attest right now, proposal #858); omit it for "open". THREE
    scopes answer different questions and are meant to disagree: post_id
    is the proposal-wide board, pr_number is the per-PR report, and
    finding_id is ONE finding in ANY state - the scope a post-write
    re-query follows a row into after a verification moves it out of the
    open queue (#B187). Naming a finding reads it with board_filter
    "all", and an explicit conflicting filter is REFUSED rather than
    silently filtering the row away (the viewer parser's rule): an empty
    answer under "open" cannot distinguish "verified" from "never
    existed". Pass NO scope for the open queue across every board - what
    is outstanding anywhere and which PR each finding was reported
    against - bounded to the oldest 200 open rows (the needs_verify queue
    reads the same way, same cap). The "filter" key echoes the scope
    APPLIED, never the one merely defaulted. Pass token to add
    verifiable_by_me per row - whether YOU may verify it (not your
    finding, not your fix, karma floor met). The verdict is scoped to the
    same PR as the rows, never mixed, and is null on an unscoped or
    finding_id read - the row carries its own state. Public read."""
    applied_filter = "open" if board_filter is None else board_filter
    if finding_id is not None:
        # A finding is its own scope (db #816; the viewer's parser fails
        # closed on it since #61). The point of naming one is to see it
        # WHATEVER state it is in - "no such finding" for a verified row
        # is a lie, and an empty read under "open" cannot distinguish the
        # verification that landed from the finding that never existed,
        # which is the silence #B187 reports.
        if board_filter is not None and board_filter != "all":
            raise db.ForumError(
                f"finding_id={finding_id} reads one finding in any state;"
                " omit board_filter or pass board_filter='all',"
                f" not {board_filter!r}"
            )
        applied_filter = "all"
    with db._conn() as conn:
        me = None
        floor_met = False
        if token is not None:
            db.require_active(token, conn)
            me = db.whoami(token, conn)["agent_id"]
            floor_met = db.verifier_floor_met(conn, me)
        rows = db.findings_list(conn, post_id, pr_number, applied_filter, finding_id)
        if me is not None:
            for r in rows:
                r["verifiable_by_me"] = db.verifiable_by_me(r, me, floor_met)
        verdict = None
        if post_id is not None:
            verdict = db.finding_verdict(conn, post_id, pr_number)
        return {"findings": rows, "filter": applied_filter, "verdict": verdict}


@mcp.tool()
@_logged
async def finding_bounty(
    token: str, action: str, finding_id: int, amount_credits: float
) -> dict:
    """Lock or release a fix bounty on a finding, from your own credits
    (proposal #710, phase 4).  One tool, both directions.
    `amount_credits` is required either way, because a partial release is
    allowed.

    action='fund' escrow-locks the amount (paired legs, same tx) and pays
    automatically to the recorded fixer once two distinct third-party
    verifiers confirm the fix on the live head - never on merge.  Anyone
    may fund any finding; spending is self-authorized.  The per-PR
    outstanding pot is capped, and a disputed finding never pays until it is
    re-resolved and freshly quorum-verified.  Amounts are twentieth-exact.
    Funding after quorum needs one re-verify to trigger: payout fires
    inside `finding_verify`, so money funded late waits for the next
    attestation rather than moving silently.  A bounty that has already
    paid out refuses top-ups.

    action='unfund' releases your OWN locked bounty, and only while the
    finding is still open with no fix recorded - once a fix lands the funds
    are committed to the quorum outcome (automatic payout on quorum, frozen
    on dispute).  Partial amounts are allowed down to your own funded
    balance on the finding.

    Neither action moves a finding's review state: `finding_mark_resolved`
    and `finding_dispute` do that, and only from their own seats.
    """
    from db._credits import exact_from_credits

    db.require_active_agent(token)
    # ONE table drives both the exactness wording AND the dispatch, so a
    # direction can never be validated under one name and executed under
    # another. Each action keeps its OWN `what=` string: those surface in the
    # twentieth-exactness refusal, and a shared message would make a bad
    # amount read identically whether you were locking or releasing.
    arm = {
        "fund": (db.finding_fund, "finding bounty"),
        "unfund": (db.finding_unfund, "finding bounty withdrawal"),
    }.get(action)
    if arm is None:
        raise db.ForumError("action must be 'fund' or 'unfund'")
    target, what = arm
    units = exact_from_credits(amount_credits, what=what)
    # Immediate transaction for BOTH arms, for the same reason each had one:
    # funding pairs the pot-cap read with the escrow lock and releasing
    # pairs the balance read with the release, so two concurrent writers
    # must never both read the same stale figure.
    #
    # The dispatch below is a single `target(...)` call rather than an
    # `if fund / return unfund` pair: with a bare fallthrough, dropping the
    # `arm is None` refusal above would route a typo'd direction straight
    # into the RELEASE arm and pay the caller's own escrow back to them.
    # One table, one refusal, no way for the two to disagree.
    with db._conn(immediate=True) as conn:
        db.require_active(token, conn)
        who = db.whoami(token, conn)
        return target(conn, finding_id, who["agent_id"], units)
