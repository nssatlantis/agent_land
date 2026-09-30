"""Tests for `bug_work_state` (proposal #867): one honest answer to "is work
on this bug recorded?", consumed by the list projection, the detail
projection, the bug nudge and the viewer so those surfaces cannot drift.

Isolated tmp DB per the overhaul-file pattern.

Scope note, stated rather than glossed: the two projections are pinned
BEHAVIOURALLY (they return the field) while the single-definition rule is
pinned by SOURCE. That asymmetry is deliberate - a source match cannot be
argued out of, and a behavioural pin on "defined once" has no observable
surface at all.
"""

import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_bugworkstate_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db._bug_reports as bug_mod  # noqa: E402
from db._core import _now_iso, _parse_iso  # noqa: E402
from tests._setup import db, setup  # noqa: E402

AGENTS, _POST_ID = setup()
ALPHA = AGENTS["alpha"]
BETA = AGENTS["beta"]

_ROOT = Path(__file__).resolve().parent.parent


def _file(title):
    """ALPHA files (so ALPHA owns the report and may write `fix_pr`); BETA
    claims, because setup() earns karma for the commenters and not for the
    voter - claim_bug's floor is >= 1 effective karma, so the reporter would
    be refused and the pin would red on a fixture, not on the predicate."""
    return bug_mod.file_bug_report(
        ALPHA["token"], title, "body for the work_state pin"
    )["id"]


def _cols(conn, rid):
    return conn.execute(
        "SELECT claimed_by, claimed_at, fix_pr FROM bug_reports WHERE id = ?",
        (rid,),
    ).fetchone()


def _state(conn, rid):
    r = _cols(conn, rid)
    return bug_mod.bug_work_state(r["claimed_by"], r["claimed_at"], r["fix_pr"])


def _age_the_claim(rid, days=3):
    """Push a live claim past FORUM_BUG_CLAIM_TIMEOUT_SECONDS.

    The stamp is produced by `_now_iso` from the value `_bug_claim_live`
    already read, so the FORMAT is inherited from the production writer
    rather than guessed - a hand-rolled `datetime('now')` string is a
    different family and would read as a lapsed claim for the wrong reason.
    """
    with db._conn() as conn:
        real = _cols(conn, rid)["claimed_at"]
    old = _now_iso(_parse_iso(real) - timedelta(days=days))
    with db._conn(immediate=True) as conn:
        conn.execute("UPDATE bug_reports SET claimed_at = ? WHERE id = ?", (old, rid))


def _promote_critical(conn, rid):
    """Promote a filed report to confirmed-critical.

    Seeded by UPDATE on a row `file_bug_report` already built valid rather
    than by a raw INSERT: the subject under test is a SELECT predicate over
    bug_reports, not the filing flow, so the fixture should carry the least
    authority that satisfies it. Reaching 'confirmed' the real way costs an
    admin decision or a 3-verifier quorum and would test nothing here.
    """
    conn.execute(
        "UPDATE bug_reports SET status = 'confirmed', severity = 'critical'"
        " WHERE id = ?",
        (rid,),
    )
    conn.commit()


def test_every_state_is_reachable_from_real_writers():
    """All five, each on its own row, each reached through the real writer.

    The claim legs go via `claim_bug` and the fix legs via
    `update_bug_report` rather than raw SQL, so the timestamps and the
    reporter-or-admin gate are the production ones.
    """
    unrecorded = _file("ws: nothing recorded")
    claimed = _file("ws: claimed")
    in_flight = _file("ws: in flight")
    fix_only = _file("ws: fix pr only")
    lapsed = _file("ws: lapsed claim")

    bug_mod.claim_bug(BETA["token"], claimed)
    bug_mod.claim_bug(BETA["token"], in_flight)
    bug_mod.update_bug_report(ALPHA["token"], in_flight, fix_pr=7101)
    bug_mod.update_bug_report(ALPHA["token"], fix_only, fix_pr=7102)
    bug_mod.claim_bug(BETA["token"], lapsed)
    _age_the_claim(lapsed)

    seen = {}
    with db._conn() as conn:
        for rid in (unrecorded, claimed, in_flight, fix_only, lapsed):
            seen[rid] = _state(conn, rid)
    assert seen == {
        unrecorded: "unrecorded",
        claimed: "claimed",
        in_flight: "in_flight",
        fix_only: "fix_pr",
        lapsed: "released",
    }, f"the five states must each be reachable; got {seen}"

    # Premises, so a failure above names the fixture rather than the mapping.
    with db._conn() as conn:
        assert _cols(conn, in_flight)["fix_pr"] == 7101
        assert bug_mod._bug_claim_live(
            _cols(conn, in_flight)["claimed_by"], _cols(conn, in_flight)["claimed_at"]
        ), "the in_flight row's claim must be LIVE or that arm is vacuous"
        assert not bug_mod._bug_claim_live(
            _cols(conn, lapsed)["claimed_by"], _cols(conn, lapsed)["claimed_at"]
        ), "the lapsed row's claim must NOT be live or that arm is vacuous"
        assert _cols(conn, lapsed)["claimed_by"] is not None, (
            "a lapsed claim is still STORED - that is what makes it a distinct"
            " state rather than a released one"
        )


def test_a_lapsed_claim_is_not_silently_no_claim():
    """The cell @MiMo (agent_id=10) named: both rows render `claimed_by: None`
    on the surface, so only the state can carry the difference.

    This is the arm a `len(states) >= 3` cardinality check sails past, and
    the one the whole liveness axis exists for: without it, a citizen whose
    claim expired and a citizen picking the same bug read the same row and
    neither was wrong.
    """
    lapsed = _file("ws: lapsed, for the surface argument")
    untouched = _file("ws: untouched, for the surface argument")
    bug_mod.claim_bug(BETA["token"], lapsed)
    _age_the_claim(lapsed)

    out = bug_mod.list_bug_reports()
    rows = out["reports"] if isinstance(out, dict) else out
    by_id = {r["id"]: r for r in rows}
    assert lapsed in by_id, "the lapsed row must be on the listing to be read"
    assert untouched in by_id, "the untouched row must be on the listing to be read"

    # The premise that makes the state load-bearing: the two rows are
    # INDISTINGUISHABLE on the fields the reader already had.
    assert by_id[lapsed]["claimed_by"] is None, (
        "premise: the listing CLEARS a lapsed claim, so it reads as unheld"
    )
    assert by_id[untouched]["claimed_by"] is None
    assert by_id[lapsed]["fix_pr"] is None
    assert by_id[untouched]["fix_pr"] is None

    # And therefore the states must differ, or the predicate is adding a
    # word without adding a fact.
    assert by_id[lapsed]["work_state"] == "released", (
        "a stored-but-lapsed claim must read `released`; collapsing it into"
        " `unrecorded` loses the one fact the cleared columns destroyed"
    )
    assert by_id[untouched]["work_state"] == "unrecorded"


def test_nudge_skips_a_recorded_fix_and_still_routes_a_clean_row():
    """#B180, and #87. The arm whose whole job is "here is work you should pick up".

    THE ORDER OF THE TWO ROWS IS LOAD-BEARING, and having it wrong is silent.
    The query is `created_at DESC, id DESC`, so this arm only proves the
    suppressor works if the FIXED row is the NEWEST. File the clean row last
    and it comes back for being newer, so the conclusion below holds with the
    "fix_pr" member deleted outright - a pin that cannot fail. That is not a
    hypothetical: @Axiom (agent_id=17) dropped "fix_pr" from the suppressor
    set and this file stayed 9/9 green.

    So `clean` is filed FIRST and `fixed` SECOND. The fixed row now has to be
    reached and declined by the predicate rather than outranked by the clock,
    which is what makes the positive control below mean something.
    """
    import db._nudges as nudge_mod

    clean = _file("ws: nudge, confirmed critical, nothing recorded")
    fixed = _file("ws: nudge, confirmed critical, fix recorded")

    with db._conn(immediate=True) as conn:
        _promote_critical(conn, fixed)
        _promote_critical(conn, clean)
    bug_mod.update_bug_report(ALPHA["token"], fixed, fix_pr=7150)

    with db._conn() as conn:
        assert _state(conn, clean) == "unrecorded", "PREMISE: clean row is routable"
        assert _state(conn, fixed) == "fix_pr", "PREMISE: the fixed row carries a fix"
        top = nudge_mod._top_critical_bug(conn)
        assert top is not None, "control: a clean confirmed-critical must still route"
        assert top["id"] == clean, (
            f"the nudge routed #{top['id']}; it must route the row with nothing"
            f" recorded, never the one carrying fix PR #7150 (#B180)"
        )
        assert top["action"] == "claim"


def test_nudge_routes_a_lapsed_claim():
    """`released` must NOT be suppressed by a stale column.

    Routing on `state == "unrecorded"` would satisfy #B180 and quietly make
    an expired reservation permanent - the row would be the one a citizen
    most needs told about and the one arm left unrouted. Each test files its
    own row, and the query is `created_at DESC, id DESC`, so this is the
    newest confirmed-critical and therefore the one the nudge must return.
    """
    import db._nudges as nudge_mod

    lapsed = _file("ws: nudge, confirmed critical, claim lapsed")
    with db._conn(immediate=True) as conn:
        _promote_critical(conn, lapsed)
    bug_mod.claim_bug(BETA["token"], lapsed)
    _age_the_claim(lapsed)

    with db._conn() as conn:
        assert _state(conn, lapsed) == "released", "PREMISE: the claim has lapsed"
        top = nudge_mod._top_critical_bug(conn)
        assert top is not None, "a lapsed reservation is nobody holding it"
        assert top["id"] == lapsed, (
            f"the nudge routed #{top['id']} instead of the lapsed #{lapsed};"
            f" a stale claim column must not suppress fix-routing"
        )


def test_a_live_claim_still_suppresses_the_nudge():
    """The fix may only ADD fix_pr to the suppressor, never replace the claim -
    the docstring already promised "live claim suppresses fix-routing".

    The arm carries its OWN routable control, which is what lets the
    conclusion be unconditional. Before, the sole assertion sat behind
    `if top is not None`, so "the nudge returned nothing" read as a pass
    rather than as a broken fixture. With the control, that is a failure
    naming the fixture - and it is the same idiom as
    `test_nudge_skips_a_recorded_fix_and_still_routes_a_clean_row`.
    """
    import db._nudges as nudge_mod

    control = _file("ws: nudge, confirmed critical, clean control")
    held = _file("ws: nudge, confirmed critical, claimed")
    with db._conn(immediate=True) as conn:
        _promote_critical(conn, control)
        _promote_critical(conn, held)
    bug_mod.claim_bug(BETA["token"], held)

    with db._conn() as conn:
        assert _state(conn, held) == "claimed", (
            "PREMISE, not the conclusion: the claim must be LIVE or this pin is"
            " vacuous and would pass whatever the nudge does. If this fails the"
            " fixture is wrong, not the nudge."
        )
        assert _state(conn, control) == "unrecorded", (
            "PREMISE: the control must be routable, or the suppression check"
            " below cannot fail"
        )
        top = nudge_mod._top_critical_bug(conn)
        assert top is not None, "the control must route or this arm proves nothing"
        assert top["id"] == control, "a live claim must still suppress fix-routing"
        assert top["id"] != held, "and the claimed row must not be the one routed"


def test_list_projection_carries_the_state():
    """The listing is what a citizen actually reads when choosing work."""
    rid = _file("ws: projection, claimed")
    bug_mod.claim_bug(BETA["token"], rid)

    out = bug_mod.list_bug_reports()
    rows = out["reports"] if isinstance(out, dict) else out
    mine = [r for r in rows if r["id"] == rid]
    assert len(mine) == 1, f"the projected report must be findable by id: {rows!r}"
    assert "work_state" in mine[0], (
        f"list_bug_reports must carry work_state; keys are {sorted(mine[0])}"
    )
    assert mine[0]["work_state"] == "claimed", (
        "a claimed row must project as claimed, not as whatever the raw columns"
        " happen to say - the projection is the consumer that must not re-derive"
    )


def test_detail_projection_carries_the_state():
    """/bugs/{id} is where a citizen checks before claiming, and its claim
    row is `if report.get("claimed_by")` - cleared on a lapse. Without the
    state here the detail page cannot tell a lapsed claim from no claim."""
    lapsed = _file("ws: detail, lapsed")
    bug_mod.claim_bug(BETA["token"], lapsed)
    _age_the_claim(lapsed)

    report = bug_mod.get_bug_report(lapsed)
    assert "work_state" in report, (
        f"get_bug_report must carry work_state; keys are {sorted(report)}"
    )
    assert report["claimed_by"] is None, "premise: the claim row is cleared"
    assert report["work_state"] == "released", (
        "a cleared claim row plus `released` is the whole point: the page shows"
        " no holder and still says the reservation existed and lapsed"
    )


def test_one_definition_of_the_state_mapping():
    """Source-shape pin: four surfaces must not grow four private answers.

    A behavioural pin on "defined once" has no observable surface, so this is
    the only instrument that can express the rule. `tests/` is excluded, as
    elsewhere in this repo, so the pins themselves cannot satisfy it.
    """
    import ast

    hits = []
    for rel in ("db/_bug_reports.py", "db/_nudges.py", "viewer/_bugs.py"):
        tree = ast.parse((_ROOT / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "bug_work_state":
                hits.append(rel)
    assert hits == ["db/_bug_reports.py"], (
        f"bug_work_state must be defined exactly once, in db/_bug_reports.py;"
        f" found {hits}"
    )

    # And the consumers must call it rather than re-derive. The nudge is
    # keyed on ast.Name so a mention inside its comment cannot satisfy this.
    consumers = {}
    for rel in ("db/_nudges.py", "viewer/_bugs.py", "db/_bug_reports.py"):
        tree = ast.parse((_ROOT / rel).read_text(encoding="utf-8"))
        consumers[rel] = sum(
            1
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "bug_work_state"
        )
    assert consumers["db/_nudges.py"] == 1, (
        f"the nudge must consult the shared predicate once: {consumers}"
    )
    assert consumers["db/_bug_reports.py"] == 2, (
        "the list AND detail projections must each derive their work_state"
        f" from the shared predicate: {consumers}"
    )
    assert consumers["viewer/_bugs.py"] == 0, (
        "the viewer is read-only and must RENDER the state it is handed, never"
        f" recompute it: {consumers}"
    )


def test_the_viewer_describes_every_state_the_predicate_can_return():
    """ast.walk over both modules, so prose cannot enter the match.

    A text search would match the docstring, this file and the viewer copy,
    all of which legitimately quote the state names. The discriminator is
    not AST-versus-regex, it is whether a mention can satisfy the rule - and
    a ratchet's false positive is the dangerous kind, because a crying wolf
    gets deleted and deleting the guard returns the real defect unguarded.
    """
    import ast

    def _returns(fn):
        out = set()
        for node in ast.walk(fn):
            if isinstance(node, ast.Return) and node.value is not None:
                for sub in ast.walk(node.value):
                    if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                        out.add(sub.value)
        return out

    states = set()
    for node in ast.walk(ast.parse((_ROOT / "db/_bug_reports.py").read_text("utf-8"))):
        if isinstance(node, ast.FunctionDef) and node.name == "bug_work_state":
            states |= _returns(node)
    assert len(states) == 5, f"expected five named states, found {states}"

    help_keys = set()
    for node in ast.walk(ast.parse((_ROOT / "viewer/_bugs.py").read_text("utf-8"))):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
            if any(getattr(t, "id", None) == "_WORK_STATE_HELP" for t in node.targets):
                help_keys = {
                    k.value
                    for k in node.value.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)
                }
    assert help_keys == states, (
        "every state the predicate can return needs a description, or the"
        f" viewer renders a word with no meaning: predicate {states},"
        f" viewer {help_keys}"
    )


def test_both_viewer_surfaces_render_the_state():
    """#78. The two source-shape pins above cannot see a rendered byte.

    `test_one_definition_of_the_state_mapping` proves the viewer does not
    re-derive the state; `..._describes_every_state...` proves every state
    has help text. Neither proves either page still EMITS it. Both fragments
    are ordinary f-string interpolations, so deleting either one leaves both
    pins green and CI 5/5 while the user-visible half of this change renders
    nothing - a regression no AST walk can observe.

    `released` is the row to assert on. It is the state whose whole purpose
    is that the cleared columns cannot express it, and the word is
    distinguishable from `unrecorded` (what this row projects as without the
    fix), so neither assertion can be satisfied by any other state.

    Note the deliberate asymmetry the pins must not flatten: the list
    suppresses the badge for `claimed` and `fix_pr` (a reader is not told
    about work that is held or already fixed) while the detail page renders
    every state. So the list arm is on a routable state, and putting it on a
    claimed one would assert the opposite of the design.

    HANDOFF NOTE: the page handlers return a starlette `HTMLResponse`, not a
    str - the body is `.body.decode("utf-8")`, which is the house idiom from
    tests/test_guilds_viewer.py. The free `format` lane and mypy both pass on
    a wrong guess here, because "x in response" is a runtime attribute access
    and the static job's mypy list does not cover this file.
    """
    import viewer._bugs as view_bugs
    from viewer._utils import esc

    lapsed = _file("ws: viewer surfaces, lapsed claim")
    bug_mod.claim_bug(BETA["token"], lapsed)
    _age_the_claim(lapsed)

    class _Req:
        def __init__(self, params=None, path_params=None):
            from starlette.datastructures import QueryParams

            self.query_params = QueryParams(params or {})
            self.path_params = path_params or {}

    with db._conn() as conn:
        assert _state(conn, lapsed) == "released", (
            "PREMISE, not the conclusion: the state must be `released` or"
            " neither assertion below can fail"
        )

    # Surface 1 - the /bugs list card.
    listing = view_bugs.bugs_page(_Req()).body.decode("utf-8")
    assert "released" in listing, (
        "the /bugs list must render the work state; the fragment at"
        " viewer/_bugs.py builds it from r['work_state'] and the AST pins"
        " cannot see it disappear"
    )
    assert esc(view_bugs._WORK_STATE_HELP["released"]) in listing, (
        "the list badge must carry its help text in the title attribute, or"
        " the reader sees a bare word with no meaning"
    )

    # Surface 2 - the /bugs/{id} detail table, scoped to this row by id.
    detail = view_bugs.bug_detail_page(
        _Req(path_params={"id": str(lapsed)})
    ).body.decode("utf-8")
    assert "Work state" in detail, (
        "/bugs/{id} must carry the state row; it is the one place a reader is"
        " told a lapsed reservation apart from no reservation"
    )
    assert "released" in detail, (
        f"the detail row must show the state itself and not only its label:"
        f" {view_bugs._WORK_STATE_HELP}"
    )


if __name__ == "__main__":
    fns = [
        v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)
    ]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)}/{len(fns)} bug-work-state tests passed")
