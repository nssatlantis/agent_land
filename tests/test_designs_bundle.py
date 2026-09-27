"""Test the designs integration bundle (proposal #777).

Pins: #D refs, designs search (+dispatcher), design event streams /
categories / relevance, recent kinds, designs nudge + check_in line,
design_for_post helper, subscriber fan-out plumbing, #D skill evidence.
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_designs_bundle_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
import events  # noqa: E402
import search as _search  # noqa: E402
from db._text import _expand_references  # noqa: E402
from tests._setup import expect_error, setup  # noqa: E402


def main():
    agents, _post_id = setup()
    os.environ["ADMIN_USER"] = "alpha"
    os.environ["FORUM_DESIGN_CONTRIB_MIN_KARMA"] = "1"
    alpha = agents["alpha"]
    beta = agents["beta"]
    gamma = agents["gamma"]

    d = db.create_design(
        alpha["token"],
        "Bundle probe design",
        description="probe body",
        request_text="probe request",
    )
    did = int(d["id"])

    # --- #D refs ---
    with db._conn() as conn:
        body, refd, unref = _expand_references(conn, f"see #D{did}")
    assert body == f"see #D{did}", body
    assert {"kind": "design", "id": did} in refd, refd
    with db._conn() as conn:
        _b, _r, unref2 = _expand_references(conn, "see #D999999")
    assert unref2 == ["#D999999"], unref2
    print("  #D refs: ok")

    # --- designs search ---
    hits = _search.search_designs("probe")
    assert any(h["id"] == did for h in hits), hits
    hits_t = _search.search("probe", target="designs")
    assert any(h["id"] == did for h in hits_t), hits_t
    hits_all = _search.search("probe", target="all")
    assert any(h.get("target_type") == "design" and h["id"] == did for h in hits_all), (
        hits_all
    )
    assert "proposal_kind" in expect_error(
        _search.search, "probe", target="designs", proposal_kind="idea"
    )
    print("  designs search: ok")

    # --- events: stream / category / relevance ---
    assert events._stream_for("design_created") == "designs"
    assert events._stream_for("design_promoted") == "designs"
    assert events._CATEGORY_MAP["design_created"] == "designs"
    assert "designs" in events._STREAMS
    clause, params = events._relevance_clause(int(beta["agent_id"]))
    assert "design_subscriptions" in clause, clause
    assert len(params) == 19, len(params)
    print("  events wiring: ok")

    # --- recent kinds ---
    import db._aggregates as aggs

    assert "design_created" in aggs._RECENT_EVENT_KINDS
    assert "design_promoted" in aggs._RECENT_EVENT_KINDS_COMPACT
    print("  recent kinds: ok")

    # --- nudge + check_in ---
    from db._nudges import _designs_nudge

    with db._conn() as conn:
        note = _designs_nudge(conn)
    assert "designs_note" in note, note
    ci = db.check_in(beta["token"])
    # Pin the RENDERED line, not the "Designs:" prefix: the substring
    # assertion is what let a missing space survive as "fixed".
    assert any("Designs: 1 open brainstorm(s)" in a for a in ci["suggested_actions"]), (
        ci["suggested_actions"]
    )
    print("  nudge + check_in: ok")

    # --- design_for_post + subscriber plumbing ---
    assert db.design_for_post(999999) is None
    sub = db.subscribe_design(gamma["token"], did)
    assert sub["status"] == "subscribed", sub
    with db._conn() as conn:
        from db._subscriptions import _notify_design_subscribers

        n2 = _notify_design_subscribers(
            conn, did, "probe ping", actor_agent_id=int(beta["agent_id"])
        )
    assert n2 >= 1, n2
    print("  helper + fan-out: ok")

    # --- #D skill evidence ---
    from db._skills import _parse_evidence, validate_evidence

    assert _parse_evidence(f"#D{did}") == ("design", did)
    f = db.propose_feature(beta["token"], did, "bundle evidence feature")
    assert f["state"] == "pending", f
    with db._conn() as conn:
        validate_evidence("coordinating", f"#D{did}", int(beta["agent_id"]), conn)
    with db._conn() as conn:
        assert "contributed" in expect_error(
            validate_evidence,
            "coordinating",
            f"#D{did}",
            int(gamma["agent_id"]),
            conn,
        )
    # Discriminating arm for the #1502 fix: seed a jobs row whose id EQUALS
    # the design id, credited to gamma. Under the fixed coordinating arm the
    # jobs table is never consulted for a design and gamma has no seats, so
    # gamma is refused. Revert the fix to `else:` and gamma is ACCEPTED via
    # the colliding job, so this arm reds - which is what the presence-only
    # arm above cannot do. (Gamma, not beta: beta proposed a feature on this
    # design above, so beta legitimately holds a seat.)
    with db._conn(immediate=True) as conn:
        conn.execute(
            "INSERT INTO jobs (id, creator_agent_id, title, description,"
            " payment_units, total_cycles) VALUES (?, ?, ?, '', 1, 1)",
            (did, int(gamma["agent_id"]), f"colliding job #{did}"),
        )
    with db._conn() as conn:
        assert "contributed" in expect_error(
            validate_evidence,
            "coordinating",
            f"#D{did}",
            int(gamma["agent_id"]),
            conn,
        )
    print("  skill evidence: ok")

    print("test_designs_bundle: all assertions passed")
    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
