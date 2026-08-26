"""github - read/write access to the society's own source repository.

Plain functions over one pooled httpx.AsyncClient to the GitHub REST API.
No MCP types, no HTTP server code - server.py wraps these as tools. Mirror of
db's role: protocol-agnostic, so a CLI or cron could reuse it too.

Two hard rules live here, server-side, so every caller goes through them:
  1. Nothing ever writes to the base branch directly. Every change goes
     through a feature branch plus a pull request.
  2. Every commit and PR carries a "Citizen: <name> (agent_id=N)" trailer
     identifying who made the change (see AGENTS.md).

Requires a GITHUB_TOKEN. Use a fine-grained PAT scoped to just this repo
(Contents read/write + Pull requests read/write + Metadata read) - see
README.md and .env.example.

This is a facade package (the db/ pattern): every name lives in exactly one
submodule - _core (transport/caches/errors), _reads (listings/composites/
stamps), _checks (CI tiered chain), _writes (mutations/edit engine),
_gitops (local-git conflict/rebase flows) - and is re-exported here so
``import github`` keeps working for every caller.

Monkeypatching rule: rebindable seams must be patched ON THEIR OWNING
SUBMODULE (e.g. ``github._core._request``, ``github._gitops._git``). The
names listed in _DYNAMIC are delegated live via module ``__getattr__``, so
READS of e.g. ``github._open_prs_cache`` always see the owning submodule's
current binding; attribute WRITES on the package do not forward, by design.
"""

from __future__ import annotations

import asyncio
from typing import Any

import config

from . import _core
from . import _reads
from . import _checks
from . import _writes as _writes
from . import _gitops as _gitops

# ── core infrastructure ─────────────────────────────────────────────────
from ._core import (  # noqa: F401
    GITHUB_REPO,
    GITHUB_BASE_BRANCH,
    RepoError,
    _TTLCache,
    _CACHE_FAILURES,
    _headers,
    _bg_loop,
    _build_client,
    _get_client,
    _sync,
    _on_bg,
    _shutdown_client,
    _arequest,
    _arequest_text,
    clear_cache,
    _validate_path,
    _pr_cache,
    _tree_cache,
    _open_prs_cache,
)

# ── reads: listings, composites, stamps ─────────────────────────────────
from ._reads import (  # noqa: F401
    repo_spec,
    base_branch,
    list_tree,
    alist_tree,
    read_file,
    aread_file,
    _slice_line_range,
    _MAX_READ_FILE_LINES,
    _CITIZEN_RE,
    _PROPOSAL_RE,
    _TRAILING_CITIZEN_RE,
    _TRAILING_PROPOSAL_RE,
    strip_trailing_citizen,
    strip_trailing_proposal,
    _MD_ESCAPES,
    _escape_md,
    pr_proposal_header,
    _PROPOSAL_HEADER_RE,
    strip_proposal_header,
    open_prs,
    list_prs,
    alist_prs,
    recently_closed_prs,
    arecently_closed_prs,
    _pr_outcome,
    get_pr,
    pr_diff,
    pr_files,
    pr_comments,
    pr_commits,
    _paginated_get,
    _apaginated_get,
    _PR_PAGE_SIZE,
)

# ── checks: CI tiered chain ─────────────────────────────────────────────
from ._checks import (  # noqa: F401
    _MAX_CHECK_RUNS,
    _MAX_FAILURE_LINES,
    _MAX_LOG_TAIL_BYTES,
    _FAILURE_MARKERS,
    _extract_failure_lines,
    _ci_state,
    _dedup_failures,
    _checks_from_check_runs,
    _checks_from_actions,
    _thin_annotation,
    _afetch_annotations,
    _afrom_check_runs,
    _afetch_jobs,
    _afetch_job_log,
    _afrom_actions,
    _achecks_impl,
    pr_checks,
    wait_for_ci,
    _REBASE_CI_TIMEOUT,
    _REBASE_CI_POLL_INTERVAL,
)

# ── writes: proposals, updates, lifecycle, edit engine ──────────────────
from ._writes import (  # noqa: F401
    propose_change,
    update_pr,
    close_pr,
    merge_pr,
    comment_on_pr,
    decline_pr,
    set_pr_labels,
    list_pr_labels,
    add_pr_label,
    remove_pr_label,
    update_pr_title,
    pr_has_label,
    _content_manifest,
    _patch_log,
    _validate_edits,
    _decode_content_text,
    _apply_edits,
    _resolve_edits,
    _branch_name,
)

# ── gitops: workspace pool, conflicts, rebases ──────────────────────────
from ._gitops import (  # noqa: F401
    _CONTEXT_LINES,
    _parse_conflict_markers,
    _ws_mode_persistent,
    _rm_readonly,
    _ws_fresh_clone,
    _ws_normalize,
    _GIT_IDENTITY_NAME,
    _GIT_IDENTITY_EMAIL,
    _clone_repo,
    _abort_merge,
    _push_ref,
    _detect_conflict_files,
    _has_conflict_markers,
    detect_merge_conflicts,
    apply_merge_resolutions,
    rebase_pr_onto_main,
)

# Rebindable seams: NOT statically imported - resolved live against the
# owning submodule so readers always see current bindings (and so the
# canonical patch targets stay unambiguous). See the monkeypatching rule
# in the module docstring.
_DYNAMIC = {
    # transport / token / cache invalidation -> _core
    "_client": "_core",
    "_loop": "_core",
    "GITHUB_TOKEN": "_core",
    "_request": "_core",
    "_request_text": "_core",
    "_ensure_token": "_core",
    "_invalidate_pr": "_core",
    # CI chain -> _checks
    "_checks_for_head": "_checks",
    "_supplement_check_run_failures": "_checks",
    # reads -> _reads
    "_PR_PAGE_CAP": "_reads",
    "_parse_citizen": "_reads",
    "_parse_proposal": "_reads",
    # edit engine cap -> _writes
    "_MAX_EDITS_PER_FILE": "_writes",
    # local git family -> _gitops
    "_repo_url": "_gitops",
    "_seed_identity": "_gitops",
    "_git": "_gitops",
    "_clone_repo": "_gitops",
    "_cleanup": "_gitops",
    "_safe_path": "_gitops",
    "_workspace": "_gitops",
    "_push_auth": "_gitops",
    "_ws_root": "_gitops",
    "_ws_git_scrub": "_gitops",
    "_ws_ensure_pool": "_gitops",
    "_ws_slots": "_gitops",
    "_workspace_queue": "_gitops",
    "_parse_decline_reason": "_reads",
}


def __getattr__(name: str) -> Any:
    """PEP 562 live delegation for the rebindable seams in _DYNAMIC."""
    owner = _DYNAMIC.get(name)
    if owner is None:
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}"
        )
    return getattr(globals()[owner], name)


# ------------------------------------------------- async surface (twins) --

def _atwin(sync_fn):
    """Give a composite flow an async face: run the whole sync function on
    the background executor so the caller's event loop never blocks, and
    the anyio worker pool stays free for other tools. Used for flows
    dominated by local git subprocess work (propose / update / close /
    conflicts) where a thread is the right tool; network-pure hot paths
    carry true native twins instead (alist_tree / aread_file / alist_prs /
    arecently_closed_prs).

    Late-binding by design: the twin resolves the function by name on each
    call - in THIS namespace, the package root, which is exactly what a
    caller rebinding ``github.<name>`` mutates - so monkeypatching the sync
    original (as the test suite does) applies to the twin too."""
    name = sync_fn.__name__

    async def twin(*args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(globals()[name], *args, **kwargs)

    twin.__name__ = f"a{name}"
    twin.__qualname__ = twin.__name__
    twin.__doc__ = sync_fn.__doc__
    return twin


# --- native composite twins: concurrent fan-out reads -----------------------
# Unlike the _atwin composites below, these run their whole read composite
# INSIDE the background loop (public wrapper = one _on_bg hop), so their
# internal requests overlap via asyncio.gather instead of chaining. The
# checks chain stays sync and rides an executor thread inside the gather:
# its tiered fallback semantics are battle-tested, and to_thread scheduled
# from the background loop cannot deadlock (worker thread != loop thread).


def _pr_comment_entries(issue: list, review: list) -> list[dict]:
    comments: list[dict] = []
    for kind, batch in (("issue", issue), ("review", review)):
        for c in batch:
            entry = {
                "id": c["id"],
                "kind": kind,
                "author": (c.get("user") or {}).get("login"),
                "body": c.get("body") or "",
                "created_at": c["created_at"],
            }
            if c.get("path") is not None:
                entry["path"] = c["path"]
            if c.get("line") is not None:
                entry["line"] = c["line"]
            comments.append(entry)
    comments.sort(key=lambda c: c["created_at"], reverse=True)
    return comments


async def _apr_comments_impl(number: int) -> list[dict]:
    # Both sources paginated (per_page=100), still fetched in parallel.
    issue, review = await asyncio.gather(
        _reads._apaginated_get(f"issues/{number}/comments"),
        _reads._apaginated_get(f"pulls/{number}/comments"),
    )
    return _pr_comment_entries(issue, review)


async def _apr_files_impl(number: int) -> list[dict]:
    # Transformed exactly like sync pr_files: the ("pr_files", n) cache key
    # is shared between the sync and native paths, so both must write the
    # same shape - otherwise whichever path warms the cache silently
    # redefines the other's contract for one TTL window. Paginated exactly
    # like sync too (per_page=100).
    raw = await _reads._apaginated_get(f"pulls/{number}/files")
    return [
        {
            "filename": f["filename"],
            "status": f.get("status"),
            "additions": f.get("additions", 0),
            "deletions": f.get("deletions", 0),
        }
        for f in raw
    ]


async def _apr_commits_impl(number: int) -> tuple[dict, list[dict]]:
    """Returns (pr, commits): the PR payload rides along because the sync
    shape needs head/base refs, and fetching it costs nothing extra when
    gathered with the first commits page."""
    pr, first = await asyncio.gather(
        _core._arequest("GET", f"pulls/{number}"),
        _core._arequest("GET", f"pulls/{number}/commits?per_page=100&page=1"),
    )
    commits: list[dict] = list(first)
    last_batch = first
    page = 2
    while len(last_batch) == 100:
        last_batch = await _core._arequest(
            "GET", f"pulls/{number}/commits?per_page=100&page={page}"
        )
        commits.extend(last_batch)
        page += 1
    return pr, commits


async def _apr_diff_impl(number: int) -> tuple[dict, list[dict]]:
    """PR fetch overlaps the first files page; later pages stay sequential
    (each needs the previous page's fullness to know whether to continue)."""
    pr, first = await asyncio.gather(
        _core._arequest("GET", f"pulls/{number}"),
        _core._arequest("GET", f"pulls/{number}/files?per_page=100&page=1"),
    )
    files: list[dict] = list(first)
    last_batch = first
    page = 2
    while len(last_batch) == 100:
        last_batch = await _core._arequest(
            "GET", f"pulls/{number}/files?per_page=100&page={page}"
        )
        files.extend(last_batch)
        page += 1
    return pr, files


async def _aget_pr_impl(number: int, *, _pr: dict | None = None) -> dict:
    """Runs entirely on the background loop. Wave 1: the PR fetch. Wave 2:
    checks (sync tiered chain on an executor thread), comments (two gathered
    sources) and files - all overlapped. Sub-caches for comments/files are
    warmed exactly like the sync path did as a side effect."""
    pr = _pr or await _core._arequest("GET", f"pulls/{number}")
    head_sha = pr["head"]["sha"]
    # Late-bound like _atwin: resolving through the owning submodule's
    # attribute means a stub installed on github._checks propagates here.
    checks_fn = _checks._checks_for_head
    checks_t = asyncio.create_task(asyncio.to_thread(checks_fn, head_sha))
    comments_t = asyncio.create_task(_apr_comments_impl(number))
    files_t = asyncio.create_task(_apr_files_impl(number))
    checks, comments, files = await asyncio.gather(checks_t, comments_t, files_t)
    _core._pr_cache.set(("pr_comments", number), comments)
    _core._pr_cache.set(("pr_files", number), files)
    return {
        "number": pr["number"],
        "title": pr["title"],
        "body": pr.get("body") or "",
        "head": pr["head"]["ref"],
        "base": pr["base"]["ref"],
        "author": (pr.get("user") or {}).get("login"),
        "state": pr.get("state"),
        "outcome": _reads._pr_outcome(pr),
        "mergeable": pr.get("mergeable"),
        "mergeable_state": pr.get("mergeable_state"),
        "commits": pr.get("commits"),
        "created_at": pr["created_at"],
        "html_url": pr["html_url"],
        "checks": checks,
        "comments": comments,
        "files": files,
    }


async def aget_pr(number: int, *, _pr: dict | None = None) -> dict:
    """Native-await twin of get_pr - same contract, same cache key, but the
    checks/comments/files reads overlap instead of chaining."""
    cache_key = ("get_pr", number)
    cached = _core._pr_cache.get(cache_key, config.PR_CACHE_SECONDS)
    if cached is not None:
        return cached
    result = await _core._on_bg(_aget_pr_impl(number, _pr=_pr))
    _core._pr_cache.set(cache_key, result)
    return result


async def apr_comments(number: int) -> list[dict]:
    """Native-await twin of pr_comments - both comment sources gathered."""
    cache_key = ("pr_comments", number)
    cached = _core._pr_cache.get(cache_key, config.PR_CACHE_SECONDS)
    if cached is not None:
        return cached
    result = await _core._on_bg(_apr_comments_impl(number))
    _core._pr_cache.set(cache_key, result)
    return result


async def apr_files(number: int) -> list[dict]:
    """Native-await twin of pr_files."""
    cache_key = ("pr_files", number)
    cached = _core._pr_cache.get(cache_key, config.PR_CACHE_SECONDS)
    if cached is not None:
        return cached
    result = await _core._on_bg(_apr_files_impl(number))
    _core._pr_cache.set(cache_key, result)
    return result


async def apr_commits(number: int) -> dict:
    """Native-await twin of pr_commits - PR payload and first commits page
    gathered, later pages sequential."""
    cache_key = ("pr_commits", number)
    cached = _core._pr_cache.get(cache_key, config.PR_CACHE_SECONDS)
    if cached is not None:
        return cached
    pr, commits = await _core._on_bg(_apr_commits_impl(number))
    result = {
        "number": number,
        "head": pr["head"]["ref"],
        "base": pr["base"]["ref"],
        "commits": [
            {
                "sha": c["sha"],
                "message": (c.get("commit") or {}).get("message") or "",
                "author_name": ((c.get("commit") or {}).get("author") or {}).get("name"),
                "author_date": ((c.get("commit") or {}).get("author") or {}).get("date"),
            }
            for c in commits
        ],
    }
    _core._pr_cache.set(cache_key, result)
    return result


async def apr_diff(number: int) -> dict:
    """Native-await twin of pr_diff - PR payload and first files page
    gathered, later pages sequential."""
    cache_key = ("pr_diff", number)
    cached = _core._pr_cache.get(cache_key, config.PR_CACHE_SECONDS)
    if cached is not None:
        return cached
    pr, files = await _core._on_bg(_apr_diff_impl(number))
    result = {
        "number": pr["number"],
        "title": pr["title"],
        "head": pr["head"]["ref"],
        "base": pr["base"]["ref"],
        "html_url": pr["html_url"],
        "files": [
            {
                "path": f["filename"],
                "status": f.get("status"),
                "additions": f.get("additions", 0),
                "deletions": f.get("deletions", 0),
                "changes": f.get("changes", 0),
                "patch": f.get("patch"),
            }
            for f in files
        ],
    }
    _core._pr_cache.set(cache_key, result)
    return result


async def apr_checks(number: int, *, _pr: dict | None = None,
                     _head_sha: str | None = None) -> dict:
    """Native-await twin of pr_checks - the tiered chain (check-runs ->
    Actions logs -> combined status) is preserved, and within a tier every
    per-run annotation / jobs-list / log download fans out concurrently.
    Log downloads are the expensive tail: each can be tens of KB behind a
    redirect. Shares the ("pr_checks", number) cache key with the sync
    face; ``_pr`` / ``_head_sha`` mirror pr_checks' private shortcuts."""
    cache_key = ("pr_checks", number)
    cached = _core._pr_cache.get(cache_key, config.PR_CACHE_SECONDS)
    if cached is not None:
        return cached
    result = await _core._on_bg(
        _checks._achecks_impl(number, _pr=_pr, _head_sha=_head_sha)
    )
    _core._pr_cache.set(cache_key, result)
    return result


apropose_change = _atwin(propose_change)
aupdate_pr = _atwin(update_pr)
aupdate_pr_title = _atwin(update_pr_title)
aclose_pr = _atwin(close_pr)
aset_pr_labels = _atwin(set_pr_labels)
acomment_on_pr = _atwin(comment_on_pr)
adetect_merge_conflicts = _atwin(detect_merge_conflicts)
aapply_merge_resolutions = _atwin(apply_merge_resolutions)
