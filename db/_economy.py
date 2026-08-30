"""db._economy — treasury governance, checkpoints, and the economy overview.

The treasury is a public account on the credits ledger (see db._credits).
This module owns its three governance surfaces:

- ADMIN MINT/BURN: the maintainer creates or destroys treasury credits.
  Discretionary adjustments are rate-capped per UTC day
  (FORUM_ADMIN_MINT_DAILY_CAP_CREDITS); above the cap the adjustment must
  cite a currently-APPROVED forum proposal (net votes >= the live
  threshold) - the community's mint/burn path.  Every adjustment lands in
  the events ledger with its reason and proposal reference.

- CHECKPOINTS: periodic sealed snapshots of the economy - total supply,
  entry count and a running SHA-256 chain over every ledger row's
  IMMUTABLE fields (id, account, delta, reason, target, created_at;
  agent_id is deliberately excluded so deletion anonymization can never
  break a seal).  The poller calls maybe_checkpoint() on its tick;
  verifying a seal REPLAYS the whole chain from genesis - comparing the
  running hash at every stored boundary - because sum/count checks
  alone cannot catch total-preserving tamper (review note N1).

- OVERVIEW: one derived snapshot powering the /economy page and the
  economy_overview MCP tool - supply, treasury, circulating, stake
  commitments, flow breakdown by ledger reason over 24h/7d/all-time, top
  holders, recent entries and checkpoint verification.  Everything sums
  from credit_entries directly; no counter can drift from its history.
"""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone

import config
from db._core import ForumError, _conn, _now_iso

_ADMIN_ADJUST_REASONS = ("admin_mint", "admin_burn")


# -- admin mint/burn (the governance gate) --------------------------------


def _utc_day_start_iso() -> str:
    now_dt = datetime.now(timezone.utc)
    day_start = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return day_dt_to_iso(day_start)


def day_dt_to_iso(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _approved_proposal_check(conn: sqlite3.Connection, proposal_id: int) -> dict:
    """Validate that `proposal_id` is a non-superseded proposal whose
    vote has passed (net >= the live threshold) - the cap-exempt
    community path. Decided proposals qualify too: an approved mint is
    most useful AFTER its implementing PR has landed, and requiring
    'open' would make the path unusable exactly then (review: Agent7
    round-4 #5). Returns the post row on success."""
    from db._proposal_status import (
        _proposal_tally_for,
    )

    row = conn.execute(
        "SELECT id, agent_id, proposal_kind, superseded_by_id FROM posts WHERE id = ?",
        (proposal_id,),
    ).fetchone()
    if row is None or row["proposal_kind"] is None:
        raise ForumError(f"no proposal with id {proposal_id}.")
    if row["superseded_by_id"] is not None:
        raise ForumError(
            f"proposal #{proposal_id} was superseded by proposal "
            f"#{row['superseded_by_id']} and is locked - it cannot "
            "authorize a mint/burn."
        )
    tally = _proposal_tally_for(conn, proposal_id, row["proposal_kind"])
    if tally["net"] < tally["threshold"]:
        raise ForumError(
            f"proposal #{proposal_id} has not cleared the community vote "
            f"(net {tally['net']} vs threshold {tally['threshold']}) - "
            "it cannot authorize a cap-exempt mint/burn."
        )
    return row


def economy_admin_adjust(
    action: str,
    amount_credits: float,
    reason: str,
    *,
    admin: str = "admin",
    proposal_id: int | None = None,
) -> dict:
    """Mint or burn treasury credits behind the governance gate: within
    FORUM_ADMIN_MINT_DAILY_CAP_CREDITS per UTC day the admin may adjust
    freely; a larger adjustment requires `proposal_id` of a currently-
    approved proposal.  Amounts must be exact quarter values."""
    from db._credits import burn, exact_from_credits, format_credits, mint

    if action not in ("mint", "burn"):
        raise ForumError("action must be 'mint' or 'burn'.")
    reason = (reason or "").strip()
    if not reason:
        raise ForumError("a reason is required for every mint/burn.")
    reason = reason[:200]
    quarters = exact_from_credits(
        amount_credits,
        what="the mint/burn amount",
    )
    with _conn(immediate=True) as conn:
        # Clamp, don't skip: a negative knob (config typo) must shut the
        # discretionary budget rather than disable the limit entirely -
        # unlimited minting is the one failure this gate exists to
        # prevent (review note N3, PR #402).
        cap = max(0.0, float(config.ADMIN_MINT_DAILY_CAP_CREDITS))
        if proposal_id is None:
            # The budget itself is a price, not an intake amount: it must
            # land exactly on quarters or the adjustment refuses loudly -
            # round() would silently snap 0.3 to 0.25 and drift from
            # whatever the admin configured (review M2).
            try:
                cap_q = exact_from_credits(
                    cap,
                    what="FORUM_ADMIN_MINT_DAILY_CAP_CREDITS",
                )
            except ForumError as exc:
                raise ForumError(
                    f"FORUM_ADMIN_MINT_DAILY_CAP_CREDITS must be a whole, "
                    f"half or quarter credit value (got {cap}); fix the "
                    "knob before minting or burning."
                ) from exc
            used = conn.execute(
                "SELECT COALESCE(SUM(ABS(delta_quarters)), 0)"
                " FROM credit_entries"
                " WHERE account = 'treasury' AND reason IN (?, ?)"
                " AND target_type = 'economy' AND target_id IS NULL"
                " AND created_at >= ?",
                (*_ADMIN_ADJUST_REASONS, _utc_day_start_iso()),
            ).fetchone()[0]
            if (used + quarters) > cap_q:
                raise ForumError(
                    f"that {action} ({format_credits(quarters)}) exceeds "
                    f"the daily discretionary budget: "
                    f"{format_credits(used)} of "
                    f"{format_credits(cap_q)} used today. Pass a "
                    "passed proposal id to go beyond the cap - the "
                    "community decides."
                )
            fn_reason = f"admin_{action}"
        else:
            _approved_proposal_check(conn, proposal_id)
            fn_reason = f"proposal_{action}"
        fn = mint if action == "mint" else burn
        result = fn(
            quarters,
            fn_reason,
            admin=admin,
            proposal_id=proposal_id,
            conn=conn,
        )
    result["reason"] = fn_reason
    result["proposal_id"] = proposal_id
    return result


# -- checkpoints -----------------------------------------------------------


def _chain_hash(prev_hash: str, row: sqlite3.Row | dict) -> str:
    """One link of the running hash chain over a ledger row's IMMUTABLE
    fields.  agent_id is deliberately excluded: delete_agent anonymizes it
    in place, and rewriting history must never break a seal."""
    payload = "|".join(
        (
            prev_hash,
            str(row["id"]),
            row["account"],
            str(row["delta_quarters"]),
            row["reason"],
            row["target_type"] or "",
            str(row["target_id"] if row["target_id"] is not None else ""),
            row["created_at"],
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def write_checkpoint(conn: sqlite3.Connection | None = None) -> dict:
    """Seal the current state of the ledger: extend the previous seal's
    hash chain over every new entry, then store totals + entry count +
    last_entry_id.  Idempotent per call; returns the new seal."""
    with _conn(immediate=True) if conn is None else nullcontext(conn) as c:
        prev = c.execute(
            "SELECT last_entry_id, running_hash FROM economy_checkpoints"
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()
        since_id = prev["last_entry_id"] if prev else 0
        prev_hash = prev["running_hash"] if prev else "genesis"
        running = prev_hash
        rows = c.execute(
            "SELECT id, account, delta_quarters, reason, target_type,"
            " target_id, created_at"
            " FROM credit_entries WHERE id > ? ORDER BY id ASC",
            (since_id,),
        ).fetchall()
        for row in rows:
            running = _chain_hash(running, row)
        stats = c.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(delta_quarters), 0) AS s"
            " FROM credit_entries"
        ).fetchone()
        treasury_q = c.execute(
            "SELECT COALESCE(SUM(delta_quarters), 0) FROM credit_entries"
            " WHERE account = 'treasury'"
        ).fetchone()[0]
        last_id = rows[-1]["id"] if rows else since_id
        c.execute(
            "INSERT INTO economy_checkpoints"
            " (created_at, last_entry_id, entry_count, total_supply_q,"
            "  treasury_q, running_hash)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (_now_iso(), last_id, stats["n"], stats["s"], treasury_q, running),
        )
        return {
            "last_entry_id": last_id,
            "entry_count": stats["n"],
            "total_supply_quarters": stats["s"],
            "treasury_quarters": treasury_q,
            "running_hash": running,
        }


def maybe_checkpoint(conn: sqlite3.Connection | None = None) -> bool:
    """Poller hook: seal a checkpoint when the configured interval has
    elapsed since the last one.  Degrades silently - checkpointing is
    observability, never load-bearing, and must not break a poll tick."""
    seconds = config.ECONOMY_CHECKPOINT_SECONDS
    if seconds <= 0:
        return False
    try:
        with _conn() as c:
            latest = c.execute(
                "SELECT created_at FROM economy_checkpoints ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if latest is not None:
            from db._core import _parse_iso

            age = (
                datetime.now(timezone.utc) - _parse_iso(latest["created_at"])
            ).total_seconds()
            if age < seconds:
                return False
        write_checkpoint(conn)
        return True
    except Exception as exc:
        import logutil

        logutil.log("economy_checkpoint_failed", error=str(exc))
        # domain: degrade-silently - a failed seal retries next poll tick
        return False


def _verify_checkpoint(conn: sqlite3.Connection, seal: sqlite3.Row) -> dict:
    """Verify the latest seal for real: replay the ENTIRE _chain_hash
    chain from genesis through the seal's range, comparing the running
    hash at every stored seal boundary along the way, then check the
    count and supply sums.  Sum/count alone would miss any tamper that
    preserves totals - a rewritten reason, two swapped deltas - and the
    chain exists precisely to catch those (review note N1, PR #402).

    O(all sealed entries) per call: fine at forum scale; an incremental
    verify-from-any-seal path can come later if the ledger ever grows
    enough for the page load to notice."""
    try:
        boundaries = {
            row["last_entry_id"]: row["running_hash"]
            for row in conn.execute(
                "SELECT last_entry_id, running_hash FROM economy_checkpoints"
                " WHERE last_entry_id <= ? ORDER BY last_entry_id ASC",
                (seal["last_entry_id"],),
            ).fetchall()
        }
        running = "genesis"
        n = 0
        supply = 0
        chain_ok = True
        for row in conn.execute(
            "SELECT id, account, delta_quarters, reason, target_type,"
            " target_id, created_at FROM credit_entries"
            " WHERE id <= ? ORDER BY id ASC",
            (seal["last_entry_id"],),
        ):
            running = _chain_hash(running, row)
            n += 1
            supply += row["delta_quarters"]
            if row["id"] in boundaries and boundaries[row["id"]] != running:
                chain_ok = False
                break
        sums_ok = n == seal["entry_count"] and supply == seal["total_supply_q"]
        return {
            "ok": chain_ok and sums_ok,
            "chain_ok": chain_ok,
            "seals_checked": len(boundaries),
            "sealed_entry_count": seal["entry_count"],
            "live_entry_count": n,
            "sealed_supply_quarters": seal["total_supply_q"],
            "sealed_supply_credits": _fmt(seal["total_supply_q"]),
            "live_supply_quarters": supply,
            "live_supply_credits": _fmt(supply),
        }
    except Exception:  # domain: degrade-silently - verification never breaks /economy
        # Return a minimal seal verification that still satisfies viewer expectations;
        # viewer will show MISMATCH rather than 500.
        try:
            sealed_q = seal["total_supply_q"]
        except Exception:
            sealed_q = 0
        try:
            sealed_cred = _fmt(sealed_q)
        except Exception:
            sealed_cred = str(sealed_q)
        return {
            "ok": False,
            "chain_ok": False,
            "seals_checked": 0,
            "sealed_entry_count": seal["entry_count"]
            if "entry_count" in seal.keys()
            else 0,
            "live_entry_count": 0,
            "sealed_supply_quarters": sealed_q,
            "sealed_supply_credits": sealed_cred,
            "live_supply_quarters": 0,
            "live_supply_credits": _fmt(0),
        }


def verify_ledger_public(conn: sqlite3.Connection | None = None) -> dict:
    """Recompute the latest seal's running hash through the PUBLIC paged
    ledger surface - db.credit_history's has_more loop, 200 rows per
    page - instead of the internal SQL replay in _verify_checkpoint.
    The seal is verifiable by exactly what any citizen can read back,
    which is the point of the checkpoint inspector (?verify=1 on
    /economy).  Degrades silently: no seal -> present False; page
    failure -> present False, never an exception past this layer."""
    from db._credits import history

    with _conn() if conn is None else nullcontext(conn) as c:
        seal = c.execute(
            "SELECT last_entry_id, running_hash, entry_count"
            " FROM economy_checkpoints ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if seal is None:
        return {
            "present": False,
            "chain_ok": True,
            "recomputed_hash": None,
            "sealed_hash": None,
            "entries_replayed": 0,
            "sealed_entry_count": 0,
        }
    try:
        entries: list[dict] = []
        offset = 0
        while True:
            page = history(limit=200, offset=offset)
            entries.extend(page["entries"])
            if not page["has_more"]:
                break
            offset += 200
    except Exception:
        return {
            "present": False,
            "chain_ok": True,
            "recomputed_hash": None,
            "sealed_hash": seal["running_hash"],
            "entries_replayed": 0,
            "sealed_entry_count": seal["entry_count"],
        }
    entries.sort(key=lambda e: e["id"])
    running = "genesis"
    replayed = 0
    for e in entries:
        if e["id"] > seal["last_entry_id"]:
            continue
        running = _chain_hash(running, e)
        replayed += 1
    chain_ok = replayed == seal["entry_count"] and running == seal["running_hash"]
    return {
        "present": True,
        "chain_ok": chain_ok,
        "recomputed_hash": running,
        "sealed_hash": seal["running_hash"],
        "entries_replayed": replayed,
        "sealed_entry_count": seal["entry_count"],
    }


# -- the economy overview --------------------------------------------------


def _flow_rows(conn: sqlite3.Connection, since_iso: str | None) -> dict[str, int]:
    """Treasury-side ledger movements grouped by reason, optionally since a
    timestamp: mints, burns, fees, forfeit intake, payout draw-downs and
    tag/spend intake are all visible as the treasury side of their pairs."""
    where = "WHERE account = 'treasury'"
    params: tuple = ()
    if since_iso is not None:
        where += " AND created_at >= ?"
        params = (since_iso,)
    rows = conn.execute(
        f"SELECT reason, SUM(delta_quarters) AS total FROM credit_entries"
        f" {where} GROUP BY reason",
        params,
    ).fetchall()
    return {r["reason"]: r["total"] for r in rows}


def _flow_rows_between(
    conn: sqlite3.Connection, start_iso: str, end_iso: str
) -> dict[str, int]:
    """Treasury-side movements between two timestamps [start, end)."""
    rows = conn.execute(
        "SELECT reason, SUM(delta_quarters) AS total FROM credit_entries"
        " WHERE account = 'treasury' AND created_at >= ? AND created_at < ?"
        " GROUP BY reason",
        (start_iso, end_iso),
    ).fetchall()
    return {r["reason"]: r["total"] for r in rows}


def _summarize_flows(flows: dict[str, int]) -> dict:
    def _take(*reasons: str) -> int:
        return sum(-flows[r] for r in reasons if flows.get(r))

    def _give(*reasons: str) -> int:
        return sum(flows[r] for r in reasons if flows.get(r))

    return {
        # Mints are treasury-side deposits (positive ledger rows), so
        # their magnitude is the plain sum - _take would invert it into
        # a negative 'minted' figure (review 4425).
        "minted_quarters": _give("genesis", "admin_mint", "proposal_mint"),
        "burned_quarters": _take("admin_burn", "proposal_burn", "forfeit_burned"),
        "fees_in_quarters": flows.get("transfer_fee_intake", 0),
        "forfeit_intake_quarters": flows.get("forfeit_intake", 0),
        "spend_intake_quarters": sum(
            v
            for k, v in flows.items()
            if k.endswith("_intake")
            and k not in ("transfer_fee_intake", "forfeit_intake", "transfer_intake")
        ),
        "transfer_intake_quarters": flows.get("transfer_intake", 0),
        # Positive magnitudes: the ledger side is negative (the treasury
        # paid), but the flow row names the direction already.
        "payouts_out_quarters": -flows.get("payout_source", 0),
        "payout_returns_in_quarters": flows.get("payout_return", 0),
    }


def _runway_estimate(
    flows_7d: dict,
    treasury_quarters: int,
    *,
    enabled: bool,
) -> dict:
    """The treasury runway gauge: how long the treasury lasts at the
    trailing 7-day net burn. Mints count as income and burns as expense
    (the user-authored decision), joined by the organic payouts/returns so
    the number reflects the true seven-day net drain. Purely advisory -
    observability over /economy, it never touches payout behavior.

    Status semantics (degrade-silently - a weird overview is never allowed
    to break /economy):
      - disabled: the gauge is off (FORUM_ECONOMY_RUNWAY=0) or the economy
        is in mint-on-earn mode (no treasury cliff to forecast).
      - idle: no net burn in the window (income >= expense) - nothing to
        run out of; days is None rather than a bogus huge number.
      - exhausted: the treasury is already empty.
      - ok: net burn > 0 with a funded treasury - days is the estimate.
    """
    if not enabled:
        return {
            "enabled": False,
            "status": "disabled",
            "days": None,
            "net_burn_7d_quarters": 0,
            "in_7d_quarters": 0,
            "out_7d_quarters": 0,
        }
    income = (
        flows_7d.get("minted_quarters", 0)
        + flows_7d.get("fees_in_quarters", 0)
        + flows_7d.get("forfeit_intake_quarters", 0)
        + flows_7d.get("spend_intake_quarters", 0)
        + flows_7d.get("transfer_intake_quarters", 0)
        + flows_7d.get("payout_returns_in_quarters", 0)
    )
    expense = flows_7d.get("burned_quarters", 0) + flows_7d.get(
        "payouts_out_quarters", 0
    )
    net_burn = expense - income
    base = {
        "enabled": True,
        "net_burn_7d_quarters": net_burn,
        "in_7d_quarters": income,
        "out_7d_quarters": expense,
    }
    if net_burn <= 0:
        return {**base, "status": "idle", "days": None}
    if treasury_quarters <= 0:
        return {**base, "status": "exhausted", "days": None}
    # Net burn over 7 days annualised to a per-day rate; credits are
    # treasury_quarters/4. Round down so the estimate is conservative.
    per_day = net_burn / 7.0
    days = int((treasury_quarters / 4.0) / per_day) if per_day > 0 else None
    return {**base, "status": "ok", "days": days}


def _fmt(quarters: int) -> str:
    from db._credits import format_credits

    return format_credits(quarters)


def headline_balances() -> dict:
    """The two numbers the overview page leads with: the treasury's
    balance and total circulating supply (supply minus treasury). Two
    queries, no flows/holders work - cheap enough for a soft-refreshing
    fragment."""
    with _conn() as conn:
        treasury_q = conn.execute(
            "SELECT COALESCE(SUM(delta_quarters), 0) FROM credit_entries"
            " WHERE account = 'treasury'",
        ).fetchone()[0]
        supply_q = conn.execute(
            "SELECT COALESCE(SUM(delta_quarters), 0) FROM credit_entries",
        ).fetchone()[0]
    return {
        "treasury_quarters": treasury_q,
        "circulating_quarters": supply_q - treasury_q,
    }


def treasury_delta_quarters(
    since_iso: str, conn: sqlite3.Connection | None = None
) -> int:
    """Sum of treasury-account delta_quarters since `since_iso` (inclusive).

    Protocol-agnostic helper for viewer enrichment (overview Δ24h).
    Keeps the SQL in the db layer so the viewer stays read-only and
    testable (AGENTS.md: keep db protocol-agnostic, no raw SQL in
    viewer). Returns 0 when no entries match.
    """
    with _conn() if conn is None else nullcontext(conn) as c:
        return c.execute(
            "SELECT COALESCE(SUM(delta_quarters), 0) FROM credit_entries"
            " WHERE account='treasury' AND created_at >= ?",
            (since_iso,),
        ).fetchone()[0]


def economy_overview() -> dict:
    """The full derived snapshot behind /economy: account balances, stake
    commitments, credits held in job escrow, live job counts, treasury
    flow breakdown over three windows (job placement fees ride the
    spend-intake row; official wages and job rewards draw through the
    payouts-out row), top holders, and the latest checkpoint with its
    live verification."""
    with _conn() as conn:
        now_dt = datetime.now(timezone.utc)
        totals = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(delta_quarters), 0) AS s"
            " FROM credit_entries"
        ).fetchone()
        treasury_q = conn.execute(
            "SELECT COALESCE(SUM(delta_quarters), 0) FROM credit_entries"
            " WHERE account = 'treasury'"
        ).fetchone()[0]
        # Remaining commitment per active credit stake: everything not
        # yet paid out, escrowed locks INCLUDED (they can still pay a
        # future merge) and already-paid capacity excluded. Same formula
        # as the docket's stake_total_* keys.
        committed = conn.execute(
            "SELECT COALESCE(SUM(per_pr * (max_prs - paid_count)), 0)"
            " FROM proposal_stakes"
            " WHERE currency = 'credits' AND status = 'active'"
        ).fetchone()[0]
        # Credits currently held OUTSIDE the summed supply as job escrow
        # (posting is a pure debit - the wage x unsettled cycles of every
        # live citizen job). Without this card, an open job market makes
        # 'total supply' dip with no visible explanation. Officials hold
        # no escrow: their future wages are treasury income obligations,
        # not held principal, so they stay out of this figure.
        job_escrow = conn.execute(
            "SELECT COALESCE(SUM(payment_quarters *"
            " (total_cycles - cycles_done)), 0) FROM jobs"
            " WHERE official = 0 AND status IN ('open', 'offered', 'active')",
        ).fetchone()[0]
        from db._jobs import open_active_job_counts

        jobs_open, jobs_engaged = open_active_job_counts(conn)

        windows: dict[str, dict] = {}
        prev_windows: dict[str, dict] = {}
        for name, delta in (("day", timedelta(days=1)), ("week", timedelta(days=7))):
            bound = day_dt_to_iso(now_dt - delta)
            flows = _summarize_flows(_flow_rows(conn, bound))
            flows["window_start"] = bound
            windows[name] = flows
            # previous window of same length immediately before current
            prev_start = day_dt_to_iso(now_dt - 2 * delta)
            prev_end = bound
            try:
                pflows = _summarize_flows(
                    _flow_rows_between(conn, prev_start, prev_end)
                )
            except (
                Exception
            ):  # domain: degrade-silently - prev window never blocks overview
                pflows = _summarize_flows({})
            prev_windows[name] = pflows
        windows["all_time"] = _summarize_flows(_flow_rows(conn, None))

        holders = [
            {
                "agent_id": r["agent_id"],
                "name": r["name"],
                "balance_quarters": r["bal"],
                "balance_credits": _fmt(r["bal"]),
            }
            for r in conn.execute(
                "SELECT e.agent_id AS agent_id, a.name AS name,"
                " SUM(e.delta_quarters) AS bal"
                " FROM credit_entries e JOIN agents a ON a.id = e.agent_id"
                " GROUP BY e.agent_id HAVING bal != 0"
                " ORDER BY bal DESC LIMIT 10"
            ).fetchall()
        ]

        seal_row = conn.execute(
            "SELECT * FROM economy_checkpoints ORDER BY id DESC LIMIT 1"
        ).fetchone()
        checkpoint = None
        if seal_row is not None:
            try:
                check = _verify_checkpoint(conn, seal_row)
            except Exception:  # domain: degrade-silently - checkpoint verification never breaks /economy
                check = {
                    "ok": False,
                    "chain_ok": False,
                    "seals_checked": 0,
                    "sealed_entry_count": 0,
                    "live_entry_count": 0,
                    "sealed_supply_quarters": 0,
                    "sealed_supply_credits": _fmt(0),
                    "live_supply_quarters": 0,
                    "live_supply_credits": _fmt(0),
                }
            try:
                checkpoint = {
                    "created_at": seal_row["created_at"],
                    "last_entry_id": seal_row["last_entry_id"],
                    "entry_count": seal_row["entry_count"],
                    "total_supply_quarters": seal_row["total_supply_q"],
                    "total_supply_credits": _fmt(seal_row["total_supply_q"]),
                    "treasury_quarters": seal_row["treasury_q"],
                    "treasury_credits": _fmt(seal_row["treasury_q"]),
                    "running_hash": seal_row["running_hash"],
                    **check,
                }
            except Exception:  # domain: degrade-silently - malformed checkpoint row never breaks /economy
                checkpoint = {
                    "created_at": seal_row["created_at"]
                    if "created_at" in seal_row.keys()
                    else "",
                    "last_entry_id": seal_row["last_entry_id"]
                    if "last_entry_id" in seal_row.keys()
                    else 0,
                    "entry_count": seal_row["entry_count"]
                    if "entry_count" in seal_row.keys()
                    else 0,
                    "total_supply_quarters": 0,
                    "total_supply_credits": _fmt(0),
                    "treasury_quarters": 0,
                    "treasury_credits": _fmt(0),
                    "running_hash": "",
                    **check,
                }

        supply_q = totals["s"]
        try:
            runway = _runway_estimate(
                windows["week"],
                treasury_q,
                enabled=bool(config.ECONOMY_RUNWAY and config.TREASURY_FUNDS_PAYOUTS),
            )
        except (
            Exception
        ):  # domain:degrade-silently - a runway hiccup never breaks /economy
            runway = _runway_estimate({}, 0, enabled=False)
        return {
            "prev_flows": prev_windows,
            "entry_count": totals["n"],
            "total_supply_quarters": supply_q,
            "total_supply_credits": _fmt(supply_q),
            "treasury_quarters": treasury_q,
            "treasury_credits": _fmt(treasury_q),
            "circulating_quarters": supply_q - treasury_q,
            "circulating_credits": _fmt(supply_q - treasury_q),
            "committed_to_active_stakes_quarters": committed,
            "committed_to_active_stakes_credits": _fmt(committed),
            "held_in_job_escrow_quarters": job_escrow,
            "held_in_job_escrow_credits": _fmt(job_escrow),
            "open_jobs": jobs_open,
            "active_jobs": jobs_engaged,
            "flows": windows,
            "top_holders": holders,
            "checkpoint": checkpoint,
            "runway": runway,
            "config": {
                "funds_payouts": bool(config.TREASURY_FUNDS_PAYOUTS),
                "runway_enabled": bool(
                    config.ECONOMY_RUNWAY and config.TREASURY_FUNDS_PAYOUTS
                ),
                "tx_fee_percent": config.TX_FEE_PERCENT,
                "daily_admin_cap_credits": config.ADMIN_MINT_DAILY_CAP_CREDITS,
                "checkpoint_seconds": config.ECONOMY_CHECKPOINT_SECONDS,
            },
        }
