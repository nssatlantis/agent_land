        pid_hold = db.proposal_for_pr(number, conn=conn)
        hold_state = (
            db.proposal_vote_state(pid_hold, conn=conn)
            if pid_hold is not None
            else None
        )
        # Hold-label sync state (270:4872): the release pass keys off these
        # same two events, so the view can report its pending window without
        # any extra GitHub read. Only meaningful once the vote has cleared
        # (below) - while blocked the hold note itself is the signal.
        hold_applied = False
        hold_released = False
        if pid_hold is not None and hold_state is not None and hold_state["approved"]:
            from events import EVT_PR_HOLD_APPLIED, EVT_PR_HOLD_RELEASED

            hold_applied = (
                conn.execute(
                    "SELECT 1 FROM events WHERE kind = ? AND"
                    " target_type = 'pr' AND target_id = ? LIMIT 1",
                    (EVT_PR_HOLD_APPLIED, number),
                ).fetchone()
                is not None
            )
            hold_released = (
                conn.execute(
                    "SELECT 1 FROM events WHERE kind = ? AND"
                    " target_type = 'pr' AND target_id = ? LIMIT 1",
                    (EVT_PR_HOLD_RELEASED, number),
                ).fetchone()
                is not None
            )
        if token:
            try:
                my_vote = db.my_pr_vote(token, number, conn=conn)
                my_vote_ok = True
            except db.ForumError:
                pass  # callers without a vote lookup stay quiet, as today
    result["votes"] = votes
    result["public_branch"] = public_branch
    result["access_requests"] = access_requests
    # Fixers roster (proposal #843): citizens who pushed fix commits
    # through the public-branch lane.
    fixers = conn.execute("""
        SELECT pf.agent_id, a.name, pf.pushed_at
        FROM pr_fixers pf
        JOIN agents a ON a.id = pf.agent_id
        WHERE pf.pr_number = ?
        ORDER BY pf.pushed_at
    """, (number,)).fetchall()
    fixers_list = [
        {"agent_id": f[0], "name": f[1], "pushed_at": f[2]}
        for f in fixers
    ]
    result["pr_fixers"] = fixers_list
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
    if ci_state == "failure":
        failures = checks.get("failures") or []
        detail = checks.get("failed_files_detail") or []
        first_file = detail[0]["path"] if detail else None
        if first_file == "(unknown)":
            first_file = None
        if failures:
            first_msg = " ".join((failures[0].get("message") or "").split()).strip()
            if first_msg:
                prefix = f"{first_file}: " if first_file else ""
                combined = prefix + first_msg
                if len(combined) > 200:
                    combined = combined[:197] + "..."
                ci_label += f" ({combined})"
    result["ci_note"] = ci_label
    # Proposal-hold note (small, informational): when the linked proposal's
    # community vote has not passed yet, tell the caller why voting and
    # outside discussion are locked and how far the vote still has to go.
    # Keyed off DB truth (the vote tally itself), not the GitHub label -
    # the label is a human marker and can fail to land; the gate cannot.
    if pid_hold is not None and hold_state is not None:
        if not hold_state["approved"]:
            result["proposal_hold"] = {
                "proposal_id": pid_hold,
                "net": hold_state["net"],
                "threshold": hold_state["threshold"],
                "message": (
                    f"Proposal #{pid_hold} has not passed its community "
                    f"vote yet ({hold_state['net']}/{hold_state['threshold']}). "
                    "PR voting is paused until it clears; discussion is "
                    "limited to the proposal's author and delegate. Vote on "
                    "the proposal now or wait for it to clear."
                ),
            }
        elif hold_applied and not hold_released:
            result["label_synced"] = False
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
            result["diff"] = {"error": "diff unavailable (GitHub API error)"}
    if include_commits:
        try:
            result["commits"] = await github.apr_commits(number)
        except (github.RepoError, OSError):
            result["commits"] = {"error": "commits unavailable (GitHub API error)"}
    if token and my_vote_ok:
        result["my_vote"] = my_vote
    return result