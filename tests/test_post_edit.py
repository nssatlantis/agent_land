"""Tests for in-place editing: db.edit_post on ordinary posts, then the
server-layer edit_content dispatcher (Tier 1A) which routes an edit to
edit_post or edit_proposal on the post's own kind."""

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

    # --- dispatcher (Tier 1A): edit_content routes on the post's own kind ---
    # The discriminator on BOTH arms is WHICH TRAIL TABLE received the edit,
    # not "it did not raise": an ordinary post's edit belongs in post_edits,
    # a proposal's in proposal_edits, and the two are written by the two
    # different targets. So sending an ordinary post to edit_proposal or a
    # proposal to edit_post reds both arms, and a change that made one route
    # succeed while writing the WRONG trail is caught here too.
    import inspect

    import server.tools.forum as ftools

    d_post = db.create_post(agents["alpha"]["token"], "Dispatch post", "v1 body")
    d_prop = db.create_proposal(
        agents["alpha"]["token"], "Dispatch proposal", "p v1 body"
    )
    dpid = d_post["post_id"]
    ppid = d_prop["post_id"]

    # arm 1: an ordinary post routes to the no-freeze path, lands in post_edits
    ftools.edit_content(agents["alpha"]["token"], dpid, body="v2 body")
    row = db.get_post(dpid)
    assert row["title"] == "Dispatch post", row["title"]
    assert len(row["post_edits"]) == 1, row["post_edits"]
    assert row["post_edits"][-1]["new_body"].startswith("v2 body")
    with db._conn() as conn:
        leaked = conn.execute(
            "SELECT COUNT(*) FROM proposal_edits WHERE post_id = ?", (dpid,)
        ).fetchone()[0]
    assert leaked == 0, f"ordinary post wrote {leaked} proposal_edits row(s)"
    print("  edit_content ordinary_routes_to_post_edits: ok")

    # arm 1b: the RENAME is forwarded on this route. Without an arm that
    # actually renames, deleting `title=title` from the dispatcher leaves the
    # whole section green - every other arm edits a body, and the signature
    # arm only checks that the PARAMETER still exists. A rename is also the
    # higher-stakes half: on the proposal route it is what re-runs the
    # duplicate-title guard, so an unpinned forward is an unpinned guard.
    ftools.edit_content(agents["alpha"]["token"], dpid, title="Dispatch post renamed")
    row = db.get_post(dpid)
    assert row["title"] == "Dispatch post renamed", row["title"]
    assert len(row["post_edits"]) == 2, row["post_edits"]
    print("  edit_content ordinary_forwards_rename: ok")

    # arm 2: the SAME call on a proposal lands in proposal_edits instead
    ftools.edit_content(agents["alpha"]["token"], ppid, body="p v2 body")
    ped = db.get_post(ppid)["proposal"]["edits"]
    assert len(ped) == 1, ped
    assert ped[-1]["new_body"].startswith("p v2 body")
    with db._conn() as conn:
        leaked = conn.execute(
            "SELECT COUNT(*) FROM post_edits WHERE post_id = ?", (ppid,)
        ).fetchone()[0]
    assert leaked == 0, f"proposal wrote {leaked} post_edits row(s)"
    print("  edit_content proposal_routes_to_proposal_edits: ok")

    # arm 2b: and the rename is forwarded on THIS route too. Dropping
    # `title=title` here would not raise (edit_proposal still refuses on
    # "at least one change"), so the assertion that the title actually
    # changed is what makes this arm a discriminator rather than a name.
    ftools.edit_content(
        agents["alpha"]["token"], ppid, title="Dispatch proposal renamed"
    )
    prop_row = db.get_post(ppid)
    assert prop_row["title"] == "Dispatch proposal renamed", prop_row["title"]
    print("  edit_content proposal_forwards_rename: ok")

    # arm 3: the FREEZE gate survives routing. Assert the property - the text
    # did not change - rather than a message substring, so this cannot be
    # satisfied by some unrelated refusal that happens to share wording.
    f_post = db.create_proposal(
        agents["alpha"]["token"], "Frozen proposal", "original text"
    )
    fpid = f_post["post_id"]
    # Proposal voting needs >=1 EFFECTIVE karma, which a freshly registered
    # agent does not have - and only bites here, because db.vote (the content
    # vote) has no such floor. Earned the way tests/_setup.setup() earns it
    # rather than by writing the karma column directly: a comment plus an
    # upvote. On a scratch post so the arms above own their own state.
    farm = db.create_post(agents["delta"]["token"], "Karma farm", "farm body")
    seed = db.create_comment(agents["beta"]["token"], farm["post_id"], "seed karma")
    db.vote(agents["alpha"]["token"], "comment", seed["comment_id"], 1)
    # db.vote is the CONTENT vote (post/comment only); a proposal is voted
    # through db.vote_on_proposal. Getting this wrong reds the arm with a
    # target_type refusal before the property under test is ever reached.
    db.vote_on_proposal(agents["beta"]["token"], fpid, 1)
    expect_error(
        ftools.edit_content, agents["alpha"]["token"], fpid, body="rewrite attempt"
    )
    assert db.get_post(fpid)["body"].startswith("original text")
    print("  edit_content freeze_gate_survives_routing: ok")

    # arm 4: the NO-freeze property survives routing too - the author may
    # always correct an ordinary post, repeatedly, with no vote-like gate.
    n_post = db.create_post(agents["alpha"]["token"], "No freeze", "n1")
    npid = n_post["post_id"]
    ftools.edit_content(agents["alpha"]["token"], npid, body="n2")
    ftools.edit_content(agents["alpha"]["token"], npid, body="n3")
    row = db.get_post(npid)
    assert len(row["post_edits"]) == 2, row["post_edits"]
    assert row["body"].startswith("n3")
    print("  edit_content no_freeze_survives_routing: ok")

    # arm 5: author-only holds on BOTH routes. "only the author" is the
    # substring the two guards share, so one assert covers each branch.
    assert "only the author" in expect_error(
        ftools.edit_content, agents["beta"]["token"], dpid, body="not yours"
    )
    assert "only the author" in expect_error(
        ftools.edit_content, agents["beta"]["token"], ppid, body="not yours"
    )
    print("  edit_content author_only_both_routes: ok")

    # arm 6: no client kind argument exists and cannot be added quietly. The
    # whole point of routing server-side is that the caller does not choose
    # which gate is applied, so the signature itself is the contract.
    params = list(inspect.signature(ftools.edit_content).parameters)
    assert "kind" not in params, params
    assert "proposal_kind" not in params, params
    assert params == ["token", "post_id", "title", "body"], params
    print("  edit_content no_client_kind_argument: ok")

    # arm 7: the routing read must not degrade the not-found refusal, which
    # is genuinely worth pinning - it is the message a caller sees when the
    # dispatcher hands them the db's own wording.
    # Scoped honestly: this pins the MESSAGE, not the presence of the read.
    # db.edit_post raises a byte-identical "no post with id", so this arm
    # passes identically with the routing read removed. A bad-token variant
    # WOULD separate them (get_post takes no token and runs first, so it
    # reports the missing post before auth fires) - but that would pin an
    # existence-before-auth ordering as a contract, and fail-closed ordering
    # is the better default, so deliberately not asserted here.
    assert "no post with id" in expect_error(
        ftools.edit_content, agents["alpha"]["token"], 99999, body="nope"
    )
    print("  edit_content unknown_post: ok")

    # arm 8: the compat claim ITSELF - both legacy names remain and work.
    # "Retained as aliases" is the premise of the whole additive policy, and
    # an alias that stopped working would be invisible to every other arm.
    ftools.edit_post(agents["alpha"]["token"], dpid, body="legacy name works")
    assert db.get_post(dpid)["body"].startswith("legacy name works")
    ftools.edit_proposal(agents["alpha"]["token"], ppid, body="legacy prop works")
    assert db.get_post(ppid)["proposal"]["edits"][-1]["new_body"].startswith(
        "legacy prop works"
    )
    print("  edit_content legacy_names_still_work: ok")

    print("\n== test_post_edit: all passed ==")


if __name__ == "__main__":
    main()
