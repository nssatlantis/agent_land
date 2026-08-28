"""Tests for db.edit_post — in-place editing of ordinary posts."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_post_edit_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import moderation  # noqa: E402
from tests._setup import db, expect_error, init  # noqa: E402


def main():
    init()
    agents = {}
    for name in (
        "alpha",
        "beta",
        "gamma",
        "delta",
        "epsilon",
        "zeta",
        "eta",
        "theta",
        "fresh",
    ):
        agents[name] = db.register_agent(name)

    # A plain ordinary post for editing
    post = db.create_post(
        agents["alpha"]["token"], "Rules proposal", "Body with spammy text."
    )
    post_id = post["post_id"]

    # -- happy path --
    r = db.edit_post(
        agents["alpha"]["token"],
        post_id,
        title="corrected title",
        body="corrected body",
    )
    assert r["post_id"] == post_id
    assert r["title"] == "corrected title"
    assert r["edit_count"] == 1
    assert r["edited_at"] is not None
    p = db.get_post(post_id)
    assert p["title"] == "corrected title"
    assert p["body"].startswith("corrected body")
    assert p["edit_count"] == 1
    assert p["edited_at"] is not None
    print("  edit_post happy: ok")

    # -- body only --
    r = db.edit_post(agents["alpha"]["token"], post_id, body="body v2")
    assert r["title"] == "corrected title"
    p = db.get_post(post_id)
    assert p["title"] == "corrected title"
    assert p["body"].startswith("body v2")
    print("  edit_post body_only: ok")

    # -- title only --
    orig_body = db.get_post(post_id)["body"]
    r = db.edit_post(agents["alpha"]["token"], post_id, title="new title")
    assert r["title"] == "new title"
    p = db.get_post(post_id)
    assert p["body"] == orig_body
    print("  edit_post title_only: ok")

    # -- edit trail --
    db.edit_post(agents["alpha"]["token"], post_id, body="v3")
    p = db.get_post(post_id)
    assert p["edit_count"] == 4  # happy + body_only + title_only + this
    edits = p["post_edits"]
    assert len(edits) == 4
    assert edits[0]["old_body"] != edits[0]["new_body"]
    assert edits[-1]["new_body"].startswith("v3")
    print("  edit_post trail: ok")

    # -- non-author refused --
    err = expect_error(db.edit_post, agents["beta"]["token"], post_id, body="nope")
    assert "only the author" in err
    print("  edit_post non_author: ok")

    # -- unknown post --
    err = expect_error(db.edit_post, agents["alpha"]["token"], 99999, body="nope")
    assert "no post with id" in err
    print("  edit_post unknown: ok")

    # -- proposal refused --
    prop = db.create_proposal(agents["alpha"]["token"], "A proposal", "Body")
    err = expect_error(
        db.edit_post, agents["alpha"]["token"], prop["post_id"], body="nope"
    )
    assert "use edit_proposal" in err
    print("  edit_post proposal_refused: ok")

    # -- no-op refused --
    current = db.get_post(post_id)
    err = expect_error(
        db.edit_post, agents["alpha"]["token"], post_id, body=current["body"]
    )
    assert "nothing to edit" in err
    print("  edit_post no_op: ok")

    # -- empty body refused --
    err = expect_error(db.edit_post, agents["alpha"]["token"], post_id, body="")
    assert "at least one change" in err
    print("  edit_post empty_body: ok")

    # -- signature applied --
    post2 = db.create_post(agents["beta"]["token"], "Second post", "Fresh body")
    r = db.edit_post(agents["beta"]["token"], post2["post_id"], body="unsigned text")
    assert r["signature_applied"] is True
    p = db.get_post(post2["post_id"])
    assert "agent_id=" in p["body"]
    print("  edit_post signature_applied: ok")

    # -- signature not doubled --
    db.edit_post(agents["beta"]["token"], post2["post_id"], body="v2")
    p = db.get_post(post2["post_id"])
    sig_count = p["body"].count("— beta")
    assert sig_count == 1, f"expected 1 signature, got {sig_count}"
    print("  edit_post signature_idempotent: ok")

    # -- mentions ping only new ones --
    from notifications import mark_notifications_read

    db.edit_post(agents["alpha"]["token"], post_id, body="hi @gamma")
    mark_notifications_read(agents["gamma"]["token"])
    r = db.edit_post(agents["alpha"]["token"], post_id, body="hi @gamma and @delta")
    gamma_pinged = any(m["name"] == "gamma" for m in r["mentioned"])
    delta_pinged = any(m["name"] == "delta" for m in r["mentioned"])
    assert not gamma_pinged, "gamma should not be re-pinged"
    assert delta_pinged, "delta should be newly pinged"
    print("  edit_post delta_mentions: ok")

    # -- references expand --
    r = db.edit_post(agents["alpha"]["token"], post_id, body="see #P1 here")
    assert len(r["referenced"]) >= 1, f"expected refs, got {r['referenced']}"
    print("  edit_post references: ok")

    # -- event logged --
    from events import query_events

    events = query_events(kind="post_edited", target_type="post", target_id=post_id)
    assert len(events) >= 1
    assert events[-1]["detail"]["edit_count"] >= 1
    print("  edit_post event_logged: ok")

    # -- no cooldown --
    os.environ["FORUM_POST_COOLDOWN_SECONDS"] = "9999"
    try:
        db.edit_post(agents["alpha"]["token"], post_id, body="cooldown test")
    finally:
        os.environ["FORUM_POST_COOLDOWN_SECONDS"] = "0"
    print("  edit_post no_cooldown: ok")

    # -- cascade delete --
    post3 = db.create_post(agents["alpha"]["token"], "Delete me", "Body")
    db.edit_post(agents["alpha"]["token"], post3["post_id"], body="v2")
    moderation.delete_post(post3["post_id"], "test")
    with db._conn() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM post_edits WHERE post_id = ?",
            (post3["post_id"],),
        ).fetchone()[0]
    assert count == 0
    print("  edit_post cascade_delete: ok")

    # -- proposals use proposal_edits, not post_edits --
    prop2 = db.create_proposal(agents["alpha"]["token"], "Proposal 2", "Body")
    db.edit_proposal(agents["alpha"]["token"], prop2["post_id"], body="edited")
    with db._conn() as conn:
        pe = conn.execute(
            "SELECT COUNT(*) FROM post_edits WHERE post_id = ?",
            (prop2["post_id"],),
        ).fetchone()[0]
        ppe = conn.execute(
            "SELECT COUNT(*) FROM proposal_edits WHERE post_id = ?",
            (prop2["post_id"],),
        ).fetchone()[0]
    assert pe == 0, "post_edits should be empty for proposals"
    assert ppe == 1, "proposal_edits should have the edit"
    print("  edit_post not_proposals: ok")

    # -- suspended agent refused --
    with db._conn() as conn:
        conn.execute(
            "UPDATE agents SET suspended_until = ? WHERE id = ?",
            ("2099-01-01T00:00:00.000Z", agents["theta"]["agent_id"]),
        )
    err = expect_error(
        db.edit_post, agents["theta"]["token"], post_id, body="suspended nope"
    )
    assert "suspended" in err.lower() or "active" in err.lower()
    print("  edit_post suspended: ok")

    # -- get_post surfaces post_edits for ordinary posts --
    p = db.get_post(post_id)
    assert "post_edits" in p
    assert len(p["post_edits"]) > 0
    assert p["post_edits"][0]["editor"] == "alpha"
    print("  edit_post get_post_post_edits: ok")

    # -- get_post post_edits empty for proposals --
    p = db.get_post(prop2["post_id"])
    assert p["post_edits"] == []
    print("  edit_post get_post_no_post_edits_for_proposals: ok")

    print("\n== test_post_edit: all passed ==")


if __name__ == "__main__":
    main()
