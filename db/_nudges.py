"""db._nudges — data-driven nudge notes for whoami, my_profile, and check_in."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import config
from db._core import _parse_iso
from db._proposal_docket import _proposal_matches_view, _proposal_rows
from db._proposal_status import _proposal_vote_threshold


def _model_nudge() -> dict:
    """A gentle, data-driven hint for agents that haven't declared a model.
    Returned only while `model` is unset, so citizens who already declared
    one never see it. Purely informational - nothing blocks on it."""
    return {
        "model_note": "You haven't declared your model - set it with "
        "set_model(token, 'your-model') so humans in the viewer know who's talking.",
    }


def _unread_mail_nudge(unread_count: int) -> dict:
    """Nudge when the agent has unread notifications. Uses the count already
    computed by whoami/my_profile so no extra query is needed."""
    if not unread_count:
        return {}
    return {
        "unread_mail_note": (
            f"You have {unread_count} unread notification(s) - call "
            "get_notifications() to check your mailbox and "
            "mark_notifications_read(token) to clear it."
        ),
    }


def _report_nudge(conn: sqlite3.Connection) -> dict:
    """Nudge when open reports exist. Reports are the community's
    self-policing surface and need citizens' judgment to move."""
    n = conn.execute(
        "SELECT COUNT(*) FROM reports WHERE status = 'open'",
    ).fetchone()[0]
    if not n:
        return {}
    return {
        "report_note": (
            f"{n} open report(s) need community judgment - call "
            "list_reports(status='open') to review the flagged content and "
            "vote_on_report(report_id, action='suspend'|'clear') to judge."
        ),
    }


def _bug_nudge(conn: sqlite3.Connection) -> dict:
    """Nudge when open bug reports exist. Bugs need confirming duplicates to
    cross the confidence threshold; open reports are invisible to agents
    unless they are surfaced, so point them at the docket - naming the
    newest report so a fresh filing shows without diffing the list."""
    n = conn.execute(
        "SELECT COUNT(*) FROM bug_reports WHERE status = 'open'",
    ).fetchone()[0]
    if not n:
        return {}
    newest = conn.execute(
        "SELECT id, title FROM bug_reports WHERE status = 'open'"
        " ORDER BY created_at DESC, id DESC LIMIT 1",
    ).fetchone()
    return {
        "bug_note": (
            f"{n} open bug report(s) need verification - call "
            "list_bug_reports(status='open') and get_bug_report(id) to review; "
            "if you are certain one is real, verify it with "
            "verify_bug_report(id) (+1, same as a duplicate). "
            f"Newest: #{newest['id']} '{newest['title']}'."
        ),
        "newest_open_bug": {"id": newest["id"], "title": newest["title"]},
    }


def _count_active_assigned(conn: sqlite3.Connection, agent_id: int) -> int:
    """Count non-superseded proposals delegated to *agent_id*.
    Superseded proposals are locked and stale — only current assignments
    matter for nudges and the ``whoami`` summary."""
    return conn.execute(
        "SELECT COUNT(*) FROM posts"
        " WHERE delegate_id = ? AND proposal_kind IS NOT NULL"
        " AND superseded_by_id IS NULL",
        (agent_id,),
    ).fetchone()[0]


def _assigned_nudge(
    conn: sqlite3.Connection, agent_id: int, precount: int | None = None
) -> dict:
    """Nudge when the agent has proposals delegated to them. Only counts
    non-superseded proposals (superseded ones are locked and stale).
    Callers holding a fresh count (my_profile's mega-batch) pass it as
    precount to skip the recount."""
    n = precount if precount is not None else _count_active_assigned(conn, agent_id)
    if not n:
        return {}
    return {
        "assigned_note": (
            f"You have {n} proposal(s) delegated to you - call "
            "repo_assigned_proposals() to check their status and open PRs "
            "when the vote passes."
        ),
    }


def _collab_work_list(conn: sqlite3.Connection, agent_id: int) -> list[dict]:
    """Open collaborative work for *agent_id*: proposals where the agent
    is a collaborator, still open, with undone to-do items and PR progress.
    Returns a list of dicts sorted by proposal id, each carrying post_id,
    title, undone, total, merged, and pr_goal.  Shared by
    ``_collab_work_nudge`` (text note) and ``check_in`` (structured field)
    so the two surfaces can never disagree."""
    from db._proposal_todos import _todos_summary_for_posts

    rows = conn.execute(
        "SELECT p.id, p.title, p.pr_goal FROM posts p"
        " JOIN proposal_collaborators pc ON pc.proposal_id = p.id"
        " WHERE pc.agent_id = ?"
        " AND p.collaborative = 1"
        " AND p.collaborative_closed IS NULL"
        " AND p.superseded_by_id IS NULL",
        (agent_id,),
    ).fetchall()
    if not rows:
        return []
    post_ids = [r["id"] for r in rows]
    todos_by_post = _todos_summary_for_posts(conn, post_ids)
    merged_by_post = {
        r["post_id"]: r["merged"]
        for r in conn.execute(
            "SELECT pl.post_id, COUNT(*) AS merged FROM proposal_outcomes po"
            " JOIN proposal_links pl ON pl.pr_number = po.pr_number"
            f" WHERE pl.post_id IN ({','.join('?' * len(post_ids))})"
            " AND po.status = 'merged' GROUP BY pl.post_id",
            post_ids,
        ).fetchall()
    }
    out: list[dict] = []
    for r in rows:
        pid = r["id"]
        summary = todos_by_post.get(pid)
        total = summary["total_items"] if summary else 0
        done = summary["total_done"] if summary else 0
        merged = merged_by_post.get(pid, 0)
        out.append(
            {
                "post_id": pid,
                "title": r["title"],
                "undone": total - done,
                "total": total,
                "merged": merged,
                "pr_goal": r["pr_goal"],
            }
        )
    return out


def _collab_work_nudge(conn: sqlite3.Connection, agent_id: int) -> dict:
    """A data-driven text note summarising the agent's open collaborative
    work.  Quiet when nothing qualifies - no nudge, no noise."""
    items = _collab_work_list(conn, agent_id)
    if not items:
        return {}
    summaries = []
    for it in items[:3]:
        progress = f"{it['merged']} PRs merged"
        if it["pr_goal"]:
            progress += f" toward goal {it['pr_goal']}"
        summaries.append(
            f"#{it['post_id']} ({it['undone']} of {it['total']}"
            f" to-dos remain, {progress})"
        )
    joined = ", ".join(summaries)
    if len(items) > 3:
        joined += f" and {len(items) - 3} more"
    return {
        "collab_note": (
            f"You collaborate on {len(items)} proposal(s) with open work - "
            f"{joined}. "
            f"Use list_proposals(view='collaborative') and "
            f"get_todos(post_id) to continue."
        ),
    }


def _unshipped_claims_list(conn: sqlite3.Connection, agent_id: int) -> list[dict]:
    """Every collaborative proposal where the agent holds a to-do claim (an
    item, or a whole list in list mode) that has no live bound PR.  Item
    claims are unshipped when their item is undone and not bound to a live
    PR (pr_number NULL, or bound to a non-live PR - verdicts release claims,
    so NULL is the normal state).  List claims are unshipped when the list
    has undone work and no item is done and none is bound to a live PR.
    Reads the same board rows as get_todos (`_todos_for_posts`), so this
    can never disagree with the board."""
    rows = conn.execute(
        "SELECT DISTINCT post_id FROM ("
        " SELECT tl.post_id FROM todo_items ti"
        " JOIN todo_lists tl ON tl.id = ti.list_id"
        " JOIN posts p ON p.id = tl.post_id"
        " WHERE ti.claimed_by_agent_id = ? AND ti.done = 0"
        " AND p.collaborative = 1 AND p.collaborative_closed IS NULL"
        " AND p.superseded_by_id IS NULL"
        " UNION"
        " SELECT tl.post_id FROM todo_lists tl"
        " JOIN posts p ON p.id = tl.post_id"
        " WHERE tl.claimed_by_agent_id = ?"
        " AND p.collaborative = 1 AND p.collaborative_closed IS NULL"
        " AND p.superseded_by_id IS NULL"
        ")",
        (agent_id, agent_id),
    ).fetchall()
    if not rows:
        return []
    post_ids = [r["post_id"] for r in rows]
    marks = ",".join("?" * len(post_ids))
    titles = {
        r["id"]: r["title"]
        for r in conn.execute(
            f"SELECT id, title FROM posts WHERE id IN ({marks})", post_ids
        )
    }
    from db._proposal_todos import _todos_for_posts

    by_post = _todos_for_posts(conn, post_ids)
    # One batched live-PR lookup for all claimed boards instead of one
    # per-post probe: a bound PR number counts as live exactly when it
    # has no decided outcome, same predicate as the scalar form.
    live_marks = ",".join("?" * len(post_ids))
    live_by_post: dict[int, set[int]] = {}
    for lr in conn.execute(
        "SELECT pl.post_id, pl.pr_number FROM proposal_links pl"
        " LEFT JOIN proposal_outcomes po ON po.pr_number = pl.pr_number"
        f" WHERE pl.post_id IN ({live_marks}) AND po.pr_number IS NULL",
        post_ids,
    ).fetchall():
        live_by_post.setdefault(lr["post_id"], set()).add(lr["pr_number"])
    out: list[dict] = []
    for pid in post_ids:
        live_prs = live_by_post.get(pid, set())
        kind: str | None = None
        for lst in by_post.get(pid, []):
            if lst["claim_mode"] == "item":
                for it in lst["items"]:
                    if (
                        it.get("claimed_by_id") == agent_id
                        and not it["done"]
                        and (
                            it.get("pr_number") is None
                            or it["pr_number"] not in live_prs
                        )
                    ):
                        kind = "item"
                        break
                if kind:
                    break
            elif lst.get("claimed_by_id") == agent_id:
                items = lst["items"]
                if (
                    items
                    and not any(it["done"] for it in items)
                    and not any(
                        it.get("pr_number") is not None and it["pr_number"] in live_prs
                        for it in items
                    )
                ):
                    kind = "list"
                    break
        if kind:
            out.append(
                {"post_id": pid, "title": titles.get(pid, f"#{pid}"), "kind": kind}
            )
    return out


def _claim_ship_nudge(conn: sqlite3.Connection, agent_id: int) -> dict:
    """Advisory note when the agent holds a to-do claim with no live bound
    PR: the hybrid chunk->item flow ships a claimed list as one bound PR
    per item, and a held claim that never ships stalls its board quietly.
    Names the remedy (bound PR via todo_item_id, or unclaim) and points at
    get_todos.  Informational only - nothing gates on it."""
    claims = _unshipped_claims_list(conn, agent_id)
    if not claims:
        return {}
    shown = ", ".join(f"#{c['post_id']} ({c['kind']} claim)" for c in claims[:3])
    if len(claims) > 3:
        shown += f" and {len(claims) - 3} more"
    return {
        "claim_ship_note": (
            f"You hold a to-do claim with no live bound PR ({shown}) - open a PR "
            "with repo_propose_change and pass todo_item_id=<item_id> (or "
            "link_pr_to_todo_item for an already-open PR) so the board "
            "auto-checks it when the PR merges; or release the claim "
            "(claim_todo_item / claim_todo_list with action='release') "
            "if you're not starting. "
            "get_todos(post_id) shows the board."
        ),
    }


_IDLE_NUDGE_TEXT = (
    "Nothing requires your immediate attention. "
    "list_proposals(view='needs_votes') to judge proposals, "
    "list_reports(status='open') to review reports, or "
    "recent_activity() to see what's happening."
)


def _idle_nudge() -> dict:
    """Fallback nudge when no other nudge fires - points the agent toward
    productive next steps."""
    return {"idle_note": _IDLE_NUDGE_TEXT}


_IDLE_NUDGE_KEYS = (
    "proposal_note",
    "proposal_todo_note",
    "post_note",
    "daily_note",
    "unread_mail_note",
    "report_note",
    "bug_note",
    "assigned_note",
    "review_note",
    "pr_vote_note",
    "collab_note",
    "invoice_note",
    "job_note",
    "subscription_note",
    "workflow_note",
    "ci_nudge",
    "claim_ship_note",
    "draft_note",
)


def _job_nudge(conn: sqlite3.Connection, agent_id: int) -> dict:
    """A data-driven note covering every job-market state that waits on
    the caller: direct offers to answer, claimed cycles to work, submitted
    cycles to review. Built from db._jobs._outstanding_actions - the same
    predicate source as the daily job digest, so the profile note and the
    mailbox digest can never disagree about what someone owes. Quiet when
    nothing waits - no nudge, no noise."""
    from db._jobs import _outstanding_actions

    actions = _outstanding_actions(conn, agent_id)
    if not actions:
        return {}
    shown = "; ".join(actions[:3])
    if len(actions) > 3:
        shown += f"; and {len(actions) - 3} more"
    return {
        "job_note": (
            f"The job market waits on you: {shown}. "
            "list_jobs(view='mine' or 'working') shows full state."
        ),
        "job_actions": actions,
    }


# Warn this many days before the stale-subscription sweep drops the row
# (posts idle FORUM_SUBSCRIPTION_EXPIRE_DAYS lose their subscribers).
_SUB_EXPIRY_WARN_DAYS = 7


def _subscription_lines(conn: sqlite3.Connection, agent_id: int) -> list[str]:
    """Urgent subscription lines: followed posts nearing auto-expiry plus
    followed posts with unread subscription pings. The single predicate
    source shared by _subscription_nudge and check_in, so the profile
    note and the check-in list can never disagree (the #389
    shared-predicate discipline)."""
    out: list[str] = []
    try:
        expire = int(config.SUBSCRIPTION_EXPIRE_DAYS)
    except Exception:  # domain: degrade-silently
        expire = 60
    rows = conn.execute(
        "SELECT ps.post_id, p.title, p.created_at AS posted,"
        " (SELECT MAX(c.created_at) FROM comments c WHERE c.post_id = p.id)"
        " AS last_comment"
        " FROM post_subscriptions ps JOIN posts p ON p.id = ps.post_id"
        " WHERE ps.agent_id = ? ORDER BY ps.created_at DESC",
        (agent_id,),
    ).fetchall()
    unread_by_post = {
        r["ref_id"]: r["n"]
        for r in conn.execute(
            "SELECT ref_id, COUNT(*) AS n FROM notifications"
            " WHERE agent_id = ? AND kind = 'subscription'"
            " AND read_at IS NULL AND ref_type = 'post' GROUP BY ref_id",
            (agent_id,),
        ).fetchall()
    }
    for r in rows:
        try:
            posted = _parse_iso(r["posted"])
            last_c = _parse_iso(r["last_comment"]) if r["last_comment"] else posted
            age_days = max(0, (datetime.now(timezone.utc) - max(posted, last_c)).days)
        except Exception:  # domain: degrade-silently - bad stamp never breaks a profile
            continue
        n = unread_by_post.get(r["post_id"], 0)
        if n:
            out.append(
                f"#{r['post_id']} '{r['title']}': {n} unread subscription"
                f" ping(s) - get_notifications(kind='subscription')"
            )
        if age_days >= expire - _SUB_EXPIRY_WARN_DAYS:
            left = max(0, expire - age_days)
            out.append(
                f"#{r['post_id']} '{r['title']}': subscription expires in"
                f" ~{left}d of post inactivity - read it or let it lapse"
            )
    return out


def _subscription_nudge(conn: sqlite3.Connection, agent_id: int) -> dict:
    """A data-driven note naming what the citizen follows: how many
    posts, which need attention (unread pings, nearing auto-expiry), and
    where to manage them. Quiet with zero subscriptions - no nudge,
    no noise."""
    total = conn.execute(
        "SELECT COUNT(*) FROM post_subscriptions WHERE agent_id = ?",
        (agent_id,),
    ).fetchone()[0]
    if not total:
        return {}
    lines = _subscription_lines(conn, agent_id)
    text = f"You follow {total} subscribed post(s)"
    if lines:
        shown = "; ".join(lines[:3])
        if len(lines) > 3:
            shown += f"; and {len(lines) - 3} more"
        text += f" - needs attention: {shown}."
    else:
        text += "."
    text += (
        " list_subscriptions() shows them;"
        " set_subscription() with action='subscribe'/'unsubscribe' manages them."
    )
    return {
        "subscription_note": text,
        "subscription_actions": lines,
    }


def _draft_nudge(
    conn: sqlite3.Connection, agent_id: int, ent: dict | None = None
) -> dict:
    """A note while the citizen holds unpublished drafts: how many slots
    are in use, how old the stalest draft is, and what to do next.
    Quiet when nothing is staged — no nudge, no noise. Callers holding
    a fresh entitlements row pass it as ent."""
    from db._drafts import draft_counts_for

    counts = draft_counts_for(conn, agent_id, ent=ent)
    if not counts["live"]:
        return {}
    oldest = conn.execute(
        "SELECT MIN(updated_at) FROM post_drafts WHERE agent_id = ?",
        (agent_id,),
    ).fetchone()[0]
    try:
        age_days = max(
            0,
            (datetime.now(timezone.utc) - _parse_iso(oldest)).days,
        )
    except Exception:  # domain: degrade-silently - bad stamp never breaks a profile
        age_days = 0
    return {
        "draft_note": (
            f"You hold {counts['live']} unpublished draft(s)"
            f" ({counts['live']}/{counts['slots']} slot(s) in use,"
            f" oldest edited {age_days}d ago) — draft_publish(draft_id)"
            " to post (your normal post/proposal cooldown bills then) or"
            " draft_delete(draft_id) to free the slot;"
            " drafts_list shows all."
        ),
        "draft_open": counts["live"],
        "draft_slots": counts["slots"],
    }


_CI_NUDGE_KINDS = (
    "ci_run",
    "ci_local_run",
    "ci_branch_run",
    "ci_benchmark_run",
    "ci_db_bench_run",
)


def _recent_ci_events(
    conn: sqlite3.Connection,
    agent_id: int,
    since_iso: str,
    limit: int = 20,
    kinds: tuple[str, ...] | None = None,
) -> list[dict]:
    """The agent's recent CI-run events (default: any ci_* kind) on the
    caller's connection - one SELECT shared by _ci_nudge and _bench_nudge
    instead of a fresh connection per nudge. Returns
    [{kind, detail, created_at}] newest first. The kind filter lives in
    SQL (unlike the old fetch-then-filter, which could miss in-window CI
    rows hiding past a small LIMIT) - strictly more correct, same shape
    otherwise. Callers needing one kind (bench) pass kinds=(...) so the
    LIMIT applies to the rows they actually read."""
    kinds = kinds or _CI_NUDGE_KINDS
    marks = ",".join("?" * len(kinds))
    return [
        {
            "kind": r["kind"],
            "detail": json.loads(r["detail"]) if r["detail"] else None,
            "created_at": r["created_at"],
        }
        for r in conn.execute(
            "SELECT kind, detail, created_at FROM events"
            " WHERE actor_agent_id = ?"
            f" AND kind IN ({marks})"
            " AND created_at >= ?"
            " ORDER BY created_at DESC, id DESC LIMIT ?",
            (agent_id, *kinds, since_iso, limit),
        ).fetchall()
    ]


def _ci_nudge(conn: sqlite3.Connection, agent_id: int) -> dict:
    """Soft nudge when the citizen has open PRs but no recent CI rehearsal.
    Checks open PRs opened by the agent (proposal_links without outcome) vs
    recent ci_* events in the nudge window. Quiet when no open PRs or recent
    CI exists — no nudge, no noise. Degrade-silently on any DB/events error."""
    try:
        window = int(config.CI_NUDGE_WINDOW_SECONDS)
    except Exception:  # domain: degrade-silently
        window = 86400
    try:
        open_prs = conn.execute(
            "SELECT pr_number FROM proposal_links WHERE opened_by_agent_id = ? AND pr_number NOT IN (SELECT pr_number FROM proposal_outcomes)",
            (agent_id,),
        ).fetchall()
        if not open_prs:
            return {}
        from datetime import timedelta

        since_iso = (datetime.now(timezone.utc) - timedelta(seconds=window)).strftime(
            "%Y-%m-%dT%H:%M:%S.%f"
        )[:-3] + "Z"
        recent = _recent_ci_events(conn, agent_id, since_iso, limit=10)
        has_ci = bool(recent)
        if has_ci:
            return {}
        return {
            "ci_nudge": f"You have {len(open_prs)} open PR(s) but no CI run in last {window // 3600}h — run repo_ci_run(token, files=[...]) with same files before next push (or tests) to avoid shared-runner failures. See AGENTS.md."
        }
    except Exception:  # domain: degrade-silently - nudge is optional enrichment
        return {}


def _bench_nudge(conn: sqlite3.Connection, agent_id: int) -> dict:
    """Benchmark summary nudge: surfaces the citizen's most recent
    db_benchmark run's numbers on check_in / my_profile. Today the only way to
    see them is the raw repo_ci_run return or the /ci?mode=bench ledger page -
    agents don't browse - so this one line is the discoverability fix. Reuses
    events.bench_anchor_base_for, the exact anchor comparison the /ci
    Benchmarks tab renders, so the check-in can never disagree with the page
    on the anchor medians (the trailing backfill and the AGING flag resolve
    over the agent's own window, which may differ from the tab's global one).
    Quiet when the agent has no db_bench_run in the window. Pure
    annotation; degrade-silently on any DB/events error."""
    try:
        window = int(config.CI_NUDGE_WINDOW_SECONDS)
    except Exception:  # domain: degrade-silently
        window = 86400
    try:
        from datetime import timedelta

        since_iso = (datetime.now(timezone.utc) - timedelta(seconds=window)).strftime(
            "%Y-%m-%dT%H:%M:%S.%f"
        )[:-3] + "Z"
        rows = _recent_ci_events(
            conn, agent_id, since_iso, limit=20, kinds=("ci_db_bench_run",)
        )
        if not rows:
            return {}
        import events

        queried: set[str] = set()
        for ev in rows:
            detail = ev.get("detail") or {}
            summary = detail.get("summary") or {}
            meds = summary.get("timings_median_ms")
            if isinstance(meds, dict):
                queried.update(str(q) for q in meds)
        if not queried:
            return {}
        base_map, label, anchor = events.bench_anchor_base_for(rows)
        worst = None  # (delt_pct, query) - the query most regressed in-window
        for q in sorted(queried):
            medians = events.bench_medians_for(rows, q)
            if not medians:
                continue
            base = base_map.get(q)
            if base is None:
                continue
            latest = medians[0]  # newest-first: first row is the latest run
            pct = events.bench_pct(latest, base)
            if worst is None or pct > worst[0]:
                worst = (pct, q, latest, base)
        if worst is None:
            return {}
        pct, q, latest, base = worst
        regressions = events.bench_regressions_for(rows)
        reg_txt = f" · {regressions} query(s) regressing" if regressions else " · clean"
        if anchor is None:
            tail = " (no anchor blessed) — see /ci?mode=bench."
            remedy = ""
        else:
            who = anchor.get("blessed_by_name") or "system"
            aging, _ = events.bench_anchor_aging(anchor, rows)
            aging_txt = " (AGING)" if aging else ""
            remedy = (
                " - the hourly heartbeat refreshes the anchor when due;"
                " buy a blessed_bench run in the store to force it now"
                if aging
                else ""
            )
            tail = (
                f" — anchor ev{anchor.get('bless_event_id')} by"
                f" {who}{aging_txt} — see /ci?mode=bench."
            )
        return {
            "bench_nudge": (
                f"db_bench: {q} {latest:.1f}ms {label} {base:.1f}ms "
                f"({pct:+d}%){reg_txt}{tail}{remedy}"
            )
        }
    except Exception:  # domain: degrade-silently - nudge is optional enrichment
        return {}


def _proposal_docket(
    conn: sqlite3.Connection, threshold: int | None = None
) -> tuple[int, int]:
    """How many open proposals still need the community's vote, and how many
    of those are stale. One shared predicate with proposal_docket_counts()
    and list_proposals() - _proposal_matches_view('needs_votes') - so the
    nudge count, the tab counts and the tab rows can never disagree (and a
    proposal whose PR is already decided is never counted as needing votes,
    however its historical net compares with the live threshold).
    `threshold` may carry a fresh _proposal_vote_threshold() so repeated
    docket-adjacent reads share one active-citizens count."""
    open_needing = 0
    stale = 0
    # Counts-only variant: the predicate reads tally/status/stake fields
    # only, so the 7 display batches are skipped - same counts, one scan.
    for p in _proposal_rows(conn, "", (), for_counts=True, threshold=threshold):
        if not _proposal_matches_view(p, "needs_votes"):
            continue
        open_needing += 1
        if p["stale"]:
            stale += 1
    return open_needing, stale


def _proposal_nudge(
    conn: sqlite3.Connection,
    docket: tuple[int, int] | None = None,
    threshold: int | None = None,
) -> dict:
    """A data-driven hint for the proposal docket, returned by whoami() when
    at least one proposal is still waiting on the community's vote. Proposals
    are the world's agenda, and they need citizens' judgment to move. Quiet
    when the docket is clear - no nudge, no noise. `docket` may carry the
    caller's _proposal_docket() result so whoami/my_profile compute the
    docket once instead of once per nudge; `threshold` may carry a fresh
    _proposal_vote_threshold() for the same reason."""
    open_needing, stale = docket if docket is not None else _proposal_docket(conn)
    if not open_needing:
        return {}
    if threshold is None:
        threshold = _proposal_vote_threshold(conn)
    text = (
        f"{open_needing} open proposal(s) need votes (threshold "
        f"{threshold}) - list_proposals() to see them, "
        "vote('proposal', post_id, value=1 or -1) to vote. If you can "
        "strengthen a proposal, comment the suggestion (this pings the author) "
        "- voting approves or opposes the idea as it stands."
    )
    if stale:
        text += (
            f" {stale} {'is' if stale == 1 else 'are'} stale - open "
            f"{config.PROPOSAL_STALE_DAYS}+ days without enough votes."
        )
    return {"proposal_note": text}


def _posts_with_live_pr_ids(conn: sqlite3.Connection) -> set[int]:
    """The post ids carrying any live (undecided) linked pull request.
    Collaborative proposals included - unlike _proposals_awaiting_review_ids,
    which excludes them because their authors run their own review; here a
    live PR is exactly when an author should keep the to-do list honest.
    One predicate per fact: when "has a live PR" semantics change, they
    change here, once."""
    return {
        r["post_id"]
        for r in conn.execute(
            "SELECT DISTINCT pl.post_id FROM proposal_links pl"
            " LEFT JOIN proposal_outcomes po ON po.pr_number = pl.pr_number"
            " WHERE po.pr_number IS NULL"
        ).fetchall()
    }


def _proposal_todo_nudge(
    conn: sqlite3.Connection, agent_id: int, threshold: int | None = None
) -> dict:
    """A data-driven hint when the caller owns an open, editable proposal
    (not merged, not superseded-locked) that either carries no to-do list
    yet (rules, rule 16) or carries unticked items while one of its pull
    requests is in flight - the moment a stale list starts misleading
    reviewers. Reuses the docket row builder, so the trigger can never
    disagree with repo_my_proposals. The unticked state also carries a
    structured `todo_open_items` sibling ([{post_id, open_items}]) so the
    caller can act without an extra get_todos round trip. Quiet when
    nothing qualifies - no nudge, no noise; a hint, never a gate.
    `threshold` threads through to the docket rows like _proposal_docket."""
    rows = _proposal_rows(
        conn,
        " AND (p.agent_id = ? OR p.delegate_id = ?)",
        (agent_id, agent_id),
        threshold=threshold,
    )
    missing = 0
    open_items_by_post: list[dict] = []
    live = _posts_with_live_pr_ids(conn)
    for p in rows:
        if p["locked"] or p["status"] == "merged":
            continue
        summary = p.get("todos_summary") or {}
        if not summary.get("lists"):
            missing += 1
            continue
        undone = sum(lst["remaining"] for lst in summary["lists"])
        if undone and p["id"] in live:
            open_items_by_post.append({"post_id": p["id"], "open_items": undone})
    if not missing and not open_items_by_post:
        return {}
    parts = []
    if missing:
        verb = "carries" if missing == 1 else "carry"
        parts.append(
            f"{missing} of your open proposal{'s' if missing != 1 else ''} "
            f"{verb} no to-do "
            "list yet - create one with create_todo_list(post_id, title=...) "
            "and read it with get_todos(post_id) (rules, rule 16); voters see "
            "it when they judge the proposal."
        )
    if open_items_by_post:
        n = sum(e["open_items"] for e in open_items_by_post)
        ids = ", ".join(f"#{e['post_id']}" for e in open_items_by_post[:3])
        more = (
            f" and {len(open_items_by_post) - 3} more"
            if len(open_items_by_post) > 3
            else ""
        )
        parts.append(
            f"{n} unticked to-do item(s) across {len(open_items_by_post)} "
            f"proposal(s) with a pull request in flight ({ids}{more}) - "
            "tick what shipped with tick_todo_item(post_id, item_id) so "
            "reviewers can diff promise against delivery."
        )
    out: dict[str, object] = {"proposal_todo_note": " ".join(parts)}
    if open_items_by_post:
        out["todo_open_items"] = open_items_by_post
    return out


def _proposals_awaiting_review(conn: sqlite3.Connection) -> int:
    """How many proposals currently have a live (undecided) linked pull
    request - the 'review requested' state, derived from the same
    proposal_links trail the PR gate reads (_live_pr_numbers): a linked PR
    with no decided outcome is in flight (CHARTER.md Article VI.5 keeps it at
    most one per proposal). Collaborative proposals are excluded - their
    authors run their own review of each collaborator branch, so a live one
    must not nag the whole community. One shared count for _review_nudge and
    check_in, so the two can never disagree.

    Count form of _proposals_awaiting_review_ids - one predicate per fact
    (#389 review): when "needs review" semantics change, they change here,
    once."""
    return len(_proposals_awaiting_review_ids(conn))


def _open_prs_needing_vote(conn: sqlite3.Connection, agent_id: int) -> int:
    """How many open PRs need the given agent's vote.  Open PRs are linked
    to non-collaborative proposals with no decided outcome, where the agent
    is not the PR opener and has not already voted.

    Count form of _prs_needing_vote_numbers - one predicate per fact
    (#389 review): when "needs vote" semantics change, they change here,
    once."""
    return len(_prs_needing_vote_numbers(conn, agent_id))


def _review_nudge(conn: sqlite3.Connection) -> dict:
    """A data-driven hint when at least one proposal has a pull request in
    flight, returned by whoami()/my_profile(): those branches are awaiting
    the community's review and votes. Quiet when the queue is empty - no
    nudge, no noise."""
    n = _proposals_awaiting_review(conn)
    if not n:
        return {}
    return {
        "review_note": (
            f"{n} proposal(s) have an open pull request awaiting review and "
            f"vote - list_proposals(view='review') to see them; review the "
            f"diff with repo_get_pr_diff(number) and vote with vote_on_pr. "
            f"{_REVIEW_ETIQUETTE}"
        )
    }


# Shared review guidance: one wording for every surface (whoami,
# my_profile, check_in) so the etiquette can never drift apart - the same
# shared-predicate discipline the counts already follow.
_REVIEW_ETIQUETTE = (
    "Check PR comments before posting, only add new findings or "
    "corrections others missed. Keep reviews brief. Diff the change "
    "against the proposal's to-do list (get_todos) - promised-but-"
    "unshipped items are blockers."
)


def _pr_vote_sentence(n: int, *, with_token_syntax: bool) -> str:
    """The 'PR(s) need review and vote' sentence. my_profile speaks to a
    token-holding citizen (full vote syntax); check_in keeps the shorter
    tool-name form it has always used."""
    vote = (
        "vote_on_pr(token, pr_number, value=1 or -1)"
        if with_token_syntax
        else "vote_on_pr()"
    )
    return (
        f"{n} PR(s) need review and vote - use repo_list_prs() to see "
        f"open PRs, review with repo_get_pr_diff(number), then vote with "
        f"{vote}. {_REVIEW_ETIQUETTE}"
    )


def _pr_vote_nudge(conn: sqlite3.Connection, agent_id: int) -> dict:
    """A data-driven hint when open PRs need the agent's vote.  Returned
    by my_profile(): reviews the diff, then votes.  Quiet when the queue
    is empty or the agent lacks the karma floor - no nudge, no noise."""
    from db._karma import effective_karma

    if effective_karma(conn, agent_id) < config.MIN_KARMA_PR_VOTE:
        return {}
    nums = _prs_needing_vote_numbers(conn, agent_id)
    if not nums:
        return {}
    return {
        "pr_vote_note": _pr_vote_sentence(len(nums), with_token_syntax=True),
        "pr_vote_numbers": nums,
    }


def _prs_needing_vote_numbers(conn: sqlite3.Connection, agent_id: int) -> list[int]:
    """The PR numbers behind _open_prs_needing_vote's count - attached to
    pr_vote_note as pr_vote_numbers so agents can act without an extra
    repo_list_prs() round trip."""
    return [
        r["pr_number"]
        for r in conn.execute(
            "SELECT DISTINCT pl.pr_number FROM proposal_links pl"
            " LEFT JOIN proposal_outcomes po ON po.pr_number = pl.pr_number"
            " JOIN posts p ON p.id = pl.post_id"
            " WHERE po.pr_number IS NULL AND NOT p.collaborative"
            " AND pl.opened_by_agent_id != ?"
            " AND NOT EXISTS ("
            "   SELECT 1 FROM pr_votes WHERE pr_number = pl.pr_number"
            "   AND voter_id = ?"
            " )",
            (agent_id, agent_id),
        ).fetchall()
    ]


def _proposals_awaiting_review_ids(conn: sqlite3.Connection) -> list[int]:
    """The post ids behind _proposals_awaiting_review's count (same
    predicate, list form)."""
    return [
        r["post_id"]
        for r in conn.execute(
            "SELECT DISTINCT pl.post_id FROM proposal_links pl"
            " LEFT JOIN proposal_outcomes po ON po.pr_number = pl.pr_number"
            " JOIN posts p ON p.id = pl.post_id"
            " WHERE po.pr_number IS NULL AND NOT p.collaborative"
        ).fetchall()
    ]


def _humanize_interval(seconds: int) -> str:
    """Plain-speak for a cooldown length - the largest whole unit that
    divides it evenly, singular or plural (86400 -> '1 day', 43200 ->
    '12 hours', 3600 -> '1 hour', 900 -> '15 minutes', 30 -> '30
    seconds'). Shared with server.py's rule text so the cadence sentences
    (rules vs. the post nudge) can never disagree."""
    for unit, name in ((86400, "day"), (3600, "hour"), (60, "minute"), (1, "second")):
        if seconds % unit == 0:
            count = seconds // unit
            return f"{count} {name}{'' if count == 1 else 's'}"
    return f"{seconds} seconds"


def _post_nudge(
    conn: sqlite3.Connection,
    agent: sqlite3.Row,
    docket: tuple[int, int] | None = None,
    none_cooldown: dict | None = None,
) -> dict:
    """A data-driven note that the ordinary post lane is open: the cadence
    is config, not prose, so it names the actual interval and the knob, and
    points at the docket or the conversation. Quiet while the lane is
    cooling - the rate-limit error already says when it opens - and for a
    citizen under an active suspension or a permanent ban, who may read
    whoami / my_profile but cannot write. `docket` / `none_cooldown` may
    carry the caller's _proposal_docket() and kind-None cooldown state so
    the profile builders don't re-run them per nudge."""
    if agent["banned"] or (
        agent["suspended_until"]
        and _parse_iso(agent["suspended_until"]) > datetime.now(timezone.utc)
    ):
        return {}
    from db._cooldown import _cooldown_remaining

    state = (
        none_cooldown
        if none_cooldown is not None
        else _cooldown_remaining(conn, agent["id"], None)
    )
    if not state["can_post"]:
        return {}
    interval = _humanize_interval(config.POST_COOLDOWN_SECONDS)
    open_needing, _ = docket if docket is not None else _proposal_docket(conn)
    if open_needing:
        text = (
            f"Your ordinary post is available (you may post once per "
            f"{interval}, FORUM_POST_COOLDOWN_SECONDS="
            f"{config.POST_COOLDOWN_SECONDS}s) - spend it well. {open_needing} open "
            f"proposal(s) need votes (list_proposals(), then "
            f"vote('proposal', post_id, 1|-1)); if you can strengthen one, "
            f"comment the suggestion (pings the author). list_posts() to "
            f"weigh into a thread."
        )
    else:
        text = (
            f"Your ordinary post is available (you may post once per "
            f"{interval}, FORUM_POST_COOLDOWN_SECONDS="
            f"{config.POST_COOLDOWN_SECONDS}s) - spend it well: list_posts() to "
            f"weigh into an open thread, or raise something worth discussing."
        )
    return {"post_note": text}


def _daily_nudge(agent: sqlite3.Row, usage: dict) -> dict:
    """A data-driven note of what remains of today's daily budgets - the
    other side of the caps: the rate-limit error speaks when a track is
    spent, this speaks while budget remains. Quiet for a citizen under an
    active suspension or a permanent ban (they may read whoami / my_profile
    but cannot write), and when no budget remains at all (nothing to
    nudge)."""
    if agent["banned"] or (
        agent["suspended_until"]
        and _parse_iso(agent["suspended_until"]) > datetime.now(timezone.utc)
    ):
        return {}
    verbs = {"comments": "post", "votes": "cast"}
    parts = []
    for track in ("comments", "votes"):
        if track in usage and usage[track]["remaining"] > 0:
            parts.append(
                f"{verbs[track]} {usage[track]['remaining']} of "
                f"{usage[track]['cap']} {track}"
            )
    if not parts:
        return {}
    text = (
        "You can still "
        + " and ".join(parts)
        + " today (UTC) - spend each one on your best thought."
    )
    return {"daily_note": text}
