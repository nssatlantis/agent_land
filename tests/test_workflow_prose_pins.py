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
mention matcher missing call forms (#B93 closed invalid), so the snapshot
stays for consistency - not because re-reads were ever shown to be flaky.
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
from tests._setup import db  # noqa: E402, I001

db.init_db()

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
        "claim_workspace",
        "workspace_rehearse",
        "workspace_push",
        "workspace_search",
        "list_workspaces",
        "release_workspace",
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
        "join_proposal",
        "list_programs",
        "get_program",
        "list_bond_series",
        "preview_bond_yield",
        "buy_bond",
        "my_bonds",
        "list_subsidy_requests",
        "request_subsidized_job",
        "cancel_subsidy_request",
        "notes_list",
        "notes_create_entry",
        "notes_create_category",
        "notes_read_entry",
        "notes_update_entry",
        "get_store_catalog",
        "redeem_bond",
        "get_notifications",
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


def test_removed_tools_absent():
    # Same span rule as the live-tool half: a removed tool reintroduced in
    # call form (`repo_my_proposals(view='mine')`) is the same drift, and an
    # exact "`name`" match would wave it straight through (#B93's mirror).
    for fname in sorted(_TEXTS):
        for name in sorted(_REMOVED):
            assert not _span_pat(name).search(_TEXTS[fname]), (
                f"{fname} names removed tool `{name}`"
            )


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
    test_spans_ascii_audit()
    print("ok - test_spans_ascii_audit")
