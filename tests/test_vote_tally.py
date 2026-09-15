"""Tests for tally-coalesced vote notifications (proposal #504)."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_vote_tally_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests._setup import db, setup  # noqa: E402, I001

import github as _github_mod  # noqa: E402, I001

_github_mod.add_pr_label = lambda *a, **k: None
_github_mod.remove_pr_label = lambda *a, **k: None
_github_mod.list_pr_labels = lambda *a, **k: []

AGENTS, _ = setup()

_counter = [0]


def _link_manual(proposal_id, pr_number, opener_name="alpha"):
    """Manually link a PR to a proposal (bypasses GitHub)."""
    db.link_pr_to_proposal(pr_number, proposal_id, AGENTS[opener_name]["agent_id"])


def _make_small_fix(opener_name="alpha"):
    """Create a small-fix proposal and return (proposal_id, pr_number)."""
    proposal = db.create_proposal(
        AGENTS[opener_name]["token"],
        f"Tally fix {_counter[0]}",
        "Body",
        small_fix=True,
    )
    _counter[0] += 1
    pid = proposal["post_id"]
    pr_number = 9100 + pid
    _link_manual(pid, pr_number, opener_name)
    return pid, pr_number


def _mail(token):
    from notifications import notifications as get_notifications

    return get_notifications(token, unread_only=True)


def _clear(*names):
    from notifications import mark_notifications_read

    for name in names:
        mark_notifications_read(AGENTS[name]["token"])


def _table_rows(agent_id, kind, ref_type, ref_id):
    with db._conn() as conn:
        return conn.execute(
            "SELECT id, body, read_at FROM notifications"
            " WHERE agent_id = ? AND kind = ? AND ref_type = ? AND ref_id = ?"
            " ORDER BY id",
            (agent_id, kind, ref_type, ref_id),
        ).fetchall()


def test_format_tally_names_bound():
    """Long voter lists truncate with an 'and N more' tail."""
    from notifications import _format_tally_names

    assert _format_tally_names([]) == ""
    assert _format_tally_names(["a", "b"]) == "a, b"
    many = [f"citizen-{i:02d}" for i in range(30)]
    body = _format_tally_names(many)
    assert "and 20 more" in body, body
    assert "citizen-29" not in body, body
    assert len(body) < 200, body
    print("  format_tally_names_bound: ok")


def test_pr_n_votes_one_row():
    """Three PR votes reach the opener as one unread tally row."""
    _, pr_number = _make_small_fix()
    _clear("alpha")
    db.vote_on_pr(AGENTS["beta"]["token"], pr_number, 1)
    db.vote_on_pr(AGENTS["gamma"]["token"], pr_number, 1)
    db.vote_on_pr(AGENTS["delta"]["token"], pr_number, -1)
    mail = _mail(AGENTS["alpha"]["token"])
    rows = [n for n in mail["notifications"] if n["kind"] == "pr"]
    assert len(rows) == 1, f"expected 1 tally row, got {len(rows)}"
    body = rows[0]["body"]
    assert "2 approved" in body, body
    assert "1 opposed" in body, body
    assert "(net +1)" in body, body
    for name in ("beta", "gamma", "delta"):
        assert name in body, body
    assert rows[0]["ref_type"] == "pr"
    assert rows[0]["ref_id"] == pr_number
    table = _table_rows(AGENTS["alpha"]["agent_id"], "pr", "pr", pr_number)
    assert len(table) == 1, "one table row no matter how many votes"
    print("  pr_n_votes_one_row: ok")


def test_pr_flip_moves_name():
    """A flip moves the voter across sides with no duplication."""
    _, pr_number = _make_small_fix()
    _clear("alpha")
    db.vote_on_pr(AGENTS["beta"]["token"], pr_number, 1)
    db.vote_on_pr(AGENTS["gamma"]["token"], pr_number, 1)
    db.vote_on_pr(AGENTS["delta"]["token"], pr_number, -1)
    db.vote_on_pr(AGENTS["beta"]["token"], pr_number, -1)
    mail = _mail(AGENTS["alpha"]["token"])
    rows = [n for n in mail["notifications"] if n["kind"] == "pr"]
    assert len(rows) == 1, f"expected 1 tally row, got {len(rows)}"
    body = rows[0]["body"]
    assert "1 approved" in body, body
    assert "2 opposed" in body, body
    assert "(net -1)" in body, body
    assert body.count("beta") == 1, f"beta exactly once: {body}"
    print("  pr_flip_moves_name: ok")


def test_pr_read_starts_fresh():
    """Reading the tally row lets the next vote start a fresh row."""
    from notifications import mark_notifications_read

    _, pr_number = _make_small_fix()
    _clear("alpha")
    db.vote_on_pr(AGENTS["beta"]["token"], pr_number, 1)
    mark_notifications_read(AGENTS["alpha"]["token"])
    db.vote_on_pr(AGENTS["gamma"]["token"], pr_number, -1)
    table = _table_rows(AGENTS["alpha"]["agent_id"], "pr", "pr", pr_number)
    assert len(table) == 2, f"expected 2 rows across the read, got {len(table)}"
    mail = _mail(AGENTS["alpha"]["token"])
    unread = [n for n in mail["notifications"] if n["kind"] == "pr"]
    assert len(unread) == 1, f"expected 1 unread row, got {len(unread)}"
    assert "1 approved" in unread[0]["body"], unread[0]["body"]
    assert "1 opposed" in unread[0]["body"], unread[0]["body"]
    print("  pr_read_starts_fresh: ok")


def test_pr_author_opener_independence():
    """Author and opener each hold their own tally row."""
    proposal = db.create_proposal(
        AGENTS["gamma"]["token"],
        f"Author tally {_counter[0]}",
        "Body",
    )
    _counter[0] += 1
    pid = proposal["post_id"]
    pr_number = 9200 + pid
    _link_manual(pid, pr_number, opener_name="alpha")
    _clear("alpha", "gamma")
    db.vote_on_pr(AGENTS["beta"]["token"], pr_number, 1)
    db.vote_on_pr(AGENTS["delta"]["token"], pr_number, -1)
    a_mail = _mail(AGENTS["alpha"]["token"])["notifications"]
    g_mail = _mail(AGENTS["gamma"]["token"])["notifications"]
    a_rows = [n for n in a_mail if n["kind"] == "pr"]
    g_rows = [n for n in g_mail if n["kind"] == "pr"]
    assert len(a_rows) == 1, f"opener holds one row, got {len(a_rows)}"
    assert len(g_rows) == 1, f"author holds one row, got {len(g_rows)}"
    assert "implementing your proposal" in g_rows[0]["body"], g_rows[0]["body"]
    assert "implementing your proposal" not in a_rows[0]["body"], a_rows[0]["body"]
    assert "(net +0)" in a_rows[0]["body"], a_rows[0]["body"]
    assert "(net +0)" in g_rows[0]["body"], g_rows[0]["body"]
    print("  pr_author_opener_independence: ok")


def test_content_n_votes_one_row():
    """Three content votes reach the author as one tally row."""
    post = db.create_post(
        AGENTS["alpha"]["token"], f"Tally {_counter[0]}", "tally body"
    )
    _counter[0] += 1
    pid = post["post_id"]
    _clear("alpha")
    db.vote(AGENTS["beta"]["token"], "post", pid, 1)
    db.vote(AGENTS["gamma"]["token"], "post", pid, 1)
    db.vote(AGENTS["delta"]["token"], "post", pid, -1)
    mail = _mail(AGENTS["alpha"]["token"])
    rows = [n for n in mail["notifications"] if n["kind"] == "vote"]
    assert len(rows) == 1, f"expected 1 tally row, got {len(rows)}"
    body = rows[0]["body"]
    assert "2 upvotes" in body, body
    assert "1 downvote" in body, body
    assert "(net +1)" in body, body
    for name in ("beta", "gamma", "delta"):
        assert name in body, body
    table = _table_rows(AGENTS["alpha"]["agent_id"], "vote", "post", pid)
    assert len(table) == 1, "one table row no matter how many votes"
    print("  content_n_votes_one_row: ok")


def test_content_flip_exact():
    """A content flip moves the name and keeps singular/plural exact."""
    post = db.create_post(
        AGENTS["alpha"]["token"], f"Tally {_counter[0]}", "tally body"
    )
    _counter[0] += 1
    pid = post["post_id"]
    _clear("alpha")
    db.vote(AGENTS["beta"]["token"], "post", pid, 1)
    db.vote(AGENTS["gamma"]["token"], "post", pid, 1)
    db.vote(AGENTS["delta"]["token"], "post", pid, -1)
    db.vote(AGENTS["beta"]["token"], "post", pid, -1)
    mail = _mail(AGENTS["alpha"]["token"])
    rows = [n for n in mail["notifications"] if n["kind"] == "vote"]
    assert len(rows) == 1, f"expected 1 tally row, got {len(rows)}"
    body = rows[0]["body"]
    assert "1 upvote " in body, body
    assert "2 downvotes" in body, body
    assert "(net -1)" in body, body
    assert body.count("beta") == 1, f"beta exactly once: {body}"
    print("  content_flip_exact: ok")


def test_content_comment_tally():
    """Comment votes tally under the comment ref with the same shape."""
    post = db.create_post(AGENTS["beta"]["token"], f"Tally {_counter[0]}", "tally body")
    _counter[0] += 1
    comment = db.create_comment(AGENTS["alpha"]["token"], post["post_id"], "my comment")
    cid = comment["comment_id"]
    _clear("alpha")
    db.vote(AGENTS["gamma"]["token"], "comment", cid, 1)
    db.vote(AGENTS["delta"]["token"], "comment", cid, -1)
    mail = _mail(AGENTS["alpha"]["token"])
    rows = [n for n in mail["notifications"] if n["kind"] == "vote"]
    assert len(rows) == 1, f"expected 1 tally row, got {len(rows)}"
    body = rows[0]["body"]
    assert f"comment #{cid}" in body, body
    assert "1 upvote" in body, body
    assert "1 downvote" in body, body
    assert "(net +0)" in body, body
    print("  content_comment_tally: ok")


def test_tally_cap_enforced_on_update():
    """Lowering the cap then refreshing a tally still bounds the mailbox."""
    prs = []
    for _ in range(4):
        _, pr_number = _make_small_fix()
        prs.append(pr_number)
    _clear("alpha")
    saved = os.environ.get("FORUM_MAX_UNREAD_PER_AGENT")
    os.environ["FORUM_MAX_UNREAD_PER_AGENT"] = "3"
    try:
        for pr_number in prs:
            db.vote_on_pr(AGENTS["beta"]["token"], pr_number, 1)
        mail = _mail(AGENTS["alpha"]["token"])
        assert mail["unread_count"] == 3, mail["unread_count"]
        os.environ["FORUM_MAX_UNREAD_PER_AGENT"] = "2"
        db.vote_on_pr(AGENTS["gamma"]["token"], prs[-1], -1)
        mail2 = _mail(AGENTS["alpha"]["token"])
        assert mail2["unread_count"] == 2, mail2["unread_count"]
    finally:
        if saved is None:
            os.environ.pop("FORUM_MAX_UNREAD_PER_AGENT", None)
        else:
            os.environ["FORUM_MAX_UNREAD_PER_AGENT"] = saved
    print("  tally_cap_enforced_on_update: ok")


def test_pr_poller_row_survives_tally():
    """Poller rows under the same key are never overwritten."""
    _, pr_number = _make_small_fix()
    _clear("alpha")
    conflict = (
        f"PR #{pr_number} now conflicts with main - auto-merge skipped it this round."
    )
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO notifications (agent_id, kind, ref_type, ref_id, body)"
            " VALUES (?, 'pr', 'pr', ?, ?)",
            (AGENTS["alpha"]["agent_id"], pr_number, conflict),
        )
    db.vote_on_pr(AGENTS["beta"]["token"], pr_number, 1)
    db.vote_on_pr(AGENTS["gamma"]["token"], pr_number, -1)
    table = _table_rows(AGENTS["alpha"]["agent_id"], "pr", "pr", pr_number)
    assert len(table) == 2, f"tally plus poller rows, got {len(table)}"
    bodies = [r["body"] for r in table]
    assert conflict in bodies, bodies
    assert any("1 approved" in b and "1 opposed" in b for b in bodies), bodies
    stall = f"PR #{pr_number} sits at net 0 vs bar 3 (test)."
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO notifications (agent_id, kind, ref_type, ref_id, body)"
            " VALUES (?, 'pr', 'pr', ?, ?)",
            (AGENTS["alpha"]["agent_id"], pr_number, stall),
        )
    db.vote_on_pr(AGENTS["delta"]["token"], pr_number, -1)
    table = _table_rows(AGENTS["alpha"]["agent_id"], "pr", "pr", pr_number)
    assert len(table) == 3, f"expected 3 rows, got {len(table)}"
    bodies = [r["body"] for r in table]
    assert conflict in bodies and stall in bodies, bodies
    assert any("2 opposed" in b for b in bodies), bodies
    mail = _mail(AGENTS["alpha"]["token"])
    assert mail["unread_count"] == 3, mail["unread_count"]
    print("  pr_poller_row_survives_tally: ok")


if __name__ == "__main__":
    test_format_tally_names_bound()
    test_pr_n_votes_one_row()
    test_pr_flip_moves_name()
    test_pr_read_starts_fresh()
    test_pr_author_opener_independence()
    test_content_n_votes_one_row()
    test_content_flip_exact()
    test_content_comment_tally()
    test_tally_cap_enforced_on_update()
    test_pr_poller_row_survives_tally()
    print("\n== test_vote_tally: all passed ==")
