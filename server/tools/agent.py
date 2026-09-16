"""server.tools.agent — per-agent delta reads (proposal #508)."""

from db import my_deltas
from db import reset_delta_cursor


def my_deltas(token: str, cursor: int | None = None, cap: int = 500) -> dict:
    """Per-agent deltas: events since last visit (delivered-only high-water
    mark, actionable == check_in parity, empty fast-path, catch-up by
    event-id). See db.my_deltas for the full contract."""
    return db.my_deltas(token, cursor, cap)


def reset_delta_cursor(token: str) -> dict:
    """Reset the caller's delivered-only high-water mark to 0, so the
    next my_deltas() call re-delivers from the beginning."""
    return db.reset_delta_cursor(token)
