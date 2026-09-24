"""Test the designs pre-idea brainstorm (proposal #652): migration, gating,
blind matrix, typo, similarity, decide/withdraw/update, caps, frozen state,
positions, events and notifications."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_designs_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db._designs as designs  # noqa: E402
import db._designs_flow as flow  # noqa: E402
from tests._setup import db, expect_error, setup  # noqa: E402


def main():
    agents, _post_id = setup()
    os.environ["ADMIN_USER"] = "alpha"
    os.environ["FORUM_DESIGN_CONTRIB_MIN_KARMA"] = "1"
    alpha = agents["alpha"]
    beta = agents["beta"]
    gamma = agents["gamma"]
    fresh = agents["fresh"]

    # --- migration -------------------------------------------------------
    with db._conn() as conn:
        for t in (
            "designs",
            "design_features",
            "design_issues",
            "design_questions",
            "design_comments",
            "design_links",
            "design_edit_log",
            "design_meta_edits",
        ):
            conn.execute(f"DROP TABLE IF EXISTS {t}")
    db.init_db()
    with db._conn() as conn:
        tables = {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        indexes = {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
    for t in (
        "designs",
        "design_features",
        "design_issues",
        "design_questions",
        "design_comments",
        "design_links",
        "design_edit_log",
        "design_meta_edits",
    ):
        assert t in tables, f"{t} missing after migration"
    for ix in (
        "idx_designs_status",
        "idx_designs_owner",
        "idx_design_features_design",
        "idx_design_features_author",
        "idx_design_issues_design",
        "idx_design_issues_feature",
        "idx_design_issues_author",
        "idx_design_questions_design",
        "idx_design_questions_asker",
        "idx_design_comments_design",
        "idx_design_edit_log_design",
        "idx_design_meta_edits_design",
    ):
        assert ix in indexes, f"{ix} missing after migration"
    print("  migration: ok")

    # --- create gating ----------------------------------------------------
    expect_error(designs.create_design, beta["token"], "Nope")
    d = designs.create_design(
        alpha["token"], "First design", "Desc", ["new_features"], "Req please"
    )
    assert d["status"] == "open", d
    expect_error(designs.create_design, alpha["token"], "First design")
    expect_error(designs.create_design, alpha["token"], "Bad tags", request_tags=["x"])
    expect_error(designs.create_design, alpha["token"], "Second design")
    print("  create gating: ok")

    # --- karma floor --------------------------------------------------------
    os.environ["FORUM_DESIGN_CONTRIB_MIN_KARMA"] = "3"
    expect_error(designs.propose_feature, fresh["token"], d["id"], "A fresh idea here")
    expect_error(designs.propose_feature, beta["token"], d["id"], "A beta idea here")
    os.environ["FORUM_DESIGN_CONTRIB_MIN_KARMA"] = "1"
    print("  karma floor: ok")

    # --- propose / blind / decide -------------------------------------------
    p1 = designs.propose_feature(beta["token"], d["id"], "Build a red widget backend")
    assert p1["state"] == "pending", p1
    got_gamma = designs.get_design(d["id"], gamma["token"])
    assert got_gamma["features"] == [], got_gamma["features"]
    got_owner = designs.get_design(d["id"], alpha["token"])
    assert len(got_owner["features"]) == 1, got_owner["features"]
    assert got_owner["is_owner"] is True
    got_pub = designs.get_design(d["id"])
    assert got_pub["features"] == []
    assert got_pub["is_owner"] is False
    r = flow.decide_feature(alpha["token"], d["id"], p1["feature_id"], True)
    assert r["approved"] is True
    got_gamma = designs.get_design(d["id"], gamma["token"])
    assert len(got_gamma["features"]) == 1, got_gamma["features"]
    assert got_gamma["features"][0]["author_name"] == "beta"
    with db._conn() as conn:
        kinds = {r["kind"] for r in conn.execute("SELECT kind FROM events").fetchall()}
        bodies = [
            r["body"]
            for r in conn.execute(
                "SELECT body FROM notifications WHERE agent_id = ?",
                (beta["agent_id"],),
            ).fetchall()
        ]
    assert "design_created" in kinds, kinds
    assert "design_decided" in kinds, kinds
    assert bodies, "decide must notify the author"
    print("  propose/blind/decide/notify: ok")

    # --- typo fast-path ------------------------------------------------------
    accepted_text = got_gamma["features"][0]["text"]
    t1 = designs.propose_feature(
        beta["token"],
        d["id"],
        accepted_text.upper(),
        op="edit",
        feature_id=p1["feature_id"],
    )
    assert t1["state"] == "accepted" and t1.get("auto") is True, t1
    with db._conn() as conn:
        owner_mails = [
            r["body"]
            for r in conn.execute(
                "SELECT body FROM notifications WHERE agent_id = ?",
                (alpha["agent_id"],),
            ).fetchall()
        ]
    assert any("typo fix" in b for b in owner_mails), owner_mails
    print("  typo fast-path: ok")

    # --- similarity warn + reason ----------------------------------------------
    expect_error(designs.propose_feature, gamma["token"], d["id"], accepted_text)
    p2 = designs.propose_feature(
        gamma["token"],
        d["id"],
        accepted_text,
        reason="gamma needs a blue variant for cold climates, distinct scope",
    )
    assert p2["state"] == "pending" and p2["warning"] is not None, p2
    print("  similarity warn: ok")

    # --- remove-own / withdraw / update-pending ----------------------------------
    expect_error(
        designs.propose_feature,
        gamma["token"],
        d["id"],
        "x",
        op="remove",
        feature_id=p1["feature_id"],
    )
    w = flow.withdraw_feature(gamma["token"], d["id"], p2["feature_id"])
    assert w["withdrawn"] is True
    p3 = designs.propose_feature(beta["token"], d["id"], "A teal gadget for editors")
    u = flow.update_pending_feature(
        beta["token"], d["id"], p3["feature_id"], text="A teal gadget for reviewers"
    )
    assert u["state"] == "pending", u
    expect_error(
        flow.update_pending_feature,
        gamma["token"],
        d["id"],
        p3["feature_id"],
        text="hijack",
    )
    print("  remove/withdraw/update-pending: ok")

    own_edit = designs.propose_feature(
        beta["token"],
        d["id"],
        "A deliberately distinct revision for the studio tool",
        op="edit",
        feature_id=p1["feature_id"],
    )
    own_remove = designs.propose_feature(
        beta["token"],
        d["id"],
        "x",
        op="remove",
        feature_id=p3["feature_id"],
    )
    got_beta = designs.get_design(d["id"], beta["token"])
    assert any(
        f["id"] == own_edit["feature_id"] and f["state"] == "pending"
        for f in got_beta["features"]
    ), got_beta["features"]
    assert any(
        f["id"] == own_remove["feature_id"] and f["state"] == "pending"
        for f in got_beta["features"]
    ), got_beta["features"]
    flow.withdraw_feature(beta["token"], d["id"], own_edit["feature_id"])
    flow.withdraw_feature(beta["token"], d["id"], own_remove["feature_id"])
    print("  own pending edit/remove visibility: ok")

    # --- positions ---------------------------------------------------------------
    flow.decide_feature(alpha["token"], d["id"], p3["feature_id"], True)
    got = designs.get_design(d["id"], alpha["token"])
    acc = [f for f in got["features"] if f["state"] == "accepted"]
    poss = [f["position"] for f in acc]
    assert poss == sorted(poss) and len(set(poss)) == len(poss) >= 2, poss
    print("  positions: ok")

    # --- caps ----------------------------------------------------------------------
    os.environ["FORUM_DESIGN_MAX_FEATURES"] = "2"
    try:
        expect_error(designs.propose_feature, beta["token"], d["id"], "over the cap")
    finally:
        os.environ.pop("FORUM_DESIGN_MAX_FEATURES", None)
    print("  caps: ok")

    # --- frozen ----------------------------------------------------------------------
    with db._conn() as conn:
        conn.execute("UPDATE designs SET status = 'archived' WHERE id = ?", (d["id"],))
    expect_error(designs.propose_feature, beta["token"], d["id"], "late idea")
    expect_error(designs.edit_design_meta, alpha["token"], d["id"], title="New title")
    with db._conn() as conn:
        conn.execute("UPDATE designs SET status = 'open' WHERE id = ?", (d["id"],))
    print("  frozen: ok")

    # --- reject path: note, event, decline-notify ----------------------------------
    p4 = designs.propose_feature(beta["token"], d["id"], "A crimson widget for labs")
    rj = flow.decide_feature(
        alpha["token"], d["id"], p4["feature_id"], False, note="not now"
    )
    assert rj["approved"] is False
    got_beta = designs.get_design(d["id"], beta["token"])
    rej = [f for f in got_beta["features"] if f["id"] == p4["feature_id"]]
    assert len(rej) == 1 and rej[0]["state"] == "rejected", got_beta["features"]
    with db._conn() as conn:
        bodies = [
            r["body"]
            for r in conn.execute(
                "SELECT body FROM notifications WHERE agent_id = ?",
                (beta["agent_id"],),
            ).fetchall()
        ]
        details = [
            r["detail"]
            for r in conn.execute(
                "SELECT detail FROM events WHERE kind = 'design_decided'"
            ).fetchall()
        ]
    assert any("declined" in b for b in bodies), bodies
    assert any("not now" in (x or "") for x in details), details
    print("  reject path: ok")

    # --- approved edit/remove stay out of citizen readers ----------------------------
    pe = designs.propose_feature(
        beta["token"],
        d["id"],
        "A magenta gadget for studios",
        op="edit",
        feature_id=p1["feature_id"],
    )
    assert pe["state"] == "pending", pe
    flow.decide_feature(alpha["token"], d["id"], pe["feature_id"], True)
    got_beta = designs.get_design(d["id"], beta["token"])
    assert all(f["id"] != pe["feature_id"] for f in got_beta["features"]), got_beta["features"]
    got_gamma = designs.get_design(d["id"], gamma["token"])
    texts = [f["text"] for f in got_gamma["features"]]
    assert texts.count("A magenta gadget for studios") == 1, texts
    pr = designs.propose_feature(
        beta["token"], d["id"], "x", op="remove", feature_id=p3["feature_id"]
    )
    flow.decide_feature(alpha["token"], d["id"], pr["feature_id"], True)
    got_beta = designs.get_design(d["id"], beta["token"])
    assert all(f["id"] != pr["feature_id"] for f in got_beta["features"]), got_beta["features"]
    got_gamma = designs.get_design(d["id"], gamma["token"])
    assert [f["text"] for f in got_gamma["features"]] == [
        "A magenta gadget for studios"
    ], got_gamma["features"]
    dock = designs.list_designs("open")
    row = [x for x in dock["designs"] if x["id"] == d["id"]][0]
    assert row["accepted"] == 1 and row["total"] == 3, row
    assert dock["total"] >= 1
    print("  ghost filter + counts: ok")

    # --- decide on a remove whose target vanished -> ForumError, not TypeError --------
    q1 = designs.propose_feature(beta["token"], d["id"], "A amber widget for sheds")
    q2 = designs.propose_feature(
        beta["token"],
        d["id"],
        "A amber widget for barns",
        op="edit",
        feature_id=q1["feature_id"],
    )
    flow.withdraw_feature(beta["token"], d["id"], q1["feature_id"])
    expect_error(flow.decide_feature, alpha["token"], d["id"], q2["feature_id"], True)
    print("  dangling target: ok")

    # --- edit_design_meta + per-field trail --------------------------------------------
    m1 = designs.edit_design_meta(
        alpha["token"], d["id"], title="Second title", description="Second desc"
    )
    assert m1["updated"] == ["description", "title", "updated_at"], m1
    with db._conn() as conn:
        trail = conn.execute(
            "SELECT old_title, new_title FROM design_meta_edits WHERE design_id = ?",
            (d["id"],),
        ).fetchall()
    assert trail[-1]["new_title"] == "Second title", [dict(r) for r in trail]
    before = len(trail)
    designs.edit_design_meta(
        alpha["token"], d["id"], request_text="Need wobble", request_tags=["new_ideas"]
    )
    with db._conn() as conn:
        trail = conn.execute(
            "SELECT old_request, new_request FROM design_meta_edits"
            " WHERE design_id = ?",
            (d["id"],),
        ).fetchall()
    assert len(trail) == before + 2, [dict(r) for r in trail]
    news = [r["new_request"] for r in trail]
    assert "Need wobble" in news and '["new_ideas"]' in news, news
    print("  meta edit + trail: ok")

    print("test_designs: all assertions passed")
    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
