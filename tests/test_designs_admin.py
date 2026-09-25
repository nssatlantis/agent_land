"""Test the designs sole-admin engine (proposal #652, PR2e): name-authority
owner ops plus parity with the token path (mirrored fixtures, identical
end states - the duplication guard)."""

import json
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_designs_admin_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, expect_error, setup  # noqa: E402, I001

import db._designs as designs  # noqa: E402
import db._designs_admin as admin  # noqa: E402
import db._designs_discuss as discuss  # noqa: E402
import db._designs_flow as flow  # noqa: E402
import db._designs_issues as issues  # noqa: E402


def _age_design(did):
    with db._conn() as conn:
        conn.execute(
            "UPDATE designs SET created_at = '2020-01-01T00:00:00.000Z' WHERE id = ?",
            (did,),
        )


def main():
    agents, _post_id = setup()
    os.environ["ADMIN_USER"] = "alpha"
    os.environ["FORUM_DESIGN_CONTRIB_MIN_KARMA"] = "1"
    alpha = agents["alpha"]
    beta = agents["beta"]

    # --- gating ------------------------------------------------------------
    d = designs.create_design(alpha["token"], "Admin design", "Desc")
    did = d["id"]
    expect_error(admin.admin_decide_feature, "beta", did, 1, True)
    expect_error(admin.admin_decide_feature, "nobody", did, 1, True)
    expect_error(admin.admin_decide_feature, "", did, 1, True)
    f0 = designs.propose_feature(beta["token"], did, "Gating engine block")
    with db._conn() as conn:
        conn.execute(
            "UPDATE agents SET suspended_until = '2999-01-01T00:00:00.000Z'"
            " WHERE name = 'alpha'"
        )
    expect_error(admin.admin_decide_feature, "alpha", did, f0["feature_id"], True)
    with db._conn() as conn:
        conn.execute(
            "UPDATE agents SET suspended_until = NULL, banned = 1 WHERE name = 'alpha'"
        )
    expect_error(admin.admin_decide_feature, "alpha", did, f0["feature_id"], True)
    with db._conn() as conn:
        conn.execute("UPDATE agents SET banned = 0 WHERE name = 'alpha'")
    print("  gating: ok")

    # --- decide feature approve + reject ------------------------------------
    f1 = designs.propose_feature(beta["token"], did, "Build a green engine")
    r = admin.admin_decide_feature("alpha", did, f1["feature_id"], True)
    assert r["approved"] is True, r
    f2 = designs.propose_feature(beta["token"], did, "Paint it plaid")
    r = admin.admin_decide_feature("alpha", did, f2["feature_id"], False, note="No")
    assert r["approved"] is False, r
    expect_error(admin.admin_decide_feature, "alpha", did, f2["feature_id"], True)
    rejected_issue = issues.propose_issue(beta["token"], did, "Issue needing a note")
    assert (
        admin.admin_decide_issue(
            "alpha", did, rejected_issue["issue_id"], False, note="Needs detail"
        )["approved"]
        is False
    )
    print("  decide-feature: ok")

    # --- decide + resolve issue ----------------------------------------------
    i1 = issues.propose_issue(beta["token"], did, "Overheats uphill")
    assert (
        admin.admin_decide_issue("alpha", did, i1["issue_id"], True)["approved"] is True
    )
    assert admin.admin_resolve_issue("alpha", did, i1["issue_id"])["resolved"] is True
    expect_error(admin.admin_resolve_issue, "beta", did, i1["issue_id"])
    print("  issues: ok")

    # --- move + answer + toggle ------------------------------------------------
    f3 = designs.propose_feature(beta["token"], did, "Add sails")
    flow.decide_feature(alpha["token"], did, f3["feature_id"], True)
    assert (
        admin.admin_move_design_item("alpha", did, "feature", f3["feature_id"], "up")[
            "moved"
        ]
        is True
    )
    expect_error(
        admin.admin_move_design_item, "alpha", did, "nope", f3["feature_id"], "up"
    )
    move_history = admin.admin_design_history("alpha", did)
    move_details = [json.loads(d["detail"]) for d in move_history["decisions"]]
    assert any(
        d.get("op") == "move"
        and d.get("kind") == "feature"
        and d.get("item_id") == f3["feature_id"]
        and d.get("direction") == "up"
        for d in move_details
    ), move_details
    q = discuss.ask_question(beta["token"], did, "What fuel?")
    a = admin.admin_answer_question("alpha", did, q["question_id"], "Sunlight.")
    assert a["state"] == "answered", a
    # --- admin-path deltas: int-guard + subscriber-only fan-out ---------------
    expect_error(
        admin.admin_move_design_item,
        "alpha",
        did,
        "feature",
        "not-an-id",
        "up",
    )
    db.subscribe_design(agents["gamma"]["token"], did)
    q2 = discuss.ask_question(beta["token"], did, "Second fuel?")
    a2 = admin.admin_answer_question("alpha", did, q2["question_id"], "Starlight.")
    assert a2["state"] == "answered", a2
    with db._conn() as conn:
        gamma_sub = conn.execute(
            "SELECT kind, ref_type, ref_id FROM notifications WHERE agent_id = ?"
            " AND read_at IS NULL AND kind = 'subscription'",
            (agents["gamma"]["agent_id"],),
        ).fetchall()
    gamma_sub = [tuple(r) for r in gamma_sub if r[1] == "design" and r[2] == did]
    assert len(gamma_sub) == 1, gamma_sub
    print("  admin-deltas: ok")
    _age_design(did)
    assert admin.admin_enable_comments("alpha", did)["comments_enabled"] is True
    assert (
        admin.admin_enable_comments("alpha", did, enabled=False)["comments_enabled"]
        is False
    )
    print("  move/answer/toggle: ok")

    # --- direct admin authoring ----------------------------------------------
    direct_feature = admin.admin_create_feature(
        "alpha", did, "Admin-authored accepted feature"
    )
    assert direct_feature["state"] == "accepted", direct_feature
    admin.admin_edit_feature(
        "alpha", did, direct_feature["feature_id"], "Admin-authored edited feature"
    )
    direct_issue = admin.admin_create_issue(
        "alpha",
        did,
        "Admin-authored accepted issue",
        feature_id=direct_feature["feature_id"],
    )
    assert direct_issue["state"] == "accepted", direct_issue
    expect_error(admin.admin_remove_feature, "alpha", did, direct_feature["feature_id"])
    pending_remove = designs.propose_feature(
        alpha["token"],
        did,
        "remove",
        op="remove",
        feature_id=direct_feature["feature_id"],
    )
    expect_error(
        flow.decide_feature,
        alpha["token"],
        did,
        pending_remove["feature_id"],
        True,
    )
    flow.withdraw_feature(alpha["token"], did, pending_remove["feature_id"])
    vanishing_feature = admin.admin_create_feature(
        "alpha", did, "Feature with stale pending edit"
    )
    stale_edit = designs.propose_feature(
        beta["token"],
        did,
        "Stale edit that must not revive a removed target",
        op="edit",
        feature_id=vanishing_feature["feature_id"],
    )
    admin.admin_remove_feature("alpha", did, vanishing_feature["feature_id"])
    expect_error(
        flow.decide_feature,
        alpha["token"],
        did,
        stale_edit["feature_id"],
        True,
    )
    assert (
        flow.decide_feature(alpha["token"], did, stale_edit["feature_id"], False)[
            "approved"
        ]
        is False
    )
    resolved_parent = admin.admin_create_feature(
        "alpha", did, "Feature with a resolved linked issue"
    )
    resolved_child = admin.admin_create_issue(
        "alpha",
        did,
        "Resolved issue that no longer blocks parent removal",
        feature_id=resolved_parent["feature_id"],
    )
    admin.admin_resolve_issue("alpha", did, resolved_child["issue_id"])
    admin.admin_remove_feature("alpha", did, resolved_parent["feature_id"])
    admin.admin_edit_issue(
        "alpha",
        did,
        resolved_child["issue_id"],
        "Edited after hidden parent removal",
    )
    with db._conn() as conn:
        kept_link = conn.execute(
            "SELECT feature_id FROM design_issues WHERE id = ?",
            (resolved_child["issue_id"],),
        ).fetchone()[0]
    assert kept_link == resolved_parent["feature_id"], kept_link
    admin.admin_edit_issue(
        "alpha",
        did,
        resolved_child["issue_id"],
        "Explicitly unlinked after hidden parent removal",
        feature_id=None,
    )
    with db._conn() as conn:
        cleared_link = conn.execute(
            "SELECT feature_id FROM design_issues WHERE id = ?",
            (resolved_child["issue_id"],),
        ).fetchone()[0]
    assert cleared_link is None, cleared_link
    admin.admin_edit_issue(
        "alpha",
        did,
        direct_issue["issue_id"],
        "Admin-authored edited issue",
        feature_id=f1["feature_id"],
    )
    history = admin.admin_design_history("alpha", did)
    assert any(
        f["id"] == direct_feature["feature_id"] and f["state"] == "accepted"
        for f in history["features"]
    ), history
    assert any(
        i["id"] == direct_issue["issue_id"] and i["state"] == "accepted"
        for i in history["issues"]
    ), history
    issue_edits = [
        e
        for e in history["edit_logs"]
        if e["kind"] == "issue" and e["feature_or_issue_id"] == direct_issue["issue_id"]
    ]
    assert issue_edits[-1]["old_text"] == (
        f"Admin-authored accepted issue [feature_id={direct_feature['feature_id']}]"
    )
    assert issue_edits[-1]["new_text"] == (
        f"Admin-authored edited issue [feature_id={f1['feature_id']}]"
    )
    decision_details = [json.loads(d["detail"]) for d in history["decisions"]]
    assert any(
        d.get("fid") == f2["feature_id"] and d.get("note") == "No"
        for d in decision_details
    ), decision_details
    assert any(
        d.get("issue_id") == rejected_issue["issue_id"]
        and d.get("note") == "Needs detail"
        for d in decision_details
    ), decision_details
    admin.admin_remove_issue("alpha", did, direct_issue["issue_id"])
    admin.admin_remove_feature("alpha", did, direct_feature["feature_id"])
    history_after = admin.admin_design_history("alpha", did)
    after_details = [json.loads(d["detail"]) for d in history_after["decisions"]]
    assert any(
        d.get("fid") == direct_feature["feature_id"]
        and d.get("direct") is True
        and d.get("op") == "remove"
        for d in after_details
    ), after_details
    os.environ["ADMIN_USER"] = "ghost-panel"
    ghost_design = admin.admin_create_design("ghost-panel", "Unregistered panel design")
    ghost_feature = admin.admin_create_feature(
        "ghost-panel", ghost_design["id"], "Unregistered panel feature"
    )
    admin.admin_remove_feature(
        "ghost-panel", ghost_design["id"], ghost_feature["feature_id"]
    )
    ghost_history = admin.admin_design_history("ghost-panel", ghost_design["id"])
    ghost_details = [json.loads(d["detail"]) for d in ghost_history["decisions"]]
    assert any(
        d.get("op") == "remove" and d.get("direct") is True for d in ghost_details
    ), ghost_details
    assert any(
        d.get("actor_name") == "ghost-panel" for d in ghost_history["decisions"]
    ), ghost_history["decisions"]
    os.environ["ADMIN_USER"] = "alpha"
    print("  direct authoring: ok")

    # --- parity: mirrored fixtures, identical end states -----------------------
    with db._conn() as conn:
        conn.execute("UPDATE designs SET created_at = '2020-01-01T00:00:00.000Z'")
    da = designs.create_design(alpha["token"], "Parity token", "Desc")
    with db._conn() as conn:
        conn.execute(
            "UPDATE designs SET created_at = '2020-01-01T00:00:00.000Z' WHERE id = ?",
            (da["id"],),
        )
    db_ = designs.create_design(alpha["token"], "Parity admin", "Desc")
    with db._conn() as conn:
        conn.execute(
            "UPDATE designs SET created_at = '2020-01-01T00:00:00.000Z' WHERE id = ?",
            (db_["id"],),
        )

    def _drive_token(did_):
        f = designs.propose_feature(beta["token"], did_, "Same engine text")
        flow.decide_feature(alpha["token"], did_, f["feature_id"], True)
        f2 = designs.propose_feature(beta["token"], did_, "Second engine text")
        flow.decide_feature(alpha["token"], did_, f2["feature_id"], True)
        issues.move_design_item(alpha["token"], did_, "feature", f2["feature_id"], "up")
        i = issues.propose_issue(beta["token"], did_, "Same overheat")
        issues.decide_issue(alpha["token"], did_, i["issue_id"], True)
        issues.resolve_issue(alpha["token"], did_, i["issue_id"])
        qq = discuss.ask_question(beta["token"], did_, "Same question")
        discuss.answer_question(alpha["token"], did_, qq["question_id"], "Sun.")
        discuss.enable_comments(alpha["token"], did_)
        discuss.enable_comments(alpha["token"], did_, enabled=False)

    def _drive_admin(did_):
        f = designs.propose_feature(beta["token"], did_, "Same engine text")
        admin.admin_decide_feature("alpha", did_, f["feature_id"], True)
        f2 = designs.propose_feature(beta["token"], did_, "Second engine text")
        admin.admin_decide_feature("alpha", did_, f2["feature_id"], True)
        admin.admin_move_design_item("alpha", did_, "feature", f2["feature_id"], "up")
        i = issues.propose_issue(beta["token"], did_, "Same overheat")
        admin.admin_decide_issue("alpha", did_, i["issue_id"], True)
        admin.admin_resolve_issue("alpha", did_, i["issue_id"])
        qq = discuss.ask_question(beta["token"], did_, "Same question")
        admin.admin_answer_question("alpha", did_, qq["question_id"], "Sun.")
        admin.admin_enable_comments("alpha", did_)
        admin.admin_enable_comments("alpha", did_, enabled=False)

    _drive_token(da["id"])
    _drive_admin(db_["id"])

    _DETAIL_DROP = ("title", "fid")

    def _norm_detail(raw):
        import json

        try:
            detail = json.loads(raw or "{}")
        except ValueError:
            return ("<unparseable>",)
        return tuple(
            sorted(
                (k, v)
                for k, v in detail.items()
                if not k.endswith("_id") and k not in _DETAIL_DROP
            )
        )

    def _shape(did_):
        with db._conn() as conn:
            feats = conn.execute(
                "SELECT state, text, position, decided_by,"
                " decided_at IS NOT NULL AS decided"
                " FROM design_features WHERE design_id = ? ORDER BY id",
                (did_,),
            ).fetchall()
            iss = conn.execute(
                "SELECT state, text, position, decided_by,"
                " decided_at IS NOT NULL AS decided,"
                " resolved_by, resolved_at IS NOT NULL AS resolved"
                " FROM design_issues WHERE design_id = ? ORDER BY id",
                (did_,),
            ).fetchall()
            quests = conn.execute(
                "SELECT state, answer FROM design_questions WHERE design_id = ?"
                " ORDER BY id",
                (did_,),
            ).fetchall()
            flag = conn.execute(
                "SELECT comments_enabled FROM designs WHERE id = ?", (did_,)
            ).fetchone()[0]
            evts = conn.execute(
                "SELECT kind, detail FROM events WHERE target_type = 'design'"
                " AND target_id = ? ORDER BY id",
                (did_,),
            ).fetchall()
            mails = conn.execute(
                "SELECT kind, COUNT(*) AS n FROM notifications"
                " WHERE ref_type = 'design' AND ref_id = ? GROUP BY kind",
                (did_,),
            ).fetchall()
        return (
            [tuple(r) for r in feats],
            [tuple(r) for r in iss],
            [tuple(r) for r in quests],
            int(flag or 0),
            sorted(r["kind"] for r in evts),
            sorted((r["kind"], _norm_detail(r["detail"])) for r in evts),
            sorted((r["kind"], r["n"]) for r in mails),
        )

    assert _shape(da["id"]) == _shape(db_["id"]), (
        _shape(da["id"]),
        _shape(db_["id"]),
    )
    print("  parity: ok")

    print("test_designs_admin: all assertions passed")
    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
