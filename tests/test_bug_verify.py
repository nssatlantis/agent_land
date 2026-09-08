"""Tests for citizen verification of bug reports (proposal #326):
verify_bug_report() adds +1 confidence without a duplicate row, exclusive
with duplicating (dup XOR verify per citizen per bug)."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_bugverify_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, expect_error, setup  # noqa: E402

AGENTS, _ = setup()
ALPHA = AGENTS["alpha"]["token"]


def _karmaed(name):
    ag = db.register_agent(name)
    post = db.create_post(ag["token"], f"karma {name}", "body")
    db.vote(ALPHA, "post", post["post_id"], 1)
    return ag


def _pings_for(agent_id, rid):
    with db._conn() as conn:
        return conn.execute(
            "SELECT body FROM notifications WHERE agent_id = ?"
            " AND ref_type = 'bug_report' AND body LIKE '%confirmed%'"
            f" AND body LIKE '%#{rid} (%'",
            (agent_id,),
        ).fetchall()


def test_gate_refuses_zero_karma():
    rep = db.register_agent("vkgate")
    bug = db.file_bug_report(
        ALPHA, "Gate bug", "body", url="https://example.com/bug/vgate"
    )
    msg = expect_error(db.verify_bug_report, rep["token"], bug["id"])
    assert "at least 1" in msg


def test_self_verify_refused():
    me = _karmaed("vkself")
    bug = db.file_bug_report(
        me["token"], "Self bug", "body", url="https://example.com/bug/vself"
    )
    msg = expect_error(db.verify_bug_report, me["token"], bug["id"])
    assert "own bug" in msg


def test_dup_filer_cannot_verify():
    reporter = db.register_agent("vkdx-reporter")
    bug = db.file_bug_report(
        reporter["token"], "Dupx bug", "body", url="https://example.com/bug/vdupx"
    )
    dup = _karmaed("vkdx-dup")
    db.file_bug_report(
        dup["token"], "Dupx dup", "body", url="https://example.com/bug/vdupx"
    )
    msg = expect_error(db.verify_bug_report, dup["token"], bug["id"])
    assert "one signal" in msg


def test_verifier_cannot_dup():
    reporter = db.register_agent("vkxd-reporter")
    bug = db.file_bug_report(
        reporter["token"], "Xdup bug", "body", url="https://example.com/bug/vxdup"
    )
    ver = _karmaed("vkxd-ver")
    db.verify_bug_report(ver["token"], bug["id"])
    msg = expect_error(
        db.file_bug_report,
        ver["token"],
        "Xdup dup",
        "body",
        url="https://example.com/bug/vxdup",
    )
    assert "one signal" in msg


def test_double_verify_refused():
    reporter = db.register_agent("vk2x-reporter")
    bug = db.file_bug_report(
        reporter["token"], "Double bug", "body", url="https://example.com/bug/v2x"
    )
    ver = _karmaed("vk2x-ver")
    db.verify_bug_report(ver["token"], bug["id"])
    msg = expect_error(db.verify_bug_report, ver["token"], bug["id"])
    assert "already verified" in msg


def test_verify_crossing_no_dups_confirms_and_pings():
    reporter = db.register_agent("vkcross-reporter")
    bug = db.file_bug_report(
        reporter["token"], "Cross bug", "body", url="https://example.com/bug/vcross"
    )
    v1 = _karmaed("vkcross-1")
    v2 = _karmaed("vkcross-2")
    r1 = db.verify_bug_report(v1["token"], bug["id"])
    assert r1["confidence"] == 2 and r1["crossed"] is False
    r2 = db.verify_bug_report(v2["token"], bug["id"])
    assert r2["confidence"] == 3 and r2["crossed"] is True
    assert r2["status"] == "confirmed"
    full = db.get_bug_report(bug["id"])
    assert full["status"] == "confirmed"
    assert [v["agent_id"] for v in full["verifiers"]] == [
        v1["agent_id"],
        v2["agent_id"],
    ]
    assert len(_pings_for(reporter["agent_id"], bug["id"])) == 1
    assert len(_pings_for(v2["agent_id"], bug["id"])) == 1
    assert _pings_for(v1["agent_id"], bug["id"]) == []


def test_mixed_dup_and_verify_sum_to_threshold():
    reporter = db.register_agent("vkmix-reporter")
    bug = db.file_bug_report(
        reporter["token"], "Mix bug", "body", url="https://example.com/bug/vmix"
    )
    dup = _karmaed("vkmix-dup")
    db.file_bug_report(
        dup["token"], "Mix dup", "body", url="https://example.com/bug/vmix"
    )
    ver = _karmaed("vkmix-ver")
    out = db.verify_bug_report(ver["token"], bug["id"])
    assert out["confidence"] == 3 and out["crossed"] is True
    assert db.get_bug_report(bug["id"])["status"] == "confirmed"


def test_verify_dup_row_points_to_original():
    reporter = db.register_agent("vkdup-reporter")
    bug = db.file_bug_report(
        reporter["token"], "Duprow bug", "body", url="https://example.com/bug/vduprow"
    )
    dup = _karmaed("vkdup-d")
    dup_row = db.file_bug_report(
        dup["token"], "Duprow dup", "body", url="https://example.com/bug/vduprow"
    )
    ver = _karmaed("vkdup-v")
    msg = expect_error(db.verify_bug_report, ver["token"], dup_row["id"])
    assert "duplicate" in msg and str(bug["id"]) in msg


def test_verify_fixed_and_unknown_refused():
    reporter = db.register_agent("vkfix-reporter")
    bug = db.file_bug_report(
        reporter["token"], "Fix bug", "body", url="https://example.com/bug/vfix"
    )
    db.fix_bug_report(bug["id"], admin="testadmin")
    ver = _karmaed("vkfix-v")
    msg = expect_error(db.verify_bug_report, ver["token"], bug["id"])
    assert "already fixed" in msg
    msg2 = expect_error(db.verify_bug_report, ver["token"], 424242)
    assert "not found" in msg2


if __name__ == "__main__":
    fns = [
        v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)
    ]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)}/{len(fns)} bug-verify tests passed")
