"""server/pr_views.py — PR view helpers, extracted from server.py."""

from __future__ import annotations

import db
import github


async def _apply_pr_labels(
    pr_number: int,
    proposal_id: int,
    extra_labels: list[str] | None = None,
) -> None:
    """Set the initial GitHub labels on a newly opened PR.
    Always adds 'review-required' to every PR (the vote sweep
    processes small-fix PRs).  extra_labels, if provided, are added alongside."""
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
        await github.aset_pr_labels(pr_number, lbls)
    except Exception:
        pass  # label failure must not block PR creation


async def _pr_view(
    number: int, token: str | None, *, include_diff: bool = False
) -> dict:
    """One assembled pull-request view for repo_get_pr: GitHub state plus
    the forum's vote tally/threshold/eligibility, a human-readable ci_note,
    the proposal-hold note when the linked proposal's vote has not cleared,
    and the caller's own vote when a token is given.  When include_diff is
    True the full per-file diff (with patch text) is included as well."""
    result = await github.aget_pr(number)
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
