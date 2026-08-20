_NETWORK_TIMEOUT_SECONDS = 10

async def _status_reads(force: bool = False) -> tuple[dict, dict, dict, list | None]:
    """The status page's shared reads: (by_name, latency, repo, prs). Both the
    full page and the soft-refresh banner/pulse fragments run the same reads
    through the same builders, so the page and its live pieces can't drift.
    The shared reads are the expensive part (db reads plus git and GitHub
    calls), and the two fragments poll them every REFRESH_SECONDS, so within
    config.STATUS_CACHE_SECONDS a fragment reuses the previous read instead
    of re-running it; the full page passes force=True - a manual visit is one
    request, not a poll loop, and always reflects the moment."""
    global _STATUS_CACHE
    ts, cached = _STATUS_CACHE
    if not force and cached is not None and time.monotonic() - ts < config.STATUS_CACHE_SECONDS:
        return cached
    # Kick off the two network-touching / git reads first so the db reads
    # below overlap them. Both are time-bounded so a slow GitHub can't block
    # the entire page; on timeout we serve the last-known cache or defaults.
    repo_task = asyncio.create_task(asyncio.to_thread(_git_sync_status))

    # Import here to avoid circular at module level: viewer_status imports
    # from viewer, and viewer imports from viewer_status.
    from viewer._helpers import _open_prs as _viewer_open_prs
    prs_task = asyncio.create_task(_viewer_open_prs())

    reads = await asyncio.gather(
        _timed("integrity_ok", db.integrity_ok),
        _timed("counts", aggregates.counts),
        _timed("list_agents", aggregates.list_agents),
        _timed("list_reports", reports.list_reports),
        _timed("list_proposals", db.list_proposals),
        _timed("list_recent_activity", lambda: aggregates.list_recent_activity(50)),
        _timed("storage_stats", db.storage_stats),
        _timed("schema_version", db.schema_version),
    )
    latency = {label: ms for label, _, ms, _ in reads}
    by_name = {label: value for label, value, _, _ in reads}
    # Collect network results with a timeout. On timeout, fall back to the
    # previous cache or safe defaults so the page always loads.
    _cached_repo = cached[2] if cached else None
    _cached_prs = cached[3] if cached else None
    try:
        repo = await asyncio.wait_for(repo_task, timeout=_NETWORK_TIMEOUT_SECONDS)
    except (asyncio.TimeoutError, Exception):
        repo = _cached_repo or {"error": "timeout", "stale": True}
    try:
        prs = await asyncio.wait_for(prs_task, timeout=_NETWORK_TIMEOUT_SECONDS)
    except (asyncio.TimeoutError, Exception):
        prs = _cached_prs
    result = (by_name, latency, repo, prs)
    _STATUS_CACHE = (time.monotonic(), result)
    return result
