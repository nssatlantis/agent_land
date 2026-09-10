"""server/poller/_anchor.py — hourly bench-anchor auto-bless tick."""

import asyncio

import logutil


def _audit_skip(reason: str, buyer_id: int | None, run_event_id: int | None) -> None:
    """Ledger-audit a due-path non-bless outcome (hold, infra, busy pool)
    so a silent loop is distinguishable from a quiet pool. Fresh-anchor
    hours log nothing to the ledger — silence is correct when nothing is
    due; the server log still records the evaluation."""
    import events

    events.log_event(
        events.EVT_BENCH_HEARTBEAT_SKIPPED,
        actor_agent_id=None,
        actor_name="system",
        detail={
            "reason": reason,
            "buyer_id": buyer_id,
            "run_event_id": run_event_id,
        },
    )


def _settle_dispatch(result: dict, buyer_id: int | None) -> dict:
    """Settle a dispatched heartbeat run. Blessed passes through; held +
    buyer refunds the price (one attempt per purchase); returned-infra +
    buyer restores the bank (nothing was judged, so the attempt never
    really happened); a failed refund also restores the bank so the buyer
    keeps a retry instead of losing both. Every non-bless lands a
    skip-audit row. Takes fabricated-or-live result dicts, so tests pin
    the whole matrix directly with no harness and no mocks."""
    import db as _db

    if result["outcome"] == "blessed":
        return {
            "outcome": "blessed",
            "decision": result["decision"],
            "run_event_id": result["run_event_id"],
            "buyer_id": buyer_id,
        }
    if result["outcome"] == "held" and buyer_id is not None:
        try:
            refund = _db.refund_blessed_bench(buyer_id)
        except Exception:  # domain: never-lose-data - bank restored below,
            # the hold audited below, retry next cycle; nothing blessed.
            with _db._conn(immediate=True) as conn:
                _db._store.restore_blessed_bench(conn, buyer_id)
            decision = f"{result['decision']} (store refund failed; bank restored)"
        else:
            decision = (
                f"{result['decision']} (store buy auto-refunded {refund['price']})"
            )
    elif result["outcome"] == "infra" and buyer_id is not None:
        with _db._conn(immediate=True) as conn:
            _db._store.restore_blessed_bench(conn, buyer_id)
        decision = f"{result['decision']}; buyer bank restored"
    else:
        decision = str(result["decision"])
    _audit_skip(decision, buyer_id, result["run_event_id"])
    return {
        "outcome": result["outcome"],
        "decision": decision,
        "run_event_id": result["run_event_id"],
        "buyer_id": buyer_id,
    }


def _heartbeat_tick() -> dict:
    """One hourly evaluation: due? → buyer? → take → dispatch → bless →
    settle. A waiting buyer spends one banked run (taken up front, at most
    one per tick); otherwise the heartbeat dispatches its own run. Settle:
    held + buyer ⇒ refund the price (one attempt per purchase, the numbers
    stay readable); infra + buyer ⇒ restore the banked run (the attempt
    never really happened, no credit movement). Fresh-anchor hours return
    a quiet skip with no ledger row. Runs in a worker thread."""
    import db
    import server.ci_runner as ci_runner

    due, why = db.bench_heartbeat_due()
    if not due:
        return {
            "outcome": "skipped",
            "decision": f"skip: {why}",
            "run_event_id": None,
            "buyer_id": None,
        }
    with db._conn(immediate=True) as conn:
        buyer_id = db._store._find_blessed_bench_buyer(conn)
        if buyer_id is not None:
            db._store._take_blessed_bench(conn, buyer_id)
    reason = "store" if buyer_id is not None else "heartbeat"
    try:
        result = ci_runner.run_heartbeat_bench(buyer_id=buyer_id, reason=reason)
    except Exception as exc:  # domain: never-lose-data - buyer bank restored
        # below, the hold is audit-rowed, retry next hour; nothing blessed.
        if buyer_id is not None:
            with db._conn(immediate=True) as conn:
                db._store.restore_blessed_bench(conn, buyer_id)
        decision = f"infra: heartbeat dispatch failed ({exc}); buyer bank restored"
        _audit_skip(decision, buyer_id, None)
        return {
            "outcome": "infra",
            "decision": decision,
            "run_event_id": None,
            "buyer_id": buyer_id,
        }
    return _settle_dispatch(result, buyer_id)


async def _bench_anchor_poller() -> None:
    """Keep the benchmark anchor fresh from execution: the hourly tick runs
    _heartbeat_tick in a worker thread (due? → buyer? → take → dispatch →
    bless → settle) and logs the outcome. Drifted anchors are never
    auto-chased — a hold surfaces via the aging reader, a store buy
    auto-refunds, and every due-path non-bless lands a skipped audit row.
    Any error is logged and retried next hour."""
    while True:
        try:
            outcome = await asyncio.to_thread(_heartbeat_tick)
            logutil.log(
                "bench_anchor_cron",
                outcome=outcome["outcome"],
                decision=outcome["decision"],
            )
        except Exception as exc:
            logutil.log(
                "bench_anchor_cron", error=str(exc)
            )  # domain: degrade-silently - tick must never stall the loop
        await asyncio.sleep(3600)
