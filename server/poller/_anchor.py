"""server/poller/_anchor.py — hourly bench-anchor auto-bless tick."""

import asyncio

import db
import logutil


async def _bench_anchor_poller() -> None:
    """Re-confirm the benchmark anchor on a quiet pool: the hourly tick
    evaluates db.bench_anchor_tick (bootstrap on first runs, reconfirm
    once the anchor outlives BENCH_ANCHOR_MAX_AGE_DAYS with small drift)
    and logs each blessing. Drifted anchors are never auto-chased - they
    surface via the aging reader for manual review. All blocking calls run
    in a worker thread so the MCP loop never stalls; any error is logged
    and retried next hour."""
    while True:
        try:
            decision = await asyncio.to_thread(db.bench_anchor_tick)
            if not decision.startswith("skip:"):
                logutil.log("bench_anchor_cron", decision=decision)
        except Exception as exc:
            logutil.log(
                "bench_anchor_cron", error=str(exc)
            )  # domain: degrade-silently - tick must never stall the loop
        await asyncio.sleep(3600)
