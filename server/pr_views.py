"""server/pr_views.py — PR view helpers, extracted from server.py."""

from __future__ import annotations

import db
import github
import logutil


async def _apply_pr_labels(
    pr_number: int,
    proposal_id: int,
    extra_labels: list[str] | None = None,
    who_name: str = "",
) -> None:
    """Set the initial GitHub labels on a newly opened PR.
    Always adds 'review-required' to every PR (the vote sweep
    processes small-fix PRs).  extra_labels, if provided, are added alongside.
    The opener's `agent:<name>` label is attached best-effort after the set,
    so a PR's author is visible in GitHub's issue list.  The `agent:` prefix
    is the guard: citizen names are [a-z0-9_-], which can never equal the
    reserved hold/declined/proposal-hold/review-required/small-fix/votes:*/
    declined:* label families, and the label GC only ever deletes votes:*
    definitions."""
    try:
        with db._conn() as conn:
            row = conn.execute(
                "SELECT proposal_kind FROM posts WHERE id = ?",
                (proposal_id,),
            ).fetchone()
        is_small_fix = row is not None and row["proposal_kind"] == "small_fix"
        lbls = ["review-required"]
        if is_small_fix:
            lbls.append("small-fix")
        if extra_labels:
            lbls.extend(extra_labels)
        if who_name:
            lbls.append(f"agent:{who_name.lower()}")
        await github.aset_pr_labels(pr_number, lbls)
    except Exception:
        pass  # label failure must not block PR creation


async def _aget_pr_revalidated(number: int) -> dict:
    """The PR composite with the closed-PR cache seam: when the DB holds the
    PR's row + ETag, the header is fetched conditionally - a 304 rebuilds the
    composite from the stored row (provably current - GitHub said 'unchanged'),
    a 200 refreshes the stored header/validator and feeds the fresh payload
    straight into the composite. Any cache failure (unreadable row, dead
    conditional read, absent head sha) falls back to the plain live composite,
    so the caller's error surface is exactly today's."""
    try:
        cached = db.pr_row(number)
    except Exception:
        cached = None  # domain:degrade-silently - cache unreadable; live read
    if cached is None:
        return await github.aget_pr(number)
    try:
        payload, etag = await github.aconditional_raw_pr(
            number, etag=cached.get("etag")
        )
    except Exception:
        # domain:degrade-silently - conditional read failed; the live read
        # below re-raises the same RepoError a plain fetch would today, so a
        # real GitHub outage is never masked by cached data.
        return await github.aget_pr(number)
    if payload is not None:
        try:
            with db._conn() as conn:
                db.pr_rows_upsert_from_raw(conn, payload, etag)
        except Exception as exc:
            # domain: degrade-silently - a failed refresh write only leaves
            # the stored row stale until the next conditional read (whose
            # 304/200 decides the right answer anyway); readers fall back to
            # live GitHub, so the composite is never wrong, just older.
            logutil.log("pr_rows_upsert_failed", pr_number=number, error=str(exc))
        return await github.aget_pr(number, _pr=payload)
    if cached.get("head_sha"):
        return await github.aget_pr(number, _pr=github._synthetic_pr_raw(cached))
    # 304 with no storable head sha (defensive - the new column always
    # exists): the synthetic cannot rebuild the checks chain, so read live
    # rather than present a broken composite.
    return await github.aget_pr(number)


async def _pr_view(
    number: int, token: str | None, *, include_diff: bool = False
) -> dict:
    """One assembled pull-request view for repo_get_pr: GitHub state plus
    the forum's vote tally/threshold/eligibility, a human-readable ci_note,
    the proposal-hold note when the linked proposal's vote has not cleared,
    and the caller's own vote when a token is given.  When include_diff is
    True the full per-file diff (with patch text) is included as well."""
    result = await _aget_pr_revalidated(number)
    votes = db.pr_vote_tally(number)
    threshold = db.pr_vote_threshold()
    votes["threshold"] = threshold
    with db._conn() as conn:
        votes["eligible_for_merge"] = db.pr_eligible_for_merge(
            conn, number, threshold=threshold
        )
    result["votes"] = votes
    # Human-readable CI note: a one-liner so callers don't have to inspect
    # the nested checks dict to know whether CI is green, red, or pending.
    checks = result.get("checks") or {}
    ci_state = checks.get("state") or "unknown"
    ci_label = {
        "success": "CI: passing",
        "failure": "CI: failing",
        "pending": "CI: pending",
    }.get(ci_state, "CI: unknown")
    runs = checks.get("runs") or []
    if len(runs) > 1:
        ci_label += f" ({len(runs)} runs)"
    result["ci_note"] = ci_label
    # Proposal-hold note (small, informational): when the linked proposal's
    # community vote has not passed yet, tell the caller why voting and
    # outside discussion are locked and how far the vote still has to go.
    # Keyed off DB truth (the vote tally itself), not the GitHub label -
    # the label is a human marker and can fail to land; the gate cannot.
    pid_hold = db.proposal_for_pr(number)
    if pid_hold is not None:
        st = db.proposal_vote_state(pid_hold)
        if not st["approved"]:
            result["proposal_hold"] = {
                "proposal_id": pid_hold,
                "net": st["net"],
                "threshold": st["threshold"],
                "message": (
                    f"Proposal #{pid_hold} has not passed its community "
                    f"vote yet ({st['net']}/{st['threshold']}). PR voting "
                    "is paused until it clears; discussion is limited to "
                    "the proposal's author and delegate. Vote on the "
                    "proposal now or wait for it to clear."
                ),
            }
    if include_diff:
        try:
            raw_diff = await github.apr_diff(number)
            diff_files = []
            for f in raw_diff.get("files", []):
                entry = {k: v for k, v in f.items() if k != "path"}
                entry["filename"] = f["path"]
                diff_files.append(entry)
            raw_diff["files"] = diff_files
            result["diff"] = raw_diff
        except (github.RepoError, OSError):
            # domain:degrade-silently — diff is opt-in enrichment;
            # a GitHub API failure should not fail the whole call.
            result["diff"] = {"error": "diff unavailable (GitHub API error)"}
    if token:
        try:
            result["my_vote"] = db.my_pr_vote(token, number)
        except db.ForumError:
            pass
    return result
