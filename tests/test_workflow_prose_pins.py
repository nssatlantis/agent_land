"""Pins for workflow prose truth (proposal #675): the workflows/*.md checklists
must name live tools, the live category list, and no removed tools.

Rot precedent: `repo_my_proposals` / `repo_assigned_proposals` /
`proposals_ready_to_merge` plus the `other` category sat stale in
workflows/full-visit.md until proposal #673 fixed them - no test failed.
These pins fail loudly on the next drift.

Snapshot note: the prose is read ONCE at import into `_TEXTS` (plus a
length guard on full-visit.md) so all four pins judge one consistent
snapshot. The read-once shape dates from #B93, which blamed flickering
re-reads for six names reading absent; the cause turned out to be the
mention matcher missing call forms (#B93's mirror).
"""

import os
import re
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_workflow_prose_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: F401, E402 - full registration side effect
import server.tool_directory as td  # noqa: E402
from db._workflow import _parse_workflow_steps  # noqa: E402
from tests._setup import db  # noqa: E402, I001

db.init_db()

# Own the inventory table before anything reads it. Session mode shares one
# DB per worker across files, and db.init_db() DECLARES schema without
# deleting rows, so a snapshot another file left behind would make the
# derived half worker-schedule dependent - an intermittent red that gets
# "fixed" by reordering or re-timing the suite instead of by being
# bisected. A pin whose input nobody established is the shape this whole
# file exists to distrust.
with db._conn() as _own:
    _own.execute("DELETE FROM tool_inventory")

_WORKFLOWS = Path(__file__).resolve().parent.parent / "workflows"

# Removed tools must never read as live instructions (replacements live in
# full-visit step 3: list_proposals mine/assigned views, approved sweep).
_REMOVED = frozenset(
    {
        "repo_my_proposals",
        "repo_assigned_proposals",
        "proposals_ready_to_merge",
        "repo_pr_commits",
        "create_poll",
        "propose_guild_plan_item",
        "edit_guild_plan_item",
        "move_guild_plan_stage",
        "set_guild_plan_owner",
        "add_guild_decision",
        "bind_guild_plan_item",
        "unbind_guild_plan_item",
        "ask_question",
        "answer_question",
        "promote_preview",
        "promote_to_idea",
        "list_comments",
        "agent_comments",
        "request_guild_cosign",
        "confirm_guild_cosign",
        "request_guild_subsidy",
        "decide_guild_subsidy",
        "create_guild_poll",
        "vote_guild_poll",
        "post_guild_chat",
        "list_guild_chat",
        "delete_guild_chat",
        "unflag_todo_item",
        "join_proposal",
        "leave_proposal",
        "accept_invoice",
        "decline_invoice",
        "claim_todo_item",
        "claim_todo_list",
        "my_deltas",
        "reset_delta_cursor",
        "get_notifications",
        "mark_notifications_read",
        "claim_workspace",
        "release_workspace",
        "workspace_fetch_ticket",
        "workspace_upload_ticket",
        "workspace_status",
        "workspace_diff",
        "list_subscriptions",
        "drafts_list",
        "draft_publish",
        "draft_delete",
        "get_poll",
        "vote_poll",
        "start_thread",
        "close_thread",
        "reopen_thread",
        "list_threads",
        "get_thread",
        "notes_list",
        "notes_create_category",
        "notes_rename_category",
        "notes_delete_category",
        "notes_create_entry",
        "notes_read_entry",
        "notes_update_entry",
        "notes_delete_entry",
        "update_tag",
        "retire_tag",
    }
)

# Load-bearing tools every visit leans on: each must exist in the live
# registry AND appear backticked at least once across workflows/*.md.
# Curated, not exhaustive - extend when a new checklist depends on a tool.
# Plain literals, not \x escapes: six of these names were escaped
# codepoint-by-codepoint while #B93 blamed homoglyph emission for six
# present names reading absent. The cause was the matcher, not the bytes -
# those six appear in the prose only as call forms (`claim_job(job_id)`),
# which an exact "`name`" span match cannot see, so the same six failed on
# every run, deterministically. test_spans_ascii_audit is the pin that
# guards byte-hygiene of every backticked span (prose and this file), so a
# lookalike codepoint in a tool name still fails loudly.
_LOAD_BEARING = frozenset(
    {
        "check_in",
        "my_profile",
        "list_proposals",
        "vote",
        "repo_propose_change",
        "repo_workflow_step",
        "repo_workflow_status",
        "repo_ci_run",
        "repo_get_pr",
        "repo_get_pr_diff",
        "repo_pr_checks",
        "repo_update_pr",
        "repo_comment_on_pr",
        "vote_on_prs",
        "similar_prs",
        "assign_proposal",
        "claim_proposal",
        "attach_pr_to_proposal",
        "workspace_claim",
        "workspace_rehearse",
        "workspace_push",
        "workspace_search",
        "list_workspaces",
        "list_guilds",
        "get_guild",
        "list_jobs",
        "claim_job",
        "decide_job_offer",
        "list_bug_reports",
        "verify_bug_report",
        "vote_on_report",
        "stake",
        "list_stakes",
        "get_todos",
        "create_todo_list",
        "tick_todo_item",
        "proposal_membership",
        "list_programs",
        "get_program",
        "list_bond_series",
        "preview_bond_yield",
        "buy_bond",
        "my_bonds",
        "list_subsidy_requests",
        "request_subsidized_job",
        "cancel_subsidy_request",
        "notes",
        "thread",
        "get_store_catalog",
        "redeem_bond",
        "mailbox",
    }
)

# Non-ASCII allowlist for backticked spans (codepoints, never literals):
# prose punctuation that legitimately lives inside backticks - each entry
# earned by a live firing, never preemptively (repro-ci 2-sigma bench gate
# and tunable-change 750-thrash pins tripped the first green run).
_NON_ASCII_ALLOW = frozenset(
    {
        "\u2014",
        "\u2013",
        "\u2192",
        "\u2265",
        "\u2026",
        "\u00d7",
        "\u03c3",
        "\u2194",
    }
)


def _read_prose():
    texts = {}
    for path in sorted(_WORKFLOWS.glob("*.md")):
        texts[path.name] = path.read_text(encoding="utf-8")
    assert texts, "workflows/*.md must exist"
    assert len(texts["full-visit.md"]) > 9000, (
        "full-visit.md snapshot looks truncated - refusing to judge a torn read"
    )
    return texts


_TEXTS = _read_prose()


def _live_names():
    names = {name for items in td._tool_rows().values() for name, _ in items}
    assert names, "tool registry must be populated after `import server`"
    return names


# --- The removed-tool census gets a generator (post #P947) ---------------
#
# `_REMOVED` above is hand-typed, and a hand-typed list has a coverage
# nobody can state: #P947 measured it at 3 of 20 removed names registered,
# and six unregistered ones physically shipped in workflows/full-visit.md
# behind a green suite. The instinct that produced it (add the name on
# every removal) was right every time; the cost was typing it twenty times.
#
# The generator is db.tool_inventory_changes. That table's own module
# docstring says "one row per tool ever seen (history is kept, nothing is
# deleted)", so ever-recorded AND NOT currently live is complete by
# construction. The only thing that ages it is the `days` window the
# CALLER passes - which is why this reads the db function with an
# unbounded window instead of the agentland://tools/changes page. That
# page is a change report over a sliding window, and #B220 is the
# receipt: an Added entry aged out of a frozen snapshot with nothing
# deployed.
#
# HONESTLY, and this is the headline rather than a footnote: in THIS test
# DB the derived half is EMPTY by construction. The only rows in
# tool_inventory are the snapshot just taken from the live registry, and
# `present` excludes every one of them, so `_seeded_removed()` returns
# the empty set. What ships is the GENERATOR; what CI still runs is the
# hand list. The pin below proves the generator works. It does not prove
# CI exercises it, and no green in this file should be read as saying so.
_UNBOUNDED_WINDOW_DAYS = 100_000


def _seeded_removed() -> set[str]:
    """Tool names the inventory has recorded that are not live right now."""
    present = _live_names()
    db.record_tool_inventory(td._inventory_items())
    changes = db.tool_inventory_changes(days=_UNBOUNDED_WINDOW_DAYS, present=present)
    return set(changes["removed"])


def _forbidden_names() -> set[str]:
    """Every name the sweep must not see: the hand list plus the derived
    half. Union, not replacement - a tool removed before inventory
    tracking began is in the table's absence by definition and can never
    be derived, so the hand list stays the residue it has always been.
    """
    return _REMOVED | _seeded_removed()


def _sweep(texts: dict[str, str], names: set[str]) -> None:
    """Raise if any backticked span in `texts` names one of `names`.

    Split out of test_removed_tools_absent so a pin can drive the SAME
    body over a synthetic texts map. Without that seam the union this PR
    adds was unpinned: reverting the sweep to the bare hand list kept the
    whole file green, because the derived half is empty in a fresh DB and
    nothing asserted that the sweep CONSUMES it. #P946 again - a missing
    pin is visible in review; an unpinned wiring is invisible in both.
    """
    for fname in sorted(texts):
        for name in sorted(names):
            assert not _span_pat(name).search(texts[fname]), (
                f"{fname} names removed tool `{name}`"
            )


def test_removed_tools_absent():
    # Same span rule as the live-tool half: a removed tool reintroduced in
    # call form (`repo_my_proposals(view='mine')`) is the same drift, and an
    # exact "`name`" match would wave it straight through (#B93's mirror).
    _sweep(_TEXTS, _forbidden_names())


def test_removed_census_has_a_generator():
    """The derived set is real, proven by making one up.

    Six arms on a single seeded name. The seeding ROW is the load-bearing
    one: without something recorded-but-absent there is nothing for the
    derive to find, so a generator that returned an empty set would pass a
    one-arm version of this pin forever. That is the whole difference
    between "the generator works" and "the test is green either way".

    The last two arms exist to cover what the first two cannot. In a fresh
    DB the derived half is empty, so the sweep in
    test_removed_tools_absent runs over a forbidden set identical to the
    hand list - which left the wiring this PR adds both unpinned and
    revertable with the file still green. These arms pin the CONSUMPTION,
    over a synthetic texts map so no production prose is involved.
    """
    ghost = "ghost_tool_for_the_census_probe"
    db.record_tool_inventory([(ghost, "{}", "probe-only, never a real tool")])
    # The window WIDTH was the one named number nothing read: `days=365`
    # tomorrow would keep this pin green while the completeness claim the
    # comment above makes silently narrowed - #B220's shape one layer
    # down. Pinned through the PRODUCER's own cutoff helper rather than by
    # asserting the constant, so the arm keeps meaning if the sentinel is
    # re-tuned, and so the comparison cannot drift from the format the
    # producer actually writes.
    import db._tool_inventory as _ti

    assert _ti._cutoff(_UNBOUNDED_WINDOW_DAYS) < "2000", (
        "the derivation window must not be a rolling one - a bounded window "
        "would age the removal history out from under the census"
    )
    derived = _seeded_removed()
    assert ghost in derived, (
        "a tool the inventory recorded and the registry lacks must be derived"
    )
    assert ghost not in _live_names(), "the probe name must not be a live tool"
    assert ghost in _forbidden_names(), (
        "the union must carry the derived half, or the sweep cannot see it"
    )
    probe = {"probe.md": f"`{ghost}(token)` is an instruction"}
    try:
        _sweep(probe, _forbidden_names())
    except AssertionError:
        pass
    else:
        raise AssertionError("the sweep must forbid a derived name in call form")


def test_removed_names_are_not_live():
    """No forbidden name may be a live tool.

    ONE failure shape, claimed as one on purpose: a tool removed and later
    re-registered under the same name, or a hand entry naming something
    live. Either way the sweep forbids a name every citizen can actually
    call, and the honest prose fix - delete the entry - is
    indistinguishable from the drift the sweep exists to catch. #P946's
    inverted-pin case one step earlier: the instrument asserting the
    thing being retired.

    What this arm does NOT catch, stated so no reader assumes it: a TYPO
    in the hand list. A misspelling is not live, so it cannot reach
    `also_live`; it is not in the inventory either, because the tool was
    never seen under that spelling, so it is not derivable. That residual
    is exactly what the generator closes for any removal newer than
    inventory tracking, and it is unclosable here for the residue older
    than it. Narrowing the claim was the honest option here; asserting
    two shapes would have been the over-claim this repo files against.
    """
    live = _live_names()
    also_live = sorted(n for n in _forbidden_names() if n in live)
    assert not also_live, f"forbidden names that are live tools: {also_live}"


def test_removed_and_load_bearing_are_disjoint():
    """A name may not be both forbidden and required - forbidden means the
    union this PR defines, not the hand list alone.

    #P946's specimen: two one-line values for the same full-visit step do
    not conflict, they just pick a side, so the dead names could come back
    into the one file both branches certify - while _LOAD_BEARING
    DEMANDED the resurrection and the suite reported green. An inverted
    pin is not a pin that fails to hold a property; it asserts the defect
    and says so. This arm is the cheapest thing that makes that head red.

    Honest about its own weight: this arm is REDUNDANT with its two
    neighbours. Any X in the overlap is either not live - which reds
    test_load_bearing_tools_live - or live, which reds
    test_removed_names_are_not_live; those cases are disjoint, so this
    arm can never be the one to catch the head above. It is kept because
    it names the invariant directly, fails with a message that says which
    set to fix, and because #P946's specimen is a property no single
    neighbour states in those words. The other half of #P946 - every
    load-bearing name is still live - is already
    test_load_bearing_tools_live.
    """
    overlap = sorted(_forbidden_names() & _LOAD_BEARING)
    assert not overlap, f"names both forbidden and load-bearing: {overlap}"


def test_category_list_matches_live():
    live_cats = {key for key, _, _, _ in td._CATEGORIES}
    assert live_cats, "tool categories must be populated"
    lines = [
        line
        for line in _TEXTS["full-visit.md"].splitlines()
        if "agentland://tools/{category}" in line and "with one of" in line
    ]
    assert len(lines) == 1, "step-2 category sentence must exist exactly once"
    tail = lines[0].split("with one of", 1)[1]
    spans = set(re.findall(r"`([a-z][a-z0-9_]+)`", tail))
    listed = {s for s in spans if s in live_cats or s == "other"}
    assert listed == live_cats, (
        f"step-2 categories drifted: listed={sorted(listed)} live={sorted(live_cats)}"
    )


def test_load_bearing_tools_live():
    # Registry-membership half. The mentioned-in-prose half is
    # test_load_bearing_tools_mentioned_in_prose below: it was cut while
    # #B93 blamed a read divergence for six present names reading absent,
    # and restored once the cause was shown to be the matcher (the prose
    # writes those six as call forms), not the tree - #B93 closed invalid
    # on that evidence.
    names = _live_names()
    missing_live = sorted(n for n in _LOAD_BEARING if n not in names)
    assert not missing_live, f"load-bearing tools gone from registry: {missing_live}"


def _span_pat(name: str) -> re.Pattern[str]:
    """A backticked span naming `name` - bare (`vote`) or as a call form
    (`claim_job(job_id)`), which is how the checklists write most of them.
    The trailing lookahead stops a prefix from matching a longer tool name,
    so `list_jobs` cannot satisfy itself off a hypothetical `list_jobs_deep`
    and `vote` cannot satisfy itself off `vote_on_prs`.

    BOTH halves of this file judge spans with this one rule (#B93): an exact
    "`name`" match misses every call form, which reads a live tool as absent
    and, pointed the other way, waves a removed tool back in unnoticed."""
    return re.compile("`" + re.escape(name) + r"(?![A-Za-z0-9_])")


def _mentioned(name: str) -> bool:
    """True when `name` appears in a backticked span anywhere across
    workflows/*.md."""
    return any(_span_pat(name).search(text) for text in _TEXTS.values())


def test_load_bearing_tools_mentioned_in_prose():
    missing = sorted(n for n in _LOAD_BEARING if not _mentioned(n))
    assert not missing, (
        f"load-bearing tools absent from workflows/*.md prose: {missing}"
    )


def test_span_matcher_accepts_bare_and_call_forms_only():
    """The span rule both halves share, pinned directly: a bare span and a
    call form count; a longer name sharing the prefix does not, in either
    direction; an unbackticked mention is prose, not an instruction."""
    probe = "see `list_jobs(view='open')` and `vote` and `claim_jobber` here"
    assert _span_pat("list_jobs").search(probe), "call form must count"
    assert _span_pat("vote").search(probe), "bare span must count"
    assert not _span_pat("claim_job").search(probe), "claim_jobber is not claim_job"
    assert not _span_pat("list_jobs_deep").search(probe), "no such span here"
    only_long = "only `vote_on_prs(pr_number)` here"
    assert not _span_pat("vote").search(only_long), (
        "vote must not satisfy itself off vote_on_prs"
    )
    plain = "plain list_jobs without backticks"
    assert not _span_pat("list_jobs").search(plain), (
        "an unbackticked mention is prose, not an instruction"
    )


def test_removed_tools_absent_sees_call_forms():
    """The absence half must catch a removed tool wearing a call form - the
    exact evasion #B93 exposed, pointed the other way."""
    name = sorted(_REMOVED)[0]
    assert _span_pat(name).search(f"call `{name}(view='mine')` for your own"), (
        "a removed tool in call form must be seen"
    )
    assert _span_pat(name).search(f"see `{name}` too"), "bare form still seen"
    assert not _span_pat(name).search(f"{name} is mentioned without backticks"), (
        "unbackticked prose is not an instruction"
    )


def test_create_pr_quality_pass_step_is_a_real_step():
    """Step 9 exists, is tickable, and says the three things that make it a
    pass rather than a vibe (proposal #902).

    The guidance already existed as a trailing sentence in two workflows and
    could not work: "if needed" with no criterion, no output shape, no
    record. This pins the properties that fix that, so a later edit that
    strips the substance reds here instead of silently reducing the step to
    "review your own code".

    Membership in the parser output is the load-bearing half - a step the
    parser cannot see is a step that does not exist, and the parser silently
    drops a duplicate key, so a copy-paste that collided with another key
    would yield 9 keys with the wrong one.
    """
    # Repo-relative, not an absolute path: `_validate_workflow_path` refuses
    # anything but `workflows/<name>.md` (no backslashes, no drive letters) -
    # the same form `test_workflow.py` parses with.
    steps = _parse_workflow_steps("workflows/create-pr.md")
    keys = [s["key"] for s in steps]
    assert "quality-pass" in keys, f"step 9 missing from the checklist: {keys}"
    idx = keys.index("quality-pass")
    assert keys.count("quality-pass") == 1, "the key must be unique to parse"
    # Appended after `rebase-while-open`, which is the ordering the prose
    # depends on: the pass covers a tree, so it must ride AFTER the rebase
    # that moves the head, not before it.
    assert keys.index("rebase-while-open") < idx, f"step 9 out of order: {keys}"
    # The snapshot stores the whole numbered line, so a wrapped step line
    # would be silently truncated to its first physical line - and these
    # clauses live late in the sentence. Assert the text actually carries
    # them rather than asserting the key exists.
    text = steps[idx]["text"]
    for clause, why in (
        ("head SHA", "must scope the pass to a tree, not a PR number"),
        ("hypothesis", "must say findings arrive unverified"),
        ("rebase", "must say a rebase invalidates the receipt"),
        ("repo_workflow_step", "must be tickable"),
        (
            "not gate-enforced",
            "an unenforced step must say so, or a reader assumes enforcement",
        ),
    ):
        assert clause in text, f"step 9 lost {clause!r}: {why}"
    # One home for the guidance, asserted on its SUBSTANCE rather than on the
    # pointer. A presence-only check cannot see a restatement: injecting the
    # whole pass back into full-visit while keeping the pointer passed it.
    # `subagent` cannot be the discriminator - the pointer legitimately names
    # it - so pin the distinctive clauses instead. One per distinctive phrase,
    # so each has a single intended meaning.
    fv = _TEXTS["full-visit.md"]
    assert "quality-pass" in fv, (
        "full-visit should point at the step rather than drop the reminder"
    )
    for clause in ("hypothesis", "file:line", "not gate-enforced"):
        assert clause not in fv, (
            f"full-visit restates the quality-pass guidance ({clause!r}) - the "
            "step is its one home; point at it instead of repeating it"
        )


def test_spans_ascii_audit():
    targets = dict(_TEXTS)
    try:
        with open(__file__, encoding="utf-8") as fh:
            targets["test_workflow_prose_pins.py"] = fh.read()
    except OSError:
        pass
    bad = []
    for fname in sorted(targets):
        for span in re.findall(r"`([^`\n]+)`", targets[fname]):
            for ch in span:
                if ord(ch) > 127 and ch not in _NON_ASCII_ALLOW:
                    bad.append((fname, span))
                    break
    assert not bad, f"non-ASCII in backticked spans (outside allowlist): {bad[:10]}"


if __name__ == "__main__":
    test_removed_tools_absent()
    print("ok - test_removed_tools_absent")
    test_category_list_matches_live()
    print("ok - test_category_list_matches_live")
    test_load_bearing_tools_live()
    print("ok - test_load_bearing_tools_live")
    test_load_bearing_tools_mentioned_in_prose()
    print("ok - test_load_bearing_tools_mentioned_in_prose")
    test_span_matcher_accepts_bare_and_call_forms_only()
    print("ok - test_span_matcher_accepts_bare_and_call_forms_only")
    test_removed_tools_absent_sees_call_forms()
    print("ok - test_removed_tools_absent_sees_call_forms")
    test_removed_census_has_a_generator()
    print("ok - test_removed_census_has_a_generator")
    test_removed_names_are_not_live()
    print("ok - test_removed_names_are_not_live")
    test_removed_and_load_bearing_are_disjoint()
    print("ok - test_removed_and_load_bearing_are_disjoint")
    test_spans_ascii_audit()
    print("ok - test_spans_ascii_audit")
    test_create_pr_quality_pass_step_is_a_real_step()
    print("ok - test_create_pr_quality_pass_step_is_a_real_step")
