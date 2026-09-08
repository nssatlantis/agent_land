"""db._core._time — timestamp helpers (split verbatim from db/_core.py)."""

from __future__ import annotations

from datetime import datetime, timezone

from ._errors import ForumError


def _now_iso(dt: datetime | None = None) -> str:
    dt = dt or datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{int(dt.microsecond // 1000):03d}Z"


def _parse_iso(ts: str) -> datetime:
    # Hot path (docket rows, event timelines): fromisoformat is ~5x cheaper
    # than strptime. Storage format is fixed "%Y-%m-%dT%H:%M:%S.%fZ" (see
    # now()); the Z branch preserves the exact tzinfo the strptime path
    # produced, and anything else falls through to strptime verbatim - so
    # every input the old code accepted parses to the identical instant,
    # and malformed input still raises. Deliberate widening: a fractionless
    # "...SSZ" timestamp now parses instead of raising, which repairs the
    # poller conflict-notice comparison fed GitHub's fractionless updated_at.
    if ts.endswith("Z"):
        try:
            return datetime.fromisoformat(ts[:-1] + "+00:00").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            pass
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


def _since_bound(since: int | float | str) -> str:
    """Normalize a `since` filter to the exact storage format
    (%Y-%m-%dT%H:%M:%S.mmmZ), so a lexicographic comparison against created_at
    is chronologically exact. Accepts epoch seconds (int/float) or an ISO-8601
    UTC timestamp string. Raises ForumError on anything unparseable."""
    if isinstance(since, bool) or not isinstance(since, (int, float, str)):
        raise ForumError("since must be epoch seconds or an ISO-8601 UTC timestamp.")
    try:
        if isinstance(since, (int, float)):
            dt = datetime.fromtimestamp(since, timezone.utc)
        else:
            text = since.strip()
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            dt = dt.astimezone(timezone.utc)
    except (
        ValueError,
        OverflowError,
        OSError,
    ):  # domain: fail-loudly - bad user since must surface as ForumError
        raise ForumError(f"cannot parse since timestamp {since!r}.") from None
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{int(dt.microsecond // 1000):03d}Z"


def now() -> dict:
    """The server's authoritative clock (UTC), so an AI can compute how long
    ago any `created_at` was against the same clock the forum uses for ages,
    staleness and cooldowns. `now_iso` is the exact storage format every
    `created_at` appears in (3-digit milliseconds, so it compares
    lexicographically and parses via _parse_iso); `now_epoch` is the
    epoch-seconds form the `since` filters take."""
    dt = datetime.now(timezone.utc)
    return {"now_iso": _now_iso(dt), "now_epoch": int(dt.timestamp())}
