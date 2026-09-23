"""Test the designs sole-admin engine (proposal #652, PR2e): name-authority
owner ops plus parity with the token path (mirrored fixtures, identical
end states - the duplication guard)."""

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


def _event_kinds(did):
    with db._conn() as conn:
        return sorted(
            r["kind"]
            for r in conn.execute(
                "SELECT kind FROM events WHERE target_type = 'design'"
                " AND target_id = ?",
                (did,),
            ).fetchall()
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
    print("  gating: ok")

    # --- decide feature approve + reject ------------------------------------
    f1 = designs.propose_feature(beta["token"], did, "Build a green engine")
    r = admin.admin_decide_feature("alpha", did, f1["feature_id"], True)
    assert r["approved"] is True, r
    f2 = designs.propose_feature(beta["token"], did, "Paint it plaid")
    r = admin.admin_decide_feature("alpha", did, f2["feature_id"], False, note="No")
    assert r["approved"] is False, r
    expect_error(admin.admin_decide_feature, "alpha", did, f2["feature_id"], True)
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
    q = discuss.ask_question(beta["token"], did, "What fuel?")
    a = admin.admin_answer_question("alpha", did, q["question_id"], "Sunlight.")
    assert a["state"] == "answered", a
    _age_design(did)
    assert admin.admin_enable_comments("alpha", did)["comments_enabled"] is True
    assert (
        admin.admin_enable_comments("alpha", did, enabled=False)["comments_enabled"]
        is False
    )
    print("  move/answer/toggle: ok")

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
    for target, decider in (
        (da["id"], lambda d, f: flow.decide_feature(alpha["token"], d, f, True)),
        (db_["id"], lambda d, f: admin.admin_decide_feature("alpha", d, f, True)),
    ):
        f = designs.propose_feature(beta["token"], target, "Same engine text")
        decider(target, f["feature_id"])
        qq = discuss.ask_question(beta["token"], target, "Same question")
        if target == da["id"]:
            discuss.answer_question(alpha["token"], target, qq["question_id"], "Sun.")
        else:
            admin.admin_answer_question("alpha", target, qq["question_id"], "Sun.")

    def _shape(did_):
        with db._conn() as conn:
            feats = conn.execute(
                "SELECT state, text, position FROM design_features"
                " WHERE design_id = ? ORDER BY id",
                (did_,),
            ).fetchall()
            quests = conn.execute(
                "SELECT state, answer FROM design_questions WHERE design_id = ?"
                " ORDER BY id",
                (did_,),
            ).fetchall()
        return (
            [tuple(r) for r in feats],
            [tuple(r) for r in quests],
            _event_kinds(did_),
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
