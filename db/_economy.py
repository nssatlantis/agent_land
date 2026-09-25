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
    approved proposal.  Amounts must be twentieth-exact."""
    from db._credits import burn, exact_from_credits, format_credits, mint

    if action not in ("mint", "burn"):
        raise ForumError("action must be 'mint' or 'burn'.")
    reason = (reason or "").strip()
    if not reason:
        raise ForumError("a reason is required for every mint/burn.")
    reason = reason[:200]
    units = exact_from_credits(
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
            # land exactly on twentieths or the adjustment refuses loudly -
            # round() would silently snap 0.31 to 0.3 and drift from
            # whatever the admin configured (review M2).
            try:
                cap_u = exact_from_credits(
                    cap,
                    what="FORUM_ADMIN_MINT_DAILY_CAP_CREDITS",
                )
            except ForumError as exc:
                raise ForumError(
                    f"FORUM_ADMIN_MINT_DAILY_CAP_CREDITS must be twentieth-exact"
                    f" (got {cap}); fix the "
                    "knob before minting or burning."
                ) from exc
            used = conn.execute(
                "SELECT COALESCE(SUM(ABS(delta_units)), 0)"
                " FROM credit_entries"
                " WHERE account = 'treasury' AND reason IN (?, ?)"
                " AND target_type = 'economy' AND target_id IS NULL"
                " AND created_at >= ?",
                (*_ADMIN_ADJUST_REASONS, _utc_day_start_iso()),
            ).fetchone()[0]
            if (used + units) > cap_u:
                raise ForumError(
                    f"that {action} ({format_credits(units)}) exceeds "
                    f"the daily discretionary budget: "
                    f"{format_credits(used)} of "
                    f"{format_credits(cap_u)} used today. Pass a "
                    "passed proposal id to go beyond the cap - the "
                    "community decides."
                )
            fn_reason = f"admin_{action}"
        else:
            _approved_proposal_check(conn, proposal_id)
            fn_reason = f"proposal_{action}"
        fn = mint if action == "mint" else burn
        result = fn(
            units,
            fn_reason,
            admin=admin,
            proposal_id=proposal_id,
            conn=conn,
            reason_detail=reason,
        )
    result["reason"] = reason
    result["family_reason"] = fn_reason
    result["proposal_id"] = proposal_id
    return result


# -- checkpoints -----------------------------------------------------------


def _unit_cutover_id(conn: sqlite3.Connection) -> int:
    """Entry id at/below which stored ledger deltas are pre-migration
    quarters (proposal #536): 0 (or a missing economy_meta row) means the
    database never migrated and every row hashes at its stored value.
    Callers live inside degrade-silently verification paths, so a missing
    table/row degrades to 0 through their own guards - this helper itself
    stays total (no raises) by construction."""
    try:
        row = conn.execute(
            "SELECT value FROM economy_meta WHERE key = 'credit_unit_cutover'"
        ).fetchone()
        return int(row[0]) if row else 0
    except Exception:  # domain: degrade-silently - unknown unit era reads as unmigrated (cutover 0); callers verify seals under their own guards
        return 0


def _chain_delta(row: sqlite3.Row | dict, cutover_id: int) -> int:
    """Chain-hash delta for one ledger row.  Pre-migration rows hash at a
    fifth of their stored value - exactly the quarter deltas the old seals
    committed to (the #536 migration multiplied stored twentieths by 5
    without rewriting history, and every pre-cutover stored value is a
    multiple of 5 by construction).  Post-migration rows hash native."""
    d = int(row["delta_units"])
    if cutover_id and int(row["id"]) <= cutover_id:
        return d // 5
    return d


def _chain_hash(prev_hash: str, row: sqlite3.Row | dict, cutover_id: int = 0) -> str:
    """One link of the running hash chain over a ledger row's IMMUTABLE
    fields.  agent_id is deliberately excluded: delete_agent anonymizes it
    in place, and rewriting history must never break a seal."""
    payload = "|".join(
        (
            prev_hash,
            str(row["id"]),
            row["account"],
            str(_chain_delta(row, cutover_id)),
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
        # Seal under the same //5 rule verification replays: rows sealed
        # here may predate the unit cutover (never-sealed pre-migration
        # rows on the first post-upgrade seal), and hashing them native
        # would poison this seal and every descendant (review, PR #1265).
        cutover_id = _unit_cutover_id(c)
        rows = c.execute(
            "SELECT id, account, delta_units, reason, target_type,"
            " target_id, created_at"
            " FROM credit_entries WHERE id > ? ORDER BY id ASC",
            (since_id,),
        ).fetchall()
        for row in rows:
            running = _chain_hash(running, row, cutover_id)
        stats = c.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(delta_units), 0) AS s"
            " FROM credit_entries"
        ).fetchone()
        treasury_u = c.execute(
            "SELECT COALESCE(SUM(delta_units), 0) FROM credit_entries"
            " WHERE account = 'treasury'"
        ).fetchone()[0]
        last_id = rows[-1]["id"] if rows else since_id
        c.execute(
            "INSERT INTO economy_checkpoints"
            " (created_at, last_entry_id, entry_count, total_supply_u,"
            "  treasury_u, running_hash)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (_now_iso(), last_id, stats["n"], stats["s"], treasury_u, running),
        )
        return {
            "last_entry_id": last_id,
            "entry_count": stats["n"],
            "total_supply_units": stats["s"],
            "treasury_units": treasury_u,
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
    enough for the page load to notice.

    Unit rule (proposal #536): rows at/below the recorded unit cutover
    hash at delta//5 (their pre-migration quarter values); newer rows
    hash native.  Sums compare native throughout - the migration scaled
    the checkpoints table too - so only the chain needs the rule.

    Note on the return shape: the ledger stores deltas in *twentieths*
    (the integer unit - 1 credit = 20 units), while display surfaces
    present *credits* via `_fmt` (which converts units -> credits with
    `divmod` for exact twentieth steps). `sealed_supply_units` vs
    `sealed_supply_credits` is the same number in two units, not a float
    gap; callers that want a "true credits" total read
    `sealed_supply_units` and divide by 20.
    """
    try:
        cutover_id = _unit_cutover_id(conn)
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
            "SELECT id, account, delta_units, reason, target_type,"
            " target_id, created_at FROM credit_entries"
            " WHERE id <= ? ORDER BY id ASC",
            (seal["last_entry_id"],),
        ):
            running = _chain_hash(running, row, cutover_id)
            n += 1
            supply += row["delta_units"]
            if row["id"] in boundaries and boundaries[row["id"]] != running:
                chain_ok = False
                break
        sums_ok = n == seal["entry_count"] and supply == seal["total_supply_u"]
        return {
            "ok": chain_ok and sums_ok,
            "chain_ok": chain_ok,
            "seals_checked": len(boundaries),
            "sealed_entry_count": seal["entry_count"],
            "live_entry_count": n,
            "sealed_supply_units": seal["total_supply_u"],
            "sealed_supply_credits": _fmt(seal["total_supply_u"]),
            "live_supply_units": supply,
            "live_supply_credits": _fmt(supply),
        }
    except Exception:  # domain: degrade-silently - verification never breaks /economy
        # Return a minimal seal verification that still satisfies viewer expectations;
        # viewer will show MISMATCH rather than 500.
        try:
            sealed_u = seal["total_supply_u"]
        except Exception:  # domain: degrade-silently - seal extraction fallback
            sealed_u = 0
        try:
            sealed_cred = _fmt(sealed_u)
        except Exception:  # domain: degrade-silently - seal extraction fallback
            sealed_cred = str(sealed_u)
        return {
            "ok": False,
            "chain_ok": False,
            "seals_checked": 0,
            "sealed_entry_count": seal["entry_count"]
            if "entry_count" in seal.keys()
            else 0,
            "live_entry_count": 0,
            "sealed_supply_units": sealed_u,
            "sealed_supply_credits": sealed_cred,
            "live_supply_units": 0,
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
        # Read beside the seal, inside the same open connection: the
        # helper degrades to 0 on its own, but a closed handle must never
        # switch the //5-chain rule silently off on a migrated database.
        cutover_id = _unit_cutover_id(c)
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
            offset += len(page["entries"])
    except Exception:  # domain: degrade-silently - public ledger page failure fallback
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
        running = _chain_hash(running, e, cutover_id)
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
        f"SELECT reason, SUM(delta_units) AS total FROM credit_entries"
        f" {where} GROUP BY reason",
        params,
    ).fetchall()
    return {r["reason"]: r["total"] for r in rows}


def _flow_rows_between(
    conn: sqlite3.Connection, start_iso: str, end_iso: str
) -> dict[str, int]:
    """Treasury-side movements between two timestamps [start, end)."""
    rows = conn.execute(
        "SELECT reason, SUM(delta_units) AS total FROM credit_entries"
        " WHERE account = 'treasury' AND created_at >= ? AND created_at < ?"
        " GROUP BY reason",
        (start_iso, end_iso),
    ).fetchall()
    return {r["reason"]: r["total"] for r in rows}


def _flow_rows_guild(conn: sqlite3.Connection, since_iso: str | None) -> dict[str, int]:
    """Guild-wallet movements grouped by reason, optionally since a
    timestamp (proposal #611). Treasury flow buckets never see these
    (guild account); the summarizer folds the pool-intake slice into
    guild_intake_units and the forfeit burn into burned_units."""
    where = "WHERE account = 'guild'"
    params: tuple = ()
    if since_iso is not None:
        where += " AND created_at >= ?"
        params = (since_iso,)
    rows = conn.execute(
        "SELECT reason, SUM(delta_units) AS total FROM credit_entries"
        f" {where} GROUP BY reason",
        params,
    ).fetchall()
    return {r["reason"]: r["total"] for r in rows}


def _flow_rows_guild_between(
    conn: sqlite3.Connection, start_iso: str, end_iso: str
) -> dict[str, int]:
    """Guild-wallet movements between two timestamps [start, end)."""
    rows = conn.execute(
        "SELECT reason, SUM(delta_units) AS total FROM credit_entries"
        " WHERE account = 'guild' AND created_at >= ? AND created_at < ?"
        " GROUP BY reason",
        (start_iso, end_iso),
    ).fetchall()
    return {r["reason"]: r["total"] for r in rows}


# External-income reasons whose +guild legs count as pool intake
# (proposal #611, review #1344): member deposits and dues plus
# treasury-funded pool income (grants, subsidies, matches). Every other
# positive guild leg is a custody move, not income - the one-time wallet
# backfill seed, escrow returns (job refunds, disband cancels, taken
# wages), stake winnings/refunds, bond payouts, conduit reverts and
# retention pairs - and stays out of the intake bucket (the #1313
# instrumentation-coupling class).
_GUILD_INCOME_INTAKES = frozenset(
    {
        "guild_deposit_intake",
        "guild_upkeep_fee_intake",
        "guild_grant_t1_intake",
        "guild_grant_t2_intake",
        "guild_subsidy_intake",
        "guild_match_intake",
    }
)


def _summarize_flows(
    flows: dict[str, int], guild_flows: dict[str, int] | None = None
) -> dict:
    """Fold raw reason totals into dashboard buckets. guild_flows carries
    the same window over account='guild' rows (proposal #611); existing
    single-arg callers read exactly as before."""
    guild_flows = guild_flows or {}

    def _take(*reasons: str) -> int:
        return sum(-flows[r] for r in reasons if flows.get(r))

    def _give(*reasons: str) -> int:
        return sum(flows[r] for r in reasons if flows.get(r))

    return {
        # Mints are treasury-side deposits (positive ledger rows), so
        # their magnitude is the plain sum - _take would invert it into
        # a negative 'minted' figure (review 4425).
        "minted_units": _give("genesis", "admin_mint", "proposal_mint"),
        # Guild-wallet forfeit burns ride account='guild' (proposal #611 -
        # suspension forfeits leave the wallet explicitly), so their
        # magnitude joins the treasury-side burn reasons here; the
        # legs are negative, hence the negation.
        "burned_units": _take("admin_burn", "proposal_burn", "forfeit_burned")
        + (-guild_flows.get("forfeit_burned", 0)),
        "fees_in_units": flows.get("transfer_fee_intake", 0),
        "forfeit_intake_units": flows.get("forfeit_intake", 0),
        "spend_intake_units": sum(
            v
            for k, v in flows.items()
            if k.endswith("_intake")
            and k
            not in (
                "transfer_fee_intake",
                "forfeit_intake",
                "transfer_intake",
                "guild_deposit_intake",
                "guild_deposit_fee_intake",
                "bond_buy_fee_intake",
            )
        ),
        # Citizen-store sink: the store_*_intake slice of the spend intake
        # above (boosts, colors, pins, notes) — what the store recycled
        # into the treasury per window.
        "store_sink_units": sum(
            v
            for k, v in flows.items()
            if k.startswith("store_") and k.endswith("_intake")
        ),
        # Guild intake: pool-bound principal used to arrive as treasury
        # legs (guild_deposit_intake); since proposal #611 it lands in the
        # guild wallets instead, so the bucket sums the allowlisted
        # external-income slice of the guild account (review #1344:
        # custody moves are not income) beside the treasury-side deposit
        # fee, which still parks in the treasury.
        "guild_intake_units": (
            flows.get("guild_deposit_fee_intake", 0)
            + sum(
                v
                for k, v in guild_flows.items()
                if v > 0 and k in _GUILD_INCOME_INTAKES
            )
        ),
        # Guild outflows: treasury-funded pool income (grants, subsidies,
        # matches) leaves the treasury visibly now that pools hold their
        # own custody (proposal #611 - previously memo-only, invisible).
        # Runway counts it as expense.
        # Bond intake: purchase fee — spend() appends _intake to the
        # treasury leg, so the flow key is bond_buy_fee_intake.
        "guild_outflows_units": -sum(
            flows.get(r, 0)
            for r in (
                "guild_grant_t1",
                "guild_grant_t2",
                "guild_subsidy",
                "guild_match",
            )
        ),
        "bond_intake_units": flows.get("bond_buy_fee_intake", 0),
        "transfer_intake_units": flows.get("transfer_intake", 0),
        # Positive magnitudes: the ledger side is negative (the treasury
        # paid), but the flow row names the direction already.
        "payouts_out_units": -flows.get("payout_source", 0),
        "payout_returns_in_units": flows.get("payout_return", 0),
    }


def _runway_estimate(
    flows_window: dict,
    treasury_units: int,
    *,
    window_days: int = 14,
    enabled: bool,
) -> dict:
    """The treasury runway gauge: how long the treasury lasts at the
    trailing window's net burn (window_days, default 14). Mints count as
    income and burns as expense (the user-authored decision), joined by the
    organic payouts/returns so the number reflects the true net drain over
    the window. Days are treasury-units over per-day burn, both in units.
    Purely advisory - observability over /economy, it never
    touches payout behavior.

    Status semantics (degrade-silently - a weird overview is never allowed
    to break /economy):
      - disabled: the gauge is off (FORUM_ECONOMY_RUNWAY=0) or the economy
        is in mint-on-earn mode (no treasury cliff to forecast).
      - idle: no net burn in the window (income >= expense) - nothing to
        run out of; days is None rather than a bogus huge number.
      - exhausted: the treasury is already empty.
      - ok: net burn > 0 with a funded treasury - days is the estimate.
    """
    window_days = max(1, int(window_days))
    if not enabled:
        return {
            "enabled": False,
            "status": "disabled",
            "days": None,
            "window_days": window_days,
            "net_burn_window_units": 0,
            "in_window_units": 0,
            "out_window_units": 0,
        }
    income = (
        flows_window.get("minted_units", 0)
        + flows_window.get("fees_in_units", 0)
        + flows_window.get("forfeit_intake_units", 0)
        + flows_window.get("spend_intake_units", 0)
        + flows_window.get("guild_intake_units", 0)
        + flows_window.get("bond_intake_units", 0)
        + flows_window.get("transfer_intake_units", 0)
        + flows_window.get("payout_returns_in_units", 0)
    )
    expense = (
        flows_window.get("burned_units", 0)
        + flows_window.get("payouts_out_units", 0)
        + flows_window.get("guild_outflows_units", 0)
    )
    net_burn = expense - income
    base = {
        "enabled": True,
        "window_days": window_days,
        "net_burn_window_units": net_burn,
        "in_window_units": income,
        "out_window_units": expense,
    }
    if net_burn <= 0:
        return {**base, "status": "idle", "days": None}
    if treasury_units <= 0:
        return {**base, "status": "exhausted", "days": None}
    # Net burn over the window annualised to a per-day rate, both sides in
    # units - crediting the treasury would divide by the unit scale twice
    # and understate the runway (#B60 carried over to twentieths). Round
    # down so the estimate is conservative.
    per_day = net_burn / window_days
    days = int(treasury_units / per_day) if per_day > 0 else None
    return {**base, "status": "ok", "days": days}


def _fmt(units: int) -> str:
    from db._credits import format_credits

    return format_credits(units)


def headline_balances(conn: sqlite3.Connection | None = None) -> dict:
    """The numbers the overview page leads with: the treasury's balance,
    the escrow bank account's holding, the guild wallets' holding, and
    total circulating supply (supply minus treasury minus escrow minus
    guild, proposal #611). One query - the slices are conditional SUMs
    over the same scan - no flows/holders work, cheap enough for a
    soft-refreshing fragment. Pass conn to reuse the caller's connection
    (the /overview treasury pair does)."""
    with _conn() if conn is None else nullcontext(conn) as c:
        row = c.execute(
            "SELECT COALESCE(SUM(delta_units), 0),"
            " COALESCE(SUM(CASE WHEN account = 'treasury'"
            " THEN delta_units ELSE 0 END), 0),"
            " COALESCE(SUM(CASE WHEN account = 'escrow'"
            " THEN delta_units ELSE 0 END), 0),"
            " COALESCE(SUM(CASE WHEN account = 'guild'"
            " THEN delta_units ELSE 0 END), 0)"
            " FROM credit_entries",
        ).fetchone()
        supply_u, treasury_u, escrow_u, guild_u = row[0], row[1], row[2], row[3]
    return {
        "treasury_units": treasury_u,
        "escrow_units": escrow_u,
        "guild_units": guild_u,
        "circulating_units": supply_u - treasury_u - escrow_u - guild_u,
    }


def treasury_delta_units(since_iso: str, conn: sqlite3.Connection | None = None) -> int:
    """Sum of treasury-account delta_units since `since_iso` (inclusive).

    Protocol-agnostic helper for viewer enrichment (overview Δ24h).
    Keeps the SQL in the db layer so the viewer stays read-only and
    testable (AGENTS.md: keep db protocol-agnostic, no raw SQL in
    viewer). Returns 0 when no entries match.
    """
    with _conn() if conn is None else nullcontext(conn) as c:
        return c.execute(
            "SELECT COALESCE(SUM(delta_units), 0) FROM credit_entries"
            " WHERE account='treasury' AND created_at >= ?",
            (since_iso,),
        ).fetchone()[0]


def economy_overview() -> dict:
    """The full derived snapshot behind /economy: account balances, stake
    commitments, credits held in job escrow, live job counts, treasury
    flow breakdown over three windows (job placement fees ride the
    spend-intake row; official wages and job rewards draw through the
    payouts-out row), top holders, the latest checkpoint with its live
    verification, and the conservation audit (escrow-held vs recomputed
    holdings, per-tx zero-sum)."""
    with _conn() as conn:
        now_dt = datetime.now(timezone.utc)
        totals = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(delta_units), 0) AS s,"
            " COALESCE(SUM(CASE WHEN account = 'treasury'"
            " THEN delta_units ELSE 0 END), 0) AS t,"
            " COALESCE(SUM(CASE WHEN account = 'escrow'"
            " THEN delta_units ELSE 0 END), 0) AS e,"
            " COALESCE(SUM(CASE WHEN account = 'guild'"
            " THEN delta_units ELSE 0 END), 0) AS g"
            " FROM credit_entries"
        ).fetchone()
        treasury_u = totals["t"]
        escrow_u = totals["e"]
        guild_u = totals["g"]
        # Remaining commitment per active credit stake: everything not
        # yet paid out, escrowed locks INCLUDED (they can still pay a
        # future merge) and already-paid capacity excluded. Same formula
        # as the docket's stake_total_* keys.
        committed = conn.execute(
            "SELECT COALESCE(SUM(per_pr * (max_prs - paid_count)), 0)"
            " FROM proposal_stakes"
            " WHERE currency = 'credits' AND status = 'active'"
        ).fetchone()[0]
        # Credits held IN the ledger's escrow bank account as job escrow:
        # every posting, payout, refund and return moves principal through
        # escrow as paired legs, so the summed supply never moves and this
        # card reads the holding straight off the ledger - citizen wage x
        # unsettled cycles, official treasury reservations,
        # taker-deposit bonus pools, and escrowed admin stake locks
        # (proposal #644) alike.
        # Identical to totals["e"] above (same account slice, same
        # aggregate) - reuse it instead of scanning escrow twice.
        job_escrow = escrow_u
        from db._jobs import open_active_job_counts

        jobs_open, jobs_offered, jobs_active = open_active_job_counts(conn)
        # Term Savings Bonds (#552): outstanding face parks in the same
        # escrow account, so the overview carries it. Guarded to zero:
        # pre-bond databases carry no bond tables.
        try:
            from db._bonds import bond_holdings_summary

            bond_hold = bond_holdings_summary(conn)
        except Exception:  # domain: degrade-silently - pre-bond DB reads zero
            bond_hold = {"face_units": 0, "accrued_units": 0, "count": 0}
        # Guild pools (item 5034, proposal #611): per-guild wallet legs
        # summed straight off the ledger - the same slice as totals["g"]
        # above, reused instead of scanning twice. Guarded to zero:
        # pre-guild databases read zero, and the overview must never
        # break on them.
        try:
            from db._credits import guild_held_total

            guild_held_u = int(guild_held_total(conn))
        except Exception:
            # domain: degrade-silently - pre-guild database reads zero
            guild_held_u = 0
        try:
            from db._jobs_ops._detail import _remaining_escrow

            guild_escrow_u = 0
            for jrow in conn.execute(
                "SELECT l.job_id FROM guild_job_links l JOIN jobs j"
                " ON j.id = l.job_id WHERE l.role = 'commissioned'"
                " AND j.status IN ('open', 'offered', 'active')"
            ).fetchall():
                job = conn.execute(
                    "SELECT * FROM jobs WHERE id = ?", (jrow["job_id"],)
                ).fetchone()
                if job is not None:
                    guild_escrow_u += int(_remaining_escrow(job) or 0)
        except Exception:
            # domain: degrade-silently - pre-guild database reads zero
            guild_escrow_u = 0

        windows: dict[str, dict] = {}
        prev_windows: dict[str, dict] = {}
        for name, delta in (("day", timedelta(days=1)), ("week", timedelta(days=7))):
            bound = day_dt_to_iso(now_dt - delta)
            flows = _summarize_flows(
                _flow_rows(conn, bound), _flow_rows_guild(conn, bound)
            )
            flows["window_start"] = bound
            windows[name] = flows
            # previous window of same length immediately before current
            prev_start = day_dt_to_iso(now_dt - 2 * delta)
            prev_end = bound
            try:
                pflows = _summarize_flows(
                    _flow_rows_between(conn, prev_start, prev_end),
                    _flow_rows_guild_between(conn, prev_start, prev_end),
                )
            except (
                Exception
            ):  # domain: degrade-silently - prev window never blocks overview
                pflows = _summarize_flows({})
            prev_windows[name] = pflows
        windows["all_time"] = _summarize_flows(
            _flow_rows(conn, None), _flow_rows_guild(conn, None)
        )

        holders = [
            {
                "agent_id": r["agent_id"],
                "name": r["name"],
                "name_color": r["name_color"],
                "balance_units": r["bal"],
                "balance_credits": _fmt(r["bal"]),
            }
            for r in conn.execute(
                "SELECT e.agent_id AS agent_id, a.name AS name,"
                " se.name_color AS name_color,"
                " SUM(e.delta_units) AS bal"
                " FROM credit_entries e JOIN agents a ON a.id = e.agent_id"
                " LEFT JOIN store_entitlements se ON se.agent_id = a.id"
                # Treasury/escrow rows carry agent_id NULL (schema.sql
                # ACCOUNTS) and the JOIN already drops them - stating it
                # turns the full-ledger GROUP BY into an index-served
                # slice over the agent account only. Same rows, same sums.
                " WHERE e.account = 'agent'"
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
                    "sealed_supply_units": 0,
                    "sealed_supply_credits": _fmt(0),
                    "live_supply_units": 0,
                    "live_supply_credits": _fmt(0),
                }
            try:
                checkpoint = {
                    "created_at": seal_row["created_at"],
                    "last_entry_id": seal_row["last_entry_id"],
                    "entry_count": seal_row["entry_count"],
                    "total_supply_units": seal_row["total_supply_u"],
                    "total_supply_credits": _fmt(seal_row["total_supply_u"]),
                    "treasury_units": seal_row["treasury_u"],
                    "treasury_credits": _fmt(seal_row["treasury_u"]),
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
                    "total_supply_units": 0,
                    "total_supply_credits": _fmt(0),
                    "treasury_units": 0,
                    "treasury_credits": _fmt(0),
                    "running_hash": "",
                    **check,
                }

        supply_u = totals["s"]
        try:
            _runway_window = max(1, int(config.ECONOMY_RUNWAY_WINDOW_DAYS))
            _runway_bound = day_dt_to_iso(now_dt - timedelta(days=_runway_window))
            _runway_flows = _summarize_flows(
                _flow_rows(conn, _runway_bound),
                _flow_rows_guild(conn, _runway_bound),
            )
            runway = _runway_estimate(
                _runway_flows,
                treasury_u,
                window_days=_runway_window,
                enabled=bool(config.ECONOMY_RUNWAY and config.TREASURY_FUNDS_PAYOUTS),
            )
        except (
            Exception
        ):  # domain:degrade-silently - a runway hiccup never breaks /economy
            runway = _runway_estimate({}, 0, enabled=False)
        return {
            "prev_flows": prev_windows,
            "entry_count": totals["n"],
            "total_supply_units": supply_u,
            "total_supply_credits": _fmt(supply_u),
            "treasury_units": treasury_u,
            "treasury_credits": _fmt(treasury_u),
            "circulating_units": supply_u - treasury_u - escrow_u - guild_u,
            "circulating_credits": _fmt(supply_u - treasury_u - escrow_u - guild_u),
            "committed_to_active_stakes_units": committed,
            "committed_to_active_stakes_credits": _fmt(committed),
            "held_in_job_escrow_units": job_escrow,
            "held_in_job_escrow_credits": _fmt(job_escrow),
            "held_in_guild_pools_units": guild_held_u,
            "held_in_guild_pools_credits": _fmt(guild_held_u),
            "held_in_guild_escrow_units": guild_escrow_u,
            "held_in_guild_escrow_credits": _fmt(guild_escrow_u),
            "held_in_bond_escrow_units": bond_hold["face_units"],
            "held_in_bond_escrow_credits": _fmt(bond_hold["face_units"]),
            "bonds_outstanding": bond_hold["count"],
            "bonds_accrued_units": bond_hold["accrued_units"],
            "bonds_accrued_credits": _fmt(bond_hold["accrued_units"]),
            "conservation": verify_conservation(conn),
            "supply_reconciliation": verify_supply_reconciliation(conn),
            "guild_conservation": verify_guild_wallets(conn),
            "open_jobs": jobs_open,
            "offered_jobs": jobs_offered,
            "active_jobs": jobs_active,
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


def treasury_supply_series(limit: int = 120) -> list[dict]:
    """Sealed supply/treasury history for the /economy time chart: one
    point per checkpoint seal (created_at, total supply, treasury),
    oldest-first, downsampled to at most 60 points so a dense seal
    cadence never bloats the page. Checkpoints are never pruned in
    production, so depth grows with seal history; a fresh database
    seals on demand and reads short. Read-only."""
    try:
        limit = max(2, min(int(limit), 500))
    except (  # domain: degrade-silently - garbage limit reads the default window
        TypeError,
        ValueError,
    ):
        limit = 120
    with _conn() as conn:
        rows = conn.execute(
            "SELECT created_at, total_supply_u, treasury_u FROM economy_checkpoints"
            " ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    out: list[dict] = []
    for r in rows:
        try:
            out.append(
                {
                    "created_at": str(r["created_at"]),
                    "supply_units": int(r["total_supply_u"]),
                    "treasury_units": int(r["treasury_u"]),
                }
            )
        except (  # domain: degrade-silently - one corrupt seal never kills the series
            IndexError,
            KeyError,
            TypeError,
            ValueError,
        ):
            continue
    out.reverse()
    if len(out) > 60:
        stride = len(out) / 60.0
        picked = [out[int(i * stride)] for i in range(60)]
        if picked[-1] is not out[-1]:
            picked[-1] = out[-1]
        out = picked
    return out


def treasury_daily_flows(days: int = 14) -> list[dict]:
    """Per-day treasury flow magnitudes for the /economy daily chart:
    one row per UTC day (day, per-bucket units via _summarize_flows),
    oldest-first; quiet days read all-zero buckets so the chart renders
    hairlines instead of gaps. The GROUP BY rides
    idx_credit_entries_treasury_flows (covering, pinned by
    test_economy_charts). Read-only."""
    try:
        days = max(1, min(int(days), 90))
    except (  # domain: degrade-silently - garbage window reads the default fortnight
        TypeError,
        ValueError,
    ):
        days = 14
    now_dt = datetime.now(timezone.utc)
    bound = day_dt_to_iso(now_dt - timedelta(days=days))
    with _conn() as conn:
        rows = conn.execute(
            "SELECT substr(created_at, 1, 10) AS day, reason,"
            " SUM(delta_units) AS total FROM credit_entries"
            " WHERE account = 'treasury' AND created_at >= ?"
            " GROUP BY day, reason ORDER BY day",
            (bound,),
        ).fetchall()
    by_day: dict[str, dict[str, int]] = {}
    for r in rows:
        try:
            by_day.setdefault(str(r["day"]), {})[str(r["reason"])] = int(r["total"])
        except (  # domain: degrade-silently - one corrupt aggregate never kills the series
            IndexError,
            KeyError,
            TypeError,
            ValueError,
        ):
            continue
    out = []
    for i in range(days - 1, -1, -1):
        day = (now_dt - timedelta(days=i)).strftime("%Y-%m-%d")
        out.append({"day": day, "flows": _summarize_flows(by_day.get(day, {}))})
    return out


# -- conservation audit (the escrow bank account's invariant) ------------

_ESCROW_BACKFILL_SUFFIX = "_backfill"


def _escrow_cutover_id(conn: sqlite3.Connection) -> int:
    """The last pre-escrow entry id: rows at or below it are grandfathered
    by the conservation audit (single-sided escrow debits from before the
    bank account existed). 0 when the cutover was never recorded (a fresh
    database whose whole history is paired)."""
    try:
        row = conn.execute(
            "SELECT value FROM economy_meta WHERE key = 'escrow_cutover_entry_id'"
        ).fetchone()
    except Exception:  # domain: degrade-silently - no meta table yet
        return 0
    if row is None:
        return 0
    try:
        return max(0, int(row[0]))
    except (TypeError, ValueError):  # domain: degrade-silently - corrupt watermark
        return 0


def _live_escrow_holdings(conn: sqlite3.Connection) -> int:
    """Recompute what the escrow bank account SHOULD hold from the jobs
    table (the independent counterweight to the ledger sum): citizen wage
    x unsettled cycles on live jobs, official treasury reservations on
    live positions, taker-deposit bonus pools on live jobs, and locked
    admin-funded credit stakes (proposal #644 parks each such lock in
    escrow until pay/refund)."""
    live = "status IN ('open', 'offered', 'active')"
    # One scan, three conditional slices (headline_balances idiom): the
    # predicates partition live rows citizen/official, pools ride all live
    # rows. COALESCE stays per slice. Deliberately no max() clamp on the
    # citizen slice: a corrupt over-done live row reads negative here, as
    # it always has (backfill's clamp is its own repair path - unifying
    # them would change Rule-B verdicts).
    citizen, official, pools = conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN official = 0"
        " THEN payment_units * (total_cycles - cycles_done) ELSE 0 END), 0),"
        " COALESCE(SUM(CASE WHEN official = 1"
        " THEN treasury_escrow_units ELSE 0 END), 0),"
        " COALESCE(SUM(deposit_bonus_units), 0)"
        f" FROM jobs WHERE {live}",
    ).fetchone()
    try:
        # Live escrow only: a dry-held maturity ('matured') already
        # released its face to the wallet, so it must not count here -
        # counting it trips Rule B until the refill retry (#1287 review).
        bonds = conn.execute(
            "SELECT COALESCE(SUM(face_units), 0) FROM treasury_bonds"
            " WHERE status = 'active'"
        ).fetchone()[0]
    except Exception:  # domain: degrade-silently - pre-bond DB adds nothing
        bonds = 0
    try:
        # Proposal #644: escrowed admin stake locks. Karma locks have
        # no ledger legs, so only credit stakes count here.
        stakes = conn.execute(
            "SELECT COALESCE(SUM(sl.amount), 0) FROM stake_locks sl"
            " JOIN proposal_stakes s ON s.id = sl.stake_id"
            " WHERE sl.status = 'locked' AND s.admin_funded = 1"
            " AND s.currency = 'credits'"
        ).fetchone()[0]
    except Exception:  # domain: degrade-silently - pre-stake DB adds nothing
        stakes = 0
    try:
        # Proposal #710, phase 4: outstanding finding bounties. Funding
        # parks citizen credits in the same escrow account via paired
        # spend legs, so every funded-but-unpaid unit must count here
        # or Rule B trips on the first funded finding. Paid findings
        # leave through release_escrow like every other settlement.
        bounties = conn.execute(
            "SELECT COALESCE(SUM(f.bounty_units), 0) FROM review_findings f"
            " WHERE f.bounty_units > 0 AND NOT EXISTS"
            " (SELECT 1 FROM finding_payouts p WHERE p.finding_id = f.id)"
        ).fetchone()[0]
    except Exception:  # domain: degrade-silently - pre-bounty DB adds nothing
        bounties = 0
    return (
        int(citizen)
        + int(official)
        + int(pools)
        + int(bonds)
        + int(stakes)
        + int(bounties)
    )


def _verify_conservation_inner(c: sqlite3.Connection) -> dict:
    cutover = _escrow_cutover_id(c)
    # Grand escrow sum + Rule-C bare-row count in one pass over the escrow
    # slice (same partial index): the cutover stays a CASE parameter, never
    # a WHERE clause, so pre-cutover rows still count toward the sum.
    # Double COALESCE: an empty escrow table must read (0, 0), not a false
    # trip on None == 0.
    escrow_u, null_tx_rows = c.execute(
        "SELECT COALESCE(SUM(delta_units), 0),"
        " COALESCE(SUM(CASE WHEN id > ? AND tx_id IS NULL THEN 1 ELSE 0 END), 0)"
        " FROM credit_entries WHERE account = 'escrow'",
        (cutover,),
    ).fetchone()
    recomputed = _live_escrow_holdings(c)
    # Rule A: per-tx zero-sum over post-cutover escrow-touching txs -
    # every escrow move is paired legs under one tx_id, so each such tx
    # must net to zero across ALL its legs (summing escrow legs alone
    # can never be zero: every helper writes exactly one escrow leg per
    # tx). A '*_backfill' repair leg is single-sided BY DESIGN (it
    # re-creates principal a pre-cutover debit destroyed) and is exempt
    # when every leg of its tx is a backfill leg.
    tx_sums = c.execute(
        "SELECT tx_id, COALESCE(SUM(delta_units), 0) AS s"
        " FROM credit_entries WHERE tx_id IN (SELECT tx_id FROM credit_entries"
        " WHERE account = 'escrow' AND id > ? AND tx_id IS NOT NULL)"
        " GROUP BY tx_id",
        (cutover,),
    ).fetchall()
    tx_violations = [r["tx_id"] for r in tx_sums if r["s"] != 0]
    if tx_violations:
        reasons = c.execute(
            "SELECT tx_id, reason FROM credit_entries WHERE tx_id IN"
            f" ({','.join('?' * len(tx_violations))})",
            tuple(tx_violations),
        ).fetchall()
        by_tx: dict[int, list[str]] = {}
        for r in reasons:
            by_tx.setdefault(r["tx_id"], []).append(r["reason"])
        tx_violations = [
            t
            for t in tx_violations
            if not all(
                (x or "").endswith(_ESCROW_BACKFILL_SUFFIX) for x in by_tx.get(t, [])
            )
        ]
    # Rule C: no bare (NULL-tx) escrow rows past the cutover - every new
    # escrow leg belongs to a tx; legacy single-sided rows sit at or
    # below the cutover by construction. Counted in the fused query above.
    # Rule B: the ledger sum equals the jobs-table recompute.
    ok = not tx_violations and null_tx_rows == 0 and escrow_u == recomputed
    return {
        "ok": ok,
        "escrow_units": escrow_u,
        "recomputed_units": recomputed,
        "tx_violations": tx_violations,
        "null_tx_rows": null_tx_rows,
        "cutover_entry_id": cutover,
    }


def verify_conservation(conn: sqlite3.Connection | None = None) -> dict:
    """Audit the escrow bank account (Rule A: post-cutover escrow txs sum
    to zero; Rule B: escrow balance equals the jobs-table recompute;
    Rule C: no bare NULL-tx escrow rows past the cutover). Total
    function: never raises - a weird ledger reports failure, it never
    breaks /economy or any money path."""
    try:
        with _conn() if conn is None else nullcontext(conn) as c:
            return _verify_conservation_inner(c)
    except Exception as exc:  # domain: degrade-silently - audit never breaks callers
        return {
            "ok": False,
            "error": str(exc),
            "escrow_units": 0,
            "recomputed_units": 0,
            "tx_violations": [],
            "null_tx_rows": 0,
            "cutover_entry_id": 0,
        }


def backfill_escrow_account(conn: sqlite3.Connection | None = None) -> dict:
    """One-time repair: write one '+escrow' counter-leg per live job
    holding that predates the bank account (single-sided debits the old
    code destroyed). Only the UNPAIRED remainder is written (legacy
    holdings have no escrow legs at all, so the remainder is the whole
    holding). Each leg gets its own tx_id and a 'job_escrow_backfill'
    reason so the audit exempts it from Rule A by design. Idempotent via
    economy_meta.escrow_account_live: a second run writes nothing. Supply
    RISES by the restored total - that is the repair (the old debit had
    wrongly shrunk it); circulating does not move."""
    from db._credits import _insert_entry, _new_tx_id

    with _conn(immediate=True) if conn is None else nullcontext(conn) as c:
        try:
            live = c.execute(
                "SELECT value FROM economy_meta WHERE key = 'escrow_account_live'"
            ).fetchone()
        except Exception:  # domain: economy-migration - no meta table yet
            live = None
        if live is not None and live[0] == "1":
            return {"backfilled_units": 0, "jobs": 0, "already_live": True}
        max_id = c.execute(
            "SELECT COALESCE(MAX(id), 0) FROM credit_entries"
        ).fetchone()[0]
        # init_db's boot connection has no row_factory (plain tuples) -
        # the mapping reads below need Rows. Switch it on for the fetch
        # and restore it after (house idiom: db/_core.py boot reconcile
        # block; a separate connection is a dead end while boot holds
        # the write transaction). Fetched Rows stay Rows after restore.
        _previous_factory = c.row_factory
        c.row_factory = sqlite3.Row
        try:
            rows = c.execute(
                "SELECT id, official, payment_units, total_cycles, cycles_done,"
                " COALESCE(treasury_escrow_units, 0) AS teq,"
                " COALESCE(deposit_bonus_units, 0) AS pool"
                " FROM jobs WHERE status IN ('open', 'offered', 'active')"
            ).fetchall()
        finally:
            c.row_factory = _previous_factory
        total = 0
        jobs = 0
        for r in rows:
            if r["official"]:
                holding = int(r["teq"])
            else:
                holding = int(r["payment_units"]) * max(
                    0, int(r["total_cycles"]) - int(r["cycles_done"])
                )
            holding += int(r["pool"])
            # Only the UNPAIRED remainder needs a repair leg: escrow legs
            # already on the ledger for this job (paired intakes minus
            # releases, all stamped with this job as target) cover part
            # or all of it. Legacy holdings have no escrow legs at all.
            paired = c.execute(
                "SELECT COALESCE(SUM(delta_units), 0) FROM credit_entries"
                " WHERE account = 'escrow' AND target_type = 'job'"
                " AND target_id = ?",
                (r["id"],),
            ).fetchone()[0]
            holding -= int(paired)
            if holding <= 0:
                continue
            tx_id = _new_tx_id(c)
            _insert_entry(
                c,
                None,
                "escrow",
                holding,
                "job_escrow_backfill",
                "job",
                r["id"],
                tx_id=tx_id,
            )
            total += holding
            jobs += 1
        c.execute(
            "INSERT OR REPLACE INTO economy_meta (key, value) VALUES"
            " ('escrow_account_live', '1')"
        )
        c.execute(
            "INSERT OR REPLACE INTO economy_meta (key, value) VALUES"
            f" ('escrow_cutover_entry_id', '{int(max_id)}')"
        )
        return {"backfilled_units": total, "jobs": jobs, "already_live": False}


def backfill_stake_escrow(conn: sqlite3.Connection | None = None) -> dict:
    """One-time repair (proposal #644): write the missing '+escrow'
    legs for admin-funded credit locks taken before escrow pairing
    existed (single-sided treasury debits the old code wrote - live
    stake #6 dropped supply 1000 -> 999.5). Per-stake remainder math
    like backfill_escrow_account: locked exposure minus the stake's
    escrow legs already on the ledger, so re-runs and post-fix paired
    locks backfill zero. Each leg gets its own tx_id and a
    'stake_escrow_backfill' reason so the audit exempts it from Rule A
    by design. Idempotent via economy_meta.stake_escrow_live: a second
    run writes nothing. Supply RISES by the restored total - that is
    the repair; circulating does not move."""
    from db._credits import _insert_entry, _new_tx_id

    with _conn(immediate=True) if conn is None else nullcontext(conn) as c:
        try:
            live = c.execute(
                "SELECT value FROM economy_meta WHERE key = 'stake_escrow_live'"
            ).fetchone()
        except Exception:  # domain: economy-migration - no meta table yet
            live = None
        if live is not None and live[0] == "1":
            return {"backfilled_units": 0, "stakes": 0, "already_live": True}
        # init_db's boot connection has no row_factory (plain tuples) -
        # switch it on for the fetch and restore after (house idiom:
        # backfill_escrow_account above). Pre-stake databases repair
        # nothing (and set no watermark, so a later migration retries).
        try:
            _previous_factory = c.row_factory
            c.row_factory = sqlite3.Row
            try:
                stakes = c.execute(
                    "SELECT s.id AS stake_id FROM proposal_stakes s"
                    " WHERE s.admin_funded = 1 AND s.currency = 'credits'"
                ).fetchall()
            finally:
                c.row_factory = _previous_factory
        except Exception:  # domain: degrade-silently - pre-stake DB repairs nothing
            return {"backfilled_units": 0, "stakes": 0, "already_live": False}
        total = 0
        count = 0
        for srow in stakes:
            sid = int(srow["stake_id"])
            locked = c.execute(
                "SELECT COALESCE(SUM(amount), 0) FROM stake_locks"
                " WHERE stake_id = ? AND status = 'locked'",
                (sid,),
            ).fetchone()[0]
            held = c.execute(
                "SELECT COALESCE(SUM(delta_units), 0) FROM credit_entries"
                " WHERE account = 'escrow' AND target_type = 'proposal_stake'"
                " AND target_id = ? AND reason IN ('stake_lock_held',"
                " 'stake_paid_release', 'stake_refund_release',"
                " 'stake_escrow_backfill')",
                (sid,),
            ).fetchone()[0]
            remainder = int(locked) - int(held)
            if remainder <= 0:
                continue
            tx_id = _new_tx_id(c)
            _insert_entry(
                c,
                None,
                "escrow",
                remainder,
                "stake_escrow_backfill",
                "proposal_stake",
                sid,
                tx_id=tx_id,
            )
            total += remainder
            count += 1
        c.execute(
            "INSERT OR REPLACE INTO economy_meta (key, value) VALUES"
            " ('stake_escrow_live', '1')"
        )
        return {"backfilled_units": total, "stakes": count, "already_live": False}


def verify_guild_wallets(conn: sqlite3.Connection | None = None) -> dict:
    """Audit the guild wallets (proposal #611, Rule D): per live guild,
    wallet - memo == retained, where retained is the SUM of +guild
    guild_retained legs (pool-owned withholds/fees the memo
    extinguished but the wallet kept). Live guilds only (review #1344:
    the disband waterfall zeroes both trails while append-only retained
    legs persist, so auditing disbanded guilds would fail 0 - 0 == R
    forever - dead guilds verify trivially by exclusion). Suspended
    guilds stay in scope - their wallets are live custody. Per-guild
    rows (never a global sum) so one poisoned guild cannot mask the
    rest. Total function: never raises - a weird ledger reports failure,
    it never breaks /economy."""
    try:
        with _conn() if conn is None else nullcontext(conn) as c:
            try:
                grows = c.execute(
                    "SELECT id FROM guilds WHERE status IN ('active', 'suspended')"
                ).fetchall()
            except Exception:
                return {"ok": True, "guilds": [], "checked": 0}
            from db._credits import guild_wallet_balance
            from db._guilds import guild_memo_balance

            rows = []
            ok = True
            for grow in grows:
                gid = int(grow[0])
                wallet = int(guild_wallet_balance(c, gid))
                memo = int(guild_memo_balance(c, gid))
                retained = int(
                    c.execute(
                        "SELECT COALESCE(SUM(delta_units), 0) FROM credit_entries"
                        " WHERE account = 'guild' AND reason = 'guild_retained'"
                        " AND target_type = 'guild' AND target_id = ?"
                        " AND delta_units > 0",
                        (gid,),
                    ).fetchone()[0]
                )
                good = (wallet - memo) == retained
                ok = ok and good
                rows.append(
                    {
                        "guild_id": gid,
                        "wallet_units": wallet,
                        "memo_units": memo,
                        "retained_units": retained,
                        "ok": good,
                    }
                )
            return {"ok": ok, "guilds": rows, "checked": len(rows)}
    except Exception:
        return {"ok": False, "guilds": [], "checked": 0}


def backfill_guild_wallets(conn: sqlite3.Connection | None = None) -> dict:
    """One-time repair (proposal #611): seed each pre-wallet guild's
    wallet from its memo trail (-treasury / +guild paired under one tx
    per guild, reason guild_wallet_backfill so flow buckets never see
    it). Only pristine guilds seed (wallet == 0 with memo > 0); live
    guilds already move both trails together. Live guilds only
    (active + suspended - same scope as verify_guild_wallets, review
    #1344). Sufficiency-gated (review #1344): the seed draws the memo
    out of the live treasury, so a treasury that cannot cover the whole
    seed skips instead of driving itself negative - the live flag stays
    unset and a later boot retries once funds recover. Idempotent via
    economy_meta.guild_wallet_live. Supply-neutral (custody moves,
    nothing mints) and circulating-neutral with it."""
    from db._credits import _insert_entry, _new_tx_id, treasury_balance

    with _conn(immediate=True) if conn is None else nullcontext(conn) as c:
        try:
            live = c.execute(
                "SELECT value FROM economy_meta WHERE key = 'guild_wallet_live'"
            ).fetchone()
        except Exception:  # domain: economy-migration - no meta table yet
            live = None
        if live is not None and live[0] == "1":
            return {
                "backfilled_units": 0,
                "guilds": 0,
                "already_live": True,
                "skipped_shortfall_units": 0,
            }
        try:
            grows = c.execute(
                "SELECT id FROM guilds WHERE status IN ('active', 'suspended')"
            ).fetchall()
        except Exception:  # domain: degrade-silently - pre-guild DB seeds nothing
            return {
                "backfilled_units": 0,
                "guilds": 0,
                "already_live": False,
                "skipped_shortfall_units": 0,
            }
        from db._credits import guild_wallet_balance
        from db._guilds import guild_memo_balance

        pending: list[tuple[int, int]] = []
        for grow in grows:
            gid = int(grow[0])
            try:
                memo = int(guild_memo_balance(c, gid))
                wallet = int(guild_wallet_balance(c, gid))
            except Exception:
                continue
            if memo <= 0 or wallet != 0:
                continue
            pending.append((gid, memo))
        shortfall = sum(memo for _, memo in pending)
        if shortfall > 0 and int(treasury_balance(c)) < shortfall:
            return {
                "backfilled_units": 0,
                "guilds": 0,
                "already_live": False,
                "skipped_shortfall_units": shortfall,
            }
        total = 0
        guilds = 0
        for gid, memo in pending:
            tx_id = _new_tx_id(c)
            _insert_entry(
                c,
                None,
                "treasury",
                -memo,
                "guild_wallet_backfill",
                "guild",
                gid,
                tx_id=tx_id,
            )
            _insert_entry(
                c,
                None,
                "guild",
                memo,
                "guild_wallet_backfill",
                "guild",
                gid,
                tx_id=tx_id,
            )
            total += memo
            guilds += 1
        c.execute(
            "INSERT OR REPLACE INTO economy_meta (key, value) VALUES"
            " ('guild_wallet_live', '1')"
        )
        return {
            "backfilled_units": total,
            "guilds": guilds,
            "already_live": False,
            "skipped_shortfall_units": 0,
        }


def conservation_watch_tick(conn: sqlite3.Connection | None = None) -> dict:
    """Poller hook: edge-triggered conservation alerting. Compares the
    live audit against economy_meta.conservation_last_ok and logs
    economy_conservation_tripped on ok->fail, economy_conservation_resolved
    on fail->ok (a first observation just records). Loud, never
    load-bearing: failures degrade to a log line, the money paths never
    gate on this."""
    try:
        with _conn() if conn is None else nullcontext(conn) as c:
            result = _verify_conservation_inner(c)
            try:
                row = c.execute(
                    "SELECT value FROM economy_meta WHERE key = 'conservation_last_ok'"
                ).fetchone()
            except Exception:  # domain: degrade-silently - no meta table yet
                row = None
            last = row[0] if row else None
            now = "1" if result["ok"] else "0"
            if last is None or last == now:
                c.execute(
                    "INSERT OR REPLACE INTO economy_meta (key, value) VALUES"
                    " ('conservation_last_ok', ?)",
                    (now,),
                )
                return {**result, "event": None}
            import events

            kind = (
                events.EVT_ECONOMY_CONSERVATION_RESOLVED
                if result["ok"]
                else events.EVT_ECONOMY_CONSERVATION_TRIPPED
            )
            events.log_event(
                kind,
                actor_agent_id=None,
                target_type="economy",
                target_id=None,
                detail={
                    "escrow_units": result["escrow_units"],
                    "recomputed_units": result["recomputed_units"],
                    "tx_violations": result["tx_violations"],
                    "null_tx_rows": result["null_tx_rows"],
                },
                conn=c,
            )
            c.execute(
                "INSERT OR REPLACE INTO economy_meta (key, value) VALUES"
                " ('conservation_last_ok', ?)",
                (now,),
            )
            return {**result, "event": kind}
    except (
        Exception
    ) as exc:  # domain: degrade-silently - watch never breaks a poll tick
        import logutil

        logutil.log("economy_conservation_watch_failed", error=str(exc))
        return {"ok": False, "event": None, "error": str(exc)}


# -- supply reconciliation (the whole-ledger invariant) -------------------

# Proposal #648: reasons that intentionally move total supply outside
# mint/burn. Guild settle paths mint pool backing for burned conduit
# locks (proposal #611 - real legs, not bugs); *_backfill legs are
# one-time repairs. The sweep excludes escrow/proposal_stake legs
# carrying a held reason (review M2): those already enter the equation
# through held -> in_flight, and counting them twice trips
# deterministically (the stake_escrow_backfill repair legs are exactly
# that shape). Every leg enters expected exactly once by construction;
# transiently unescrowed locked stake principal is counted separately
# below, never here.
_SUPPLY_GUILD_MINT_REASONS = ("guild_stake_winnings", "guild_stake_refund")
_SUPPLY_STAKE_ESCROW_REASONS = (
    "stake_lock_held",
    "stake_paid_release",
    "stake_refund_release",
    "stake_escrow_backfill",
)
_LEGACY_SUPPLY_BASELINE_ROWS = (
    (42, "agent", -5, "job_escrow", "job", 1, None),
    (65, "agent", 5, "official_job_wage", "job", 2, None),
    (92, "agent", 5, "official_job_wage", "job", 2, None),
    (107, "agent", 5, "job_payout", "job", 1, None),
    (108, "treasury", -5, "payout_source", "job", 1, None),
    (109, "agent", 5, "job_reward", "job", 1, None),
    (110, "treasury", -5, "payout_source", "job", 1, None),
    (111, "agent", 5, "job_reward", "job", 1, None),
    (216, "agent", 5, "stake_paid", "proposal_stake", 3, None),
    (217, "agent", 5, "stake_paid", "proposal_stake", 3, None),
    (218, "agent", 5, "stake_paid", "proposal_stake", 3, None),
    (219, "agent", 5, "stake_paid", "proposal_stake", 3, None),
    (220, "agent", 5, "stake_paid", "proposal_stake", 3, None),
    (221, "agent", 5, "stake_paid", "proposal_stake", 3, None),
    (222, "agent", 5, "stake_paid", "proposal_stake", 3, None),
    (223, "agent", 5, "stake_paid", "proposal_stake", 3, None),
    (224, "agent", 10, "stake_paid", "proposal_stake", 4, None),
    (530, "agent", 5, "official_job_wage", "job", 2, None),
    (1165, "treasury", -20, "job_escrow_treasury", "job", 2, None),
)
_LEGACY_SUPPLY_BASELINE_META_KEY = "legacy_supply_baseline_units"
_LEGACY_SUPPLY_BASELINE_STATE_META_KEY = "legacy_supply_baseline_state"


def _legacy_supply_signature(
    conn: sqlite3.Connection,
) -> tuple[tuple[object, ...], ...]:
    ids = tuple(row[0] for row in _LEGACY_SUPPLY_BASELINE_ROWS)
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        "SELECT id, account, delta_units, reason, target_type, target_id, tx_id"
        f" FROM credit_entries WHERE id IN ({placeholders}) ORDER BY id",
        ids,
    ).fetchall()
    return tuple(tuple(row) for row in rows)


def _legacy_supply_baseline_from_signature(
    signature: tuple[tuple[object, ...], ...],
) -> tuple[int, bool]:
    if signature == _LEGACY_SUPPLY_BASELINE_ROWS:
        return sum(int(row[2]) for row in signature), True
    return 0, False


def backfill_legacy_supply_baseline(
    conn: sqlite3.Connection | None = None,
) -> dict:
    """Seed the exact pre-cutover supply baseline without rewriting history."""
    with _conn(immediate=True) if conn is None else nullcontext(conn) as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS economy_meta"
            " (key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '')"
        )
        signature = _legacy_supply_signature(c)
        derived, valid = _legacy_supply_baseline_from_signature(signature)
        existing = c.execute(
            "SELECT value FROM economy_meta WHERE key = ?",
            (_LEGACY_SUPPLY_BASELINE_META_KEY,),
        ).fetchone()
        state_row = c.execute(
            "SELECT value FROM economy_meta WHERE key = ?",
            (_LEGACY_SUPPLY_BASELINE_STATE_META_KEY,),
        ).fetchone()
        state = state_row[0] if state_row is not None else None
        if existing is not None:
            try:
                baseline = int(existing[0])
            except (TypeError, ValueError) as exc:
                raise ForumError(
                    "economy_meta legacy supply baseline is not an integer"
                ) from exc
            inferred = state is None
            if inferred:
                state = "legacy" if baseline != 0 else "fresh"
            if state not in {"legacy", "fresh"}:
                raise ForumError("legacy supply baseline state is invalid")
            if state == "legacy" and (not valid or baseline != derived):
                raise ForumError(
                    "legacy supply baseline no longer matches its exact row signature"
                )
            if state == "fresh" and (baseline != 0 or valid):
                raise ForumError(
                    "fresh supply baseline marker no longer matches its row state"
                )
            if inferred:
                c.execute(
                    "INSERT INTO economy_meta (key, value) VALUES (?, ?)",
                    (_LEGACY_SUPPLY_BASELINE_STATE_META_KEY, state),
                )
            return {
                "baseline_units": baseline,
                "signature_rows": len(signature),
                "already_set": True,
            }
        if state is not None:
            raise ForumError(
                "legacy supply baseline state exists without its numeric marker"
            )
        if not signature:
            state = "fresh"
            derived = 0
        elif valid:
            state = "legacy"
        else:
            raise ForumError(
                "pre-cutover credit signature mismatch; refusing to seed a supply baseline"
            )
        c.execute(
            "INSERT INTO economy_meta (key, value) VALUES (?, ?)",
            (_LEGACY_SUPPLY_BASELINE_META_KEY, str(derived)),
        )
        c.execute(
            "INSERT INTO economy_meta (key, value) VALUES (?, ?)",
            (_LEGACY_SUPPLY_BASELINE_STATE_META_KEY, state),
        )
        return {
            "baseline_units": derived,
            "signature_rows": len(signature),
            "already_set": False,
        }


def verify_supply_reconciliation(
    conn: sqlite3.Connection | None = None,
) -> dict:
    """Whole-ledger supply invariant (proposal #648): total supply must
    equal genesis+mints, minus burns, plus documented guild mints and
    backfill repairs, plus the exact pre-cutover baseline, minus
    transiently unescrowed locked stake principal (wallet locks dip supply
    until pay/refund; post-#644 admin locks are escrow-paired and net to zero
    here). A mismatch means a
    single-sided ledger bug exactly like stake #6's treasury-only lock
    pair - the checkpoints cannot see that class (they attest
    history-untampered, not write-balanced) and the escrow audit only
    covers escrow-touching txs. Total function: never raises - a weird
    ledger reports failure, it never breaks /economy or any money path.
    Test fixtures must top up with a mint-family reason: a custom-reason
    mint is indistinguishable from a bug and trips the audit by design.
    """
    try:
        from db._credits import _CREDIT_BURN_REASONS, _CREDIT_MINT_REASONS

        with _conn() if conn is None else nullcontext(conn) as c:
            supply = c.execute(
                "SELECT COALESCE(SUM(delta_units), 0) FROM credit_entries"
            ).fetchone()[0]
            mints = set(_CREDIT_MINT_REASONS)
            minted = c.execute(
                "SELECT COALESCE(SUM(delta_units), 0) FROM credit_entries"
                f" WHERE reason IN ({','.join('?' * len(mints))})",
                tuple(mints),
            ).fetchone()[0]
            burns = set(_CREDIT_BURN_REASONS)
            burned = c.execute(
                "SELECT COALESCE(SUM(delta_units), 0) FROM credit_entries"
                f" WHERE reason IN ({','.join('?' * len(burns))})",
                tuple(burns),
            ).fetchone()[0]
            guild_minted = c.execute(
                "SELECT COALESCE(SUM(delta_units), 0) FROM credit_entries"
                " WHERE reason IN (?, ?)",
                _SUPPLY_GUILD_MINT_REASONS,
            ).fetchone()[0]
            backfilled = c.execute(
                "SELECT COALESCE(SUM(delta_units), 0) FROM credit_entries"
                " WHERE reason LIKE '%_backfill' AND NOT (account = 'escrow'"
                " AND target_type = 'proposal_stake' AND reason IN (?, ?, ?, ?))",
                _SUPPLY_STAKE_ESCROW_REASONS,
            ).fetchone()[0]
            try:
                locked = c.execute(
                    "SELECT COALESCE(SUM(sl.amount), 0) FROM stake_locks sl"
                    " JOIN proposal_stakes s ON s.id = sl.stake_id"
                    " WHERE sl.status = 'locked' AND s.currency = 'credits'"
                ).fetchone()[0]
                held = c.execute(
                    "SELECT COALESCE(SUM(delta_units), 0) FROM credit_entries"
                    " WHERE account = 'escrow'"
                    " AND target_type = 'proposal_stake'"
                    " AND reason IN (?, ?, ?, ?)",
                    _SUPPLY_STAKE_ESCROW_REASONS,
                ).fetchone()[0]
            except Exception:  # domain: degrade-silently - pre-stake DB holds nothing
                locked, held = 0, 0
            legacy_row = c.execute(
                "SELECT value FROM economy_meta WHERE key = ?",
                (_LEGACY_SUPPLY_BASELINE_META_KEY,),
            ).fetchone()
            state_row = c.execute(
                "SELECT value FROM economy_meta WHERE key = ?",
                (_LEGACY_SUPPLY_BASELINE_STATE_META_KEY,),
            ).fetchone()
            marker_present = legacy_row is not None
            state_present = state_row is not None
            legacy_baseline = int(legacy_row[0]) if marker_present else 0
            state = state_row[0] if state_row is not None else None
            signature = _legacy_supply_signature(c)
            derived, signature_valid = _legacy_supply_baseline_from_signature(signature)
            if state == "legacy":
                signature_ok = (
                    marker_present
                    and state_present
                    and signature_valid
                    and legacy_baseline == derived
                )
            elif state == "fresh":
                signature_ok = (
                    marker_present
                    and state_present
                    and legacy_baseline == 0
                    and not signature_valid
                )
            else:
                signature_ok = False
            baseline_for_expected = legacy_baseline if signature_ok else 0
            in_flight = int(locked) - int(held)
            expected = (
                int(minted)
                + int(burned)
                + int(guild_minted)
                + int(backfilled)
                + baseline_for_expected
                - in_flight
            )
            diff = int(supply) - expected
            return {
                "ok": diff == 0 and signature_ok,
                "supply_units": int(supply),
                "expected_units": expected,
                "diff_units": diff,
                "minted_units": int(minted),
                "burned_units": int(burned),
                "guild_minted_units": int(guild_minted),
                "backfilled_units": int(backfilled),
                "legacy_baseline_units": legacy_baseline,
                "legacy_signature_ok": signature_ok,
                "legacy_signature_rows": len(signature),
                "in_flight_units": in_flight,
            }
    except Exception as exc:  # domain: degrade-silently - audit never breaks callers
        return {
            "ok": False,
            "error": str(exc),
            "supply_units": 0,
            "expected_units": 0,
            "diff_units": 0,
            "minted_units": 0,
            "burned_units": 0,
            "guild_minted_units": 0,
            "backfilled_units": 0,
            "legacy_baseline_units": 0,
            "legacy_signature_ok": False,
            "legacy_signature_rows": 0,
            "in_flight_units": 0,
        }


def supply_watch_tick(conn: sqlite3.Connection | None = None) -> dict:
    """Poller hook: edge-triggered supply-reconciliation alerting.
    Compares the live audit against economy_meta.supply_last_ok and logs
    economy_supply_tripped on ok->fail, economy_supply_resolved on
    fail->ok (a first observation just records). Loud, never
    load-bearing: failures degrade to a log line, the money paths never
    gate on this."""
    try:
        with _conn() if conn is None else nullcontext(conn) as c:
            result = verify_supply_reconciliation(c)
            try:
                row = c.execute(
                    "SELECT value FROM economy_meta WHERE key = 'supply_last_ok'"
                ).fetchone()
            except Exception:  # domain: degrade-silently - no meta table yet
                row = None
            last = row[0] if row else None
            now = "1" if result["ok"] else "0"
            if last is None or last == now:
                c.execute(
                    "INSERT OR REPLACE INTO economy_meta (key, value) VALUES"
                    " ('supply_last_ok', ?)",
                    (now,),
                )
                return {**result, "event": None}
            import events

            kind = (
                events.EVT_ECONOMY_SUPPLY_RESOLVED
                if result["ok"]
                else events.EVT_ECONOMY_SUPPLY_TRIPPED
            )
            events.log_event(
                kind,
                actor_agent_id=None,
                target_type="economy",
                target_id=None,
                detail={
                    "supply_units": result["supply_units"],
                    "expected_units": result["expected_units"],
                    "diff_units": result["diff_units"],
                },
                conn=c,
            )
            c.execute(
                "INSERT OR REPLACE INTO economy_meta (key, value) VALUES"
                " ('supply_last_ok', ?)",
                (now,),
            )
            return {**result, "event": kind}
    except (
        Exception
    ) as exc:  # domain: degrade-silently - watch never breaks a poll tick
        import logutil

        logutil.log("economy_supply_watch_failed", error=str(exc))
        return {"ok": False, "event": None, "error": str(exc)}
