"""github._checks - CI detail for pull requests.

The tiered read chain behind repo_pr_checks and get_pr's ``checks`` field:
(1) check runs with failure annotations, (2) GitHub Actions workflow runs
with error lines pulled from capped log tails, (3) the combined commit
status. Each tier's API failure falls into the next; only a total outage
yields None. The sync chain and the native-await twin share mapping shapes
and cache keys; within a tier the async path fans annotation/job/log fetches
out concurrently.
"""

from __future__ import annotations

import asyncio
import re
import time

import config

from . import _core
from ._core import GITHUB_REPO, RepoError

# Caps on CI-detail reads (pr_checks). Read caps are client-ergonomics bounds
# - module constants, deliberately not config.py tunables, so no drift-
# manifest churn for a bound the operator never turns.
_MAX_CHECK_RUNS = 50
_MAX_FAILURE_LINES = 30
_MAX_LOG_TAIL_BYTES = 65536

_FAILURE_MARKERS = (
    "error:",
    "error ",
    "failed",
    "traceback",
    "assertionerror",
    "mypy:",
    "ruff",
    "fatal",
    "exit code",
)


def _extract_failure_lines(log: str) -> list[str]:
    """Scan a CI log for the lines that carry failures (error markers, test
    failures, mypy/ruff output). Only the last _MAX_LOG_TAIL_BYTES are
    scanned - a log's interesting end is what matters - and each hit is
    trimmed, so the tool returns signal, not megabytes."""
    tail = (log or "")[-_MAX_LOG_TAIL_BYTES:]
    hits = []
    for line in tail.splitlines():
        low = line.lower()
        if any(marker in low for marker in _FAILURE_MARKERS):
            hits.append(line.strip()[:500])
    return hits


def _superseded_ids(runs: list[dict]) -> set:
    """Ids of runs that a newer run of the same check name supersedes.

    Both tier queries are head_sha-scoped, so one head's run list can hold
    several runs of a single check: GitHub cancels a superseded run
    automatically when a newer one starts for the same ref, and both entries
    describe the same head. Ranking by the numeric run id both tiers already
    expose makes the newest authoritative - which is also what GitHub's own
    UI renders. Bug #B123.

    Two deliberate refusals to collapse, both because collapsing too eagerly
    would trade a false red for a false GREEN, which is the worse direction
    for a gate:
      - an unnamed run is not verifiably the same logical check as another
        unnamed run, so each gets its own key and supersedes nothing;
      - a run with no numeric id cannot be ranked, so it is never reported
        superseded and the list degrades to the pre-#B123 behaviour instead
        of guessing.
    """
    best: dict[tuple, tuple[tuple[int, int], object]] = {}
    superseded: set = set()
    for i, r in enumerate(runs):
        rid = r.get("id")
        name = (r.get("name") or "").strip()
        key = ("named", name) if name else ("anon", i)
        rank = (rid if isinstance(rid, int) else -1, i)
        cur = best.get(key)
        if cur is None:
            best[key] = (rank, rid)
        elif rank > cur[0]:
            best[key] = (rank, rid)
            superseded.add(cur[1])
        else:
            superseded.add(rid)
    return {s for s in superseded if s is not None}


def _ci_state(mapped: list[dict], superseded: set) -> str:
    """One green/red/pending verdict across a run list: 'failure' when any
    run failed, 'pending' while any is unfinished, else 'success'.
    Includes 'error' (the combined commit status API's configuration-failure
    state) alongside the check-run / Actions failure vocabularies.

    `superseded` is the calling tier's _superseded_ids(runs) set, and it is
    computed from the RAW run list on purpose: that is the only place where a
    missing name is still visibly missing. _map_run substitutes "check" /
    "workflow" for an unnamed run, so a verdict that re-derived supersession
    from `mapped` would see two unrelated unnamed runs as one check name and
    let the newer hide the older's failure - a false green. Bug #B123.

    The verdict reads the newest run per check name, so a superseded
    duplicate that GitHub cancelled automatically cannot outvote the run
    that describes the tip. 'cancelled' remains a failure when it is the
    NEWEST run of its name - a cancelled tip genuinely has no green verdict.
    """
    current = [r for r in mapped if r.get("id") not in superseded]
    if any(
        r["conclusion"]
        in ("failure", "cancelled", "timed_out", "action_required", "error")
        for r in current
    ):
        return "failure"
    if any(r["conclusion"] is None or r["status"] != "completed" for r in current):
        return "pending"
    return "success"


def _dedup_failures(failures: list[dict]) -> list[dict]:
    """Deduplicate failure entries by normalized message (whitespace-collapsed,
    lowercased). Preserves insertion order - the first occurrence wins, so
    log lines (added first) beat annotations."""
    seen: set[str] = set()
    out: list[dict] = []
    for f in failures:
        key = " ".join((f.get("message") or "").split()).lower()
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        out.append(f)
    return out


def _map_run(r: dict, *, name_default: str, status_default: str) -> dict:
    """One run's docket entry - the name/status/conclusion/html_url shape
    shared by the check-runs and Actions tiers and both async twins. The
    tiers differ only in their defaults: check runs queue while Actions
    completes, and their unnamed-run labels differ."""
    return {
        "name": r.get("name") or name_default,
        "status": r.get("status") or status_default,
        "conclusion": r.get("conclusion"),
        "html_url": r.get("html_url"),
        # The run id is the tiebreak _superseded_ids ranks on, and it lets a
        # reader name the exact run a verdict came from without parsing
        # html_url. Additive: every pre-existing key is unchanged.
        "id": r.get("id"),
    }


def _checks_from_check_runs(runs: list[dict]) -> dict:
    """Map check runs (the richest tier) to per-check entries and pull the
    failure annotations - path, start line, message - capped, so a red PR
    carries its reason in the tool result."""
    mapped: list[dict] = []
    failures: list[dict] = []
    superseded = _superseded_ids(runs)
    for r in runs:
        name = r.get("name") or "check"
        mapped.append(_map_run(r, name_default="check", status_default="queued"))
        if r.get("id") in superseded:
            # A newer run of this check already answered for the head; this
            # copy's annotations describe a superseded attempt, not the tip.
            continue
        if r.get("conclusion") not in (
            "failure",
            "cancelled",
            "timed_out",
            "action_required",
        ):
            continue
        run_id = r.get("id")
        annotations: list[dict] = []
        if run_id is not None:
            try:
                annotations = (
                    _core._request(
                        "GET", f"check-runs/{run_id}/annotations?per_page=100"
                    )
                    or []
                )
            except RepoError:
                annotations = []
        for a in annotations[:_MAX_FAILURE_LINES]:
            failures.append(
                {
                    "name": name,
                    "path": " ".join((a.get("path") or "").split()).strip() or None,
                    "line": a.get("start_line"),
                    "message": " ".join((a.get("message") or "").split())[:2000],
                    "log_url": r.get("html_url"),
                }
            )
    return {
        "source": "check_runs",
        "state": _ci_state(mapped, superseded),
        "runs": mapped,
        "failures": failures,
    }


def _checks_from_actions(runs: list[dict]) -> dict:
    """Map GitHub Actions workflow runs; for each failed run, fetch the jobs
    and pull error lines from a capped log tail. Degrades per-failure: a job
    or log that cannot be read leaves the run link, never an error."""
    mapped: list[dict] = []
    failures: list[dict] = []
    superseded = _superseded_ids(runs)
    for r in runs:
        name = r.get("name") or "workflow"
        conclusion = r.get("conclusion")
        run_id = r.get("id")
        mapped.append(_map_run(r, name_default="workflow", status_default="completed"))
        if run_id in superseded:
            # A newer run of this workflow already answered for the head; do
            # not spend a jobs fetch or a log tail on a superseded attempt.
            continue
        if conclusion not in ("failure", "cancelled", "timed_out") or run_id is None:
            continue
        jobs: list[dict] = []
        try:
            jobs = (
                _core._request("GET", f"actions/runs/{run_id}/jobs?per_page=100") or {}
            ).get("jobs") or []
        except RepoError:
            jobs = []
        for job in jobs:
            if job.get("conclusion") not in ("failure", "cancelled", "timed_out"):
                continue
            job_name = job.get("name") or "job"
            job_id = job.get("id")
            lines: list[str] = []
            log_url = None
            if job_id is not None:
                try:
                    lines = _extract_failure_lines(
                        _core._request_text("GET", f"actions/jobs/{job_id}/logs") or ""
                    )
                    log_url = f"https://github.com/{GITHUB_REPO}/actions/runs/{run_id}/job/{job_id}"
                except RepoError:
                    lines = []
            for line in lines[:_MAX_FAILURE_LINES]:
                failures.append(
                    {
                        "name": f"{name} / {job_name}",
                        "message": line,
                        "log_url": log_url,
                    }
                )
    return {
        "source": "actions",
        "state": _ci_state(mapped, superseded),
        "runs": mapped,
        "failures": failures,
    }


_EXIT_CODE_RE = re.compile(r"(?:process completed with )?exit code \d+")


def _thin_annotation(f: dict) -> bool:
    """True when a failure entry carries nothing an agent can act on:
    no message at all, or GitHub's stock 'exit code N' /
    'Process completed with exit code N.' annotation (with or without a
    file path). Such entries are why the log-tail supplement exists."""
    msg = " ".join((f.get("message") or "").split()).strip().lower()
    if msg.endswith("."):
        msg = msg[:-1]
    return (not msg) or bool(_EXIT_CODE_RE.fullmatch(msg))


def _supplement_check_run_failures(result: dict, head_sha: str) -> None:
    """When the check-runs tier answered red and at least one annotation
    is thin (content-free - empty or a bare 'exit code N'), fetch the
    Actions log error lines for the same head and merge them in front of
    the annotations. Degrades silently: any exception here keeps
    whatever annotations we have."""
    failures = result.get("failures") or []
    if failures and not any(_thin_annotation(f) for f in failures):
        return
    try:
        data = _core._request(
            "GET", f"actions/runs?head_sha={head_sha}&per_page={_MAX_CHECK_RUNS}"
        )
        runs = data.get("workflow_runs") or []
        if not runs:
            return
        actions = _checks_from_actions(runs)
        log_lines = actions.get("failures") or []
        if not log_lines:
            return
        merged = log_lines + result["failures"]
        result["failures"] = _dedup_failures(merged)
    except Exception:
        pass


def _checks_for_head(head_sha: str) -> dict | None:
    """CI detail for one commit, tiered and never failing the read: (1) check
    runs with annotations, then (2) GitHub Actions workflow runs with log
    error-lines, then (3) the combined commit status. Each tier's 403/404
    falls into the next; only a total outage yields None."""
    try:
        data = _core._request(
            "GET", f"commits/{head_sha}/check-runs?per_page={_MAX_CHECK_RUNS}"
        )
        runs = data.get("check_runs") or []
        if runs:
            result = _checks_from_check_runs(runs)
            if result["state"] == "failure":
                _supplement_check_run_failures(result, head_sha)
            return result
    except RepoError:
        pass
    try:
        data = _core._request(
            "GET", f"actions/runs?head_sha={head_sha}&per_page={_MAX_CHECK_RUNS}"
        )
        runs = data.get("workflow_runs") or []
        if runs:
            return _checks_from_actions(runs)
    except RepoError:
        pass
    try:
        data = _core._request("GET", f"commits/{head_sha}/status")
        statuses = data.get("statuses") or []
        return {
            "source": "statuses",
            "state": data.get("state") or ("unknown" if not statuses else "pending"),
            "runs": [
                {
                    "name": s.get("context") or "status",
                    "status": "completed",
                    "conclusion": s.get("state"),
                    "html_url": s.get("target_url"),
                }
                for s in statuses
            ],
            "failures": [
                {
                    "name": s.get("context") or "status",
                    "message": " ".join((s.get("description") or "").split()),
                    "log_url": s.get("target_url"),
                }
                for s in statuses
                if s.get("state") in ("failure", "error")
            ],
        }
    except RepoError:
        return None


async def _afetch_annotations(run_id):
    """One check-run's annotations, empty on any API failure - mirrors the
    sync tier's per-run degrade."""
    if run_id is None:
        return []
    try:
        return (
            await _core._arequest(
                "GET", f"check-runs/{run_id}/annotations?per_page=100"
            )
            or []
        )
    except RepoError:
        # domain: degrade-silently - one unreadable annotation set keeps
        # its run entry; sibling runs' annotations still land.
        return []


async def _afrom_check_runs(runs):
    """Async twin of _checks_from_check_runs: identical mapping and
    failure-entry shapes, but every failed run's annotation fetch is
    gathered concurrently instead of chaining."""
    mapped = []
    failed = []
    superseded = _superseded_ids(runs)
    for r in runs:
        name = r.get("name") or "check"
        mapped.append(_map_run(r, name_default="check", status_default="queued"))
        if r.get("id") in superseded:
            # Sync twin's counterpart to the skip in _checks_from_check_runs.
            continue
        if r.get("conclusion") not in (
            "failure",
            "cancelled",
            "timed_out",
            "action_required",
        ):
            continue
        failed.append((name, r.get("id"), r.get("html_url")))
    ann_lists = (
        list(await asyncio.gather(*[_afetch_annotations(rid) for _, rid, _u in failed]))
        if failed
        else []
    )
    failures = []
    for (name, _run_id, run_url), anns in zip(failed, ann_lists, strict=True):
        for a in anns[:_MAX_FAILURE_LINES]:
            failures.append(
                {
                    "name": name,
                    "path": " ".join((a.get("path") or "").split()).strip() or None,
                    "line": a.get("start_line"),
                    "message": " ".join((a.get("message") or "").split())[:2000],
                    "log_url": run_url,
                }
            )
    return {
        "source": "check_runs",
        "state": _ci_state(mapped, superseded),
        "runs": mapped,
        "failures": failures,
    }


async def _afetch_jobs(run_id):
    """One workflow run's jobs, empty on any API failure - mirrors the
    sync tier's per-run degrade."""
    if run_id is None:
        return []
    try:
        data = (
            await _core._arequest("GET", f"actions/runs/{run_id}/jobs?per_page=100")
            or {}
        )
        return data.get("jobs") or []
    except RepoError:
        # domain: degrade-silently - one unreadable jobs list keeps the
        # run link; sibling runs still enrich.
        return []


async def _afetch_job_log(run_id, job_id):
    """A failed job's extracted log error lines plus its web URL -
    mirrors the sync tier's per-job degrade (unreadable log -> no lines,
    no link fabricated)."""
    if job_id is None or run_id is None:
        return [], None
    try:
        lines = _extract_failure_lines(
            await _core._arequest_text("GET", f"actions/jobs/{job_id}/logs") or ""
        )
        return (
            lines[:_MAX_FAILURE_LINES],
            f"https://github.com/{GITHUB_REPO}/actions/runs/{run_id}/job/{job_id}",
        )
    except RepoError:
        # domain: degrade-silently - an unfetchable log keeps the job's
        # place in the batch without fabricating content.
        return [], None


async def _afrom_actions(runs):
    """Async twin of _checks_from_actions: identical mapping and
    failure-entry shapes; failed runs' job lists are gathered
    concurrently, then ALL failed jobs' logs are downloaded concurrently
    (the expensive tail - each can be tens of KB behind a redirect)."""
    mapped = []
    failed_runs = []
    superseded = _superseded_ids(runs)
    for r in runs:
        name = r.get("name") or "workflow"
        mapped.append(_map_run(r, name_default="workflow", status_default="completed"))
        if r.get("id") in superseded:
            # Async twin's counterpart to the skip in _checks_from_actions.
            continue
        if r.get("conclusion") not in ("failure", "cancelled", "timed_out"):
            continue
        failed_runs.append((name, r.get("id"), r.get("html_url")))
    job_lists = (
        list(await asyncio.gather(*[_afetch_jobs(rid) for _n, rid, _u in failed_runs]))
        if failed_runs
        else []
    )
    failed_jobs = []
    for (name, run_id, _url), jobs in zip(failed_runs, job_lists, strict=True):
        for job in jobs:
            if job.get("conclusion") not in ("failure", "cancelled", "timed_out"):
                continue
            failed_jobs.append(
                (name, run_id, f"{name} / {job.get('name') or 'job'}", job.get("id"))
            )
    log_results = (
        list(
            await asyncio.gather(
                *[_afetch_job_log(rid, jid) for _n, rid, _jn, jid in failed_jobs]
            )
        )
        if failed_jobs
        else []
    )
    failures = []
    for (_name, _run_id, fq_name, _jid), (lines, log_url) in zip(
        failed_jobs, log_results, strict=True
    ):
        for line in lines:
            failures.append({"name": fq_name, "message": line, "log_url": log_url})
    return {
        "source": "actions",
        "state": _ci_state(mapped, superseded),
        "runs": mapped,
        "failures": failures,
    }


async def _asupplement_check_run_failures(result, head_sha):
    """Async twin of _supplement_check_run_failures - the same
    per-annotation thin gate and merge order, built on the concurrent
    Actions readers."""
    failures = result.get("failures") or []
    if failures and not any(_thin_annotation(f) for f in failures):
        return
    try:
        data = await _core._arequest(
            "GET",
            f"actions/runs?head_sha={head_sha}&per_page={_MAX_CHECK_RUNS}",
        )
        runs = data.get("workflow_runs") or []
        if not runs:
            return
        actions = await _afrom_actions(runs)
        log_lines = actions.get("failures") or []
        if not log_lines:
            return
        merged = log_lines + result["failures"]
        result["failures"] = _dedup_failures(merged)
    except Exception:
        # domain: degrade-silently - supplement is best-effort enrichment;
        # any failure keeps whatever annotations we already have.
        pass


async def _achecks_impl(number, *, _pr=None, _head_sha=None):
    """Native body behind apr_checks: the same tiered chain as
    _checks_for_head, with intra-tier fan-out. Never fails the read."""
    if _head_sha:
        head_sha = _head_sha
    else:
        pr = _pr or await _core._arequest("GET", f"pulls/{number}")
        head_sha = pr["head"]["sha"]
    try:
        data = await _core._arequest(
            "GET",
            f"commits/{head_sha}/check-runs?per_page={_MAX_CHECK_RUNS}",
        )
        runs = data.get("check_runs") or []
        if runs:
            result = await _afrom_check_runs(runs)
            if result["state"] == "failure":
                await _asupplement_check_run_failures(result, head_sha)
            return result
    except RepoError:
        # domain: degrade-silently - fall through to the Actions tier on
        # any check-runs API failure, exactly like the sync chain.
        pass
    try:
        data = await _core._arequest(
            "GET",
            f"actions/runs?head_sha={head_sha}&per_page={_MAX_CHECK_RUNS}",
        )
        runs = data.get("workflow_runs") or []
        if runs:
            return await _afrom_actions(runs)
    except RepoError:
        # domain: degrade-silently - fall through to combined status on
        # any Actions API failure, exactly like the sync chain.
        pass
    try:
        data = await _core._arequest("GET", f"commits/{head_sha}/status")
        statuses = data.get("statuses") or []
        return {
            "source": "statuses",
            "state": data.get("state") or ("unknown" if not statuses else "pending"),
            "runs": [
                {
                    "name": s.get("context") or "status",
                    "status": "completed",
                    "conclusion": s.get("state"),
                    "html_url": s.get("target_url"),
                }
                for s in statuses
            ],
            "failures": [
                {
                    "name": s.get("context") or "status",
                    "message": " ".join((s.get("description") or "").split()),
                    "log_url": s.get("target_url"),
                }
                for s in statuses
                if s.get("state") in ("failure", "error")
            ],
        }
    except RepoError:
        # domain: degrade-silently - a total outage yields the None shape
        # callers already treat as unknown.
        return None


def pr_checks(
    number: int, *, _pr: dict | None = None, _head_sha: str | None = None
) -> dict:
    """One pull request's CI detail: per-run name/status/conclusion plus the
    actionable failures (annotations with path/line/message, or error lines
    extracted from a capped Actions log tail). The backend is tiered (check
    runs -> Actions workflow runs -> combined commit status) and never fails
    the read: `source` names which tier answered, `state` is 'success' /
    'failure' / 'pending' / 'unknown'. get_pr's `checks` field uses the same
    builder, so a red PR carries its reason everywhere it is read.

    Cached for PR_CACHE_SECONDS (default 45 s).  ``_pr`` is an optional
    pre-fetched raw PR dict to avoid a redundant API call; ``_head_sha`` is
    a private shortcut for callers that already hold the head sha (the CI
    poller) - it skips the PR fetch entirely."""
    cache_key = ("pr_checks", number)
    cached = _core._pr_cache.get(cache_key, config.PR_CACHE_SECONDS)
    if cached is not None:
        return cached
    if _head_sha:
        head_sha = _head_sha
    else:
        pr = _core._request("GET", f"pulls/{number}")
        head_sha = pr["head"]["sha"]
    checks = _checks_for_head(head_sha) or {
        "source": None,
        "state": "unknown",
        "runs": [],
        "failures": [],
    }
    result = {"number": number, "head_sha": head_sha, **checks}
    _core._pr_cache.set(cache_key, result)
    return result


# Maximum seconds to wait for CI after a rebase before giving up.
_REBASE_CI_TIMEOUT = 1800
_REBASE_CI_POLL_INTERVAL = 30


def wait_for_ci(
    number: int,
    *,
    sha: str = "",
    timeout_seconds: int = _REBASE_CI_TIMEOUT,
    poll_interval: int = _REBASE_CI_POLL_INTERVAL,
) -> str:
    """Poll a PR's CI status until it reaches a terminal state.

    Returns "success", "failure", or "timeout".  Used after
    rebase_pr_onto_main to verify the rebased branch still passes
    CI before auto-merge.
    """
    deadline = time.time() + timeout_seconds
    while True:
        checks = pr_checks(number, _head_sha=sha or None)
        state = checks.get("state", "unknown")
        if state in ("success", "failure"):
            return state
        if time.time() >= deadline:
            return "timeout"
        time.sleep(poll_interval)
