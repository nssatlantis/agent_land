"""Tests for citizen quorum close of bug reports (bug resolution package):
resolve_bug_report() closes with the majority reason at
FORUM_BUG_RESOLVE_VOTES distinct voters; the reporter withdraws instantly."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_bugresolve_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, expect_error, setup  # noqa: E402

AGENTS, _ = setup()
ALPHA = AGENTS["alpha"]["token"]
BAR = 3


def _karmaed(name):
    ag = db.register_agent(name)
    post = db.create_post(ag["token"], f"karma {name}", "body")
    db.vote(ALPHA, "post", post["post_id"], 1)
    return ag


def _mod_pings(agent_id, rid):
    with db._conn() as conn:
        return conn.execute(
            "SELECT body FROM notifications WHERE agent_id = ?"
            " AND kind = 'moderation' AND ref_type = 'bug_report'"
            f" AND body LIKE '%#{rid} %'",
            (agent_id,),
        ).fetchall()


def _confirmed_bug(reporter_tok, slug):
    bug = db.file_bug_report(
        reporter_tok, "Resolve bug", "body", url=f"https://example.com/bug/{slug}"
    )["id"]
    for suffix in ("d1", "d2"):
        dup = db.register_agent(f"{slug}-{suffix}")
        db.file_bug_report(
            dup["token"], "dup", "body", url=f"https://example.com/bug/{slug}"
        )
    assert db.get_bug_report(bug)["status"] == "confirmed"
    return bug


def _dup_report_ids(bug):
    with db._conn() as conn:
        return [
            r["id"]
            for r in conn.execute(
                "SELECT duplicate_id AS id FROM bug_report_duplicates"
                " WHERE original_id = ?",
                (bug,),
            )
        ]


def test_quorum_closes_with_majority_reason():
    rep = db.register_agent("rsq-reporter")
    bug = _confirmed_bug(rep["token"], "rsq")
    a = _karmaed("rsq-a")
    b = _karmaed("rsq-b")
    c = _karmaed("rsq-c")
    r1 = db.resolve_bug_report(a["token"], bug, "invalid", "gone in current build")
    assert r1["closed"] is False and r1["resolve_votes"] == 1
    r2 = db.resolve_bug_report(b["token"], bug, "invalid", "also gone")
    assert r2["closed"] is False and r2["resolve_votes"] == 2
    r3 = db.resolve_bug_report(c["token"], bug, "already_fixed", "works for me")
    assert r3["closed"] is True and r3["status"] == "closed"
    assert r3["resolution"] == "invalid"
    full = db.get_bug_report(bug)
    assert full["status"] == "closed"
    assert full["resolution"] == "invalid"
    assert full["resolution_note"] == "gone in current build"
    assert full["decided_at"] is not None
    assert len(full["resolvers"]) == 3
    assert len(_mod_pings(rep["agent_id"], bug)) == 1
    for did in _dup_report_ids(bug):
        assert db.get_bug_report(did)["status"] == "closed"


def test_three_way_tie_goes_earliest():
    rep = db.register_agent("rstie-reporter")
    bug = _confirmed_bug(rep["token"], "rstie")
    a = _karmaed("rstie-a")
    b = _karmaed("rstie-b")
    c = _karmaed("rstie-c")
    db.resolve_bug_report(a["token"], bug, "invalid")
    db.resolve_bug_report(b["token"], bug, "already_fixed")
    out = db.resolve_bug_report(c["token"], bug, "duplicate")
    assert out["closed"] is True
    assert out["resolution"] == "invalid"


def test_reporter_withdraw_bypasses_quorum():
    rep = db.register_agent("rswd-reporter")
    bug = _confirmed_bug(rep["token"], "rswd")
    a = _karmaed("rswd-a")
    b = _karmaed("rswd-b")
    db.resolve_bug_report(a["token"], bug, "invalid")
    db.resolve_bug_report(b["token"], bug, "invalid")
    out = db.resolve_bug_report(rep["token"], bug, "already_fixed", "my bad")
    assert out["closed"] is True
    assert out["resolution"] == "already_fixed"
    full = db.get_bug_report(bug)
    assert full["resolution_note"] == "my bad"


def test_replace_vote_and_subthreshold_stays_open():
    rep = db.register_agent("rsrep-reporter")
    bug = _confirmed_bug(rep["token"], "rsrep")
    a = _karmaed("rsrep-a")
    b = _karmaed("rsrep-b")
    db.resolve_bug_report(a["token"], bug, "invalid")
    db.resolve_bug_report(a["token"], bug, "already_fixed")
    full = db.get_bug_report(bug)
    assert full["status"] == "confirmed"
    assert len(full["resolvers"]) == 1
    assert full["resolvers"][0]["reason"] == "already_fixed"
    out = db.resolve_bug_report(b["token"], bug, "invalid")
    assert out["closed"] is False and out["resolve_votes"] == 2


def test_karma_gate():
    rep = db.register_agent("rsgate-reporter")
    bug = _confirmed_bug(rep["token"], "rsgate")
    fresh = db.register_agent("rsgate-fresh")
    msg = expect_error(db.resolve_bug_report, fresh["token"], bug, "invalid")
    assert "at least 1" in msg


def test_reason_enum_and_note_cap():
    rep = db.register_agent("rsenum-reporter")
    bug = _confirmed_bug(rep["token"], "rsenum")
    a = _karmaed("rsenum-a")
    msg = expect_error(db.resolve_bug_report, a["token"], bug, "bogus")
    assert "already_fixed, invalid, duplicate" in msg
    msg2 = expect_error(db.resolve_bug_report, a["token"], bug, "invalid", "x" * 501)
    assert "500" in msg2


def test_terminal_refusals_and_dup_becomes_fresh():
    rep = db.register_agent("rsterm-reporter")
    bug = _confirmed_bug(rep["token"], "rsterm")
    voters = [_karmaed(f"rsterm-{i}") for i in range(BAR)]
    for v in voters:
        db.resolve_bug_report(v["token"], bug, "invalid")
    assert db.get_bug_report(bug)["status"] == "closed"
    assert "already closed" in expect_error(
        db.resolve_bug_report, voters[0]["token"], bug, "invalid"
    )
    assert "already closed" in expect_error(
        db.verify_bug_report, voters[0]["token"], bug
    )
    fresh_dup = db.file_bug_report(
        voters[0]["token"], "Rsterm dup", "body", url="https://example.com/bug/rsterm"
    )
    assert fresh_dup["duplicate_of"] is None
    assert fresh_dup["status"] == "open"


def test_reopen_restores_lifecycle():
    rep = db.register_agent("rsreo-reporter")
    bug = _confirmed_bug(rep["token"], "rsreo")
    voters = [_karmaed(f"rsreo-{i}") for i in range(BAR)]
    for v in voters:
        db.resolve_bug_report(v["token"], bug, "invalid")
    out = db.reopen_bug_report(bug, admin="testadmin")
    assert out["status"] == "open"
    full = db.get_bug_report(bug)
    assert full["decided_at"] is None
    assert full["resolution"] is None
    assert len(full["resolvers"]) == BAR
    ver = _karmaed("rsreo-ver")
    back = db.verify_bug_report(ver["token"], bug)
    assert back["confidence"] == 4
    msg = expect_error(db.reopen_bug_report, bug, admin="testadmin")
    assert "not closed" in msg


def test_admin_confirm_pings_reporter():
    rep = db.register_agent("rscping-reporter")
    bug = db.file_bug_report(
        rep["token"], "Ping bug", "body", url="https://example.com/bug/rscping"
    )["id"]
    db.confirm_bug_report(bug, admin="testadmin")
    with db._conn() as conn:
        rows = conn.execute(
            "SELECT body FROM notifications WHERE agent_id = ?"
            " AND ref_type = 'bug_report' AND body LIKE '%confirmed%'"
            f" AND body LIKE '%#{bug} (%'",
            (rep["agent_id"],),
        ).fetchall()
    assert len(rows) == 1
    assert "admin" in rows[0]["body"]


def test_stale_flag():
    rep = db.register_agent("rsstale-reporter")
    bug = db.file_bug_report(
        rep["token"], "Stale bug", "body", url="https://example.com/bug/rsstale"
    )["id"]
    assert db.get_bug_report(bug)["stale"] is False
    with db._conn(immediate=True) as conn:
        conn.execute(
            "UPDATE bug_reports SET created_at = '2020-01-01T00:00:00.000Z'"
            " WHERE id = ?",
            (bug,),
        )
    assert db.get_bug_report(bug)["stale"] is True
    rows = db.list_bug_reports(status="open")["reports"]
    assert [r for r in rows if r["id"] == bug][0]["stale"] is True


def test_sweep_retires_confirmed_orphan():
    rep = db.register_agent("rssweep-reporter")
    bug = _confirmed_bug(rep["token"], "rssweep")
    # Simulate the pre-fix dead letter: parent fixed while its dups sat at
    # confirmed (the gap the retire guard used to have).
    with db._conn(immediate=True) as conn:
        conn.execute(
            "UPDATE bug_reports SET status = 'fixed',"
            " decided_at = '2020-01-01T00:00:00.000Z' WHERE id = ?",
            (bug,),
        )
    # NOTE: the sweep is global over the shared test DB, so earlier tests'
    # confirmed-but-unresolved dups match too - assert at least ours, plus
    # row-level flips below. A second sweep is deterministically empty.
    with db._conn() as conn:
        assert db.sweep_retire_duplicates(conn) >= 2
    for did in _dup_report_ids(bug):
        assert db.get_bug_report(did)["status"] == "fixed"
    with db._conn() as conn:
        assert db.sweep_retire_duplicates(conn) == 0


if __name__ == "__main__":
    fns = [
        v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)
    ]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)}/{len(fns)} bug-resolve tests passed")
