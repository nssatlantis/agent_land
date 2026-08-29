"""The PR-comment nudge: once per new batch of OUT-OF-BAND comments.

Covers server.poller.sweep_pr_comments - the mailbox 'pr' notification for
GitHub-UI conversation and inline review notes, the comments
repo_comment_on_pr does not already ping about - with an injected fake
comments builder, so no GitHub call ever happens.
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_pr_comments_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402, I001
from server.poller import sweep_pr_comments  # noqa: E402, I001
from notifications import notifications as get_notifications  # noqa: E402, I001


def _comment(cid: int, author: str) -> dict:
    return {
        "id": cid,
        "kind": "issue",
        "author": author,
        "body": f"review feedback {cid}",
        "created_at": "2026-08-01T00:00:00.000Z",
    }


def _open_pr(
    number: int, title: str = "The change", citizen: dict | None = None
) -> dict:
    return {
        "number": number,
        "title": title,
        "head_sha": "sha1",
        "body": "",
        "citizen": citizen,
    }


def _mark(pr_number: int) -> int | None:
    with db._conn() as conn:
        row = conn.execute(
            "SELECT last_comment_id FROM pr_comment_seen WHERE pr_number = ?",
            (pr_number,),
        ).fetchone()
    return row["last_comment_id"] if row else None


def main():
    agents, post_id = setup()
    owner = agents["alpha"]
    other = agents["beta"]

    # PR 7001 is linked to alpha's proposal; 7002 stays unlinked (owner
    # resolved from the citizen trailer hint instead).
    db.link_pr_to_proposal(7001, post_id, owner["agent_id"])
    n1 = _open_pr(7001, citizen={"name": "alpha", "agent_id": owner["agent_id"]})
    before = get_notifications(owner["token"], unread_only=True)["unread_count"]

    # Fresh PR: baselines to the current max id WITH NO notify, so a PR's
    # pre-alert comment history never replays into the mailbox.
    assert (
        sweep_pr_comments(
            [n1],
            comments_fn=lambda _: [
                _comment(101, "beta"),
                _comment(100, "gamma"),
            ],
        )
        == []
    )
    assert (
        get_notifications(owner["token"], unread_only=True)["unread_count"] == before
    ), "baseline never notifies"
    assert _mark(7001) == 101, "baseline row records the max comment id"

    # New comments above the mark -> exactly ONE nudge; the opener's own
    # comments are skipped and old ones are not re-reported.
    def comments2(number: int) -> list[dict]:
        return [
            _comment(105, "beta"),
            _comment(104, "delta"),
            _comment(103, "Alpha"),  # the opener themselves - skipped
            _comment(102, "gamma"),
            _comment(101, "beta"),  # already accounted for at baseline
        ]

    assert sweep_pr_comments([n1], comments_fn=comments2) == [7001]
    after = get_notifications(owner["token"], unread_only=True)
    assert after["unread_count"] == before + 1, "one PR-comment nudge"
    nudge = after["notifications"][0]
    assert nudge["kind"] == "pr"
    assert nudge["ref_type"] == "pr"
    assert nudge["ref_id"] == 7001
    assert nudge["actor"] is None, "server-sourced mail"
    assert "3 new comment(s) on PR #7001" in nudge["body"], nudge["body"]
    assert "beta" in nudge["body"] and "delta" in nudge["body"], nudge["body"]
    assert "Alpha" not in nudge["body"], "opener's own comment is not attributed"
    assert _mark(7001) == 105, "watermark advanced past the newest comment"

    # Same comments again -> no second nudge (exactly-once per batch).
    assert sweep_pr_comments([n1], comments_fn=comments2) == []
    assert (
        get_notifications(owner["token"], unread_only=True)["unread_count"]
        == before + 1
    )

    # An in-band comment - repo_comment_on_pr already raised the mark when
    # it pinged the owner, so the sweep must not double-fire on it.
    def comments3(number: int) -> list[dict]:
        return [_comment(106, "beta")]

    with db._conn() as conn:
        conn.execute(
            "UPDATE pr_comment_seen SET last_comment_id = 106 WHERE pr_number = 7001"
        )
    assert sweep_pr_comments([n1], comments_fn=comments3) == [], (
        "an in-band comment the forum already pings about is never re-pinged"
    )

    # A genuinely new batch above the bumped mark still nudges (only 107).
    def comments4(number: int) -> list[dict]:
        return [_comment(107, "zeta"), _comment(106, "beta")]

    assert sweep_pr_comments([n1], comments_fn=comments4) == [7001]
    assert _mark(7001) == 107

    # All-self comments: nothing fresh, but the mark still advances.
    def comments5(number: int) -> list[dict]:
        return [_comment(202, "Alpha"), _comment(201, "alpha")]

    with db._conn() as conn:
        conn.execute(
            "UPDATE pr_comment_seen SET last_comment_id = 200 WHERE pr_number = 7001"
        )
    assert sweep_pr_comments([n1], comments_fn=comments5) == [], (
        "all-self-comments pings nobody"
    )
    assert _mark(7001) == 202, "self-comments advance the mark silently"

    # Trailer fallback: an unlinked PR resolves its owner from the citizen
    # hint - baselines first, then nudges.
    n2 = _open_pr(
        7002, "Other change", citizen={"name": "beta", "agent_id": other["agent_id"]}
    )
    other_before = get_notifications(other["token"], unread_only=True)["unread_count"]
    assert sweep_pr_comments([n2], comments_fn=comments2) == []
    assert sweep_pr_comments([n2], comments_fn=comments4) == [7002], (
        "unlinked PR nudged via the trailer owner"
    )
    assert (
        get_notifications(other["token"], unread_only=True)["unread_count"]
        == other_before + 1
    )

    # Per-entry fault isolation (resilience #2953): a nudge failure on one
    # PR must not abort the rest of the batch.
    import notifications as _notif_module

    def build_two_prs():
        return [
            _open_pr(
                7011, "Boom", citizen={"name": "alpha", "agent_id": owner["agent_id"]}
            ),
            _open_pr(
                7013, "Fine", citizen={"name": "beta", "agent_id": other["agent_id"]}
            ),
        ]

    real_notify = _notif_module._notify

    def flaky_notify(
        conn, agent_id, kind, target_type, target_id, body, actor_agent_id=None
    ):
        if target_id == 7011:
            raise RuntimeError("boom in nudge")
        return real_notify(
            conn,
            agent_id,
            kind,
            target_type,
            target_id,
            body,
            actor_agent_id=actor_agent_id,
        )

    sweep_pr_comments(build_two_prs(), comments_fn=comments2)  # baseline both
    _notif_module._notify = flaky_notify
    flaky_before = get_notifications(other["token"], unread_only=True)["unread_count"]
    try:
        flaky_notified = sweep_pr_comments(build_two_prs(), comments_fn=comments4)
    finally:
        _notif_module._notify = real_notify
    assert flaky_notified == [7013], "a failing nudge on 7011 must not starve 7013"
    assert (
        get_notifications(other["token"], unread_only=True)["unread_count"]
        == flaky_before + 1
    ), "7013 still nudged despite 7011's notify failure"

    print("test_pr_comment_sweep.py ok")


if __name__ == "__main__":
    main()
