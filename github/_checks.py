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


def _ci_state(mapped: list[dict]) -> str:
    """One green/red/pending verdict across a run list: 'failure' when any
    run failed, 'pending' while any is unfinished, else 'success'.
    Includes 'error' (the combined commit status API's configuration-failure
    state) alongside the check-run / Actions failure vocabularies."""
    if any(
        r["conclusion"]
        in ("failure", "cancelled", "timed_out", "action_required", "error")
        for r in mapped
    ):
        return "failure"
    if any(r["conclusion"] is None or r["status"] != "completed" for r in mapped):
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


def _checks_from_check_runs(runs: list[dict]) -> dict:
    """Map check runs (the richest tier) to per-check entries and pull the
    failure annotations - path, start line, message - capped, so a red PR
    carries its reason in the tool result."""
    mapped: list[dict] = []
    failures: list[dict] = []
    for r in runs:
        name = r.get("name") or "check"
        mapped.append(
            {
                "name": name,
                "status": r.get("status") or "queued",
                "conclusion": r.get("conclusion"),
                "html_url": r.get("html_url"),
            }
        )
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
                    "path": a.get("path"),
                    "line": a.get("start_line"),
                    "message": (a.get("message") or "")[:2000],
                    "log_url": r.get("html_url"),
                }
            )
    return {
        "source": "check_runs",
        "state": _ci_state(mapped),
        "runs": mapped,
        "failures": failures,
    }


def _checks_from_actions(runs: list[dict]) -> dict:
    """Map GitHub Actions workflow runs; for each failed run, fetch the jobs
    and pull error lines from a capped log tail. Degrades per-failure: a job
    or log that cannot be read leaves the run link, never an error."""
    mapped: list[dict] = []
    failures: list[dict] = []
    for r in runs:
        name = r.get("name") or "workflow"
        conclusion = r.get("conclusion")
        run_id = r.get("id")
        run_url = r.get("html_url")
        mapped.append(
            {
                "name": name,
                "status": r.get("status") or "completed",
                "conclusion": conclusion,
                "html_url": run_url,
            }
        )
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
        "state": _ci_state(mapped),
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
    """When the check-runs tier answered red but its annotations are thin
    (every entry is content-free - empty or a bare 'exit code N'), fetch
    the Actions log error lines for the same head and merge them in front
    of the annotations. Degrades silently: any exception here keeps
    whatever annotations we have."""
    failures = result.get("failures") or []
    if failures and not all(_thin_annotation(f) for f in failures):
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
                    "message": s.get("description") or "",
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
    for r in runs:
        name = r.get("name") or "check"
        mapped.append(
            {
                "name": name,
                "status": r.get("status") or "queued",
                "conclusion": r.get("conclusion"),
                "html_url": r.get("html_url"),
            }
        )
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
                    "path": a.get("path"),
                    "line": a.get("start_line"),
                    "message": (a.get("message") or "")[:2000],
                    "log_url": run_url,
                }
            )
    return {
        "source": "check_runs",
        "state": _ci_state(mapped),
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
    for r in runs:
        name = r.get("name") or "workflow"
        mapped.append(
            {
                "name": name,
                "status": r.get("status") or "completed",
                "conclusion": r.get("conclusion"),
                "html_url": r.get("html_url"),
            }
        )
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
        "state": _ci_state(mapped),
        "runs": mapped,
        "failures": failures,
    }


async def _asupplement_check_run_failures(result, head_sha):
    """Async twin of _supplement_check_run_failures - the same
    thin-annotation gate and merge order, built on the concurrent
    Actions readers."""
    failures = result.get("failures") or []
    if failures and not all(_thin_annotation(f) for f in failures):
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
                    "message": s.get("description") or "",
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

    Cached for PR_CACHE_SECONDS (default 30 s).  ``_pr`` is an optional
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
