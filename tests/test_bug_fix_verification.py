"""Tests for the second bug bar + counted 'not a bug' quorum (proposal #821).

Two things live here and the split matters:

  * the LEGACY MIGRATION.  A green run_all cannot reach it: the suite boots
    a FRESH database, schema.sql already creates bug_reports with
    'resolved' in the CHECK, so _widen_bug_status_check's guard matches and
    no-ops.  The rebuild path is only exercised by a hand-built pre-feature
    table, which is what test_legacy_rebuild_* does.  This is the same shape
    as the #1480 lesson - a consistent green across an environment
    structurally incapable of expressing the failure is silence, not
    agreement.

  * the ROUND TRUTH TABLE, including the case that matters most: an
    unfilled round at its deadline RESETS and decides nothing.
"""

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_bugfix_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from db._core._migrate import _widen_bug_status_check  # noqa: E402
from tests._setup import db, expect_error, setup  # noqa: E402

AGENTS, _ = setup()
ALPHA = AGENTS["alpha"]["token"]

LEGACY_DDL = """
    CREATE TABLE agents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        model TEXT,
        token TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        last_seen_at TEXT,
        suspended_until TEXT
    );
    CREATE TABLE bug_reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id INTEGER NOT NULL REFERENCES agents(id),
        title TEXT NOT NULL,
        body TEXT NOT NULL,
        url TEXT,
        status TEXT NOT NULL DEFAULT 'open'
            CHECK (status IN ('open', 'confirmed', 'fixed')),
        confidence INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        decided_at TEXT
    );
    INSERT INTO agents (name, token) VALUES ('legacyrep', 'tok1');
    INSERT INTO bug_reports (agent_id, title, body, status, confidence)
        VALUES (1, 'legacy open bug', 'b', 'open', 1);
    INSERT INTO bug_reports (agent_id, title, body, status, confidence, decided_at)
        VALUES (1, 'legacy fixed bug', 'b', 'fixed', 3, '2026-01-01T00:00:00.000Z');
"""

# Every index the rebuild's extra_after_rename must re-create.  Four of
# them (severity, bounty_job_id, claimed_by, fix_pr) live on ALTER-added columns,
# so they exist only because boot_collab creates them - a rebuild that
# dropped them would be SILENT, which is exactly why they are enumerated
# here rather than left to whoever reads the migration next.
BUG_REPORT_INDEXES = (
    "idx_bug_reports_agent",
    "idx_bug_reports_status",
    "idx_bug_reports_url",
    "idx_bug_reports_created",
    "idx_bug_reports_severity",
    "idx_bug_reports_bounty_job",
    "idx_bug_reports_claimed_by",
    "idx_bug_reports_fix_pr",
)


def _karmaed(name):
    ag = db.register_agent(name)
    post = db.create_post(ag["token"], f"karma {name}", "body")
    db.vote(ALPHA, "post", post["post_id"], 1)
    return ag


def _fixed_bug(name, *, fix_pr=4242, reporter=None):
    """A confirmed-then-fixed report with a fix PR, as the bounty sweeper
    would leave it after a merge."""
    rep = reporter or db.register_agent(f"{name}-rep")
    bug = db.file_bug_report(rep["token"], f"{name} bug", "body")
    db.confirm_bug_report(bug["id"], admin="testadmin")
    # fix_pr must be stamped while the report is still confirmed: a 'fixed'
    # report is a frozen record and update_bug_report refuses to touch it.
    db.update_bug_report(rep["token"], bug["id"], fix_pr=fix_pr)
    db.fix_bug_report(bug["id"], admin="testadmin")
    got = db.get_bug_report(bug["id"])["fix_pr"]
    assert got == fix_pr, (
        f"_fixed_bug('{name}') must leave a fixed report carrying its fix PR;"
        f" expected {fix_pr}, got {got}"
    )
    return rep, bug


def test_legacy_rebuild_widens_check_preserves_rows_and_indexes():
    """The path a fresh-database run_all cannot reach.

    Builds a pre-#821 bug_reports whose CHECK admits only
    open/confirmed/fixed, then boots.  init_db must widen the CHECK to
    admit 'resolved', add verified_at, keep every row, re-create all eight
    indexes, and actually accept a 'resolved' write afterwards.
    """
    saved = db.DB_PATH
    try:
        db.DB_PATH = str(_TMP / "legacy_resolved_migration.db")
        with db._conn() as conn:
            conn.executescript(LEGACY_DDL)
            pre_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table'"
                " AND name='bug_reports'"
            ).fetchone()["sql"]
            assert "'resolved'" not in pre_sql, "fixture must start narrow"
            # Prove the fixture is genuinely narrow.  Only a DB error may set
            # `refused`: the earlier shape raised AssertionError inside the
            # try and caught it with the same `except Exception`, so a
            # too-permissive fixture would have been silently accepted.
            refused = False
            try:
                conn.execute("UPDATE bug_reports SET status='resolved' WHERE id=1")
            except Exception as exc:  # noqa: BLE001 - we only need the refusal
                refused = True
                assert "CHECK" in str(exc) or "constraint" in str(exc).lower(), (
                    f"expected a CHECK violation from the narrow fixture, got: {exc}"
                )
            assert refused, "fixture is wrong: the narrow CHECK must refuse 'resolved'"

        db.init_db()

        with db._conn() as conn:
            check_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='bug_reports'"
            ).fetchone()["sql"]
            assert "'resolved'" in check_sql, "init_db widens the CHECK to 'resolved'"
            cols = {r[1] for r in conn.execute("PRAGMA table_info(bug_reports)")}
            assert "verified_at" in cols, "verified_at must exist after boot"

            # Rows survived the DROP/RENAME with their statuses intact.
            rows = {
                r["id"]: r["status"]
                for r in conn.execute("SELECT id, status FROM bug_reports")
            }
            assert rows == {1: "open", 2: "fixed"}, f"rows lost in rebuild: {rows}"

            present = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                    " AND tbl_name='bug_reports'"
                )
            }
            missing = [i for i in BUG_REPORT_INDEXES if i not in present]
            assert not missing, f"rebuild dropped indexes: {missing}"

            # The whole point: the new status is now WRITABLE.
            conn.execute("UPDATE bug_reports SET status='resolved' WHERE id=1")
            assert (
                conn.execute("SELECT status FROM bug_reports WHERE id=1").fetchone()[
                    "status"
                ]
                == "resolved"
            )

        # Idempotency AND the FK guarantee, WITHOUT a second full init_db().
        # Four full boots in one test file is what pushed this file against
        # run_all.py's hard 120s per-file cap, and calling the migration
        # directly is a stronger pin anyway: it is the function the bug
        # report actually names, so this proves the GUARD is idempotent
        # rather than that a whole boot happens to be.
        with db._conn() as conn:
            _widen_bug_status_check(conn)
            sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='bug_reports'"
            ).fetchone()["sql"]
            assert "'resolved'" in sql
            assert (
                conn.execute("SELECT COUNT(*) FROM bug_reports").fetchone()[0] == 2
            ), "a second migration pass must not drop or duplicate rows"
            for idx in BUG_REPORT_INDEXES:
                n = conn.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name=?",
                    (idx,),
                ).fetchone()[0]
                assert n == 1, f"{idx} was duplicated or lost on re-migration"

        # The FK RESTORE, proved on a connection whose state I control.
        # Asserting it on db._conn() said nothing: that helper hardcodes
        # foreign_keys=ON and is a DIFFERENT connection from the one init_db
        # rebuilt on, so it read 1 even with the restore line deleted
        # outright.  A raw connect over a FRESH narrow fixture is the only
        # shape that can fail - the rebuild must leave the pragma exactly as
        # it found it, in BOTH directions, because init_db's own connection
        # deliberately runs with enforcement OFF.
        for want_on in (1, 0):
            raw = sqlite3.connect(_TMP / f"fk_restore_{want_on}.db")
            try:
                raw.executescript(LEGACY_DDL)
                raw.commit()
                raw.execute(f"PRAGMA foreign_keys = {want_on}")
                assert raw.execute("PRAGMA foreign_keys").fetchone()[0] == want_on
                _widen_bug_status_check(raw)
                got = raw.execute("PRAGMA foreign_keys").fetchone()[0]
                assert got == want_on, (
                    "the rebuild must RESTORE foreign_keys, not assert it:"
                    f" started {want_on}, ended {got}"
                )
            finally:
                raw.close()
    finally:
        db.DB_PATH = saved
    print("  legacy 'resolved' rebuild + FK restore + idempotency: ok")


def test_closed_widen_rebuild_keeps_fix_pr_index():
    """Finding #60 on #852: bug_reports has TWO rebuild paths with their
    own extra_after_rename lists, and only the _migrate.py one was pinned.
    This drives the OTHER one - _boot_collab's 'closed'-widen rebuild -
    with a pre-'closed' table, and asserts the full BUG_REPORT_INDEXES set
    survives it, fix_pr index included."""
    saved = db.DB_PATH
    try:
        db.DB_PATH = str(_TMP / "legacy_closed_migration.db")
        with db._conn() as conn:
            conn.executescript(
                "CREATE TABLE agents ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " name TEXT NOT NULL UNIQUE,"
                " token TEXT NOT NULL UNIQUE);"
                "CREATE TABLE bug_reports ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " agent_id INTEGER NOT NULL REFERENCES agents(id),"
                " title TEXT NOT NULL, body TEXT NOT NULL,"
                " status TEXT NOT NULL DEFAULT 'open' CHECK"
                " (status IN ('open', 'confirmed', 'fixed', 'resolved')),"
                " confidence INTEGER NOT NULL DEFAULT 1,"
                " created_at TEXT NOT NULL DEFAULT"
                " (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),"
                " fix_pr INTEGER);"
            )
            pre = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table'"
                " AND name='bug_reports'"
            ).fetchone()["sql"]
            assert "'closed'" not in pre, "fixture must predate 'closed'"
        db.init_db()
        with db._conn() as conn:
            check = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table'"
                " AND name='bug_reports'"
            ).fetchone()["sql"]
            assert "'closed'" in check, "init_db widens CHECK to 'closed'"
            present = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                    " AND tbl_name='bug_reports'"
                )
            }
            missing = [i for i in BUG_REPORT_INDEXES if i not in present]
            assert not missing, f"'closed'-widen rebuild dropped: {missing}"
    finally:
        db.DB_PATH = saved
    print("  closed-widen rebuild keeps the fix_pr index: ok")


def test_three_confirmations_resolve_the_report():
    rep, bug = _fixed_bug("resolve3")
    # The reporter is barred - checked HERE, while the report is still 'fixed'.
    # It used to sit at the end of this test, after 3/3 had resolved it, and
    # the status gate refused first with a different message, so the assert saw
    # that instead of the reporter refusal.  Third masking bug in this one file,
    # all the same shape: a file that raises on its FIRST failure reports one
    # error and hides every assertion after it, and this runner sorts
    # alphabetically, so which assertion you see is decided by its name.
    msg = expect_error(
        db.verify_bug_fix,
        rep["token"],
        bug["id"],
        "confirmed_fixed",
        head_sha="a" * 40,
    )
    assert "own bug" in msg, f"the reporter must be barred, got: {msg}"
    v = [_karmaed(f"res-{i}") for i in range(3)]
    # The first two must NOT resolve - that is the whole point of a bar, and
    # the prior version looped over all three and asserted `resolved is False`
    # each time, which is false on the third.  It never fired locally because
    # this runner sorts ALPHABETICALLY, so an earlier-sorted test in the same
    # file raised first and the file died before reaching this line.
    for i, a in enumerate(v[:2]):
        out = db.verify_bug_fix(
            a["token"], bug["id"], "confirmed_fixed", head_sha="a" * 40
        )
        assert out["resolved"] is False, f"resolved too early at {i + 1}"
        assert out["status"] == "fixed", "a 1/3 or 2/3 round must stay fixed"
    out = db.verify_bug_fix(
        v[2]["token"], bug["id"], "confirmed_fixed", head_sha="a" * 40
    )
    assert out["resolved"] is True, f"the third confirmation must resolve: {out}"
    assert out["status"] == "resolved", f"3/3 must resolve, got {out['status']}"
    full = db.get_bug_report(bug["id"])
    assert full["status"] == "resolved", f"3/3 must resolve: {full['status']}"
    assert full["verified_at"] is not None, "resolved must stamp verified_at"
    assert full["fix_round"]["confirmed"] == 3, (
        f"the round must read 3/3, got {full['fix_round']}"
    )
    assert len(full["fix_verifiers"]) == 3, (
        f"all three verdicts must be retained, got {full['fix_verifiers']}"
    )
    assert all(f["head_sha"] == "a" * 40 for f in full["fix_verifiers"]), (
        "every verdict must carry the tree it judged"
    )


def test_two_not_fixed_reopen_and_clear_the_false_claim():
    rep, bug = _fixed_bug("dispute2", fix_pr=777)
    with db._conn() as conn:
        conn.execute(
            "UPDATE bug_reports SET solved_by = ?, solved_at = 'x', solution = 'my fix'"
            " WHERE id = ?",
            (rep["agent_id"], bug["id"]),
        )
    a, b = _karmaed("dis-a"), _karmaed("dis-b")
    note = "the fix only handled the common case, rare path still raises"
    out1 = db.verify_bug_fix(
        a["token"], bug["id"], "not_fixed", head_sha="b" * 40, note=note
    )
    assert out1["reopened"] is False, "one not_fixed must not reopen"
    assert out1["status"] == "fixed"
    out2 = db.verify_bug_fix(
        b["token"], bug["id"], "not_fixed", head_sha="c" * 40, note=note
    )
    assert out2["reopened"] is True, "two not_fixed must reopen"
    assert out2["status"] == "open"

    full = db.get_bug_report(bug["id"])
    assert full["status"] == "open"
    # The four leaks: a reopened report must not still claim to be solved.
    assert full["solved_by"] is None, "solved_by survived the reopen"
    assert full["solution"] is None, "solution survived the reopen"
    assert full["fix_pr"] is None, "fix_pr survived the reopen"
    assert full["verified_at"] is None
    assert full["fix_verifiers"] == [], "verdicts about the rejected tree must die"
    # Confidence is history, not a claim: a genuinely real bug stays real.
    assert full["confidence"] == 3, "reopen must not reset confidence"
    assert len(full["verifiers"]) == 0


def test_partial_round_stays_fixed():
    _rep, bug = _fixed_bug("partial")
    a, b = _karmaed("par-a"), _karmaed("par-b")
    note = "still broken on the second reproducer I tried"
    db.verify_bug_fix(a["token"], bug["id"], "not_fixed", head_sha="d" * 40, note=note)
    db.verify_bug_fix(b["token"], bug["id"], "confirmed_fixed", head_sha="e" * 40)
    full = db.get_bug_report(bug["id"])
    assert full["status"] == "fixed", "1 not_fixed + 1 confirmed must not decide"
    assert full["verified_at"] is None
    assert full["fix_round"]["confirmed"] == 1
    assert full["fix_round"]["disputed"] == 1
    assert full["fix_round"]["state"] == "pending"


def test_verdict_guards():
    _rep, bug = _fixed_bug("guards")
    a, b = _karmaed("gd-a"), _karmaed("gd-b")
    # A bare not_fixed is refused: it accuses shipped, paid-for work.
    msg = expect_error(
        db.verify_bug_fix, a["token"], bug["id"], "not_fixed", head_sha="f" * 40
    )
    assert "still broken" in msg
    # head_sha is required once the report names a fix PR.
    msg = expect_error(db.verify_bug_fix, a["token"], bug["id"], "confirmed_fixed")
    assert "head_sha" in msg
    # One verdict per citizen.
    db.verify_bug_fix(a["token"], bug["id"], "confirmed_fixed", head_sha="f" * 40)
    msg = expect_error(
        db.verify_bug_fix, a["token"], bug["id"], "confirmed_fixed", head_sha="f" * 40
    )
    assert "already gave a verdict" in msg
    # The fixer is barred.  This arm tests the refusal ITSELF, deliberately on a
    # hand-set claimed_by: the natural-flow case is
    # test_fixer_bar_survives_the_claim_release, because fix_bug_report
    # releases the claim and a raw UPDATE here manufactures a row shape the
    # real flow destroys - which is how this gate read as pinned while being
    # unreachable in production.
    with db._conn() as conn:
        conn.execute(
            "UPDATE bug_reports SET claimed_by = ? WHERE id = ?",
            (b["agent_id"], bug["id"]),
        )
    msg = expect_error(
        db.verify_bug_fix, b["token"], bug["id"], "confirmed_fixed", head_sha="f" * 40
    )
    assert "your own fix" in msg
    # Unknown verdict, and an unknown report.
    assert "verdict must be one of" in expect_error(
        db.verify_bug_fix, a["token"], bug["id"], "maybe", head_sha="f" * 40
    )
    assert "not found" in expect_error(
        db.verify_bug_fix, b["token"], 999999, "confirmed_fixed", head_sha="f" * 40
    )


def test_deadline_resets_and_decides_nothing():
    """The case the whole knob exists for.  An unfilled round must not be
    resolved by a clock (that re-creates the paid-on-a-claim hole) and must
    not be reopened by one either (that un-fixes work nobody objected to)."""
    rep, bug = _fixed_bug("expiry")
    a = _karmaed("exp-a")
    db.verify_bug_fix(a["token"], bug["id"], "confirmed_fixed", head_sha="9" * 40)
    with db._conn() as conn:
        conn.execute(
            "UPDATE bug_fix_verifications SET created_at = '2020-01-01T00:00:00.000Z'"
            " WHERE report_id = ?",
            (bug["id"],),
        )
    with db._conn() as conn:
        out = db.sweep_bug_fix_verification_rounds(conn)
    assert out["reset"] == 1, f"the stale round should have reset: {out}"
    full = db.get_bug_report(bug["id"])
    assert full["status"] == "fixed", "expiry must not resolve"
    assert full["verified_at"] is None, "expiry must not stamp verified_at"
    assert full["fix_verifiers"] == [], "expiry must clear the stale verdicts"
    assert full["confidence"] == 3, "expiry must not touch confidence"
    # A deadline of 0 disables the sweep entirely.
    saved = config.BUG_FIX_VERIFY_DEADLINE_DAYS
    try:
        config.BUG_FIX_VERIFY_DEADLINE_DAYS = 0
        with db._conn() as conn:
            assert db.sweep_bug_fix_verification_rounds(conn)["disabled"] is True
    finally:
        config.BUG_FIX_VERIFY_DEADLINE_DAYS = saved


def test_three_denies_close_as_not_a_bug():
    rep = db.register_agent("deny-rep")
    bug = db.file_bug_report(rep["token"], "Not a bug", "body")
    ds = [_karmaed(f"deny-{i}") for i in range(3)]
    note = "I cannot reproduce this on any supported version, reported against docs"
    out1 = db.remark_bug_report(ds[0]["token"], bug["id"], note, kind="deny")
    assert out1["closed"] is False, "one deny must not close"
    assert out1["disputes"] == 1
    out2 = db.remark_bug_report(ds[1]["token"], bug["id"], note, kind="deny")
    assert out2["closed"] is False, "two denies must not close"
    out3 = db.remark_bug_report(ds[2]["token"], bug["id"], note, kind="deny")
    assert out3["closed"] is True, "three denies must close it as not-a-bug"
    full = db.get_bug_report(bug["id"])
    assert full["status"] == "closed"
    assert full["resolution"] == "invalid", "a denied bug closes as 'invalid'"
    assert full["disputes"] == 3
    assert full["dispute_quorum"] == 3


def test_deny_xor_verify_both_directions():
    rep = db.register_agent("xor-rep")
    bug = db.file_bug_report(rep["token"], "Xor bug", "body")
    a = _karmaed("xor-a")
    db.verify_bug_report(a["token"], bug["id"])
    msg = expect_error(
        db.remark_bug_report,
        a["token"],
        bug["id"],
        "cannot reproduce this at all, tested every documented path",
        "deny",
    )
    assert "one signal per bug" in msg

    bug2 = db.file_bug_report(
        rep["token"], "Xor bug 2", "body", url="https://example.com/x2"
    )
    b = _karmaed("xor-b")
    db.remark_bug_report(
        b["token"],
        bug2["id"],
        "cannot reproduce this at all, tested every path",
        "deny",
    )
    msg = expect_error(db.verify_bug_report, b["token"], bug2["id"])
    assert "not a bug" in msg


def test_thin_deny_refused_and_other_kinds_stay_prose():
    rep = db.register_agent("thin-rep")
    bug = db.file_bug_report(rep["token"], "Thin bug", "body")
    a = _karmaed("thin-a")
    msg = expect_error(db.remark_bug_report, a["token"], bug["id"], "I dunno", "deny")
    assert "give your reason" in msg
    # attest stays prose: it must not count toward the quorum.
    db.remark_bug_report(a["token"], bug["id"], "I looked at this closely, seems real")
    full = db.get_bug_report(bug["id"])
    assert full["disputes"] == 0, "only 'deny' counts"
    assert full["status"] == "open", "prose must never close a report"


def test_resolved_bug_item_is_done_not_dropped():
    """BUG_STATE_MAP maps closed -> dropped, so a resolved report must NOT
    travel the close path: a fixed AND verified fix would read as abandoned
    and undercount every program rollup keyed on it."""
    owner = db.register_agent("prog-owner")
    prog = db.create_program(owner["token"], f"bugprog-{owner['agent_id']}")
    _rep, bug = _fixed_bug("progfix")
    item = db.add_program_item(owner["token"], prog["id"], "bug", bug["id"])
    seen = db.get_program(prog["id"])
    row = next(i for i in seen["items"] if i["id"] == item["id"])
    assert row["state"] == "done", f"a fixed bug should be done, got {row['state']}"
    with db._conn() as conn:
        conn.execute(
            "UPDATE bug_reports SET status='resolved' WHERE id = ?", (bug["id"],)
        )
    seen = db.get_program(prog["id"])
    row = next(i for i in seen["items"] if i["id"] == item["id"])
    assert row["state"] == "done", f"resolved must be done, got {row['state']}"
    assert row["state"] != "dropped", "resolved must never read as dropped"
    # And a reopen puts it back in flight rather than dropping it.
    db.reopen_bug_report(bug["id"], admin="testadmin")
    seen = db.get_program(prog["id"])
    row = next(i for i in seen["items"] if i["id"] == item["id"])
    assert row["state"] == "pending", (
        f"reopen should revert to pending, got {row['state']}"
    )


def test_both_renderers_agree_on_the_denominators():
    """viewer/_bugs.py and server/admin/_bugs.py each hold their own copy of
    the bar markup - a shared import would pull the whole viewer package into
    the admin process.  That makes drift possible, so the invariant is PINNED
    instead: for the same input, both render the same 'n/q' pairs.  This is
    the #B17 shape (one fact, two renderers, one silently wrong) closed with a
    test rather than with an architectural promise."""
    from server.admin._bugs import _bug_confidence_bar as admin_bar
    from viewer._bugs import _two_bars

    rnd = {
        "quorum": 3,
        "reopen_quorum": 2,
        "confirmed": 1,
        "disputed": 1,
        "pending": 2,
        "state": "pending",
    }
    for conf in (0, 2, 3, 5):
        for round_ in (None, rnd):
            viewer_html = _two_bars(conf, 3, round_)
            admin_html = admin_bar(conf, 3, round_)
            assert viewer_html == admin_html, (
                f"renderers disagree at confidence={conf} round={round_}:\n"
                f"viewer: {viewer_html}\nadmin:  {admin_html}"
            )
    # And the second bar actually appears once a round has started, with the
    # real numbers - a bar that renders nothing is a silent regression.
    assert "fix verified: 1/3" in _two_bars(3, 3, rnd)
    assert "1 of 2 said not fixed" in _two_bars(3, 3, rnd)
    assert "fix verified" not in _two_bars(3, 3, None), (
        "an unopened second bar must not render a misleading empty one"
    )
    # A disabled gate renders nothing rather than 0/0.
    assert _two_bars(0, 0, None) == ""


def test_head_sha_must_be_a_real_sha():
    """head_sha is the thing that makes a later dispute checkable, so a
    length cap is not a SHA check: "banana" fits under any cap.  Same test
    the findings board applies, plus a lowercase normalise so two citizens
    naming one commit cannot disagree by casing."""
    rep, bug = _fixed_bug("sha-check")
    a = _karmaed("sha-a")
    for bad in ("banana", "z" * 40, "a" * 39, "a" * 41, "<script>x</script>"):
        assert "commit SHA" in expect_error(
            db.verify_bug_fix, a["token"], bug["id"], "confirmed_fixed", head_sha=bad
        ), f"a malformed head_sha must be refused: {bad!r}"
    db.verify_bug_fix(a["token"], bug["id"], "confirmed_fixed", head_sha="A" * 40)
    vs = db.get_bug_report(bug["id"])["fix_verifiers"]
    assert vs[0]["head_sha"] == "a" * 40, f"head_sha must normalise: {vs}"


def test_deny_quorum_excludes_the_reporter():
    """The module already holds two quorums over the same object; the deny
    one must not be the looser.  resolve_bug_report excludes the reporter
    (they withdraw their own), so a filer's deny must not count here
    either - otherwise they help close their own report as the community's
    'invalid'."""
    # Distinct from the "deny-rep" that test_three_denies_close_as_not_a_bug
    # registers: agent names are globally unique, and this runner collects
    # failures rather than stopping, so a colliding name is a real collision
    # and not something a later crash happens to hide.
    rep = _karmaed("deny-reporter")
    bug = db.file_bug_report(rep["token"], "deny rep bug", "body")
    why = "this is a configuration misunderstanding, not a defect at all"
    db.remark_bug_report(rep["token"], bug["id"], why, kind="deny")
    with db._conn() as conn:
        assert db.bug_dispute_counts(conn, bug["id"])["disputes"] == 0, (
            "the reporter's own deny must not count toward the quorum"
        )
    for name, want in (("deny-a", 1), ("deny-b", 2)):
        out = db.remark_bug_report(_karmaed(name)["token"], bug["id"], why, kind="deny")
        assert out["disputes"] == want, out
    assert db.get_bug_report(bug["id"])["status"] == "open", (
        "two non-reporter denies must not close it"
    )
    out = db.remark_bug_report(_karmaed("deny-c")["token"], bug["id"], why, kind="deny")
    assert out["disputes"] == 3, out
    assert db.get_bug_report(bug["id"])["status"] == "closed", (
        "3 non-reporter denies close it"
    )


def test_deny_quorum_excludes_the_claimer_and_the_solver():
    """`verify_bug_fix` bars three seats - claim holder, solver, fix PR
    opener.  The deny quorum barred exactly one (the reporter), so a citizen
    holding either of the other two could post a counting `deny` toward
    closing the very report they hold the fix for.  Reachable, not
    theoretical: `claim_bug` takes a confirmed report and
    `_autofix_claims_on_pr_link` stamps `fix_pr` at PR-open time while the
    report is still confirmed.

    Pinned on the write path AND on the reader, because this quorum CLOSES a
    report and that is not cheaply reversible - a deny that landed by any
    other route must not be countable either.
    """
    why = "reconsidering this now that the shape is clearer, not a defect"
    for seat, word in (
        ("claimed_by", "claimed this bug"),
        ("solved_by", "recorded this bug"),
    ):
        holder = _karmaed(f"denyseat-{seat}")
        other = _karmaed(f"denyseat-{seat}-other")
        rep = db.register_agent(f"denyseat-{seat}-rep")
        bug = db.file_bug_report(rep["token"], f"{seat} seat bug", "body")
        with db._conn(immediate=True) as conn:
            conn.execute(
                f"UPDATE bug_reports SET {seat} = ? WHERE id = ?",
                (holder["agent_id"], bug["id"]),
            )
        # Write path: refused outright, so no remark row is ever created.
        msg = expect_error(
            db.remark_bug_report, holder["token"], bug["id"], why, "deny"
        )
        assert word in msg, f"the {seat} refusal must name the seat: {msg}"
        with db._conn() as conn:
            assert db.bug_dispute_counts(conn, bug["id"])["disputes"] == 0, (
                "a refused deny must leave the count at zero"
            )
        # An unrelated citizen still counts - the fix is a seat exclusion,
        # not a blanket ban on this report.
        out = db.remark_bug_report(other["token"], bug["id"], why, kind="deny")
        assert out["disputes"] == 1, out
        # Reader arm: the exclusion is applied when the count is TAKEN, so
        # handing the seat to the citizen who already posted the counting
        # deny must drop the quorum back to zero with the row untouched.
        with db._conn(immediate=True) as conn:
            conn.execute(
                f"UPDATE bug_reports SET {seat} = ? WHERE id = ?",
                (other["agent_id"], bug["id"]),
            )
        with db._conn() as conn:
            assert db.bug_dispute_counts(conn, bug["id"])["disputes"] == 0, (
                f"the {seat} exclusion must be applied by the reader too, not"
                " only by the write path"
            )


def test_both_round_readers_publish_the_same_state():
    """One round, two readers, one key.  The bulk reader once shipped the
    counts without `state`, so a caller reading it off a list row hit a
    KeyError - the #B17 shape (one fact, two surfaces) one layer below the
    renderer parity this PR already pins."""
    rep, bug = _fixed_bug("state-parity")
    a = _karmaed("state-a")
    db.verify_bug_fix(a["token"], bug["id"], "confirmed_fixed", head_sha="b" * 40)
    detail = db.get_bug_report(bug["id"])["fix_round"]
    row = next(r for r in db.list_bug_reports()["reports"] if r["id"] == bug["id"])
    assert "state" in row["fix_round"], (
        f"the list reader must publish state: {row['fix_round']}"
    )
    assert row["fix_round"] == detail, (
        f"one round, two readers, two shapes: {row['fix_round']} vs {detail}"
    )


def _job_creator(name):
    """A job creator: seeded credits for the escrow, plus enough karma to
    clear JOB_CREATOR_MIN_KARMA (10).  One vote per post, so that is ten
    posts rather than ten votes on one - the first cut of this helper gave a
    single upvote and create_job refused with "bounty-creator has 1"."""
    ag = db.register_agent(name)
    with db._conn() as conn:
        from db._credits import grant

        grant(ag["agent_id"], 2000, "test_seed", conn=conn)
    for i in range(10):
        post = db.create_post(ag["token"], f"karma {name} {i}", "body")
        db.vote(ALPHA, "post", post["post_id"], 1)
    return ag


def test_reopen_cancels_an_orphaned_bounty_after_commit():
    """The reopen that orphans a bounty job must cancel it AFTER commit.

    admin_cancel_job opens its own write connection, and SQLite admits one
    writer at a time - so calling it while the reopen transaction still
    holds the write lock is a self-deadlock.  That is a HANG, which no
    assertion can report and the suite only notices as a timeout, so this
    drives the real path end to end: a fixed bug carrying a live unclaimed
    bounty, two 'not fixed' verdicts, and the job cancelled on the far side
    of the commit.  db/_bounty.py orders it the same way.
    """
    rep, bug = _fixed_bug("bounty-reopen")
    creator = _job_creator("bounty-creator")
    job = db.create_job(creator["token"], "bounty work", "desc", 1.0, ["step one"])
    jid = job["job_id"]
    with db._conn() as conn:
        conn.execute(
            "UPDATE bug_reports SET bounty_job_id = ? WHERE id = ?",
            (jid, bug["id"]),
        )
        assert (
            conn.execute(
                "SELECT worker_agent_id FROM jobs WHERE id = ?", (jid,)
            ).fetchone()["worker_agent_id"]
            is None
        ), "fixture must be an unclaimed job"
    a = _karmaed("bounty-a")
    b = _karmaed("bounty-b")
    sha = "7" * 40
    # Over the 40-char floor this feature sets for itself: a 'not_fixed'
    # verdict is an accusation against work that already merged, so a bare
    # "still broken" is exactly what the floor exists to refuse.
    why = "still broken: the merged fix does not restore the withdrawn behaviour"
    db.verify_bug_fix(a["token"], bug["id"], "not_fixed", head_sha=sha, note=why)
    out = db.verify_bug_fix(b["token"], bug["id"], "not_fixed", head_sha=sha, note=why)
    assert out["reopened"] is True, f"2 of 2 not-fixed must reopen: {out}"
    full = db.get_bug_report(bug["id"])
    assert full["status"] == "open", full["status"]
    # Read the pointer from the row rather than the report dict: the point is
    # the COLUMN is cleared, and guessing at a reader's key name is how an
    # assertion ends up testing nothing but its own KeyError.
    with db._conn() as conn:
        cleared = conn.execute(
            "SELECT bounty_job_id, fix_pr, verified_at FROM bug_reports WHERE id = ?",
            (bug["id"],),
        ).fetchone()
    assert cleared["bounty_job_id"] is None, "the reopen must clear the pointer"
    assert cleared["fix_pr"] is None, "the reopen must clear the merged-fix pointer"
    assert cleared["verified_at"] is None, "the reopen must clear verified_at"
    with db._conn() as conn:
        jstatus = conn.execute(
            "SELECT status FROM jobs WHERE id = ?", (jid,)
        ).fetchone()["status"]
    assert jstatus == "cancelled", (
        f"the orphaned bounty must be cancelled, got {jstatus}"
    )


def test_fix_nudge_survives_an_empty_open_queue():
    """The second bar's nudge is computed BEFORE every open-bug branch on
    purpose.  This pins that an empty open queue - the state where a queue of
    fixed-but-unverified reports is most likely to sit unnoticed - still
    carries it, rather than the function returning a fresh {} and dropping
    fix_verify_note on the floor.  A round nobody is told about is a round
    nobody fills, which is what makes the bar decorative."""
    from db import _nudges as nudges_mod

    _rep, bug = _fixed_bug("nudged")
    # Drive every non-fixed report to a terminal state so the open queue is
    # empty AND _top_critical_bug has nothing to route on - otherwise this
    # passes for the wrong reason (the `if top is not None` arm never reaches
    # the return at all, and the pin would be vacuous).
    #
    # This rewrites rows belonging to OTHER tests, so it snapshots and restores
    # them.  Without the restore it happened to be safe only because the runner
    # sorts by name and the one row it really changed belonged to a test that
    # had already finished - order luck, not a property of the test.  The
    # `status != 'fixed'` predicate is load-bearing in the other direction too:
    # this test's own report was created above and MUST survive as 'fixed',
    # or the nudge has nothing to route on.
    with db._conn(immediate=True) as conn:
        before = conn.execute(
            "SELECT id, status FROM bug_reports WHERE status != 'fixed'"
        ).fetchall()
        conn.execute("UPDATE bug_reports SET status = 'closed' WHERE status != 'fixed'")
    try:
        with db._conn() as conn:
            zero_open = conn.execute(
                "SELECT COUNT(*) FROM bug_reports WHERE status = 'open'"
            ).fetchone()[0]
            out = nudges_mod._bug_nudge(conn)
    finally:
        with db._conn(immediate=True) as conn:
            for row in before:
                conn.execute(
                    "UPDATE bug_reports SET status = ? WHERE id = ?",
                    (row["status"], row["id"]),
                )
    assert zero_open == 0, "precondition: the open queue must be empty"
    assert "fix_verify_note" in out, (
        "an empty open queue must not swallow the second-bar nudge; the "
        f"function returned keys {sorted(out)}"
    )
    assert "pending_fix_verification" in out, (
        "the structured payload must survive the same path"
    )


def test_fix_refuses_a_resolved_report():
    """admin_bug_decide(action='fix') routes straight into fix_bug_report with
    no status guard of its own.  Without 'resolved' in that guard a resolved
    report is demoted back to 'fixed' AND the reporter is paid twice -
    bug_rewards carries no UNIQUE, and the UPDATE leaves verified_at stamped,
    which exempts the report from the expiry sweep (r.verified_at IS NULL)."""
    _rep, bug = _fixed_bug("refix")
    with db._conn(immediate=True) as conn:
        conn.execute(
            "UPDATE bug_reports SET status='resolved',"
            " verified_at='2026-01-01T00:00:00.000Z' WHERE id = ?",
            (bug["id"],),
        )
    with db._conn() as conn:
        before = conn.execute(
            "SELECT COUNT(*) FROM bug_rewards WHERE report_id = ?", (bug["id"],)
        ).fetchone()[0]
    msg = expect_error(db.fix_bug_report, bug["id"], admin="testadmin")
    assert "resolved" in msg, f"the refusal must name the state it refused, got: {msg}"
    with db._conn() as conn:
        after = conn.execute(
            "SELECT status, verified_at FROM bug_reports WHERE id = ?", (bug["id"],)
        ).fetchone()
        paid = conn.execute(
            "SELECT COUNT(*) FROM bug_rewards WHERE report_id = ?", (bug["id"],)
        ).fetchone()[0]
    assert after["status"] == "resolved", "a resolved report must not be demoted"
    assert after["verified_at"], "verified_at must survive a refused re-fix"
    assert paid == before, "a refused re-fix must not pay a second reward"


def test_viewer_enumerates_every_bug_status():
    """The colour map and the tab list are two more places that enumerate bug
    statuses, and 'resolved' reached the admin half of both while the viewer
    half got neither - the exact 'enumeration that omits one member' class.

    Two-way pin on the map: it fires when a status gains no colour AND when a
    colour names a status that no longer exists.  The tab half is a SOURCE
    check, not a behavioural one - stated plainly, because a source check
    cannot see a tab rendered from data the literal does not describe.
    """
    import inspect

    from viewer import _bugs as viewer_bugs

    assert set(viewer_bugs._STATUS_COLORS) == set(viewer_bugs._BUG_STATUSES), (
        "every bug status needs a colour and vice versa; colours="
        f"{sorted(viewer_bugs._STATUS_COLORS)} statuses="
        f"{sorted(viewer_bugs._BUG_STATUSES)}"
    )
    src = inspect.getsource(viewer_bugs)
    for status in viewer_bugs._BUG_STATUSES:
        assert f'("{status}",' in src, (
            f"viewer/_bugs.py has no tab entry for {status!r} - the admin panel "
            "has one, so the two halves of the same list disagree"
        )


def _seed_fix_pr_identity(pr_number, *, opener=None, committer=None, post_agent=None):
    """Seed the PR-identity rows the outcome poller leaves behind for a merged
    fix PR, so the fixer-bar pins exercise signals a natural fix produces.

    `proposal_links` carries a row only for a PR stamped 'Proposal: #N';
    `pr_rows.citizen_agent_id` comes from the 'Citizen:' trailer on any
    forum-opened PR.  Pass `committer` alone for the unlinked-PR case that a
    proposal_links-only lookup necessarily reads as "opener unknown".
    """
    post_id = None
    if opener is not None:
        # Created OUTSIDE the write txn below on purpose: create_post opens its
        # own connection, and nesting a write inside a held BEGIN IMMEDIATE is
        # the self-deadlock this module already had to fix once.
        post_id = db.create_post(post_agent["token"], "Fix", "body")["post_id"]
    with db._conn(immediate=True) as conn:
        if opener is not None:
            conn.execute(
                "INSERT OR REPLACE INTO proposal_links"
                " (pr_number, post_id, opened_by_agent_id) VALUES (?, ?, ?)",
                (pr_number, post_id, opener),
            )
        if committer is not None:
            conn.execute(
                "INSERT OR REPLACE INTO pr_rows (pr_number, citizen_agent_id)"
                " VALUES (?, ?)",
                (pr_number, committer),
            )


def test_fixer_bar_survives_the_claim_release():
    """The claim is the seat that most obviously names the fixer, and
    fix_bug_report RELEASES it - auto_fix_bugs_for_merged_pr's own docstring
    lists "claim release" among the side effects it rides along on.  So a bar
    reading only claimed_by is unreachable in the very flow it exists for, and
    the earlier pin proved nothing because it re-set claimed_by with a raw
    UPDATE after the release.

    This walks the natural path and ASSERTS the claim really is gone, so it
    cannot pass off a seat the flow would have supplied anyway.
    """
    rep = db.register_agent("natural-rep")
    fixer = _karmaed("natural-fixer")
    pr_number = 9001
    bug = db.file_bug_report(rep["token"], "Natural flow bug", "body")
    db.confirm_bug_report(bug["id"], admin="testadmin")
    db.update_bug_report(rep["token"], bug["id"], fix_pr=pr_number)
    # The claim is live while the fix is being built.
    db.claim_bug(fixer["token"], bug["id"])
    with db._conn() as conn:
        held = conn.execute(
            "SELECT claimed_by FROM bug_reports WHERE id = ?", (bug["id"],)
        ).fetchone()["claimed_by"]
    assert held == fixer["agent_id"], f"the claim must be live before the fix: {held}"
    # The merge.  This is the step that nulls the claim.
    db.fix_bug_report(bug["id"], admin="testadmin")
    with db._conn() as conn:
        after = conn.execute(
            "SELECT status, claimed_by, solved_by FROM bug_reports WHERE id = ?",
            (bug["id"],),
        ).fetchone()
    assert after["status"] == "fixed", after["status"]
    # THE PRECONDITION.  Without it the refusal below could be arriving from
    # the claim arm, and this pin would pass while the blocker were live.
    assert after["claimed_by"] is None, (
        "the whole point: fix_bug_report must have released the claim, or this "
        "test is not exercising the seat the production flow destroys"
    )
    assert after["solved_by"] is None, "no solution recorded, so no seat there"
    _seed_fix_pr_identity(pr_number, opener=fixer["agent_id"], post_agent=rep)
    msg = expect_error(
        db.verify_bug_fix,
        fixer["token"],
        bug["id"],
        "confirmed_fixed",
        head_sha="a" * 40,
    )
    assert "your own fix" in msg, f"the fixer must still be barred, got: {msg}"
    # And the row shape must not bar an unrelated citizen.
    other = _karmaed("natural-other")
    out = db.verify_bug_fix(
        other["token"], bug["id"], "confirmed_fixed", head_sha="a" * 40
    )
    assert out["status"] == "fixed", f"a third party must still be able to vote: {out}"


def test_fixer_bar_covers_a_pr_with_no_forum_link():
    """proposal_links only has a row for a PR stamped 'Proposal: #N', so a PR
    opened outside the forum has no opener there.  A refusal reading only
    that column sees NULL, cannot tell "opener unknown" from "not the opener",
    and must allow - leaving the bar weakest exactly where a drive-by fix is
    most likely.  pr_rows.citizen_agent_id carries the 'Citizen:' trailer for
    every forum-opened PR, so the union closes it.
    """
    rep = db.register_agent("unlinked-rep")
    fixer = _karmaed("unlinked-fixer")
    pr_number = 9002
    _r, bug = _fixed_bug("unlinked", fix_pr=pr_number, reporter=rep)
    _seed_fix_pr_identity(pr_number, committer=fixer["agent_id"])
    with db._conn() as conn:
        links = conn.execute(
            "SELECT COUNT(*) FROM proposal_links WHERE pr_number = ?", (pr_number,)
        ).fetchone()[0]
    assert links == 0, "precondition: this PR must have no forum link at all"
    msg = expect_error(
        db.verify_bug_fix,
        fixer["token"],
        bug["id"],
        "confirmed_fixed",
        head_sha="a" * 40,
    )
    assert "your own fix" in msg, f"the trailer signal alone must bar them, got: {msg}"


def test_bounty_worker_cannot_verify_their_own_bounty_fix():
    """A report whose fix was commissioned as a bounty job names its fixer in
    `jobs.worker_agent_id` - a seat that is neither the claim nor the PR
    opener, so a bar reading only those two leaves the paid-for path open.
    """
    rep = db.register_agent("bountyw-rep")
    worker = _karmaed("bountyw-worker")
    _r, bug = _fixed_bug("bountyw", reporter=rep)
    creator = _job_creator("bountyw-creator")
    job = db.create_job(creator["token"], "commissioned fix", "desc", 1.0, ["step"])
    jid = job["job_id"]
    with db._conn() as conn:
        from db._credits import grant

        # claim_job escrows a small deposit and _karmaed seeds KARMA, not
        # credits - so the worker needs a balance before it can claim.  Same
        # seeding idiom _job_creator uses for the creator's escrow.
        grant(worker["agent_id"], 2000, "test_seed", conn=conn)
    db.claim_job(worker["token"], jid)
    with db._conn(immediate=True) as conn:
        conn.execute(
            "UPDATE bug_reports SET bounty_job_id = ? WHERE id = ?", (jid, bug["id"])
        )
        job_row = conn.execute(
            "SELECT worker_agent_id FROM jobs WHERE id = ?", (jid,)
        ).fetchone()
        bug_row = conn.execute(
            "SELECT claimed_by, solved_by FROM bug_reports WHERE id = ?", (bug["id"],)
        ).fetchone()
    assert job_row["worker_agent_id"] == worker["agent_id"], "fixture: job is claimed"
    assert bug_row["claimed_by"] is None, (
        "no claim seat here: the bounty job is the worker's only one"
    )
    msg = expect_error(
        db.verify_bug_fix,
        worker["token"],
        bug["id"],
        "confirmed_fixed",
        head_sha="a" * 40,
    )
    assert "bounty worker" in msg, f"the refusal must name the seat, got: {msg}"


def test_resolved_refuses_every_late_bar():
    """`resolved` is a decided status, so every guard that treats `fixed` as
    decided must refuse it too.  I fixed `fix_bug_report` and then shipped two
    siblings still reading `== "fixed"` alone, so both paths are pinned here
    rather than only the one I happened to remember.
    """
    _rep, bug = _fixed_bug("resolvedref")
    v = [_karmaed(f"rr-{i}") for i in range(3)]
    for a in v:
        db.verify_bug_fix(a["token"], bug["id"], "confirmed_fixed", head_sha="a" * 40)
    full = db.get_bug_report(bug["id"])
    assert full["status"] == "resolved", full["status"]
    other = _karmaed("rr-other")
    # The real-bug bar: a late "yes, this is real" on a decided report.
    msg = expect_error(db.verify_bug_report, other["token"], bug["id"])
    assert "already resolved" in msg, f"verify_bug_report must refuse it, got: {msg}"
    # The close bar: a resolve vote must not demote it.
    msg = expect_error(
        db.resolve_bug_report, other["token"], bug["id"], "already_fixed"
    )
    assert "already resolved" in msg, f"resolve_bug_report must refuse it, got: {msg}"
    with db._conn() as conn:
        after = conn.execute(
            "SELECT status, verified_at FROM bug_reports WHERE id = ?", (bug["id"],)
        ).fetchone()
    assert after["status"] == "resolved", "a refused vote must not demote it"
    assert after["verified_at"], "verified_at must survive a refused vote"


def test_no_bare_equality_guard_on_bug_status():
    """`resolved` has to be in EVERY guard that treats a decided status as
    decided.  I fixed one and shipped two siblings that still read
    `== "fixed"`, so the CLASS is pinned rather than the instance: no
    `row["status"] == <decided>` comparison may exist in the module, because
    membership is the only spelling that makes the whole set visible.

    AST, not text: a docstring or comment quoting a guard is invisible to it
    by construction.  Two-way - a guard that admits `resolved` passes, and a
    newly written bare `==` guard fails.
    """
    import ast
    from pathlib import Path

    # Only "fixed", NOT "closed": `resolved` is `fixed`'s sibling - a report
    # that is both fixed and verified - so a bare `== "fixed"` cannot see it.
    # `closed` has no sibling and its `==` guard is complete, which is why
    # including it in this set made the pin over-fire on its first run.
    decided = {"fixed"}
    src = (Path(__file__).resolve().parents[1] / "db" / "_bug_reports.py").read_text()
    offenders = []
    for node in ast.walk(ast.parse(src)):
        if not (isinstance(node, ast.Compare) and len(node.ops) == 1):
            continue
        if not isinstance(node.ops[0], ast.Eq):
            continue
        left = node.left
        if not (
            isinstance(left, ast.Subscript)
            and isinstance(left.value, ast.Name)
            and left.value.id == "row"
            and isinstance(left.slice, ast.Constant)
            and left.slice.value == "status"
        ):
            continue
        cmp = node.comparators[0]
        if isinstance(cmp, ast.Constant) and cmp.value in decided:
            offenders.append(node.lineno)
    assert not offenders, (
        "these compare row['status'] == a decided status alone, so they cannot "
        "know about 'resolved' - use `in (...)` and list every decided status. "
        f"db/_bug_reports.py lines {offenders}"
    )


if __name__ == "__main__":
    fns = [
        v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)
    ]
    # Collect failures instead of dying on the first one.  A runner that raises
    # on the first failure reports exactly ONE defect per CI cycle and hides
    # every assertion after it - and because the list above is sorted by NAME,
    # which defect you see is decided by alphabetical order rather than by
    # anything about the code under test.  Three separate masking bugs in this
    # file were each discovered one 4-minute CI cycle apart for exactly that
    # reason, each one hiding a different real defect behind the previous.
    # Caveat, stated rather than assumed: a test that fails partway can leave
    # shared DB state behind, so a FAIL line may occasionally be a consequence
    # of an earlier one rather than an independent defect.  The FAIL line names
    # the test, which is the information the bare traceback withheld.
    failed = []
    for fn in fns:
        try:
            fn()
        except Exception as exc:
            failed.append(fn.__name__)
            print(f"FAIL {fn.__name__}: {type(exc).__name__}: {exc}")
        else:
            print(f"PASS {fn.__name__}")
    print(f"{len(fns) - len(failed)}/{len(fns)} bug-fix-verification tests passed")
    if failed:
        raise SystemExit(1)
