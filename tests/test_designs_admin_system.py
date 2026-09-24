"""Test panel-operated system-owned designs (proposal #694): no citizen row
required - the panel login is the authentication, creations are
system-owned (owner NULL), triage/meta/close run under panel authority."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_designs_system_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, expect_error, setup  # noqa: E402, I001

import db._designs as designs  # noqa: E402
import db._designs_admin as admin  # noqa: E402
import db._designs_discuss as discuss  # noqa: E402
import db._designs_issues as issues  # noqa: E402


def main():
    agents, _post_id = setup()
    os.environ["FORUM_DESIGN_CONTRIB_MIN_KARMA"] = "1"
    # Deliberately unregistered: no citizen named panel-admin exists.
    os.environ["ADMIN_USER"] = "panel-admin"
    alpha = agents["alpha"]
    beta = agents["beta"]
    gamma = agents["gamma"]

    # --- gating ------------------------------------------------------------
    expect_error(admin.admin_create_design, "beta", "Nope", "Desc")
    expect_error(admin.admin_create_design, "", "Nope", "Desc")
    old_admin = os.environ["ADMIN_USER"]
    os.environ["ADMIN_USER"] = ""
    try:
        expect_error(admin.admin_create_design, "panel-admin", "Nope", "Desc")
    finally:
        os.environ["ADMIN_USER"] = old_admin
    print("  gating: ok")

    # --- system create -------------------------------------------------------
    d = admin.admin_create_design(
        "panel-admin",
        "System Brainstorm",
        description="Panel-made",
        request_tags=["new_ideas"],
        request_text="Wanted: better mornings",
    )
    did = d["id"]
    assert d["owner_admin_id"] is None, d
    assert d["status"] == "open", d
    with db._conn() as conn:
        row = conn.execute(
            "SELECT actor_agent_id, kind, detail FROM events"
            " WHERE target_type = 'design' AND target_id = ?"
            " ORDER BY id DESC LIMIT 1",
            (did,),
        ).fetchone()
    assert row["kind"] == "design_created", dict(row)
    assert row["actor_agent_id"] is None, dict(row)
    expect_error(admin.admin_create_design, "panel-admin", "System Brainstorm", "Dup")
    expect_error(
        admin.admin_create_design,
        "panel-admin",
        "Other",
        "Desc",
        request_tags=["bogus"],
    )
    expect_error(admin.admin_create_design, "panel-admin", "Second Today", "Cap")
    print("  create: ok")

    # --- citizen contribute + panel triage ------------------------------------
    f1 = designs.propose_feature(beta["token"], did, "Panel green engine")
    i1 = issues.propose_issue(beta["token"], did, "Panel overheats uphill")
    q1 = discuss.ask_question(beta["token"], did, "Panel fuel?")
    r = admin.admin_decide_feature("panel-admin", did, f1["feature_id"], True)
    assert r["approved"] is True, r
    expect_error(
        admin.admin_move_design_item,
        "panel-admin",
        did,
        "feature",
        "not-an-id",
        "up",
    )
    db.subscribe_design(gamma["token"], did)
    a = admin.admin_answer_question("panel-admin", did, q1["question_id"], "Starlight.")
    assert a["state"] == "answered", a
    with db._conn() as conn:
        gamma_sub = conn.execute(
            "SELECT kind, ref_type, ref_id FROM notifications WHERE agent_id = ?"
            " AND read_at IS NULL AND kind = 'subscription'",
            (gamma["agent_id"],),
        ).fetchall()
    gamma_sub = [tuple(n) for n in gamma_sub if n[1] == "design" and n[2] == did]
    assert len(gamma_sub) == 1, gamma_sub
    assert (
        admin.admin_decide_issue("panel-admin", did, i1["issue_id"], True)["approved"]
        is True
    )
    print("  triage: ok")

    # --- blind spot-check: anon sees accepted, beta sees own ------------------
    anon = designs.get_design(did)
    assert all(f["state"] == "accepted" for f in anon["features"]), anon["features"]
    assert any(f["text"] == "Panel green engine" for f in anon["features"])
    f2 = designs.propose_feature(beta["token"], did, "Panel pending sails")
    mine = designs.get_design(did, viewer_token=beta["token"])
    assert any(f["text"] == "Panel pending sails" for f in mine["features"])
    print("  blind: ok")

    # --- meta edit + trail ------------------------------------------------------
    m = admin.admin_edit_design_meta(
        "panel-admin",
        did,
        title="System Brainstorm v2",
        request_tags=["new_ideas", "improvements"],
    )
    assert m["updated"] == ["request_tags", "title", "updated_at"], m
    with db._conn() as conn:
        trail = conn.execute(
            "SELECT editor_id, old_title, new_title FROM design_meta_edits"
            " WHERE design_id = ? ORDER BY id",
            (did,),
        ).fetchall()
    assert len(trail) == 1, [dict(r) for r in trail]
    assert trail[0]["editor_id"] is None, dict(trail[0])
    assert trail[0]["new_title"] == "System Brainstorm v2", dict(trail[0])
    u = admin.admin_edit_design_meta(
        "panel-admin",
        did,
        title="System Brainstorm v2",
        request_tags=["new_ideas", "improvements"],
    )
    assert u.get("unchanged") is True, u
    print("  meta: ok")

    # --- close 2-step + frozen ----------------------------------------------------
    first = admin.admin_close_design("panel-admin", did)
    assert first.get("need_confirm") is True, first
    assert first["pending_features"] == 1, first
    done = admin.admin_close_design("panel-admin", did, confirm=True)
    assert done["status"] == "archived", done
    expect_error(admin.admin_decide_feature, "panel-admin", did, f2["feature_id"], True)
    closed = designs.get_design(did)
    assert closed["status"] == "archived", closed["status"]
    print("  close: ok")

    with db._conn() as conn:
        conn.execute(
            "UPDATE designs SET created_at = '2000-01-01T00:00:00.000Z' WHERE id = ?",
            (did,),
        )
        cur = conn.execute(
            "INSERT INTO designs (title, description, request_tags, request_text,"
            " status, owner_admin_id, created_at, updated_at)"
            " VALUES ('Citizen owned', '', '[]', '', 'open', ?, ?, ?)",
            (
                gamma["agent_id"],
                designs._now_iso(),
                designs._now_iso(),
            ),
        )
        citizen_did = int(cur.lastrowid or 0)
    prior_admin = os.environ["ADMIN_USER"]
    os.environ["ADMIN_USER"] = alpha["name"]
    try:
        registered = admin.admin_create_design(
            alpha["name"], "Registered admin system", "Audit id"
        )
        registered_did = int(registered["id"])
        with db._conn() as conn:
            create_event = conn.execute(
                "SELECT actor_agent_id FROM events WHERE target_type = 'design'"
                " AND target_id = ? AND kind = 'design_created' ORDER BY id DESC LIMIT 1",
                (registered_did,),
            ).fetchone()
        assert create_event["actor_agent_id"] == alpha["agent_id"], dict(create_event)
        admin.admin_edit_design_meta(
            alpha["name"], registered_did, title="Registered admin system v2"
        )
        with db._conn() as conn:
            meta_trail = conn.execute(
                "SELECT editor_id FROM design_meta_edits WHERE design_id = ?"
                " ORDER BY id DESC LIMIT 1",
                (registered_did,),
            ).fetchone()
        assert meta_trail["editor_id"] == alpha["agent_id"], dict(meta_trail)
        admin.admin_close_design(alpha["name"], registered_did)
        with db._conn() as conn:
            archive_event = conn.execute(
                "SELECT actor_agent_id FROM events WHERE target_type = 'design'"
                " AND target_id = ? AND kind = 'design_archived' ORDER BY id DESC LIMIT 1",
                (registered_did,),
            ).fetchone()
        assert archive_event["actor_agent_id"] == alpha["agent_id"], dict(archive_event)
    finally:
        os.environ["ADMIN_USER"] = prior_admin
    expect_error(admin.admin_design_pending, "panel-admin", citizen_did)
    expect_error(
        admin.admin_edit_design_meta,
        "panel-admin",
        citizen_did,
        title="Panel must not cross ownership",
    )
    expect_error(admin.admin_close_design, "panel-admin", citizen_did)
    print("  ownership + audit: ok")

    print("test_designs_admin_system: all assertions passed")
    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
