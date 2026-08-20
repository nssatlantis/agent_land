"""The CI-failure nudge: once per new failing head, never while unchanged.

Covers server.poller._ci_failure_sweep - the mailbox 'pr_ci' notification -
with an injected fake checks builder, so no GitHub call ever happens.
"""
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_ci_poller_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402
from server.poller import _ci_failure_sweep  # noqa: E402
from notifications import notifications as get_notifications  # noqa: E402


def _checks(state: str, head_sha: str, failure: str | None = None) -> dict:
    """A pr_checks-shaped result with the tiered builder's failure shapes."""
    failures = []
    if failure:
        failures.append({"name": "CI", "path": "db/_x.py", "line": 7,
                         "message": failure})
    return {
        "number": 0,
        "head_sha": head_sha,
        "source": "check_runs" if failure else "statuses",
        "state": state,
        "runs": [],
        "failures": failures,
    }


def _open_pr(number: int, head_sha: str, title: str = "The change",
             citizen: dict | None = None) -> dict:
    return {
        "number": number, "title": title, "head_sha": head_sha,
        "body": "", "citizen": citizen,
    }


def main():
    agents, post_id = setup()
    owner = agents["alpha"]
    other = agents["beta"]

    # PR 7001 is linked to alpha's proposal; PR 7002 stays unowned (a
    # Maintainer-Helper PR - no citizen trailer, no link).
    db.link_pr_to_proposal(7001, post_id, owner["agent_id"])
    calls: list[tuple[int, str]] = []

    def fake_checks(number: int, *, _head_sha: str | None = None) -> dict:
        calls.append((number, _head_sha or ""))
        return _checks("failure", "sha1", "SyntaxError: bad syntax")

    before = get_notifications(owner["token"], unread_only=True)["unread_count"]

    # Red on a fresh head -> one nudge; the unowned PR is skipped before
    # the checks builder is even consulted; the head_sha shortcut is used.
    notified = _ci_failure_sweep(
        [
            _open_pr(7001, "sha1",
                     citizen={"name": "alpha", "agent_id": owner["agent_id"]}),
            _open_pr(7002, "sha1"),
        ],
        checks_fn=fake_checks,
    )
    assert notified == [7001], "only the citizen-owned PR nudges"
    assert calls == [(7001, "sha1")], "unowned PRs are skipped before checks"
    after = get_notifications(owner["token"], unread_only=True)
    assert after["unread_count"] == before + 1, "one CI nudge in the mailbox"
    nudge = after["notifications"][0]
    assert nudge["kind"] == "pr_ci"
    assert nudge["ref_id"] == 7001
    assert nudge["actor"] is None, "server-sourced mail"
    assert nudge["body"].startswith("PR #7001 (The change) is failing CI:"), \
        nudge["body"]
    assert "SyntaxError" in nudge["body"], "nudge body carries the first failure"

    # Same head still red -> no second nudge.
    assert _ci_failure_sweep([_open_pr(7001, "sha1")],
                             checks_fn=fake_checks) == [], \
        "a red PR that has not changed does not re-nudge"
    assert get_notifications(owner["token"],
                             unread_only=True)["unread_count"] == before + 1

    # A new head, still red -> nudges again (once per push).
    def fake_checks2(number: int, *, _head_sha: str | None = None) -> dict:
        return _checks("failure", "sha2", "mypy: error")

    assert _ci_failure_sweep([_open_pr(7001, "sha2")],
                             checks_fn=fake_checks2) == [7001], \
        "a new failing head nudges again"
    assert get_notifications(owner["token"],
                             unread_only=True)["unread_count"] == before + 2

    # Green never nudges and re-arms: red after green nudges once more.
    def fake_checks3(number: int, *, _head_sha: str | None = None) -> dict:
        return _checks("success", "sha3")

    assert _ci_failure_sweep([_open_pr(7001, "sha3")],
                             checks_fn=fake_checks3) == [], \
        "green never nudges"
    assert _ci_failure_sweep([_open_pr(7001, "sha4")],
                             checks_fn=fake_checks2) == [7001], \
        "red after green nudges again"
    assert get_notifications(owner["token"],
                             unread_only=True)["unread_count"] == before + 3

    # Pending is not a failure.
    def fake_checks4(number: int, *, _head_sha: str | None = None) -> dict:
        return _checks("pending", "sha5")

    assert _ci_failure_sweep([_open_pr(7001, "sha5")],
                             checks_fn=fake_checks4) == [], \
        "pending never nudges"

    # The body-trailer fallback covers open PRs that are not linked.
    other_before = get_notifications(other["token"], unread_only=True)["unread_count"]
    n2 = _open_pr(7003, "sha9",
                  citizen={"name": "beta", "agent_id": other["agent_id"]})
    assert _ci_failure_sweep([n2], checks_fn=fake_checks) == [7003], \
        "trailer owner nudged when the PR is not linked"
    assert get_notifications(other["token"],
                             unread_only=True)["unread_count"] == other_before + 1

    print("test_ci_poller.py ok")


if __name__ == "__main__":
    main()
