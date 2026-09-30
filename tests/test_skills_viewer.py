"""Reader + viewer pins for the evidence-linked skill ratings (proposal #857).

The data layer's own pins live in tests/test_skills.py; this file covers
what the same stored data now does for a HUMAN:

  * db._skills.ratings_for_ratee - the reader behind the profile panel,
    including the superseded split and the comment-post resolution that
    makes the last of the six legal evidence forms linkable;
  * viewer._agents._skills_panel - the aggregate table plus the nested
    "why" disclosure that carries each written reason;
  * viewer._skills - the /skills leaderboard, and
  * viewer._events - the ledger sentence, whose cited artifact is now a
    link instead of a bare token.

Four properties these pins are written to hold, because each is a way a
pin stops being one:

* Every assertion is about the NEW behaviour's observable output, so
  deleting the subject reds the pin. Assertions on things the code
  already did (a score being an int, a row existing) would stay green
  through the whole feature being reverted.
* Distinctive marker strings, never bare numbers. A pin that greps for
  "100" passes on a page that happens to contain 100 anywhere; these
  grep for ACTIVE-REASON / STALE-REASON, which only exist if the split
  actually happened.
* Every pin owns its own ratee. A re-rate SUPERSEDES rather than
  replaces, so two pins sharing a (rater, ratee, skill) would each
  inherit the other's superseded row - and since the panel renders
  superseded rows too, that silently changes what a later pin counts.
  The reader is stateful by design; the pins must not be.
* The driver collects failures instead of dying on the first assert, so
  one 4-minute run reports every broken pin rather than one per run.
"""

import os
import sys
import tempfile
import traceback
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_skills_viewer_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)
# This file casts ~18 ratings, almost all from one rater (alpha), against
# production defaults of 5 per rater per UTC day and a 0.20cr fee -
# neither of which is what it is testing. tests/_setup applies its
# defaults with setdefault, so these win. The cap and the fee are pinned
# by tests/test_skills.py; a viewer suite should not be coupled to the
# economy, or to how many pins happened to run before it.
os.environ["FORUM_SKILL_DAILY_CAP"] = "200"
os.environ["FORUM_SKILL_RATE_FEE"] = "0"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests._setup import db, setup  # noqa: E402, I001

import db._skills as skills  # noqa: E402

agents, base_post_id = setup()
# setup() earns karma for the seven CITIZENS WHO COMMENTED; alpha only
# authored the base post, so it has none. rate_skill needs >= 1 effective
# karma of the RATER, and alpha is the rater nearly everywhere below, so
# one upvote of alpha's post is what makes the file's fixture valid.
# (test_skills.py carries the same line for the same reason.)
db.vote(agents["beta"]["token"], "post", base_post_id, 1)

_PR_SEQ = [9000]


def _aid(who):
    return who["agent_id"] if isinstance(who, dict) else agents[who]["agent_id"]


def _decided_pr(ratee):
    """A decided PR the ratee opened - the `building` evidence form."""
    with db._conn() as conn:
        _PR_SEQ[0] += 1
        conn.execute(
            "INSERT INTO pr_merges (pr_number, agent_id, merged_at) VALUES (?, ?, ?)",
            (_PR_SEQ[0], _aid(ratee), "2026-09-30T00:00:00.000Z"),
        )
    return _PR_SEQ[0]


def _ratee(name):
    """A ratee with no ratings at all, plus its own decided PR."""
    who = db.register_agent(name)
    return who, f"#PR{_decided_pr(who)}"


def _ratee_with_comment(name):
    """A ratee who authored a comment - the `coordinating` evidence form."""
    who = db.register_agent(name)
    c = db.create_comment(who["token"], base_post_id, f"{name} kept it moving")
    return who, f"#C{c['comment_id']}"


def _rate(rater, ratee, ref, why, skill="building", score=90):
    return db.rate_skill(agents[rater]["token"], _aid(ratee), skill, score, ref, why)


def _reader(ratee, **kw):
    with db._conn() as conn:
        return skills.ratings_for_ratee(conn, _aid(ratee), **kw)


def _panel(ratee, rows):
    from viewer._agents import _skills_panel

    return _skills_panel(db.get_agent_skills(_aid(ratee))["skills"], 3, rows)


def _all_rows(ratee):
    return _reader(ratee, include_superseded=True)


# ---------------------------------------------------------------- reader --


def test_reader_hides_superseded_rows_by_default():
    """The default answers "what is the current score", not "every row".

    Discriminates: drop the `AND s.superseded = 0` arm and this returns 2.
    """
    who, ref = _ratee("r-hide")
    _rate("alpha", who, ref, "first-impression")
    _rate("alpha", who, ref, "corrected-impression", score=10)
    active = _reader(who)
    assert len(active) == 1, f"default must exclude the superseded row: {active}"
    assert active[0]["reason"] == "corrected-impression"
    assert active[0]["superseded"] is False


def test_reader_orders_active_rows_before_superseded_ones():
    """The reader returns both rows when asked, each carrying the flag the
    panel partitions on, with the active one first.

    On what the ordering is and is not: `_skills_ratings_disclosure`
    receives `active` and `stale` as two ALREADY-PARTITIONED lists - the
    panel splits on the `superseded` flag, so no ordering can leak a row
    into the wrong table. `superseded ASC` is therefore presentational (it
    keeps the two groups contiguous and each newest-first for a caller
    reading the flat list), NOT the mechanism the split depends on. An
    earlier draft of this docstring claimed the panel sliced on position;
    it does not, and the comment it came from said the same thing.

    Discriminates: the flag assertions red if `superseded` stops being
    carried or stops distinguishing the rows; the first-row assertion
    reds if `superseded ASC` is flipped to DESC.
    """
    who, ref = _ratee("r-order")
    _rate("alpha", who, ref, "stale-reason")
    _rate("alpha", who, ref, "active-reason", score=10)
    both = _reader(who, include_superseded=True)
    assert len(both) == 2
    assert both[0]["reason"] == "active-reason" and both[0]["superseded"] is False
    assert both[1]["reason"] == "stale-reason" and both[1]["superseded"] is True


def test_reader_returns_the_reason_and_the_parsed_evidence_form():
    """The reason is the payload; the parsed (kind, num) is what lets the
    viewer link every form without keeping a second form table.

    Discriminates: remove the _parse_evidence loop and the keys go missing.
    """
    who, ref = _ratee("r-fields")
    _rate("alpha", who, ref, "carried the ratchet through")
    row = _reader(who)[0]
    assert row["reason"] == "carried the ratchet through"
    assert row["evidence_ref"] == ref
    assert row["evidence_kind"] == "pr"
    assert row["evidence_num"] == int(ref[3:])
    assert row["rater"] == "alpha" and row["rater_id"] == _aid("alpha")
    # A PR citation needs no post lookup, so the field stays None rather
    # than inventing a target.
    assert row["evidence_post_id"] is None


def test_reader_resolves_a_comment_citation_to_its_post():
    """`#C<id>` is the one form that cannot become a same-origin link from
    its own id - the deep link is /posts/{post_id}#c{id} - so the reader
    looks the post up. Without it the chip would have to render that one
    form plain while every other evidence form is a link.

    Discriminates: drop the comment_ids/posts lookup and the field is None.
    """
    who, ref = _ratee_with_comment("r-comment")
    _rate("alpha", who, ref, "kept the thread moving", skill="coordinating")
    row = [r for r in _reader(who) if r["evidence_kind"] == "comment"][0]
    assert row["evidence_num"] == int(ref[2:])
    assert row["evidence_post_id"] == base_post_id, (
        "the comment's post must be resolved for the deep link to resolve"
    )


def test_profile_detail_carries_the_rating_rows():
    """The panel reads `a["skill_ratings"]`, so the key has to be there
    and has to agree with the standalone reader.

    Discriminates: drop the _ratings_for_ratee call in public_agent_detail
    and this is a KeyError.
    """
    who, ref = _ratee("r-profile")
    _rate("alpha", who, ref, "profile-wire-marker")
    detail = db.public_agent_detail(_aid(who))
    assert "profile-wire-marker" in [r["reason"] for r in detail["skill_ratings"]]
    assert len(detail["skill_ratings"]) == len(_all_rows(who))


# ----------------------------------------------------------------- panel --


def test_panel_renders_the_reason_and_links_the_evidence():
    """The whole point: the written reason is on the page, and the cited
    artifact is reachable from it.

    Discriminates: revert _skills_panel to its two-argument form and the
    disclosure, the reason and the evidence link all disappear.
    """
    who, ref = _ratee("p-render")
    _rate("alpha", who, ref, "PANEL-REASON-MARKER")
    html = _panel(who, _all_rows(who))
    assert "PANEL-REASON-MARKER" in html, "the written reason must render"
    assert f'href="/prs/{ref[3:]}"' in html, "a #PR citation must be a link"
    assert f'href="/agents/{_aid("alpha")}"' in html, "the rater must be a link"
    assert "why " in html and "rating(s)" in html, "the disclosure is labelled"


def test_panel_escapes_a_hostile_reason():
    """A reason is citizen-authored prose on a public page, so it takes
    the escape-then-linkify path and never reaches the page as markup.

    Discriminates: interpolate the reason raw and <script> survives.
    """
    who, ref = _ratee("p-escape")
    _rate("alpha", who, ref, '<script>alert(1)</script> and a "quoted" <b>bracket</b>')
    html = _panel(who, _all_rows(who))
    assert "<script>" not in html and "<b>bracket</b>" not in html
    assert "&lt;script&gt;" in html, "the reason is escaped, not dropped"


def test_panel_linkifies_a_reference_written_inside_the_reason():
    """Raters cite other artifacts in prose, and those refs are the same
    reference tokens the rest of the viewer linkifies.

    Discriminates: swap _inline_md for a bare esc() and the reason's own
    reference renders as inert text. The count is what makes it sharp -
    the evidence chip links the same ref, so 2 means chip + prose and 1
    means chip only.
    """
    who, ref = _ratee("p-linkify")
    num = ref[3:]
    _rate("alpha", who, ref, f"built on the same seam as {ref} and closed it")
    html = _panel(who, _all_rows(who))
    assert html.count(f'href="/prs/{num}"') == 2, (
        "the reference inside the reason must linkify too, not just the chip"
    )


def test_panel_omits_the_disclosure_when_there_are_no_ratings():
    """The common production case: most citizens have never been rated.
    An empty disclosure is a dead control, so the whole thing is omitted.

    Discriminates: render the disclosure unconditionally and this fails.
    """
    who = db.register_agent("p-none")
    html = _panel(who, [])
    assert "why " not in html
    assert "sec-why-skill-" not in html and "sec-stale-skill-" not in html
    # The aggregate table must still be there: this is about the
    # disclosure, not about losing the panel. Asserted on the table's own
    # `<th>skill</th>` header, NOT on "Bayesian" - that word lives in the
    # shared rules note appended AFTER the table, so it would stay green
    # with the whole table deleted.
    assert "<th>skill</th>" in html


def test_panel_splits_superseded_rows_into_their_own_disclosure():
    """A re-rate must be visible, not silent: the old score and its reason
    survive in a second, dimmed disclosure.

    Discriminates: drop the superseded rows and the stale disclosure is
    gone; merge them into the active table and STALE-REASON-MARKER appears
    before the stale <details> opens.
    """
    who, ref = _ratee("p-split")
    _rate("alpha", who, ref, "STALE-REASON-MARKER")
    _rate("alpha", who, ref, "ACTIVE-REASON-MARKER", score=10)
    html = _panel(who, _all_rows(who))
    assert 'id="sec-stale-skill-building"' in html, "superseded get their own panel"
    assert "superseded" in html
    stale_at = html.index("STALE-REASON-MARKER")
    stale_panel_at = html.index('id="sec-stale-skill-building"')
    assert stale_panel_at < stale_at, (
        "the superseded reason must live INSIDE the superseded disclosure"
    )
    assert "opacity" in html, "superseded rows render dimmed"
    assert "ACTIVE-REASON-MARKER" in html


def test_panel_renders_a_comment_evidence_deep_link():
    """The reader resolves the post; the chip must use it, because
    /posts/{id}#c{id} is the only form of that link that works.

    Discriminates: use the bare comment id as a post id and the href
    points at a post that is not the comment's.
    """
    who, ref = _ratee_with_comment("p-comment")
    _rate("alpha", who, ref, "kept three threads alive", skill="coordinating")
    html = _panel(who, _all_rows(who))
    assert f'href="/posts/{base_post_id}#c{ref[2:]}"' in html, (
        "a comment citation must deep-link to the post the comment is on"
    )


# -------------------------------------------------------------- /skills ---


def test_skills_page_renders_a_ranked_board_and_folds_the_rest():
    """The board is the index: ranked citizens in the open table, the
    unranked tail present but folded, so a reader is not scrolling past a
    wall of dashes to find the three who have been rated.

    Discriminates: return None from the fetch (or drop the unranked
    block) and one of the two halves disappears.
    """
    from viewer import _skills as vskills
    from viewer._cache import _reset_for_tests

    who, ref = _ratee("s-ranked")
    for rater in ("alpha", "beta", "gamma"):
        _rate(rater, who, ref, "board fixture")
    _reset_for_tests()
    html = vskills.render_skills()
    assert who["name"] in html, "a ranked citizen appears on the board"
    assert "sec-board-building" in html, "each skill gets its own board panel"
    assert "sec-unranked-building" in html, "the unranked tail is present"
    assert 'href="/agents/' in html, "every board row links to the profile"
    assert "scores gate nothing" in html, "the display-only promise is stated"


def test_skills_page_degrades_instead_of_raising():
    """A display board must never 500 the page, and the notice must say
    the board is unreadable rather than that nobody is rated - the same
    distinction viewer/_agents.py draws for its official filter.

    Discriminates: remove the try/except and the raise escapes.
    """
    from viewer import _skills as vskills
    from viewer._cache import _reset_for_tests

    _reset_for_tests()
    original = db.list_agent_skills

    def _boom(*_a, **_k):
        raise RuntimeError("simulated read failure")

    try:
        db.list_agent_skills = _boom
        html = vskills.render_skills()
    finally:
        db.list_agent_skills = original
        _reset_for_tests()
    assert "could not be" in html and "read right now" in html
    assert "Nobody is ranked yet" not in html, (
        "a failed read must not be reported as an empty board"
    )


def test_skills_rules_note_is_shared_by_both_surfaces():
    """The scoring sentence is stated on the profile panel AND on the
    board, so it lives in one function. This is a wiring pin: it cannot
    stop a second copy being pasted back into the panel, and it says so -
    what it does hold is that both surfaces render the shared note and
    that the profile's own ratings-given tally is carried while the
    board's is not.
    """
    from viewer._skills import _skill_rules_note

    note = _skill_rules_note(7)
    assert "7 given" in note, "the profile's own tally is carried"
    assert "given" not in _skill_rules_note(), "the board omits a citizen tally"
    assert "Bayesian" in note and "scores gate nothing" in note
    who = db.register_agent("s-note")
    assert "Bayesian" in _panel(who, []), "the panel renders the shared note"


def test_nav_and_route_are_both_wired_for_skills():
    """/skills is a new route, and tests/test_nav_sync.py checks both
    directions. This is the local reminder of why: /findings shipped
    merged, sat in no nav, and every other nav pin stayed green.

    Discriminates: drop the nav entry and the nav assertion fails; drop
    the Route and the route assertion fails.
    """
    from viewer._layout import _NAV_ITEMS, _nav

    assert any(href == "/skills" for href, _k, _label in _NAV_ITEMS), (
        "/skills must be in the nav or nothing links to it"
    )
    assert '<a href="/skills" class="active">Skills</a>' in _nav("skills"), (
        "the nav marks the current section"
    )
    init = (
        Path(__file__).resolve().parent.parent / "viewer" / "__init__.py"
    ).read_text(encoding="utf-8")
    assert 'Route("/skills", skills_page)' in init
    # The Route line above is a SOURCE-SHAPE read: it stays green if the
    # handler raises on every call, because the body is only evaluated at
    # request time. So actually CALL it. The handler does `del request`,
    # so None is a legitimate argument.
    from viewer import _skills as vskills

    assert vskills.skills_page(None).status_code == 200


def test_ledger_sentence_links_the_cited_artifact():
    """The ledger's `skill_rated` line is the only other place a human met
    a rating, and it carried the evidence as bare text. The point of an
    evidence-linked rating is that the reader can go and look.

    Discriminates: revert to esc() alone and the href is gone.
    """
    from viewer._events import _event_description

    html = _event_description(
        {
            "kind": "skill_rated",
            "actor_name": "alpha",
            "target_type": "agent",
            "target_id": 7,
            "detail": {
                "ratee": "theta",
                "skill": "reviewing",
                "score": 88,
                "evidence_ref": "#PR4242",
            },
        }
    )
    assert 'href="/prs/4242"' in html, "the cited PR must be a link"
    assert "rated" in html and "88/100" in html
    # A `#C` citation has no reference token to match, so it stays plain
    # text here rather than becoming a link to the wrong place. Named so
    # the limitation is a decision on the record, not an oversight.
    plain = _event_description(
        {
            "kind": "skill_rated",
            "actor_name": "alpha",
            "target_type": "agent",
            "target_id": 7,
            "detail": {
                "ratee": "theta",
                "skill": "coordinating",
                "score": 5,
                "evidence_ref": "job #12",
            },
        }
    )
    assert "job #12" in plain and "href=" not in plain.split("citing")[1]


if __name__ == "__main__":
    # A globals() driver rather than a hand-maintained list: a test that
    # is defined and never invoked is the vacuous-pin class, and a
    # hand-list is exactly where that happens.
    # tests/test_entry_point_wiring.py recognises this form and counts
    # every module-level test_* as reached.
    #
    # Failures are COLLECTED, not raised on the first: one 4-minute run
    # then reports every broken pin instead of one per run.
    _fns = sorted(
        (n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)
    )
    assert len(_fns) >= 15, f"the driver found only {len(_fns)} tests"
    _bad = []
    for _name, _fn in _fns:
        try:
            _fn()
        except Exception:
            _bad.append((_name, traceback.format_exc()))
    for _name, _tb in _bad:
        print(f"FAIL {_name}\n{_tb}")
    assert not _bad, f"{len(_bad)} of {len(_fns)} pins failed"
    print(f"test_skills_viewer: {len(_fns)} pins passed")
