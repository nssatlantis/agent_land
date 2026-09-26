"""Regression guards for the supersede-aware CI verdict (bug #B123).

GitHub cancels a superseded workflow run automatically when a newer one starts
for the same ref, and both tier queries are head_sha-scoped - so one head's
run list can hold a green terminal run *and* an older cancelled duplicate of
the same check. Folding 'cancelled' into 'failure' let the superseded copy
outvote the run that actually describes the tip: PR #1478 reported
"CI: failing (7 runs)" on a head whose newest test/static runs were both green,
because two *older* runs of the same names had been cancelled by supersession.

These pins drive the real delivery path - _checks_for_head -> _core._request
over an httpx MockTransport, the house _install_mock pattern - rather than
calling _ci_state directly, because a pin on the pure helper would not prove
the tier functions wire the collapse in. Both verdict directions are covered:
without the flip case the first pin could not fail for the reason its name
claims, and without the unnamed-run and no-id pins a fix that collapsed too
eagerly would trade a false red for a false green.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

import github._checks as gh_checks  # noqa: E402
import github._core as gh_core  # noqa: E402

gh_core.GITHUB_TOKEN = "test-token"  # satisfies _ensure_token(); no network touched

_HEAD = "a80eaa2f"


def _install_mock(handler):
    """Point the module's shared client at an httpx.MockTransport-backed
    client. Returns the previous client for restoration."""
    old = gh_core._client
    gh_core._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.github.com",
    )
    return old


def _check_runs_handler(runs, annotations=None):
    """Serve exactly one head's check-runs tier.

    Every other tier answers empty or 404, so the test cannot accidentally
    pass on the Actions or combined-status path instead of the one under test.
    """
    ann = annotations or {}

    def handler(request):
        path = request.url.path
        if "/check-runs/" in path and path.endswith("/annotations"):
            rid = int(path.split("/check-runs/")[1].split("/")[0])
            return httpx.Response(200, json=ann.get(rid, []))
        if path.endswith("/check-runs"):
            return httpx.Response(
                200, json={"check_runs": runs, "total_count": len(runs)}
            )
        if path.endswith("/actions/runs"):
            return httpx.Response(200, json={"workflow_runs": []})
        if path.endswith("/jobs"):
            return httpx.Response(200, json={"jobs": []})
        return httpx.Response(404, json={"message": "Not Found"})

    return handler


def _verdict(runs, annotations=None):
    """The checks result for one head, through the real read path."""
    old = _install_mock(_check_runs_handler(runs, annotations))
    try:
        result = gh_checks._checks_for_head(_HEAD)
    finally:
        gh_core._client = old
    assert result is not None, "the check-runs tier answered nothing"
    assert result["source"] == "check_runs", result
    return result


def _run(rid, name, conclusion, status="completed"):
    entry = {"id": rid, "status": status, "conclusion": conclusion}
    if name is not None:
        entry["name"] = name
    return entry


def test_superseded_cancelled_does_not_fail_a_green_head():
    """#B123. The newest run of a check is authoritative, so an older duplicate
    that GitHub cancelled automatically must not decide the verdict. This is
    the exact #1478 shape: older test/static cancelled, newer green."""
    result = _verdict(
        [
            _run(36213323700, "test", "cancelled"),
            _run(36213323983, "test", "success"),
        ]
    )
    assert result["state"] == "success", result
    assert result["failures"] == [], result
    print("  superseded cancelled run does not fail a green head: ok")


def test_newer_failure_still_fails_over_older_success():
    """The flip. Without this the pin above could pass for the wrong reason -
    a collapse that simply ignored red runs would satisfy it too."""
    result = _verdict(
        [
            _run(36213323983, "test", "success"),
            _run(36213324000, "test", "failure"),
        ]
    )
    assert result["state"] == "failure", result
    print("  newer failure still fails over an older success: ok")


def test_cancelled_newest_run_is_still_a_failure():
    """Guards against 'fix' = deleting 'cancelled' from the failure tuple.
    A cancelled tip has no green verdict, so it must stay red."""
    result = _verdict([_run(1, "test", "cancelled")])
    assert result["state"] == "failure", result
    print("  a cancelled newest run is still a failure: ok")


def test_superseded_cancellation_contributes_no_failure_annotation():
    """The second half of the defect. The verdict fix alone would still let
    the discounted run contribute its 'Canceling since a higher priority
    waiting request ...' annotation to failures, leaving a success verdict
    carrying failure entries."""
    cancel_note = (
        "Canceling since a higher priority waiting request for "
        "CI-refs/pull/1478/merge exists"
    )
    result = _verdict(
        [_run(100, "test", "cancelled"), _run(200, "test", "success")],
        {100: [{"path": "tests/test_x.py", "start_line": 1, "message": cancel_note}]},
    )
    assert result["state"] == "success", result
    assert result["failures"] == [], result
    print("  superseded cancellation contributes no failure annotation: ok")


def test_unnamed_runs_never_supersede_each_other():
    """Two unnamed runs are not verifiably the same logical check. _map_run
    defaults a missing name to 'check', so collapsing them would let a newer
    success hide an older real failure - a false green for a false red."""
    result = _verdict([_run(300, None, "failure"), _run(400, None, "success")])
    assert result["state"] == "failure", result
    print("  unnamed runs never supersede each other: ok")


def test_runs_without_ids_fall_back_to_any_failure():
    """A run with no numeric id cannot be ranked, so nothing is discounted and
    the verdict degrades to the pre-#B123 behaviour rather than guessing."""
    result = _verdict([_run(None, "test", "cancelled"), _run(None, "test", "success")])
    assert result["state"] == "failure", result
    print("  runs without ids fall back to any-failure: ok")


if __name__ == "__main__":
    test_superseded_cancelled_does_not_fail_a_green_head()
    test_newer_failure_still_fails_over_older_success()
    test_cancelled_newest_run_is_still_a_failure()
    test_superseded_cancellation_contributes_no_failure_annotation()
    test_unnamed_runs_never_supersede_each_other()
    test_runs_without_ids_fall_back_to_any_failure()
    print("CI verdict supersede guards: ok")
