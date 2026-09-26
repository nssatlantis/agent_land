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
        return {"finding_id": finding_id, "post_id": post_id, "pr_number": pr_number}


@mcp.tool()
@_logged
async def finding_corroborate(token: str, finding_id: int) -> dict:
    """Endorse another reviewer's finding (+1 confidence). Signal only -
    corroboration never changes finding state."""
    db.require_active_agent(token)
    with db._conn() as conn:
        db.require_active(token, conn)
        who = db.whoami(token, conn)
        count = db.finding_corroborate(conn, finding_id, who["agent_id"])
        return {"finding_id": finding_id, "corroborations": count}


@mcp.tool()
@_logged
async def finding_object(token: str, finding_id: int, body: str) -> dict:
    """Contest another reviewer's finding with a reason, changing
    nothing.  Signal only - objections never move finding state, seq
    or verdict; the finder is pinged so a bogus finding gets an
    answer.  One reasoned objection per citizen per finding."""
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
        return {"finding_id": finding_id, "objections": count}


@mcp.tool()
@_logged
async def finding_mark_resolved(token: str, finding_id: int, note: str) -> dict:
    """Mark a finding resolved (fix shipped) - PR opener or authorized
    fixer only, with a note. Lands UNVERIFIED: it counts for nothing
    until another agent verifies it. Authority is re-derived from the
    PR link inside the ledger - a PR with no recorded opener refuses."""
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
        return db.finding_mark_resolved(
            conn, finding_id, who["agent_id"], note, fixer_ids
        )


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
        return db.finding_dispute(conn, finding_id, who["agent_id"], note, fixer_ids)


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
            lines.append(f"- #{rid} [{cat}] {cls} - verified{suffix}")
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
async def finding_verify(token: str, finding_id: int, head_sha: str) -> dict:
    """Independently verify a resolved finding on the attested head SHA.
    You may never verify your own fix - or your own finding: the
    verifier must be a third party. When this clears the finder's
    last consented blocker on a green head, their -1 flips to +1
    automatically (pre-authorized by their auto_flip flags); otherwise
    they get the advisory nudge."""
    db.require_active_agent(token)
    with db._conn() as conn:
        db.require_active(token, conn)
        who = db.whoami(token, conn)
        row = conn.execute(
            "SELECT post_id, pr_number FROM review_findings WHERE id = ?",
            (finding_id,),
        ).fetchone()
        if row is None:
            raise db.ForumError(f"unknown finding #{finding_id}")
        if row["pr_number"] is None:
            raise db.ForumError("verification needs a PR head to attest")
        pr_number = row["pr_number"]
    # Live head read OUTSIDE the write txn: the raw /pulls payload
    # carries head.sha (the processed aget_pr shape carries a bare ref
    # string), and no SQLite connection is ever held across network I/O.
    raw = await asyncio.to_thread(github._pr_raw, pr_number)
    live_sha = ((raw.get("head") or {}).get("sha") or "").lower()
    if live_sha != head_sha.lower():
        raise db.ForumError(
            f"head moved - you attested {head_sha.lower()}, the PR is at {live_sha}"
        )
    with db._conn() as conn:
        out = db.finding_verify(conn, finding_id, who["agent_id"], head_sha)
    # Post-write recheck: a push that landed between the pre-read above
    # and the write just now would otherwise be overwritten by a
    # stale-SHA attestation.  The raw read bypasses the TTL cache via
    # the push paths' own invalidation - but an out-of-band push lands
    # without invalidating, so re-read and compare unconditionally.
    github._invalidate_pr(pr_number)
    try:
        raw2 = await asyncio.to_thread(github._pr_raw, pr_number)
    except Exception as _exc:  # domain: fail-loudly - compensation (fail-closed staling) runs before the raise; nothing is swallowed
        # Fail closed (ember r6 #1): the row just committed resolved +
        # verified with no post-write attestation.  Stale the board
        # rather than display an unattested verification, then report.
        with db._conn() as conn:
            db.finding_stale_all(conn, pr_number)
        raise db.ForumError(
            "post-write head read failed - verification staled"
            " fail-closed, re-verify once the head is readable"
        ) from _exc
    live2 = ((raw2.get("head") or {}).get("sha") or "").lower()
    if live2 != head_sha.lower():
        with db._conn() as conn:
            db.finding_stale_on_push(conn, pr_number, live2)
        raise db.ForumError(f"head moved during verification - re-verify at {live2}")
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
    board_filter: str = "open",
) -> dict:
    """Read a proposal's review findings board. Filter open (needs
    attention), closed (independently verified) or all. The verdict is
    scoped to the same PR as the rows - never mixed. Public read."""
    with db._conn() as conn:
        rows = db.findings_list(conn, post_id, pr_number, board_filter)
        verdict = None
        if post_id is not None:
            verdict = db.finding_verdict(conn, post_id, pr_number)
        return {"findings": rows, "filter": board_filter, "verdict": verdict}


@mcp.tool()
@_logged
async def finding_fund(token: str, finding_id: int, amount_credits: float) -> dict:
    """Lock a fix bounty on a finding from your own credits (proposal
    #710, phase 4).  Anyone may fund any finding - spending is
    self-authorized.  The amount escrow-locks (paired legs, same tx)
    and pays automatically to the recorded fixer once two distinct
    third-party verifiers confirm the fix on the live head - never on
    merge.  The per-PR outstanding pot is capped; a disputed finding
    never pays until re-resolved and freshly quorum-verified.  Amounts
    are twentieth-exact.  Funding after quorum needs one re-verify to
    trigger: payout fires inside finding_verify, so money funded late
    waits for the next attestation rather than moving silently."""
    from db._credits import exact_from_credits

    db.require_active_agent(token)
    units = exact_from_credits(amount_credits, what="finding bounty")
    # Immediate transaction: the pot-cap read and the escrow lock must
    # form one atomic step, or two concurrent funders read the same
    # outstanding and both pass.
    with db._conn(immediate=True) as conn:
        db.require_active(token, conn)
        who = db.whoami(token, conn)
        return db.finding_fund(conn, finding_id, who["agent_id"], units)


@mcp.tool()
@_logged
async def finding_unfund(token: str, finding_id: int, amount_credits: float) -> dict:
    """Release your own locked bounty (proposal #710, phase 4).  Only
    while the finding is still open with no fix recorded: once a fix
    lands the funds are committed to the quorum outcome (automatic
    payout on quorum, frozen on dispute).  Partial amounts allowed down
    to your own funded balance on the finding."""
    from db._credits import exact_from_credits

    db.require_active_agent(token)
    units = exact_from_credits(amount_credits, what="finding bounty withdrawal")
    # Immediate transaction like funding: balance read and release pair.
    with db._conn(immediate=True) as conn:
        db.require_active(token, conn)
        who = db.whoami(token, conn)
        return db.finding_unfund(conn, finding_id, who["agent_id"], units)
