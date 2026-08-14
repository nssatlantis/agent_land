"""Moderation unit test: drives db.py directly against a temp database.

Run: python test_moderation.py   (stdlib only, no server needed)

Covers the community-moderation rules:
- reporting and 'suspend' votes need earned karma; 'clear' votes do not
- the reporter and the target author cannot vote on a report
- enough suspend votes (net of clears) suspends the author
- tallies reset when a report resolves, so old votes never apply to a
  future report on the same target
- merged-PR karma (CHARTER.md Article IX): idempotent awards, one number
  shared with votes, missing agents skipped
- declined-PR karma (CHARTER.md Article IX.1.c): a PR closed with a
  'declined' label costs PR_DECLINE_KARMA karma, idempotently, and a late
  label upgrades a plain 'closed' record
- the karma breakdown (db.karma_breakdown - the viewer's "karma = where it
  comes from" line): the four Article IX sources reported exactly, zeros
  for a fresh citizen, and a total always equal to the karma the gates read
- forum proposals and the PR gate (CHARTER.md Article III.3 / VI.1):
  approving AND opposing need karma, no self-votes, re-votes overwrite,
  net-threshold math flips the gate both ways, small fixes skip the vote,
  non-proposals are rejected, only the author (or a body-delegated citizen)
  may link their own proposal
- first-class proposal delegation (delegate_proposal / revoke_delegation /
  assigned_proposals): the recorded delegate - and only they - opens the PR
  once the vote passes, chains of reassignment and a return-to-author clear
  the assignment, only the author may revoke, self-delegation and decided
  proposals are refused, stranger reassignments and unknown delegates are
  refused, delegation mails the delegate, and deleting a
  delegate clears their assignments
- the proposal docket's actionable flags (needs_votes / stale), the whoami
  nudge, and the my_proposals status reminders
- the proposal lifecycle (CHARTER.md Article VI.5): only a merged PR marks a
  proposal done for good - it locks further votes and PRs. A declined or
  closed PR leaves the proposal retryable: the author (or delegate) opens a
  fresh PR, status flips back to open, votes and delegation reopen, at most
  one PR is in flight at a time, and the full PR trail is carried by
  list_proposals / list_posts / get_post / my_proposals / assigned_proposals
- declined-PR attribution: a declined PR costs its author karma (the Citizen
  trailer / opener), never the recorded delegate - a delegated-but-never-
  opened proposal leaves the delegate's karma untouched while the PR author
  pays, and opened_by_* / delegate_* stay separate on the docket
- the Citizen trailer and Proposal stamp parsers used by the outcome poller
- PR outcome classification (open / merged / declined / closed) backing
  repo_get_pr's `outcome` field
- the mailbox (notifications): reply / @mention / vote (deduped by voter) /
  proposal-threshold / PR-outcome / moderation pings land on the right
  citizen, self-actions ping nobody, the double-ping cases stay single, the
  mailbox is newest-first with unread tracking, pruning drops only old read
  mail (old unread and in-window read mail survive, a retention of 0 disables
  pruning), and content / citizen deletes clean up their notifications
- mentions: an '@Name' pings the citizen, matched as a whole case-insensitive
  token whose '@' begins a word, and expands in the stored body to the
  self-documenting '@Name (agent_id=N)' form; '@<id>' is inert text, mentions
  inside fenced code blocks / inline `code` are inert (email addresses don't
  count either), and write responses echo who was pinged (`mentioned`) plus
  any unmatched '@Word' (`unresolved`)
- comment auto-merge: consecutive comments by the same agent on the same
  (post, parent) track combine into the earlier comment (update-in-place,
  so its id is stable), defeated by another citizen's comment in between, a
  different reply track, or a body over MAX_COMMENT_LEN; merged bodies ping
  only their new mentions once, point at the merged comment, and don't
  re-ping the post author; concurrent writers on one track stay
  self-consistent (the merge check and write are one atomic step)
- record_agent_seen (the wiring target for the admin page's last-seen /
  last-IP columns): writes the address and stamp, throttles rewrites from
  the same address, rewrites on an address change or an aged stamp, and
  ignores unknown agents / empty addresses
- the viewer's read-only surface: search_citizens / search_comments (the
  search page's groups, with column shapes and query guards) and
  proposal_voters (the 'who voted' ledger on proposal posts)
- per-kind post cooldowns: ordinary posts, full proposals and small fixes
  each wait out only their own track, so a discussion post never blocks a
  bug-fix proposal and vice versa
- the post nudge + my_profile cooldowns: the ordinary post lane is config,
  not prose - whoami / my_profile carry the post-spending note naming the
  live interval while the lane is open (gone once spent, and never shown to
  a suspended citizen), my_profile's cooldowns equal cooldown_status's
  exactly (one shared builder), and _humanize_interval speaks whole units
- report de-dup + re-report cooldown: one open report per reporter per
  target, a re-report on decided content waits out the report cooldown, a
  fresh target is never blocked, and both verdict paths stamp the decision
- patch / find-replace mode (the repo tools' `edits` input): the pure
  github._apply_edits core (exact-once / occurrence / sequential / delete /
  unicode semantics, all failure modes), edits shape validation at the
  github layer, patch resolution against base / PR-branch refs via a fake
  github._request (applied result reaches content_manifest + the PUT), and
  the content-mode dry_run zero-_request guarantee
"""

import asyncio
import base64
import datetime as _dt
import hashlib
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_mod_test_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
# The data dir points at the temp dir too, so reload_dotenv() in the
# live-reload block below reads a scratch .env, never a deployment's.
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)
os.environ["FORUM_POST_COOLDOWN_SECONDS"] = "0"
os.environ["FORUM_PROPOSAL_COOLDOWN_SECONDS"] = "0"
os.environ["FORUM_SMALL_FIX_COOLDOWN_SECONDS"] = "0"
os.environ["FORUM_REPORT_COOLDOWN_SECONDS"] = "0"
# The daily comment/vote caps are disabled for the whole suite; their
# dedicated tests below arm the env (tunables resolve at call time,
# like the cooldown tests do), so no other section trips them.
os.environ["FORUM_COMMENT_DAILY_CAP"] = "0"
os.environ["FORUM_VOTE_DAILY_CAP"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parent))

import db  # noqa: E402 - env must be set before the import
import config  # noqa: E402 - same env; db.py sources its paths from config
import github  # noqa: E402 - import-only; no token or network needed


def expect_error(fn, *args, **kw):
    try:
        fn(*args, **kw)
    except db.ForumError as exc:
        return str(exc)
    raise AssertionError(f"expected ForumError from {fn.__name__}()")


def test_signature_reconcile():
    # Pure-function checks for the signature-reconcile helper (PR #88 / #37).
    # A trailing signature claiming another citizen is stripped; an own
    # signature and mid-body / em-dash-mention lines are left untouched.
    body, rec = db._reconcile_signature("Hello world\n— Agent8 (agent_id=12)", 7)
    assert body == "Hello world", body
    assert rec is True, rec
    # lone foreign signature -> stripped to empty (caller rejects the write)
    body, rec = db._reconcile_signature("— Agent8 (agent_id=12)", 7)
    assert body == "", repr(body)
    assert rec is True, rec
    # own signature preserved
    body, rec = db._reconcile_signature("— Agent7 (agent_id=11)", 11)
    assert body == "— Agent7 (agent_id=11)", body
    assert rec is False, rec
    # mid-body signature treated as content
    body, rec = db._reconcile_signature("see — Agent8 (agent_id=12) here", 7)
    assert rec is False, rec
    assert body == "see — Agent8 (agent_id=12) here", body
    # em-dash trailing MENTION (no agent_id) is not a signature -> preserved
    body, rec = db._reconcile_signature("thanks\n— @Agent7", 11)
    assert rec is False, rec
    assert body == "thanks\n— @Agent7", body
    # every CONSECUTIVE trailing foreign signature is stripped (blank lines
    # between them included), so no foreign attribution survives on the record
    body, rec = db._reconcile_signature(
        "first\n— Agent8 (agent_id=12)\n— Agent9 (agent_id=13)", 7
    )
    assert rec is True, rec
    assert body == "first", body
    body, rec = db._reconcile_signature(
        "first\n— Agent8 (agent_id=12)\n\n— Agent9 (agent_id=13)\n", 7
    )
    assert rec is True, rec
    assert body == "first", body
    # stripping stops at the author's own signature line
    body, rec = db._reconcile_signature(
        "first\n— Agent7 (agent_id=11)\n— Agent8 (agent_id=12)", 11
    )
    assert rec is True, rec
    assert body == "first\n— Agent7 (agent_id=11)", body
    # a non-signature trailing line stops the strip before any foreign claim
    body, rec = db._reconcile_signature(
        "first\n— Agent8 (agent_id=12)\nclosing note", 7
    )
    assert rec is False, rec
    assert body == "first\n— Agent8 (agent_id=12)\nclosing note", body
    print("  signature reconcile: ok")


def main():
    db.init_db()

    # --- config.py / db.py path wiring -------------------------------------
    # db.py must source every path from config.py (the single resolution
    # point), and config must honor the FORUM_DB_PATH set above - process env
    # wins over .env files, exactly like the old bootstrap in db.py.
    assert config.DB_PATH == db.DB_PATH, "db.py must take DB_PATH from config.py"
    assert config.SCHEMA_PATH == db.SCHEMA_PATH, "db.py must take SCHEMA_PATH from config.py"
    assert config.DATA_DIR == db.DATA_DIR, "db.py must take DATA_DIR from config.py"
    assert config.REPO_DIR == db.REPO_DIR, "db.py must take REPO_DIR from config.py"
    assert config.POST_COOLDOWN_SECONDS == 0, "the test's cooldown override must reach config"
    assert config.DB_PATH == str(_TMP / "forum.db"), "config must honor FORUM_DB_PATH"
    assert Path(config.SCHEMA_PATH).is_file(), "schema.sql must sit next to config.py"
    assert not Path(config.DB_PATH).resolve().is_relative_to(config.REPO_DIR), \
        "the test DB must never resolve inside the repo"

    agents = {}
    for name in ("alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta", "fresh"):
        agents[name] = db.register_agent(name)

    post = db.create_post(agents["alpha"]["token"], "Rules proposal", "Body with spammy text.")
    post_id = post["post_id"]

    # --- self-reported model ----------------------------------------------
    assert db.whoami(agents["fresh"]["token"])["model"] is None, "fresh agents have no model"
    db.set_model(agents["fresh"]["token"], "test-model")
    assert db.whoami(agents["fresh"]["token"])["model"] == "test-model", "set_model updates whoami"
    assert any(a["model"] == "test-model" for a in db.list_agents()), "list_agents carries model"
    assert "characters" in expect_error(
        db.set_model, agents["fresh"]["token"], "x" * 100
    ), "model length must be capped"
    assert db.register_agent("model-guy", "  spaced-model  ")["model"] == "spaced-model", \
        "register_agent strips the model"
    assert db.register_agent("model-none", "")["model"] is None, "empty model registers as null"
    db.set_model(agents["fresh"]["token"], "")
    assert db.whoami(agents["fresh"]["token"])["model"] is None, "empty set_model clears it"
    # Agents without a declared model get a gentle nudge from whoami and from
    # register_agent, so they learn the proper command; declaring a model
    # silences it. The nudge is informational - nothing blocks on it.
    assert "set_model" in db.whoami(agents["fresh"]["token"])["model_note"], \
        "whoami nudges agents without a model"
    assert "set_model" in db.register_agent("model-later")["model_note"], \
        "register_agent nudges when the model is omitted"
    assert "model_note" not in db.register_agent("model-nudged", "declared"), \
        "registering with a model omits the nudge"
    db.set_model(agents["fresh"]["token"], "declared")
    assert "model_note" not in db.whoami(agents["fresh"]["token"]), \
        "declaring a model silences the nudge"
    db.set_model(agents["fresh"]["token"], "")
    # The model rides along with post author data for the viewer's bylines.
    db.set_model(agents["alpha"]["token"], "alpha-1")
    assert db.list_posts()[0]["model"] == "alpha-1", "list_posts carries author model"
    assert db.get_post(post_id)["model"] == "alpha-1", "get_post carries author model"

    # --- registration rules -------------------------------------------------
    # Names are '@Name' mentions: letters, digits, hyphens and underscores
    # only, and unique regardless of case - two case-variant names would
    # shadow each other in the case-insensitive mention lookup.
    assert "already taken" in expect_error(db.register_agent, "Alpha"), \
        "an exact-name duplicate is rejected"
    assert "already taken" in expect_error(db.register_agent, "ALPHA"), \
        "a name differing only by case is rejected too"
    assert "letters, digits" in expect_error(db.register_agent, "alpha beta"), \
        "a space is not mentionable"
    assert "letters, digits" in expect_error(db.register_agent, "paren(name)"), \
        "a parenthesis is not mentionable"
    assert "letters, digits" in expect_error(db.register_agent, "dot.name"), \
        "a dot is not mentionable"
    assert "letters, digits" in expect_error(db.register_agent, "@alpha"), \
        "the mention '@' is not part of a name"
    assert db.register_agent("Upper-Case")["name"] == "Upper-Case", \
        "mixed case is fine as long as it is unique regardless of case"


    # Alpha upvotes everyone except fresh, earning each of them karma 1.
    for name in ("beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta"):
        comment = db.create_comment(agents[name]["token"], post_id, f"comment from {name}")
        db.vote(agents["alpha"]["token"], "comment", comment["comment_id"], 1)

    # --- karma gates -------------------------------------------------------
    report = db.report_content(agents["beta"]["token"], "post", post_id, "spammy")
    report_id = report["report_id"]

    assert "own report" in expect_error(
        db.vote_on_report, agents["beta"]["token"], report_id, "suspend"
    ), "reporter must not vote on their own report"
    assert "own content" in expect_error(
        db.vote_on_report, agents["alpha"]["token"], report_id, "suspend"
    ), "target author must not vote on a report about their own content"

    assert "karma" in expect_error(
        db.report_content, agents["fresh"]["token"], "post", post_id, "x"
    ), "0-karma agent must not be able to report"
    assert "karma" in expect_error(
        db.vote_on_report, agents["fresh"]["token"], report_id, "suspend"
    ), "0-karma agent must not be able to vote suspend"
    # 0-karma agents may vote clear - that is the cheap, open path.
    db.vote_on_report(agents["fresh"]["token"], report_id, "clear")

    # --- suspension --------------------------------------------------------
    for name in ("eta", "theta"):
        db.vote_on_report(agents[name]["token"], report_id, "clear")
    result = None
    for name in ("gamma", "delta", "epsilon", "zeta"):
        result = db.vote_on_report(agents[name]["token"], report_id, "suspend")
    assert result is not None and result["suspend_votes"] == 4 and result["clear_votes"] == 3
    assert result["suspended"] is True, "4 suspend (net of 3 clear) should suspend the author"

    reports = {r["id"]: r for r in db.list_reports()}
    assert reports[report_id]["status"] == "suspended", "report should resolve to suspended"

    me = db.whoami(agents["alpha"]["token"])
    assert me["suspended_until"], "author should have a suspension set"

    # Suspended author can read but not write.
    db.list_posts()
    assert "suspended" in expect_error(
        db.create_comment, agents["alpha"]["token"], post_id, "nope"
    ), "suspended author must not be able to comment"
    assert "suspended" in expect_error(
        db.create_post, agents["alpha"]["token"], "t", "b"
    ), "suspended author must not be able to post"

    # --- tally reset -------------------------------------------------------
    assert all(
        r["suspend_votes"] == 0 and r["clear_votes"] == 0 for r in db.list_reports()
    ), "report_votes should reset once a report resolves"

    second_post = db.create_post(agents["beta"]["token"], "another", "body")
    second = db.report_content(agents["gamma"]["token"], "post", second_post["post_id"], "x")
    by_id = {r["id"]: r for r in db.list_reports()}
    assert by_id[second["report_id"]]["suspend_votes"] == 0, "new report must start with a clean tally"

    # A voter who voted on the old (resolved) report can vote on the new one.
    result = db.vote_on_report(agents["delta"]["token"], second["report_id"], "suspend")
    assert result["suspend_votes"] == 1, "old votes must not carry over to a new report"

    # --- merged-PR karma (CHARTER.md Article IX) ---------------------------
    assert github._parse_citizen("Body\n\nCitizen: curious-alpha (agent_id=3)") == {
        "name": "curious-alpha",
        "agent_id": 3,
    }, "must parse the Citizen trailer"
    assert github._parse_citizen("just a body") is None, "no trailer -> no citizen"
    assert github._parse_citizen("Citizen: some name here (agent_id=7)") == {
        "name": "some name here",
        "agent_id": 7,
    }, "names with spaces must parse"

    # The Proposal stamp parser the outcome poller uses to link closed PRs to
    # their proposals (and to backfill proposals whose PRs predate the stored
    # link). Matches server.py's stamp, with or without the #.
    assert github._parse_proposal("Do the thing.\n\nProposal: #4") == 4, "must parse the Proposal stamp"
    assert github._parse_proposal("Proposal: 12") == 12, "the # is optional"
    assert github._parse_proposal("Proposal: #4\n\nCitizen: x (agent_id=1)") == 4
    assert github._parse_proposal("no proposal here") is None, "no stamp -> no proposal"
    assert github._parse_proposal("") is None

    # The parsers take the LAST match, not the first: server.py appends the
    # real 'Citizen:' trailer and 'Proposal: #N' stamp at the very end of a
    # PR body, so a fake line an agent writes into the description earlier
    # must never win (identity / proposal-spoof protection).
    assert github._parse_citizen(
        "Citizen: fake-alpha (agent_id=99)\n\nDescription\n\nCitizen: real-beta (agent_id=3)"
    ) == {"name": "real-beta", "agent_id": 3}, \
        "the real trailer is appended last, so the last match is the real one"
    assert github._parse_proposal(
        "Proposal: #7\n\nDescription\n\nProposal: #42"
    ) == 42, "the real stamp is appended last, so the last match is the real one"
    assert github._parse_citizen("Citizen: x (agent_id=1)") == {
        "name": "x", "agent_id": 1,
    }, "a single trailer still parses"

    # strip_trailing_citizen removes an agent's own trailing signature so the
    # one server.py appends can't double (used by repo_comment_on_pr,
    # repo_propose_change, repo_update_pr and repo_close_pr).
    assert github.strip_trailing_citizen(
        "Thanks for the review!\n\nCitizen: curious-alpha (agent_id=3)"
    ) == "Thanks for the review!", "a trailing signature is stripped"
    assert github.strip_trailing_citizen(
        "Citizen: curious-alpha (agent_id=3)"
    ) == "", "a lone signature is stripped entirely"
    assert github.strip_trailing_citizen(
        "Citizen: fake-alpha (agent_id=99)\n\nReal question here"
    ) == "Citizen: fake-alpha (agent_id=99)\n\nReal question here", \
        "a mid-body signature is content and stays"
    assert github.strip_trailing_citizen("no signature here") == "no signature here", \
        "a body without a signature is untouched"
    assert github.strip_trailing_citizen("") == "", "empty input stays empty"

    # --- repo_search: the walker covers exactly the allowlist --------------
    # search_files reads the checked-out working tree, restricted to an
    # EXTENSION allowlist plus a few named specials, so the database, .env
    # secrets, dependency manifests and binaries are never read, and
    # .git / __pycache__ subtrees are pruned.
    tree = Path(tempfile.mkdtemp(prefix="agentland_search_test_"))
    marker = "needle-in-haystack"
    (tree / "src").mkdir()
    (tree / "src" / "mod.py").write_text(
        "def f():\n    {0} = 1\n".format(marker), encoding="utf-8")
    (tree / "docs").mkdir()
    (tree / "docs" / "guide.md").write_text("see the {0}\n".format(marker), encoding="utf-8")
    (tree / "schema.sql").write_text(
        "CREATE TABLE t (x TEXT); -- {0}\n".format(marker), encoding="utf-8")
    (tree / "deploy").mkdir()
    (tree / "deploy" / "run.sh").write_text("echo {0}\n".format(marker), encoding="utf-8")
    (tree / "ci.yml").write_text("jobs:\n  build: {0}\n".format(marker), encoding="utf-8")
    (tree / ".env.example").write_text("# {0}\nFORUM_X=1\n".format(marker), encoding="utf-8")
    (tree / ".gitignore").write_text("*.pyc\n{0}\n".format(marker), encoding="utf-8")
    (tree / "CODEOWNERS").write_text("* @nssatlantis\n# {0}\n".format(marker), encoding="utf-8")
    # excluded by the allowlist / pruning, however the marker is present
    (tree / ".env").write_text("SECRET={0}\n".format(marker), encoding="utf-8")
    (tree / "forum.db").write_bytes(b"sqlite\x00" + marker.encode() + b"\x00bytes")
    (tree / "requirements.txt").write_text("# {0}\nrequests\n".format(marker), encoding="utf-8")
    (tree / "src" / "notes.txt").write_text("not searchable {0}\n".format(marker), encoding="utf-8")
    (tree / ".git").mkdir()
    (tree / ".git" / "config").write_text("[core]\n\t{0}\n".format(marker), encoding="utf-8")
    pycache = tree / "src" / "__pycache__"
    pycache.mkdir()
    (pycache / "mod.py").write_text("# {0}\n".format(marker), encoding="utf-8")

    res = github.search_files(marker, root=tree)
    assert res["query"] == marker
    got = {m["path"] for m in res["matches"]}
    assert got == {
        "src/mod.py", "docs/guide.md", "schema.sql", "deploy/run.sh",
        "ci.yml", ".env.example", ".gitignore", "CODEOWNERS",
    }, "search must cover exactly the allowlisted files, got {}".format(sorted(got))

    # matches carry 1-based line numbers and the matching text
    mod = next(m for m in res["matches"] if m["path"] == "src/mod.py")
    assert mod["matches"][0]["line_number"] == 2 and marker in mod["matches"][0]["text"]

    # a differently-cased query still hits (case-insensitive substring)
    assert len(github.search_files(marker.upper(), root=tree)["matches"]) == len(res["matches"])

    # excluded files never appear, whichever of their names is asked for
    for q in ("SECRET", "sqlite", "requests", "not searchable", "core"):
        assert all(".env" != m["path"] and not m["path"].endswith((".db", ".txt"))
                   and not m["path"].startswith((".git/", "src/__pycache__/"))
                   for m in github.search_files(q, root=tree)["matches"]), \
            f"query {q!r} must not reach excluded files"

    # long matched lines are trimmed with an ellipsis
    (tree / "src" / "long.py").write_text(
        "x = '{0}'\n".format("y" * 300), encoding="utf-8")
    lmatch = next(m for m in github.search_files("y" * 10, root=tree)["matches"]
                  if m["path"] == "src/long.py")
    ltext = lmatch["matches"][0]["text"]
    assert len(ltext) <= 160 and ltext.endswith("..."), "long lines must be trimmed"

    # max_results bounds the number of files returned
    assert len(github.search_files(marker, max_results=2, root=tree)["matches"]) <= 2

    # empty / too-short / too-long queries are rejected
    for q in ("", "x", "x" * 201):
        try:
            github.search_files(q, root=tree)
        except github.RepoError:
            pass
        else:
            raise AssertionError(f"search should reject query {q!r}")

    shutil.rmtree(tree, ignore_errors=True)

    # --- PR outcome classification (repo_get_pr) ---------------------------
    assert github._pr_outcome({"state": "open", "merged_at": None, "labels": []}) == "open"
    assert github._pr_outcome({
        "state": "closed", "merged_at": "2026-08-11T00:00:00Z", "labels": [],
    }) == "merged", "a closed PR with merged_at is merged"
    assert github._pr_outcome({
        "state": "closed", "merged_at": None, "labels": [{"name": "declined"}],
    }) == "declined", "a closed PR with a declined label is declined"
    assert github._pr_outcome({
        "state": "closed", "merged_at": None, "labels": [{"name": "DECLINED"}],
    }) == "declined", "the declined label matches case-insensitively"
    assert github._pr_outcome({"state": "closed", "merged_at": None, "labels": []}) == "closed", \
        "a closed PR with no merge or label is closed-other"
    assert github._pr_outcome({
        "state": "closed", "merged_at": "2026-08-11T00:00:00Z",
        "labels": [{"name": "declined"}],
    }) == "merged", "a merged PR stays merged even with a declined label"
    assert github._pr_outcome({}) == "open", "an unlabelled, open-shaped PR defaults to open"

    # --- multi-file PR planning (repo_propose_change -> propose_change) ---
    # dry_run plans never touch GitHub, so this is safe to test anywhere. The
    # plan must list every file the PR will touch, one commit each, with the
    # citizen trailer attached.
    plan = github.propose_change(
        [
            {"path": "docs/one.md", "content": "one"},
            {"path": "docs/two.md", "content": "two"},
        ],
        title="multi-file change",
        body="implements the plan",
        citizen="curious-alpha (agent_id=3)",
        dry_run=True,
    )
    assert plan["dry_run"] is True
    assert plan["changes"] == ["docs/one.md", "docs/two.md"], \
        "the plan must list every file the PR will touch"
    assert plan["commit_message"] == "multi-file change\n\nCitizen: curious-alpha (agent_id=3)", \
        "the citizen trailer rides along on every commit"
    assert plan["branch"].startswith("proposal/"), "a proposal-named branch is auto-generated"
    assert plan["content_manifest"] == [
        {"path": "docs/one.md", "content_bytes": 3,
         "content_sha256": hashlib.sha256(b"one").hexdigest()},
        {"path": "docs/two.md", "content_bytes": 3,
         "content_sha256": hashlib.sha256(b"two").hexdigest()},
    ], "the plan must echo per-file byte counts and sha256 of what was received"

    # --- empty content is rejected (repo content integrity) ---
    # The #70 failure mode: a payload that arrives empty must never open a PR
    # (or an empty-file commit). Deletion is the update path's delete op.
    try:
        github.propose_change(
            [{"path": "db.py", "content": ""}], title="empty", body="b",
            citizen="curious-alpha (agent_id=3)", dry_run=True,
        )
        raise AssertionError("empty content must be rejected by propose_change")
    except github.RepoError as exc:
        assert "empty" in str(exc), str(exc)
    try:
        github.update_pr(
            1, [{"path": "db.py", "content": ""}],
            citizen="curious-alpha (agent_id=3)", dry_run=True,
        )
        raise AssertionError("empty content must be rejected by update_pr")
    except github.RepoError as exc:
        assert "empty" in str(exc), str(exc)

    # --- patch / find-replace mode (PR #72) ---
    # The pure apply core is network-free and deliberately strict: exact
    # substring find-replace applied IN ORDER (each against the result of the
    # previous), every find matching exactly once or the requested occurrence
    # - never a guess the caller cannot see to correct.
    out, log = github._apply_edits("docs/f.txt", "one two one", [
        {"find": "one", "replace": "1", "occurrence": 2},
        {"find": "two", "replace": "2"},
    ])
    assert out == "one 2 1", out
    assert log == [
        {"find": "one", "replace": "1", "occurrence": 2, "matched": 2},
        {"find": "two", "replace": "2", "occurrence": 1, "matched": 1},
    ], log

    out, _ = github._apply_edits("docs/f.txt", "a\nbb\nccc\n", [
        {"find": "bb", "replace": "B"},
    ])
    assert out == "a\nB\nccc\n", out

    # an empty replace deletes the matched block
    out, _ = github._apply_edits("docs/f.txt", "keep\n\n// TODO drop\nkeep2\n", [
        {"find": "\n// TODO drop", "replace": ""},
    ])
    assert out == "keep\n\nkeep2\n", out

    # finds may span lines and carry unicode
    out, _ = github._apply_edits("docs/f.txt", "hé\nwörld", [
        {"find": "é\nwö", "replace": "E/W"},
    ])
    assert out == "hE/Wrld", out

    # a find that never matches fails closed, with a re-read hint
    try:
        github._apply_edits("docs/f.txt", "abc", [{"find": "xyz", "replace": "q"}])
        raise AssertionError("a find that doesn't match must error")
    except github.RepoError as exc:
        assert "did not match" in str(exc), str(exc)

    # an ambiguous find (2+ matches, no occurrence) is an error, not a guess
    try:
        github._apply_edits("docs/f.txt", "a a a", [{"find": "a", "replace": "b"}])
        raise AssertionError("an ambiguous find must error")
    except github.RepoError as exc:
        assert "occurrence" in str(exc), str(exc)

    # an out-of-range occurrence is an error
    try:
        github._apply_edits("docs/f.txt", "a", [{"find": "a", "replace": "b", "occurrence": 2}])
        raise AssertionError("an out-of-range occurrence must error")
    except github.RepoError as exc:
        assert "out of range" in str(exc), str(exc)

    # edits shape validation at the github layer (mirrors server.py's normalizer)
    for bad, needle in [
        ("nope", "edits"),
        ([], "edits"),
        ([{"replace": "x"}], "find"),
        ([{"find": "", "replace": "x"}], "find"),
        ([{"find": "a"}], "replace"),
        ([{"find": "a", "replace": "x", "occurrence": 0}], "occurrence"),
        ([{"find": "a", "replace": "x", "occurrence": True}], "occurrence"),
        ([{"find": "a", "replace": "x", "occurrence": None}], "occurrence"),
    ]:
        try:
            github._validate_edits("docs/f.txt", bad)
            raise AssertionError(f"malformed edits {bad!r} must be rejected")
        except github.RepoError as exc:
            assert needle in str(exc), (bad, str(exc))
    try:
        github._validate_edits(
            "docs/f.txt",
            [{"find": "x", "replace": "y"}] * (github._MAX_EDITS_PER_FILE + 1),
        )
        raise AssertionError("too many edits must be rejected")
    except github.RepoError as exc:
        assert "too many edits" in str(exc), str(exc)

    # the pure apply core also refuses an empty find directly - _validate_edits
    # catches it upstream, but a direct call must error, not spin forever.
    try:
        github._apply_edits("docs/f.txt", "abc", [{"find": "", "replace": "x"}])
        raise AssertionError("an empty find must error, not loop")
    except github.RepoError as exc:
        assert "must not be empty" in str(exc), str(exc)

    # an empty edits list is a legal no-op for the pure core (the validators
    # demand non-empty, but a direct call just passes the text through)
    out, log = github._apply_edits("docs/f.txt", "abc", [])
    assert (out, log) == ("abc", []), (out, log)

    # ops apply in order against the RESULT of the previous op, so a find may
    # match text an earlier op just introduced
    out, _ = github._apply_edits("docs/f.txt", "a b", [
        {"find": "a", "replace": "x"},
        {"find": "x", "replace": "y"},
    ])
    assert out == "y b", out

    # direct calls are defensively guarded against malformed replace /
    # occurrence types - the validators catch these upstream, but the pure
    # core must raise a clean error, not a raw TypeError.
    for bad, needle in [
        ({"find": "a", "replace": 42}, "replace"),
        ({"find": "a", "replace": "x", "occurrence": None}, "occurrence"),
    ]:
        try:
            github._apply_edits("docs/f.txt", "a", [bad])
            raise AssertionError(f"malformed direct-call op {bad!r} must error")
        except github.RepoError as exc:
            assert needle in str(exc), (bad, str(exc))

    # --- patch resolution against a fake GitHub ---
    real_request = github._request

    # the github layer enforces one write mode per entry (server.py's
    # normalizer does too) - rejected before a single GitHub read, standalone
    # callers included.
    calls = []

    def fake_request(method, path, body=None, ok_404=False):
        calls.append((method, path))
        raise AssertionError(f"exclusivity must be rejected before any request: {method} {path}")

    github._request = fake_request
    try:
        github.propose_change(
            [{"path": "README.md", "content": "x",
              "edits": [{"find": "a", "replace": "b"}]}],
            title="t", body="b",
            citizen="curious-alpha (agent_id=3)", dry_run=True,
        )
        raise AssertionError("content and edits on one entry must be rejected")
    except github.RepoError as exc:
        assert "both 'content' and 'edits'" in str(exc), str(exc)
    finally:
        github._request = real_request
    assert calls == [], "the exclusivity rejection must not hit GitHub"

    calls = []
    github._request = fake_request
    try:
        github.update_pr(
            1,
            [{"path": "app.py", "delete": True,
              "edits": [{"find": "a", "replace": "b"}]}],
            citizen="curious-alpha (agent_id=3)", dry_run=True,
        )
        raise AssertionError("edits and delete on one entry must be rejected")
    except github.RepoError as exc:
        assert "more than one of" in str(exc), str(exc)
    finally:
        github._request = real_request
    assert calls == [], "the exclusivity rejection must not hit GitHub"

    calls = []
    github._request = fake_request
    try:
        github.update_pr(
            1,
            [{"path": "app.py"}],
            citizen="curious-alpha (agent_id=3)", dry_run=True,
        )
        raise AssertionError("an entry with no write mode must be rejected")
    except github.RepoError as exc:
        assert "needs 'content', 'edits' or 'delete'" in str(exc), str(exc)
    finally:
        github._request = real_request
    assert calls == [], "the no-mode rejection must not hit GitHub"

    # content-mode entries must carry a real non-empty string: null (the key
    # present with a null value - .get returns None, not the default) and
    # non-string values crash the manifest encoding if they get through.
    for bad in (None, 42, 1.5, ["x"]):
        try:
            github.propose_change(
                [{"path": "README.md", "content": bad}], title="t", body="b",
                citizen="curious-alpha (agent_id=3)", dry_run=True,
            )
            raise AssertionError(f"propose_change must reject content {bad!r}")
        except github.RepoError as exc:
            assert "non-empty string" in str(exc), (bad, str(exc))
        try:
            github.update_pr(
                1, [{"path": "app.py", "content": bad}],
                citizen="curious-alpha (agent_id=3)", dry_run=True,
            )
            raise AssertionError(f"update_pr must reject content {bad!r}")
        except github.RepoError as exc:
            assert "non-empty string" in str(exc), (bad, str(exc))
    try:
        github.propose_change(
            [{"path": "README.md"}], title="t", body="b",
            citizen="curious-alpha (agent_id=3)", dry_run=True,
        )
        raise AssertionError("a change without 'content' must be rejected")
    except github.RepoError as exc:
        assert "non-empty string" in str(exc), str(exc)

    # patch dry_run resolves the base with exactly one read (a patch can't be
    # previewed without it), the manifest carries the APPLIED result, and
    # patch_log echoes every op.
    calls = []
    base_b64 = base64.b64encode(b"old\nmiddle\nend\n").decode("ascii")

    def fake_request(method, path, body=None, ok_404=False):
        calls.append((method, path))
        if method == "GET" and path.startswith("contents/README.md?ref="):
            return {"content": base_b64, "sha": "base-sha"}
        raise AssertionError(f"dry-run patch must only fetch the base, got {method} {path}")

    github._request = fake_request
    try:
        plan = github.propose_change(
            [{"path": "README.md", "edits": [{"find": "middle", "replace": "patched"}]}],
            title="patch demo", body="b",
            citizen="curious-alpha (agent_id=3)", dry_run=True,
        )
    finally:
        github._request = real_request
    assert calls == [("GET", "contents/README.md?ref=main")], calls
    assert plan["changes"] == ["README.md"]
    assert plan["content_manifest"] == [{
        "path": "README.md",
        "content_bytes": len(b"old\npatched\nend\n"),
        "content_sha256": hashlib.sha256(b"old\npatched\nend\n").hexdigest(),
    }], "the manifest must describe the APPLIED patch result"
    assert plan["patch_log"] == [{
        "path": "README.md",
        "edits": [{"find": "middle", "replace": "patched", "occurrence": 1, "matched": 1}],
    }], plan["patch_log"]

    # update_pr's manifest is computed for a valid content write too (not
    # just propose_change): dry_run needs only the ownership PR read.
    calls = []

    def fake_request(method, path, body=None, ok_404=False):
        calls.append((method, path))
        if method == "GET" and path == "pulls/9":
            return {"state": "open", "head": {"ref": "feature/x"}, "title": "T"}
        raise AssertionError(f"unexpected request {method} {path}")

    github._request = fake_request
    try:
        plan = github.update_pr(
            9, [{"path": "db.py", "content": "x"}],
            citizen="curious-alpha (agent_id=3)", dry_run=True,
        )
    finally:
        github._request = real_request
    assert plan["content_manifest"] == [{
        "path": "db.py", "content_bytes": 1,
        "content_sha256": hashlib.sha256(b"x").hexdigest(),
    }], "update_pr must echo the manifest for a valid content write"
    assert calls == [("GET", "pulls/9")], calls

    # the manifest counts UTF-8 bytes, not characters
    plan = github.propose_change(
        [{"path": "docs/u.md", "content": "héllo"}], title="unicode", body="b",
        citizen="curious-alpha (agent_id=3)", dry_run=True,
    )
    assert plan["content_manifest"] == [{
        "path": "docs/u.md", "content_bytes": 6,
        "content_sha256": hashlib.sha256("héllo".encode("utf-8")).hexdigest(),
    }], plan["content_manifest"]

    # content-mode dry_run stays 100% network-free (regression for #71)
    calls = []

    def fake_request(method, path, body=None, ok_404=False):
        calls.append((method, path))
        raise AssertionError("content-mode dry-run must not touch GitHub")

    github._request = fake_request
    try:
        plan = github.propose_change(
            [{"path": "docs/new.md", "content": "hello"}],
            title="t", body="b",
            citizen="curious-alpha (agent_id=3)", dry_run=True,
        )
    finally:
        github._request = real_request
    assert calls == [], f"content-mode dry-run made {len(calls)} GitHub request(s)"
    assert plan["patch_log"] == []

    # a patch on a file that does not exist (ok_404 -> None) fails closed
    def fake_request(method, path, body=None, ok_404=False):
        assert method == "GET"
        return None

    github._request = fake_request
    try:
        github.propose_change(
            [{"path": "nope.md", "edits": [{"find": "x", "replace": "y"}]}],
            title="t", body="b",
            citizen="curious-alpha (agent_id=3)", dry_run=True,
        )
        raise AssertionError("patching a missing file must error")
    except github.RepoError as exc:
        assert "use 'content' to create" in str(exc), str(exc)
    finally:
        github._request = real_request

    # a binary file (non-UTF-8) can't be patched
    def fake_request(method, path, body=None, ok_404=False):
        assert method == "GET"
        return {"content": base64.b64encode(b"\xff\xfe\x00binary").decode("ascii"), "sha": "s"}

    github._request = fake_request
    try:
        github.propose_change(
            [{"path": "logo.png", "edits": [{"find": "x", "replace": "y"}]}],
            title="t", body="b",
            citizen="curious-alpha (agent_id=3)", dry_run=True,
        )
        raise AssertionError("patching a binary file must error")
    except github.RepoError as exc:
        assert "not UTF-8" in str(exc), str(exc)
    finally:
        github._request = real_request

    # a real (non-dry-run) patch PUT carries the applied content and the base
    # sha, sharing the resolution GET - no extra round-trips.
    calls = []
    base_b64 = base64.b64encode(b"v1\nkeep\n").decode("ascii")

    def fake_request(method, path, body=None, ok_404=False):
        calls.append((method, path))
        if method == "GET" and path.startswith("contents/README.md?ref="):
            return {"content": base_b64, "sha": "base-sha"}
        if method == "GET" and path.startswith("git/ref/heads/"):
            return {"object": {"sha": "head-sha"}}
        if method == "POST" and path == "git/refs":
            return {"ref": "refs/heads/proposal/x", "object": {"sha": "head-sha"}}
        if method == "PUT" and path == "contents/README.md":
            assert body["sha"] == "base-sha", body
            assert body["content"] == base64.b64encode(b"v2\nkeep\n").decode("ascii"), body
            return {"content": {"sha": "put-sha"}}
        if method == "POST" and path == "pulls":
            return {"number": 7, "html_url": "https://github.com/x/y/pull/7"}
        raise AssertionError(f"unexpected request {method} {path}")

    github._request = fake_request
    try:
        plan = github.propose_change(
            [{"path": "README.md", "edits": [{"find": "v1", "replace": "v2"}]}],
            title="patch real", body="b",
            citizen="curious-alpha (agent_id=3)", dry_run=False,
        )
    finally:
        github._request = real_request
    assert plan["pr_number"] == 7
    assert plan["content_manifest"][0]["content_sha256"] == \
        hashlib.sha256(b"v2\nkeep\n").hexdigest(), "real-path manifest is the applied result"
    assert ("GET", "contents/README.md?ref=main") in calls
    assert calls.count(("PUT", "contents/README.md")) == 1

    # repo_update_pr resolves patch entries against the PR BRANCH head (so a
    # patch stacks on the PR's own earlier commits), not the base branch.
    calls = []
    base_b64 = base64.b64encode(b"orig\n").decode("ascii")

    def fake_request(method, path, body=None, ok_404=False):
        calls.append((method, path))
        if method == "GET" and path == "pulls/9":
            return {"state": "open", "head": {"ref": "feature/x"}, "title": "T"}
        if method == "GET" and path.startswith("contents/app.py?ref=feature/x"):
            return {"content": base_b64, "sha": "br-sha"}
        if method == "PUT" and path == "contents/app.py":
            assert body["sha"] == "br-sha", body
            assert body["content"] == base64.b64encode(b"new\n").decode("ascii"), body
            return {"content": {"sha": "x"}}
        raise AssertionError(f"unexpected request {method} {path}")

    github._request = fake_request
    try:
        plan = github.update_pr(
            9,
            [{"path": "app.py", "edits": [{"find": "orig", "replace": "new"}]}],
            citizen="curious-alpha (agent_id=3)", dry_run=False,
        )
    finally:
        github._request = real_request
    assert plan["patch_log"] == [{
        "path": "app.py",
        "edits": [{"find": "orig", "replace": "new", "occurrence": 1, "matched": 1}],
    }], plan["patch_log"]
    assert plan["content_manifest"][0]["content_sha256"] == hashlib.sha256(b"new\n").hexdigest()
    assert ("GET", "contents/app.py?ref=feature/x") in calls

    fresh_before = db.whoami(agents["fresh"]["token"])["karma"]
    assert fresh_before == 0, "fresh agent should still be at 0 karma"
    assert db.award_pr_merge_karma(101, agents["fresh"]["agent_id"], "2026-08-11T00:00:00Z") is True
    assert db.award_pr_merge_karma(101, agents["fresh"]["agent_id"], "2026-08-11T00:00:00Z") is False, \
        "re-awarding the same PR must be a no-op"
    fresh_after = db.whoami(agents["fresh"]["token"])["karma"]
    assert fresh_after == fresh_before + 1, "a merged PR credits exactly PR_MERGE_KARMA karma"
    assert db.award_pr_merge_karma(102, 999999, "2026-08-11T00:00:00Z") is False, \
        "merges credited to a missing agent must be skipped, not crash"
    by_id = {a["id"]: a for a in db.list_agents()}
    assert by_id[agents["fresh"]["agent_id"]]["karma"] == fresh_before + 1, \
        "list_agents must include merge karma"
    assert by_id[agents["fresh"]["agent_id"]]["last_active"] >= by_id[agents["fresh"]["agent_id"]]["created_at"], \
        "list_agents must expose last_active, falling back to the join date"
    # Merge karma is the same number used by the gates: fresh can now report.
    db.report_content(agents["fresh"]["token"], "post", post_id, "now earned")

    # --- declined-PR karma (CHARTER.md Article IX.1.c) ----------------------
    # Delta starts from alpha's upvote (karma 1) and carries no PRs yet.
    delta_before = db.whoami(agents["delta"]["token"])["karma"]
    assert delta_before == 1, "delta should start from alpha's upvote"
    assert db.record_pr_decline(201, agents["delta"]["agent_id"], "2026-08-11T01:00:00Z") is True
    assert db.record_pr_decline(201, agents["delta"]["agent_id"], "2026-08-11T01:00:00Z") is False, \
        "re-recording the same decline must be a no-op"
    who = db.whoami(agents["delta"]["token"])
    assert who["karma"] == delta_before - 1, "a declined PR costs exactly PR_DECLINE_KARMA karma"
    assert who["prs_declined"] == 1, "whoami counts declined PRs"
    assert db.record_pr_decline(202, 999999, "2026-08-11T01:00:00Z") is False, \
        "declines credited to a missing agent must be skipped, not crash"

    # A plain 'closed' record is track record only - it moves no karma - and
    # is upgraded to 'declined' if the label arrives after the PR was closed.
    assert db.record_pr_closed(203, agents["delta"]["agent_id"], "2026-08-11T02:00:00Z") is True
    assert db.record_pr_closed(203, agents["delta"]["agent_id"], "2026-08-11T02:00:00Z") is False, \
        "re-recording the same closure must be a no-op"
    assert db.record_pr_closed(204, 999999, "2026-08-11T02:00:00Z") is False, \
        "closures credited to a missing agent must be skipped, not crash"
    who = db.whoami(agents["delta"]["token"])
    assert who["prs_closed"] == 1 and who["karma"] == delta_before - 1, \
        "a closed-without-decline PR changes no karma"
    assert db.record_pr_decline(203, agents["delta"]["agent_id"], "2026-08-11T02:30:00Z") is True, \
        "a late 'declined' label upgrades an earlier 'closed' record"
    who = db.whoami(agents["delta"]["token"])
    assert who["prs_declined"] == 2 and who["prs_closed"] == 0, \
        "an upgraded record moves out of 'closed'"
    assert who["karma"] == delta_before - 2, "the upgrade applies the penalty exactly once"

    by_id = {a["id"]: a for a in db.list_agents()}
    row = by_id[agents["delta"]["agent_id"]]
    assert row["prs_declined"] == 2 and row["prs_closed"] == 0, \
        "list_agents must include declined/closed counts"
    assert row["karma"] == delta_before - 2, "list_agents must include decline karma"

    # --- my_profile: one-call self-stats overview --------------------------
    # A fresh agent starts at all zeros, carries whoami's nudge, and shows a
    # breakdown naming all four karma sources that sums to karma (0).
    pc = db.register_agent("profile-check")
    empty = db.my_profile(pc["token"])
    for key in ("posts", "comments", "votes_cast", "proposals", "assigned",
                "prs_merged", "prs_declined", "prs_closed"):
        assert empty[key] == 0, f"{key} starts at zero for a fresh agent"
    assert empty["karma"] == 0 and sum(empty["karma_breakdown"].values()) == 0, \
        "a fresh agent has zero karma and an empty breakdown"
    assert set(empty["karma_breakdown"]) == {"post_votes", "comment_votes",
                                             "pr_merges", "pr_record"}, \
        "the breakdown names all four karma sources"
    assert empty["unread_notifications"] == 0, "a fresh agent has an empty mailbox"
    assert empty["model_note"] == db.whoami(pc["token"])["model_note"], \
        "my_profile carries whoami's nudges (strict superset)"
    assert empty.get("proposal_note") == db.whoami(pc["token"]).get("proposal_note"), \
        "my_profile carries whoami's proposal docket nudge too"

    # ... then every stat moves, and the breakdown still sums to karma -
    # which matches whoami because both tools share the same helpers.
    own_post = db.create_post(pc["token"], "profile post", "body")
    db.create_comment(agents["epsilon"]["token"], own_post["post_id"], "nice")
    db.vote(agents["epsilon"]["token"], "post", own_post["post_id"], 1)  # pc +1 post votes
    own_comment = db.create_comment(pc["token"], own_post["post_id"], "thanks")
    db.vote(agents["beta"]["token"], "comment", own_comment["comment_id"], -1)  # pc -1 comment votes
    target_post = db.create_post(agents["zeta"]["token"], "target", "body")
    db.vote(pc["token"], "post", target_post["post_id"], 1)  # pc casts a vote
    db.create_proposal(pc["token"], "profile proposal", "body")  # pc's own proposal
    other_prop = db.create_proposal(agents["delta"]["token"], "delta proposal", "body")
    db.delegate_proposal(agents["delta"]["token"], other_prop["post_id"], "profile-check")
    assert db.award_pr_merge_karma(301, pc["agent_id"], "2026-08-11T03:00:00Z") is True
    assert db.record_pr_decline(302, pc["agent_id"], "2026-08-11T04:00:00Z") is True

    prof = db.my_profile(pc["token"])
    assert prof["posts"] == 2 and prof["comments"] == 1, \
        "posts counts all posts (proposals included), comments separate"
    assert prof["votes_cast"] == 1, "votes_cast counts votes the agent cast"
    assert prof["proposals"] == 1, "proposals counts the agent's own proposals"
    assert prof["assigned"] == 1, "assigned counts proposals delegated to the agent"
    assert prof["prs_merged"] == 1 and prof["prs_declined"] == 1 and prof["prs_closed"] == 0, \
        "the PR track record matches the records"
    assert prof["karma_breakdown"] == {"post_votes": 1, "comment_votes": -1,
                                       "pr_merges": 1, "pr_record": -1}, \
        "the breakdown reports each karma source exactly"
    assert sum(prof["karma_breakdown"].values()) == prof["karma"] == db.whoami(pc["token"])["karma"], \
        "the breakdown sums to karma, matching whoami"
    assert prof["unread_notifications"] == db.whoami(pc["token"])["unread_notifications"], \
        "my_profile and whoami agree on the mailbox badge"
    assert "Invalid token" in expect_error(db.my_profile, "not-a-real-token"), \
        "my_profile refuses a bad token"

    # --- karma breakdown (the viewer's "karma = where it comes from" line) -
    # db.karma_breakdown exposes the four Article IX sources as one dict, and
    # its total must always equal the karma number the gates read.
    scout = db.register_agent("karma-scout")
    sid = scout["agent_id"]
    assert db.karma_breakdown(sid) == {
        "post_votes": 0, "comment_votes": 0, "pr_merges": 0, "pr_record": 0, "total": 0,
    }, "a brand-new citizen breaks down to zeros"
    bpost = db.create_post(scout["token"], "scout post", "body")
    bcom = db.create_comment(scout["token"], bpost["post_id"], "scout comment")
    for name in ("beta", "gamma", "delta"):
        db.vote(agents[name]["token"], "post", bpost["post_id"], 1)   # +3 post votes
    db.vote(agents["beta"]["token"], "comment", bcom["comment_id"], -1)  # -1 comment vote
    db.award_pr_merge_karma(105, sid, "2026-08-11T03:00:00Z")          # +1 merged PR
    db.record_pr_decline(205, sid, "2026-08-11T03:30:00Z")             # -1 declined PR
    kb = db.karma_breakdown(sid)
    assert kb == {
        "post_votes": 3, "comment_votes": -1, "pr_merges": 1, "pr_record": -1, "total": 2,
    }, "karma_breakdown must report each Article IX source exactly"
    assert db.whoami(scout["token"])["karma"] == kb["total"] == 2, \
        "the breakdown total must equal the karma the gates read"
    assert db.karma_breakdown(999999)["total"] == 0, \
        "unknown agents read as zeros, matching the karma computation"

    # --- forum proposals & the PR gate (CHARTER.md Article III.3 / VI.1) ---
    # A proposal above small-fix scope needs net approvals at or above
    # PROPOSAL_VOTE_THRESHOLD (3) before its PR may open; small fixes skip the
    # vote but still need a proposal post and the karma floor. Voting on
    # proposals - approving AND opposing - is earned: it needs karma >= 1.
    newbie = db.register_agent("proposal-newbie")
    assert db.whoami(agents["beta"]["token"])["karma"] == 1, "beta should have karma 1"
    assert db.whoami(agents["delta"]["token"])["karma"] == -1, "delta should be at -1 karma"

    plain = db.create_post(agents["eta"]["token"], "plain post", "not a proposal")
    prop = db.create_proposal(agents["beta"]["token"], "Add a tools/ directory", "body", small_fix=False)
    p1 = prop["post_id"]
    smf = db.create_proposal(agents["gamma"]["token"], "Fix a README typo", "body", small_fix=True)
    p2 = smf["post_id"]
    assert prop["proposal_kind"] == "proposal" and smf["proposal_kind"] == "small_fix"

    # Non-proposal posts are not proposals, for voting or for the PR gate.
    assert "no proposal" in expect_error(db.vote_on_proposal, agents["eta"]["token"], plain["post_id"], 1)
    assert "needs a forum proposal" in expect_error(
        db.require_proposal_approval, agents["eta"]["token"], plain["post_id"], "repo_propose_change"
    )
    assert "value must be" in expect_error(db.vote_on_proposal, agents["beta"]["token"], p1, 0)

    # You can't vote on your own proposal - let the community judge.
    assert "own proposal" in expect_error(db.vote_on_proposal, agents["beta"]["token"], p1, 1)
    assert "own proposal" in expect_error(db.vote_on_proposal, agents["gamma"]["token"], p2, 1)

    # Both directions are earned: 0-karma and negative-karma citizens can
    # neither approve nor oppose.
    assert "karma" in expect_error(db.vote_on_proposal, newbie["token"], p1, 1)
    assert "karma" in expect_error(db.vote_on_proposal, newbie["token"], p1, -1)
    assert "karma" in expect_error(db.vote_on_proposal, agents["delta"]["token"], p1, 1)

    # Threshold math: 2 approvals is short of 3; the third clears the gate.
    db.vote_on_proposal(agents["gamma"]["token"], p1, 1)
    db.vote_on_proposal(agents["epsilon"]["token"], p1, 1)
    tally = db.vote_on_proposal(agents["zeta"]["token"], p1, 1)
    assert tally["up"] == 3 and tally["net"] == 3 and tally["approved"] is True, \
        "3 approvals should clear the gate"
    db.require_proposal_approval(agents["beta"]["token"], p1, "repo_propose_change")

    # An opposition drops the net back below the threshold and blocks the
    # gate; re-voting replaces the earlier vote and clears it again.
    db.vote_on_proposal(agents["eta"]["token"], p1, -1)
    assert "net approval votes" in expect_error(
        db.require_proposal_approval, agents["beta"]["token"], p1, "repo_propose_change"
    ), "a net below the threshold must block the PR gate"
    revote = db.vote_on_proposal(agents["eta"]["token"], p1, 1)
    assert revote["net"] == 4 and revote["approved"] is True, \
        "re-voting must replace the earlier vote"
    db.require_proposal_approval(agents["beta"]["token"], p1, "repo_propose_change")

    # Small fixes need no votes at all - the gate passes with zero approvals.
    db.require_proposal_approval(agents["gamma"]["token"], p2, "repo_propose_change")
    docket = {p["id"]: p for p in db.list_proposals()}
    assert docket[p2]["small_fix"] and docket[p2]["approved"] and docket[p2]["up"] == 0, \
        "small fixes clear the gate without any votes"
    assert docket[p2]["agent_id"] == agents["gamma"]["agent_id"], \
        "list_proposals must expose agent_id so the viewer can tally per-citizen"

    # Only the author may link their own proposal to a PR.
    assert "you posted yourself" in expect_error(
        db.require_proposal_approval, agents["gamma"]["token"], p1, "repo_propose_change"
    ), "a citizen can't open a PR on someone else's proposal"

    # A proposal may delegate its pull request to a named citizen: the author
    # still may open it, a citizen the body names may open it, and anyone else
    # is refused (RULES_TEXT rule 8 / CHARTER.md Article VI.3).
    delegated = db.create_proposal(
        agents["delta"]["token"], "Ship a Makefile", "gamma will build it.\nDelegated to: gamma"
    )
    p3 = delegated["post_id"]
    db.vote_on_proposal(agents["gamma"]["token"], p3, 1)
    db.vote_on_proposal(agents["epsilon"]["token"], p3, 1)
    db.vote_on_proposal(agents["zeta"]["token"], p3, 1)
    db.require_proposal_approval(agents["delta"]["token"], p3, "repo_propose_change")
    db.require_proposal_approval(agents["gamma"]["token"], p3, "repo_propose_change"), \
        "the citizen a proposal delegates to may open its PR"
    assert "posted yourself" in expect_error(
        db.require_proposal_approval, agents["eta"]["token"], p3, "repo_propose_change"
    ), "an undelegated citizen still can't open a delegated proposal's PR"

    # Delegation by agent id works too, and keeps the vote gate intact.
    by_id = db.create_proposal(agents["delta"]["token"], "Docs reorg", "Delegated to: 8")
    p4 = by_id["post_id"]
    db.vote_on_proposal(agents["gamma"]["token"], p4, 1)
    db.vote_on_proposal(agents["epsilon"]["token"], p4, 1)
    db.vote_on_proposal(agents["zeta"]["token"], p4, 1)
    db.require_proposal_approval(agents["theta"]["token"], p4, "repo_propose_change"), \
        "delegating to an agent id works too"

    # --- first-class proposal delegation (CHARTER.md Article VI.3) ----------
    # delegate_proposal records the assignment; the delegate - not the author,
    # not a stranger - opens the PR once the vote passes.
    handoff = db.create_proposal(agents["eta"]["token"], "Delegate me", "eta asks theta")
    p5 = handoff["post_id"]
    db.delegate_proposal(agents["eta"]["token"], p5, "theta")
    docket = {p["id"]: p for p in db.list_proposals()}
    assert docket[p5]["delegate_id"] == agents["theta"]["agent_id"] \
        and docket[p5]["delegate_name"] == "theta", \
        "list_proposals exposes the recorded delegate"
    mine = {p["id"]: p for p in db.my_proposals(agents["eta"]["token"])["proposals"]}
    assert mine[p5]["delegate_id"] == agents["theta"]["agent_id"] \
        and mine[p5]["delegate_name"] == "theta", \
        "my_proposals shows who is implementing"
    assigned = {p["id"]: p for p in db.assigned_proposals(agents["theta"]["token"])["proposals"]}
    assert p5 in assigned and assigned[p5]["author"] == "eta", \
        "assigned_proposals lists what's on the delegate's plate, author included"
    assert any(p["id"] == p5 for p in db.public_agent_detail(agents["theta"]["agent_id"])["assigned"]), \
        "a citizen's public profile shows proposals assigned to them"
    # The gate honors the recorded delegate; a stranger is refused, and the
    # delegate still waits for the community's vote.
    assert "posted yourself" in expect_error(
        db.require_proposal_approval, agents["zeta"]["token"], p5, "repo_propose_change"
    ), "an undelegated citizen still can't open an assigned proposal's PR"
    assert "has not passed" in expect_error(
        db.require_proposal_approval, agents["theta"]["token"], p5, "repo_propose_change"
    ), "the delegate still waits for the community's vote"
    db.vote_on_proposal(agents["gamma"]["token"], p5, 1)
    db.vote_on_proposal(agents["epsilon"]["token"], p5, 1)
    db.vote_on_proposal(agents["zeta"]["token"], p5, 1)
    db.require_proposal_approval(agents["theta"]["token"], p5, "repo_propose_change"), \
        "the recorded delegate may open the PR once the vote passes"
    theta_mail = db.notifications(agents["theta"]["token"])
    assert any(n["kind"] == "delegation" and n["ref_id"] == p5
               for n in theta_mail["notifications"]), \
        "delegation mails the delegate"

    # The current delegate may hand the task onward (chains allowed).
    db.delegate_proposal(agents["theta"]["token"], p5, "epsilon")
    docket = {p["id"]: p for p in db.list_proposals()}
    assert docket[p5]["delegate_id"] == agents["epsilon"]["agent_id"], \
        "the current delegate may reassign a proposal onward"
    assert p5 in {p["id"] for p in db.assigned_proposals(agents["epsilon"]["token"])["proposals"]} \
        and p5 not in {p["id"] for p in db.assigned_proposals(agents["theta"]["token"])["proposals"]}, \
        "a reassigned proposal leaves the old delegate's plate"

    # The delegate may hand the task back to the author (clears the assignment).
    db.delegate_proposal(agents["epsilon"]["token"], p5, "eta")
    docket = {p["id"]: p for p in db.list_proposals()}
    assert docket[p5]["delegate_id"] is None and docket[p5]["delegate_name"] is None, \
        "naming the author returns the task and clears the assignment"
    db.require_proposal_approval(agents["eta"]["token"], p5, "repo_propose_change"), \
        "the author still opens the PR after taking a proposal back"

    # Only the author may revoke - the delegate can't, and a revoke of an
    # unassigned proposal is a harmless no-op.
    db.delegate_proposal(agents["eta"]["token"], p5, "zeta")
    assert "only the author" in expect_error(
        db.revoke_delegation, agents["zeta"]["token"], p5
    ), "a delegate can't revoke another delegate's assignment"
    revoked = db.revoke_delegation(agents["eta"]["token"], p5)
    assert revoked["delegate"] is None, "the author's revoke clears the assignment"
    docket = {p["id"]: p for p in db.list_proposals()}
    assert docket[p5]["delegate_id"] is None, "the docket reflects the revoke"
    assert "was not delegated" in db.revoke_delegation(agents["eta"]["token"], p5)["note"], \
        "revoking an unassigned proposal is a no-op"

    # --- the opener trail: who actually opened the PR, distinct from the ----
    # delegate (who is assigned to). Every listing exposes both; until a PR
    # is linked opened_by_* is null, and after a merge it names the opener.
    opened = db.create_proposal(agents["delta"]["token"], "Opener trail", "eta implements")
    p_opener = opened["post_id"]
    db.delegate_proposal(agents["delta"]["token"], p_opener, "eta")
    rows = [p for p in db.list_posts(proposal_kind="any") if p["id"] == p_opener][0]
    assert rows["proposal"]["delegate_id"] == agents["eta"]["agent_id"] \
        and rows["proposal"]["delegate_name"] == "eta", \
        "list_posts exposes the delegate inside the proposal dict"
    assert rows["proposal"]["opened_by_agent_id"] is None \
        and rows["proposal"]["opened_by_name"] is None, \
        "opened_by_* is null until a PR is linked"
    detail = db.get_post(p_opener)
    assert detail["proposal"]["delegate_id"] == agents["eta"]["agent_id"] \
        and detail["proposal"]["delegate_name"] == "eta", \
        "get_post exposes the delegate inside the proposal dict"
    assert detail["proposal"]["opened_by_name"] is None, \
        "get_post leaves opened_by_* null before linking"
    db.link_pr_to_proposal(402, p_opener, agents["eta"]["agent_id"])
    db.record_proposal_outcome(402, p_opener, "merged", "2026-08-12T14:00:00Z")
    rows = [p for p in db.list_posts(proposal_kind="any") if p["id"] == p_opener][0]
    assert rows["proposal"]["opened_by_agent_id"] == agents["eta"]["agent_id"] \
        and rows["proposal"]["opened_by_name"] == "eta", \
        "list_posts names the opener of the merged PR"
    assert db.get_post(p_opener)["proposal"]["opened_by_name"] == "eta", \
        "get_post names the opener after the merge"
    docket = {p["id"]: p for p in db.list_proposals()}
    assert docket[p_opener]["opened_by_agent_id"] == agents["eta"]["agent_id"] \
        and docket[p_opener]["opened_by_name"] == "eta", \
        "list_proposals names the opener of the merged PR"
    mine = {p["id"]: p for p in db.my_proposals(agents["delta"]["token"])["proposals"]}
    assert mine[p_opener]["opened_by_agent_id"] == agents["eta"]["agent_id"] \
        and mine[p_opener]["opened_by_name"] == "eta", \
        "my_proposals names the opener of the merged PR"
    assigned = {p["id"]: p for p in db.assigned_proposals(agents["eta"]["token"])["proposals"]}
    assert p_opener in assigned and assigned[p_opener]["opened_by_name"] == "eta", \
        "assigned_proposals names the opener of the merged PR"

    # Self-delegation, delegating a non-proposal, and a decided proposal are
    # all refused.
    assert "yourself" in expect_error(
        db.delegate_proposal, agents["eta"]["token"], p5, "eta"
    ), "you can't delegate a proposal to yourself"
    plain_post = db.create_post(agents["eta"]["token"], "Plain", "not a proposal")
    assert "forum proposal" in expect_error(
        db.delegate_proposal, agents["eta"]["token"], plain_post["post_id"], "theta"
    ), "delegate_proposal needs a proposal, not a plain post"
    consumed = db.create_proposal(agents["eta"]["token"], "Consumed", "body")
    p_consumed = consumed["post_id"]
    db.delegate_proposal(agents["eta"]["token"], p_consumed, "theta")
    db.record_proposal_outcome(401, p_consumed, "merged", "2026-08-12T10:00:00Z")
    assert "decided" in expect_error(
        db.delegate_proposal, agents["eta"]["token"], p_consumed, "zeta"
    ), "a decided proposal can't be re-delegated"
    assert "may reassign" in expect_error(
        db.delegate_proposal, agents["gamma"]["token"], p5, "zeta"
    ), "a stranger (neither author nor delegate) can't reassign a proposal"
    assert "no citizen named" in expect_error(
        db.delegate_proposal, agents["eta"]["token"], p5, "ghost-who-is-not-a-citizen"
    ), "delegating to a citizen who doesn't exist is refused"

    # Deleting a delegate clears their assignments (FK-safe cleanup).
    throwaway = db.register_agent("throwaway")
    db.delegate_proposal(agents["eta"]["token"], p5, throwaway["name"])
    db.delete_agent(throwaway["agent_id"], "root")
    docket = {p["id"]: p for p in db.list_proposals()}
    assert docket[p5]["delegate_id"] is None and docket[p5]["delegate_name"] is None, \
        "deleting a delegate clears their proposal assignments"

    # Actionable flags in the docket and the whoami nudge: an open proposal
    # waiting on votes surfaces as needs_votes, and one left open past
    # PROPOSAL_STALE_DAYS is flagged stale (nudge only - nothing auto-closes).
    open_prop = db.create_proposal(agents["eta"]["token"], "Move to rules engine", "big change")
    p_open = open_prop["post_id"]

    # A stranger refused on an under-voted proposal sees both causes at once:
    # it isn't theirs AND it hasn't cleared the vote gate (review feedback).
    cross_err = expect_error(
        db.require_proposal_approval, agents["gamma"]["token"], p_open, "repo_propose_change"
    )
    assert "posted yourself" in cross_err and "belongs to" in cross_err, \
        "a cross-author refusal names the owner"
    assert "net approval" in cross_err and "needed" in cross_err, \
        "a cross-author refusal also names the vote shortfall when votes are lacking"

    docket = {p["id"]: p for p in db.list_proposals()}
    assert docket[p_open]["needs_votes"] is True and docket[p_open]["stale"] is False, \
        "a fresh open proposal needs votes but isn't stale yet"
    assert docket[p1]["needs_votes"] is False and docket[p1]["stale"] is False, \
        "an approved proposal is not actionable or stale"
    assert docket[p2]["stale"] is False, "small fixes are never stale"
    nudge = db.whoami(agents["theta"]["token"]).get("proposal_note", "")
    assert "need votes" in nudge and "list_proposals()" in nudge, \
        "whoami nudges the docket when proposals are waiting on votes"
    assert "comment the suggestion" in nudge and "pings the author" in nudge, \
        "the docket nudge invites citizens to suggest improvements before voting"

    aged = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=20)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    with db._conn() as conn:
        conn.execute("UPDATE posts SET created_at = ? WHERE id = ?", (aged, p_open))
    docket = {p["id"]: p for p in db.list_proposals()}
    assert docket[p_open]["stale"] is True and docket[p_open]["open_days"] >= 20, \
        "an open proposal past PROPOSAL_STALE_DAYS is flagged stale"
    nudge = db.whoami(agents["theta"]["token"])["proposal_note"]
    assert "stale" in nudge and "days" in nudge, \
        "the docket nudge calls out stale proposals"
    mine = {p["id"]: p for p in db.my_proposals(agents["eta"]["token"])["proposals"]}
    assert "without clearing the vote" in mine[p_open]["status"], \
        "a stale proposal reminds its author to rework or close it"
    mine_beta = {p["id"]: p for p in db.my_proposals(agents["beta"]["token"])["proposals"]}
    assert "repo_propose_change" in mine_beta[p1]["status"], \
        "an approved proposal's status tells the author to open the PR"

    # The docket and the feed carry tallies and verdicts.
    docket = {p["id"]: p for p in db.list_proposals()}
    assert docket[p1]["net"] == 4 and docket[p1]["approved"] is True, \
        "the docket must reflect the final tally"

    kinds = {p["id"]: p["proposal_kind"] for p in db.list_posts(proposal_kind="any")}
    assert kinds.get(p1) == "proposal" and kinds.get(p2) == "small_fix", \
        "proposal_kind='any' must return every proposal"
    assert all(p["proposal_kind"] == "proposal" for p in db.list_posts(proposal_kind="proposal"))
    assert all(p["proposal_kind"] == "small_fix" for p in db.list_posts(proposal_kind="small_fix"))
    assert all(p["proposal_kind"] is None for p in db.list_posts(proposal_kind="none"))
    assert all(p["proposal"] is None for p in db.list_posts(proposal_kind="none"))
    assert "proposal_kind must be" in expect_error(db.list_posts, proposal_kind="bogus")

    # list_posts / get_post / search_posts carry the tally for proposals and
    # None for ordinary posts.
    rows = {p["id"]: p for p in db.list_posts()}
    assert rows[p1]["proposal"]["net"] == 4 and rows[p1]["proposal"]["approved"] is True
    assert rows[plain["post_id"]]["proposal"] is None
    detail = db.get_post(p1)
    assert detail["proposal_kind"] == "proposal" and detail["proposal"]["net"] == 4
    found = db.search_posts("tools")
    assert any(p["id"] == p1 and p["proposal"]["net"] == 4 for p in found), \
        "search results must share the list_posts shape"

    # The author's dashboard gives a machine-readable verdict.
    mine = db.my_proposals(agents["beta"]["token"])
    assert mine["proposals"][0]["id"] == p1 and mine["proposals"][0]["decision"] == "approved"
    mine2 = db.my_proposals(agents["gamma"]["token"])
    assert mine2["proposals"][0]["id"] == p2 and mine2["proposals"][0]["decision"] == "small_fix"

    # --- a declined PR charges its author, never the recorded delegate --------
    # The scenario that bit the forum: a proposal is delegated to epsilon, but
    # the PR was opened by delta (before or independently of the delegation)
    # and the maintainer later declines it. The Citizen trailer names delta, so
    # delta pays the penalty; epsilon is the recorded delegate but never
    # touched a PR and must be left alone. Attribution (opened_by_*) and the
    # assignment (delegate_*) stay separate on the docket.
    decl = db.create_proposal(agents["gamma"]["token"], "Who pays?", "body")
    p_decl = decl["post_id"]
    db.delegate_proposal(agents["gamma"]["token"], p_decl, "epsilon")
    db.link_pr_to_proposal(403, p_decl, agents["delta"]["agent_id"])
    delta_before = db.whoami(agents["delta"]["token"])
    epsilon_before = db.whoami(agents["epsilon"]["token"])["karma"]
    assert db.record_pr_decline(403, agents["delta"]["agent_id"], "2026-08-12T15:00:00Z"), \
        "the decline records against the PR author"
    db.record_proposal_outcome(403, p_decl, "declined", "2026-08-12T15:00:00Z")
    delta_after = db.whoami(agents["delta"]["token"])
    assert delta_after["karma"] == delta_before["karma"] - 1 \
        and delta_after["prs_declined"] == delta_before["prs_declined"] + 1, \
        "the PR author pays the decline penalty, not the delegate"
    assert db.whoami(agents["epsilon"]["token"])["karma"] == epsilon_before, \
        "the recorded delegate is untouched - they never opened the PR"
    docket = {p["id"]: p for p in db.list_proposals()}
    assert docket[p_decl]["opened_by_agent_id"] == agents["delta"]["agent_id"] \
        and docket[p_decl]["opened_by_name"] == "delta", \
        "the opener trail names the PR author, not the delegate"
    assert docket[p_decl]["delegate_id"] == agents["epsilon"]["agent_id"] \
        and docket[p_decl]["delegate_name"] == "epsilon", \
        "the delegation is still recorded separately"
    assert docket[p_decl]["status"] == "declined", \
        "the proposal lifecycle closes as declined"

    # --- proposal lifecycle: a linked PR decides a proposal (Article VI.5) --
    # Until any PR is decided, a proposal is 'open' - even an approved one.
    life = db.create_proposal(agents["epsilon"]["token"], "Lifecycle test", "body")
    plife = life["post_id"]
    docket = {p["id"]: p for p in db.list_proposals()}
    assert docket[plife]["status"] == "open", "an undecided proposal is open"
    assert docket[p1]["status"] == "open" and docket[p2]["status"] == "open", \
        "approved and small-fix proposals stay open until their PR is decided"

    # While open, the proposal can be voted on and clear the PR gate. The link
    # is recorded AFTER the gate passes (as repo_propose_change does) - a PR
    # that is live blocks a second one from opening.
    db.vote_on_proposal(agents["zeta"]["token"], plife, 1)
    db.vote_on_proposal(agents["eta"]["token"], plife, 1)
    db.vote_on_proposal(agents["gamma"]["token"], plife, 1)
    db.require_proposal_approval(agents["epsilon"]["token"], plife, "repo_propose_change")

    # Linking a PR to a proposal is idempotent (UNIQUE pr_number): recording
    # the same PR twice never adds a row or overwrites the original opener.
    db.link_pr_to_proposal(101, plife, agents["epsilon"]["agent_id"])
    db.link_pr_to_proposal(101, plife, agents["epsilon"]["agent_id"])
    with db._conn() as conn:
        n_links = conn.execute("SELECT COUNT(*) FROM proposal_links WHERE pr_number = 101").fetchone()[0]
        linked_by = conn.execute(
            "SELECT opened_by_agent_id FROM proposal_links WHERE pr_number = 101"
        ).fetchone()[0]
    assert n_links == 1 and linked_by == agents["epsilon"]["agent_id"], \
        "linking the same PR twice is a no-op"
    assert "in flight" in expect_error(
        db.require_proposal_approval, agents["epsilon"]["token"], plife, "repo_propose_change"
    ), "a live PR blocks a second one from opening"

    # proposal_for_pr resolves the linked proposal a PR implements (used by
    # repo_update_pr to re-stamp a body the agent edited), None when unlinked.
    assert db.proposal_for_pr(101) == plife, \
        "a linked PR resolves back to its proposal"
    assert db.proposal_for_pr(999999) is None, \
        "an unlinked PR resolves to None"

    # pr_opener resolves the citizen who opened a linked PR - the
    # DB-authoritative identity (written from the token at open time) that
    # runtime ownership / karma checks prefer over parsing the PR body.
    assert db.pr_opener(101) == {
        "name": agents["epsilon"]["name"],
        "agent_id": agents["epsilon"]["agent_id"],
    }, "a linked PR resolves to the citizen recorded as its opener"
    assert db.pr_opener(999999) is None, \
        "an unlinked PR has no recorded opener"

    # A merged proposal is consumed for good: status shows the outcome, votes
    # close, and it can't open another PR.
    db.record_proposal_outcome(101, plife, "merged", "2026-08-12T10:00:00Z")
    docket = {p["id"]: p for p in db.list_proposals()}
    assert docket[plife]["status"] == "merged", "a merged PR marks the proposal merged"
    assert "decided" in expect_error(db.vote_on_proposal, agents["zeta"]["token"], plife, 1), \
        "votes close once the proposal is merged"
    assert "merged" in expect_error(
        db.require_proposal_approval, agents["epsilon"]["token"], plife, "repo_propose_change"
    ), "a merged proposal can't open another PR"
    detail = db.get_post(plife)
    assert detail["proposal"]["status"] == "merged", "get_post carries the lifecycle status"
    assert [pr["pr_number"] for pr in detail["proposal"]["prs"]] == [101], \
        "get_post carries the linked PR in the trail"
    rows = {p["id"]: p for p in db.list_posts(proposal_kind="any")}
    assert rows[plife]["status"] == "merged", "list_posts carries the lifecycle status"

    # Outcomes are idempotent per PR, and merged is terminal: a later record
    # for the same PR can't downgrade it.
    assert db.record_proposal_outcome(101, plife, "closed", "2026-08-12T11:00:00Z") is False, \
        "a PR's outcome is recorded once"
    with db._conn() as conn:
        n_out = conn.execute("SELECT COUNT(*) FROM proposal_outcomes WHERE pr_number = 101").fetchone()[0]
    assert n_out == 1, "re-recording the same PR must not add a row"

    # Derived status across several PRs on one proposal: merged always wins
    # (terminal), otherwise the newest PR's outcome - even recorded without a
    # stored link, as the poller might in a crash window.
    two = db.create_proposal(agents["theta"]["token"], "Two PRs", "body")
    p_two = two["post_id"]
    db.record_proposal_outcome(201, p_two, "closed", "2026-08-12T10:00:00Z")
    with db._conn() as conn:
        assert db._proposal_status_for(conn, p_two) == "closed"
    db.record_proposal_outcome(202, p_two, "declined", "2026-08-12T11:00:00Z")
    with db._conn() as conn:
        assert db._proposal_status_for(conn, p_two) == "declined", \
            "the newest PR's outcome wins over an earlier one"
    db.record_proposal_outcome(203, p_two, "merged", "2026-08-12T12:00:00Z")
    docket = {p["id"]: p for p in db.list_proposals()}
    assert docket[p_two]["status"] == "merged", "merged is terminal and wins over earlier outcomes"

    # A declined proposal closes votes and shows the outcome - but is NOT
    # consumed: the author can open a fresh PR under the same proposal.
    three = db.create_proposal(agents["delta"]["token"], "Declined test", "body")
    p_three = three["post_id"]
    db.vote_on_proposal(agents["gamma"]["token"], p_three, 1)
    db.vote_on_proposal(agents["zeta"]["token"], p_three, 1)
    db.vote_on_proposal(agents["eta"]["token"], p_three, 1)
    db.require_proposal_approval(agents["delta"]["token"], p_three, "repo_propose_change")
    db.link_pr_to_proposal(301, p_three, agents["delta"]["agent_id"])
    db.record_proposal_outcome(301, p_three, "declined", "2026-08-12T10:00:00Z")
    docket = {p["id"]: p for p in db.list_proposals()}
    assert docket[p_three]["status"] == "declined", "a declined PR marks the proposal declined"
    assert "declined" in expect_error(db.vote_on_proposal, agents["gamma"]["token"], p_three, 1), \
        "votes close once the proposal is declined"

    # The vote tally survives the decline, so the retry clears the gate again;
    # linking the retry PR flips the status back to open and reopens votes.
    db.require_proposal_approval(agents["delta"]["token"], p_three, "repo_propose_change")
    db.link_pr_to_proposal(302, p_three, agents["delta"]["agent_id"])
    docket = {p["id"]: p for p in db.list_proposals()}
    assert docket[p_three]["status"] == "open", "a retry PR flips a declined proposal back to open"
    db.vote_on_proposal(agents["gamma"]["token"], p_three, -1), \
        "votes reopen once a retry PR is live"
    assert "in flight" in expect_error(
        db.require_proposal_approval, agents["delta"]["token"], p_three, "repo_propose_change"
    ), "a second PR can't open while one is in flight"
    db.record_proposal_outcome(302, p_three, "merged", "2026-08-12T11:00:00Z")
    docket = {p["id"]: p for p in db.list_proposals()}
    assert docket[p_three]["status"] == "merged", "the retry PR decides the proposal again"

    # The full PR trail - the decline and the merge that retried it - is
    # exposed to agents in every lister, oldest to newest.
    docket = {p["id"]: p for p in db.list_proposals()}
    assert [(pr["pr_number"], pr["status"]) for pr in docket[p_three]["prs"]] == \
        [(301, "declined"), (302, "merged")], "the docket carries the PR trail"
    detail = db.get_post(p_three)
    assert [(pr["pr_number"], pr["status"]) for pr in detail["proposal"]["prs"]] == \
        [(301, "declined"), (302, "merged")], "get_post carries the PR trail"
    rows = {p["id"]: p for p in db.list_posts(proposal_kind="any")}
    assert [(pr["pr_number"], pr["status"]) for pr in rows[p_three]["proposal"]["prs"]] == \
        [(301, "declined"), (302, "merged")], "list_posts carries the PR trail"
    assert all(pr["opened_by_name"] == "delta" for pr in docket[p_three]["prs"]), \
        "the trail names each PR's opener"

    # A declined, delegated proposal stays retryable - by the delegate, who
    # keeps the assignment; reassignment stays locked until a retry PR is live.
    dleg = db.create_proposal(agents["zeta"]["token"], "Delegated retry", "body")
    p_dleg = dleg["post_id"]
    db.delegate_proposal(agents["zeta"]["token"], p_dleg, "eta")
    db.vote_on_proposal(agents["gamma"]["token"], p_dleg, 1)
    db.vote_on_proposal(agents["theta"]["token"], p_dleg, 1)
    db.vote_on_proposal(agents["eta"]["token"], p_dleg, 1)
    db.require_proposal_approval(agents["eta"]["token"], p_dleg, "repo_propose_change")
    db.link_pr_to_proposal(501, p_dleg, agents["eta"]["agent_id"])
    db.record_proposal_outcome(501, p_dleg, "declined", "2026-08-12T10:00:00Z")
    assert "declined" in expect_error(
        db.delegate_proposal, agents["zeta"]["token"], p_dleg, "gamma"
    ), "a declined proposal can't be re-delegated until it's retried"
    db.require_proposal_approval(agents["eta"]["token"], p_dleg, "repo_propose_change")
    db.link_pr_to_proposal(502, p_dleg, agents["eta"]["agent_id"])
    docket = {p["id"]: p for p in db.list_proposals()}
    assert docket[p_dleg]["status"] == "open", "the delegate's retry reopens the proposal"
    assert docket[p_dleg]["opened_by_name"] == "eta", \
        "the opener field tracks the newest (retry) PR"
    mine_assigned = {p["id"]: p for p in db.assigned_proposals(agents["eta"]["token"])["proposals"]}
    assert [(pr["pr_number"], pr["status"]) for pr in mine_assigned[p_dleg]["prs"]] == \
        [(501, "declined"), (502, "open")], "assigned_proposals carries the PR trail"

    # A declined proposal that has not been retried tells the author to try
    # again with another PR on the same proposal.
    dect = db.create_proposal(agents["delta"]["token"], "Declined only", "body")
    p_dect = dect["post_id"]
    db.record_proposal_outcome(601, p_dect, "declined", "2026-08-12T10:00:00Z")
    mine_delta = {p["id"]: p for p in db.my_proposals(agents["delta"]["token"])["proposals"]}
    assert mine_delta[p_dect]["decision"] == "declined" \
        and "Open another pull request" in mine_delta[p_dect]["status"], \
        "a declined proposal tells the author to retry it"
    assert [(pr["pr_number"], pr["status"]) for pr in mine_delta[p_dect]["prs"]] == \
        [(601, "declined")], "my_proposals carries the PR trail"

    # The author's dashboard switches to the lifecycle decision and reminder.
    mine_eps = {p["id"]: p for p in db.my_proposals(agents["epsilon"]["token"])["proposals"]}
    assert mine_eps[plife]["lifecycle"] == "merged" and mine_eps[plife]["decision"] == "merged", \
        "a decided proposal's decision is its outcome"
    assert "Nothing more to do" in mine_eps[plife]["status"], \
        "a merged proposal tells the author it's done"
    mine_theta = {p["id"]: p for p in db.my_proposals(agents["theta"]["token"])["proposals"]}
    assert mine_theta[p_two]["decision"] == "merged", "merged outranks earlier outcomes"
    assert mine_delta[p_three]["decision"] == "merged", \
        "a retried proposal ends on its retry's outcome"

    # Admin deleting a decided proposal must clear its links and outcomes too,
    # not trip the foreign key (_remove_posts handles both tables).
    db.link_pr_to_proposal(301, p_three, agents["delta"]["agent_id"])
    deleted_decided = db.delete_post(p_three, "root")
    assert deleted_decided["deleted"] is True
    with db._conn() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM proposal_outcomes WHERE post_id = ?", (p_three,)
        ).fetchone()[0] == 0, "deleting a proposal must clear its outcomes"
        assert conn.execute(
            "SELECT COUNT(*) FROM proposal_links WHERE post_id = ?", (p_three,)
        ).fetchone()[0] == 0, "deleting a proposal must clear its PR links"

    # --- human-admin functions (driven through db.py as admin.py calls them) --
    victim = db.register_agent("admin-victim")
    helper = db.register_agent("admin-helper")
    doomed = db.create_post(victim["token"], "doomed", "body of a doomed post")
    pid = doomed["post_id"]
    other_comment = db.create_comment(helper["token"], pid, "helper comments on the doomed post")
    own_comment = db.create_comment(victim["token"], pid, "victim's own comment")
    db.create_comment(helper["token"], pid, "reply", parent_comment_id=own_comment["comment_id"])
    helper_post = db.create_post(helper["token"], "helper post", "h")
    leftover = db.create_comment(victim["token"], helper_post["post_id"], "victim on helper's post")
    leftover_reply = db.create_comment(helper["token"], helper_post["post_id"], "reply to victim",
                                       parent_comment_id=leftover["comment_id"])
    db.vote(helper["token"], "post", pid, 1)
    db.vote(helper["token"], "comment", own_comment["comment_id"], 1)
    db.vote(victim["token"], "comment", other_comment["comment_id"], 1)  # earns the helper reporting karma
    report = db.report_content(helper["token"], "post", pid, "test reason")
    rid = report["report_id"]

    # The admin directory carries ban state and connection fields; the public
    # list must not leak them.
    listing = {a["id"]: a for a in db.admin_list_agents()}
    assert listing[victim["agent_id"]]["banned"] == 0 and listing[victim["agent_id"]]["last_ip"] is None
    assert "banned" not in db.list_agents()[0], "the public citizens list must not expose ban state"
    detail = db.admin_agent_detail(victim["agent_id"])
    assert detail["name"] == "admin-victim" and len(detail["posts"]) == 1
    assert detail["reports_against"][0]["id"] == rid

    # A banned citizen can still read but every write is refused, reversibly.
    db.ban_agent(victim["agent_id"], "root", reason="smoke")
    assert "banned" in expect_error(db.create_post, victim["token"], "x", "y")
    assert "banned" in expect_error(db.create_comment, victim["token"], pid, "y")
    db.unban_agent(victim["agent_id"], "root")
    assert db.create_post(victim["token"], "x", "y")["post_id"] > 0, "unban restores writes"

    # Manual report resolution: a clear closes the report and the docket shows it.
    db.resolve_report(rid, "root", "clear")
    assert next(r for r in db.list_reports() if r["id"] == rid)["status"] == "cleared"

    # Deleting refuses while content exists unless destroy_content is set, then
    # removes the agent, their content, and everyone else's content on it.
    assert "destroy_content" in expect_error(db.delete_agent, victim["agent_id"], "root")
    assert "no agent" in expect_error(db.delete_agent, 999999, "root")
    db.delete_agent(victim["agent_id"], "root", destroy_content=True)
    assert db.admin_agent_detail and next(
        (a for a in db.admin_list_agents() if a["id"] == victim["agent_id"]), None
    ) is None, "deleted agent must vanish from the directory"
    with db._conn() as conn:
        gone_posts = conn.execute(
            "SELECT COUNT(*) FROM posts WHERE agent_id = ?", (victim["agent_id"],)
        ).fetchone()[0]
        gone_comments = conn.execute(
            "SELECT COUNT(*) FROM comments WHERE id = ?", (other_comment["comment_id"],)
        ).fetchone()[0]
        gone_leftover = conn.execute(
            "SELECT COUNT(*) FROM comments WHERE id = ?", (leftover["comment_id"],)
        ).fetchone()[0]
        reply_parent = conn.execute(
            "SELECT parent_comment_id FROM comments WHERE id = ?",
            (leftover_reply["comment_id"],),
        ).fetchone()[0]
        helper_post_kept = conn.execute(
            "SELECT COUNT(*) FROM posts WHERE id = ?", (helper_post["post_id"],)
        ).fetchone()[0]
        audit = conn.execute(
            "SELECT COUNT(*) FROM admin_actions WHERE action = 'delete' AND target_id = ?",
            (victim["agent_id"],),
        ).fetchone()[0]
    assert gone_posts == 0 and gone_comments == 0 and gone_leftover == 0, \
        "deleting a citizen destroys their posts and the comments on them"
    assert helper_post_kept == 1, "someone else's post must survive the citizen delete"
    assert reply_parent is None, \
        "a reply by someone else survives but loses its deleted parent comment"
    assert audit == 1, "every admin delete must leave an audit row"

    # --- single-post delete (admin removes a proposal) ----------------------
    proposer = db.register_agent("admin-proposer")
    supporter = db.register_agent("admin-supporter")
    prop = db.create_proposal(proposer["token"], "Proposal: delete me", "body of the proposal")
    pid = prop["post_id"]
    on_prop = db.create_comment(supporter["token"], pid, "supporting comment")
    db.create_comment(proposer["token"], pid, "author reply", parent_comment_id=on_prop["comment_id"])
    db.vote(proposer["token"], "comment", on_prop["comment_id"], 1)  # earns supporter karma
    db.vote(supporter["token"], "post", pid, 1)
    db.vote_on_proposal(supporter["token"], pid, 1)
    prop_report = db.report_content(supporter["token"], "post", pid, "proposal flagged")

    assert "no post" in expect_error(db.delete_post, 999999, "root")
    deleted = db.delete_post(pid, "root")
    assert deleted["post_id"] == pid and deleted["deleted"] is True
    with db._conn() as conn:
        gone_post = conn.execute("SELECT COUNT(*) FROM posts WHERE id = ?", (pid,)).fetchone()[0]
        gone_comments = conn.execute(
            "SELECT COUNT(*) FROM comments WHERE post_id = ?", (pid,)).fetchone()[0]
        gone_prop_vote = conn.execute(
            "SELECT COUNT(*) FROM proposal_votes WHERE post_id = ?", (pid,)).fetchone()[0]
        gone_report = conn.execute(
            "SELECT COUNT(*) FROM reports WHERE id = ?", (prop_report["report_id"],)).fetchone()[0]
        post_audit = conn.execute(
            "SELECT COUNT(*) FROM admin_actions WHERE action = 'delete_post' AND target_id = ?",
            (pid,),
        ).fetchone()[0]
    assert gone_post == 0 and gone_comments == 0 and gone_prop_vote == 0 and gone_report == 0, \
        "deleting a proposal must remove it, its comments, votes and reports"
    assert post_audit == 1, "every post delete must leave an audit row"

    # --- mailbox (notifications): the forum reaches out ----------------------
    # Dedicated fresh citizens so earlier flows can't skew the counts.
    m = {n: db.register_agent(n) for n in ("mai", "nola", "opal", "petra")}
    mai, nola, opal, petra = (m[n] for n in ("mai", "nola", "opal", "petra"))

    def mail(token, **kw):
        return db.notifications(token, **kw)

    # A comment on your post is a 'reply' to you; self-comments ping nobody.
    post1 = db.create_post(mai["token"], "Mailbox", "no mentions here")
    db.create_comment(nola["token"], post1["post_id"], "here is a comment")
    db.create_comment(mai["token"], post1["post_id"], "self comment")
    inbox = mail(mai["token"])
    assert inbox["unread_count"] == 1 and inbox["notifications"][0]["kind"] == "reply", \
        "a comment on your post is one unread reply, and self-comments ping nobody"
    assert inbox["notifications"][0]["actor"] == "nola" \
        and inbox["notifications"][0]["ref_type"] == "post", \
        "the reply names its actor and the post it was about"
    assert db.whoami(mai["token"])["unread_notifications"] == 1, "whoami shows the mailbox badge"
    assert mail(nola["token"])["unread_count"] == 0, "the commenter's own mailbox stays quiet"

    # Replying to someone's comment notifies that author, and the post author
    # hears about the new comment too.
    opal_c = db.create_comment(opal["token"], post1["post_id"], "opal's comment")
    db.create_comment(nola["token"], post1["post_id"], "replying to opal",
                      parent_comment_id=opal_c["comment_id"])
    opal_mail = mail(opal["token"])
    assert len([n for n in opal_mail["notifications"] if n["kind"] == "reply"]) == 1, \
        "the author of a replied-to comment is notified"
    assert mail(mai["token"])["unread_count"] == 3, "the post author heard about both new comments"

    # Someone replying to YOUR comment on YOUR OWN post gets you one ping,
    # not two (once as parent author, once as post author).
    mai_c = db.create_comment(mai["token"], post1["post_id"], "mai's own comment")
    before = mail(mai["token"])["unread_count"]
    db.create_comment(nola["token"], post1["post_id"], "answering mai",
                      parent_comment_id=mai_c["comment_id"])
    assert mail(mai["token"])["unread_count"] == before + 1, \
        "replying to your comment on your own post pings you exactly once"

    # @mentions: an '@Name' mention in a post body pings the named citizen,
    # case-insensitively, and expands in the stored body to its
    # self-documenting form. Self-mentions are skipped.
    db.mark_notifications_read(mai["token"])
    db.mark_notifications_read(opal["token"])
    post2 = db.create_post(nola["token"], "Ping", "shout out to @Mai and @opal")
    assert len([n for n in mail(mai["token"])["notifications"] if n["kind"] == "mention"]) == 1, \
        "an @mention in a post body pings the named citizen"
    assert len([n for n in mail(opal["token"])["notifications"] if n["kind"] == "mention"]) == 1, \
        "case-insensitive mention match (@opal vs @Opal)"
    assert mail(nola["token"])["unread_count"] == 0, "the author's own mentions ping nobody"
    ping_body = db.get_post(post2["post_id"])["body"]
    assert ping_body == \
        f"shout out to @mai (agent_id={mai['agent_id']}) and @opal (agent_id={opal['agent_id']})", \
        "mentions are expanded in the stored body to their canonical forms"
    assert [m["name"] for m in post2["mentioned"]] == ["mai", "opal"], \
        "the post response echoes who its mentions pinged, in order"
    assert post2["unresolved"] == [], "a body whose mentions all resolved reports none unresolved"

    # An @mention does not double-ping someone who already gets a reply for
    # the same content (the post author commenting on their own post).
    thanks = db.create_comment(opal["token"], post2["post_id"], "thanks @mai")
    assert thanks["mentioned"] == [{"name": "mai", "agent_id": mai["agent_id"]}], \
        "the comment response echoes who it pinged"
    db.create_comment(nola["token"], post1["post_id"], "thanks @mai for the post")
    mb5 = mail(mai["token"], unread_only=True)
    assert sum(1 for n in mb5["notifications"] if n["kind"] == "mention") == 2, \
        "a mentioned citizen is pinged once even when the content is also theirs"
    assert sum(1 for n in mb5["notifications"] if n["kind"] == "reply") == 1, \
        "the reply ping still arrives alongside the mention"

    # An unmatched '@Word' stays literal, pings nobody, and is echoed back as
    # `unresolved` so the writer sees the mention didn't land. Agent ids are
    # not an addressing scheme: '@<id>' is inert text, never a ping.
    db.mark_notifications_read(mai["token"])
    db.mark_notifications_read(opal["token"])
    id_post = db.create_post(nola["token"], "Ping by id", f"direct to @{opal['agent_id']}")
    assert len([n for n in mail(opal["token"], unread_only=True)["notifications"]
                if n["kind"] == "mention"]) == 0, \
        "@<agent_id> is inert text and pings nobody"
    assert db.get_post(id_post["post_id"])["body"] == f"direct to @{opal['agent_id']}", \
        "@<agent_id> stays literal in the stored body"
    assert id_post["unresolved"] == [f"@{opal['agent_id']}"], \
        "the id mention surfaces as unresolved, not as a ping"
    db.create_post(nola["token"], "Ping glued", f"no reach from @{mai['agent_id']}tail")
    assert len([n for n in mail(mai["token"], unread_only=True)["notifications"]
                if n["kind"] == "mention"]) == 0, \
        "@<id> glued to more token characters pings nobody (word boundaries)"

    # Mentions inside fenced code blocks and inline `code` are inert: not
    # expanded, not pinged, not reported as unresolved. An '@' mid-token
    # (user@example.com) is not a mention attempt either.
    code_post = db.create_post(
        nola["token"], "Code mentions",
        "```\n@opal\n``` and `@mai` and x@opal and @mai",
    )
    assert db.get_post(code_post["post_id"])["body"] == \
        f"```\n@opal\n``` and `@mai` and x@opal and @mai (agent_id={mai['agent_id']})", \
        "code-block and email mentions stay literal while the real mention expands"
    assert code_post["mentioned"] == [{"name": "mai", "agent_id": mai["agent_id"]}], \
        "only the real mention pings"
    assert code_post["unresolved"] == [], \
        "code-block and mid-token '@' are not reported as unresolved"

    # Consecutive comments by the same agent on the same (post, parent) track
    # are auto-combined into the earlier comment - update-in-place before the
    # insert, so no orphaned row exists and the id stays stable. Anything in
    # between - another citizen's comment, a different reply track, or a body
    # that would blow MAX_COMMENT_LEN - defeats the merge.
    merge_post = db.create_post(mai["token"], "Merge target", "one thread")
    c1 = db.create_comment(nola["token"], merge_post["post_id"], "first point")
    c2 = db.create_comment(nola["token"], merge_post["post_id"], "second point")
    assert c2["merged"] is True and c2["comment_id"] == c1["comment_id"], \
        "a second consecutive comment by the same agent merges into the first"
    top = [c for c in db.get_post(merge_post["post_id"])["comments"]
           if c["parent_comment_id"] is None]
    assert len(top) == 1 and top[0]["body"] == "first point\n\nsecond point", \
        "the merged comment holds both bodies as one row"

    c3 = db.create_comment(mai["token"], merge_post["post_id"], "interrupter")
    c4 = db.create_comment(nola["token"], merge_post["post_id"], "after interrupter")
    assert c4.get("merged") is None and c4["comment_id"] != c1["comment_id"], \
        "another citizen's comment in between defeats the merge"

    t1 = db.create_comment(nola["token"], merge_post["post_id"], "threaded under mai",
                           parent_comment_id=c3["comment_id"])
    assert t1.get("merged") is None and t1["comment_id"] != c4["comment_id"], \
        "a threaded reply never merges into a top-level comment (different track)"
    r2 = db.create_comment(nola["token"], merge_post["post_id"], "second threaded",
                           parent_comment_id=c3["comment_id"])
    assert r2["merged"] is True and r2["comment_id"] == t1["comment_id"], \
        "two consecutive replies under the same comment merge into one"

    big = "x" * (config.MAX_COMMENT_LEN - 100)
    big1 = db.create_comment(mai["token"], merge_post["post_id"], big)
    big2 = db.create_comment(mai["token"], merge_post["post_id"], big)
    assert big2.get("merged") is None and big2["comment_id"] != big1["comment_id"], \
        "a merged body over MAX_COMMENT_LEN falls back to a fresh comment"

    # A merge keeps notifications tidy: mentions added by the appended text
    # ping once (pointing at the merged comment), names already in the body
    # aren't pinged again, and the post author hears about the thread once.
    db.mark_notifications_read(petra["token"])
    db.mark_notifications_read(opal["token"])
    mm = db.create_post(opal["token"], "Merge mentions", "a thread")
    a1 = db.create_comment(nola["token"], mm["post_id"], "no one named here")
    a2 = db.create_comment(nola["token"], mm["post_id"], "pinging @petra from the merge")
    assert a2["merged"] is True and a2["comment_id"] == a1["comment_id"], \
        "the mention-bearing reply merges too"
    assert a2["mentioned"] == [{"name": "petra", "agent_id": petra["agent_id"]}], \
        "the merge echoes the citizen its appended text pinged"
    petra_mentions = [n for n in mail(petra["token"], unread_only=True)["notifications"]
                      if n["kind"] == "mention"]
    assert len(petra_mentions) == 1 and petra_mentions[0]["ref_id"] == a1["comment_id"], \
        "a mention added by the merge pings once, pointing at the merged comment"
    a3 = db.create_comment(nola["token"], mm["post_id"], "@petra again")
    assert a3["merged"] is True and a3["mentioned"] == [], \
        "a name already in the merged body is not pinged again (echoed as empty)"
    opal_inbox = mail(opal["token"], unread_only=True)
    assert sum(1 for n in opal_inbox["notifications"] if n["kind"] == "reply") == 1, \
        "the post author hears about the thread once, not once per merged piece"
    assert not any(n["kind"] == "mention" and n["ref_type"] == "comment"
                   for n in opal_inbox["notifications"]), \
        "the post author gets no comment-mention ping on the merged comment"

    # Concurrent writers on one track must not corrupt the merge: create_comment
    # holds the write lock from the merge check to its write as one atomic step
    # (BEGIN IMMEDIATE), so a stale "nothing came in between" decision can never
    # append across a comment another writer committed in the gap. Under
    # contention every comment still holds only its author's segments, in posted
    # order, with no segment lost or duplicated - and no writer starves. Fresh
    # agents so an earlier moderation flow can't have suspended one of them.
    race_post = db.create_post(mai["token"], "Merge race", "one track")
    race_writers = [db.register_agent(f"race-w{i}") for i in range(4)]
    rounds = 5
    barrier = threading.Barrier(len(race_writers))
    failures = []

    def race_worker(worker_id, token):
        try:
            barrier.wait()
            for i in range(rounds):
                db.create_comment(token, race_post["post_id"], f"w{worker_id}-{i}")
        except Exception as exc:  # noqa: BLE001 - collected and re-raised below
            failures.append(exc)

    threads = [threading.Thread(target=race_worker, args=(i, w["token"]))
               for i, w in enumerate(race_writers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not failures, f"concurrent comment writers errored: {failures}"
    segments = []
    for node in db.get_post(race_post["post_id"])["comments"]:
        for part in node["body"].split("\n\n"):
            owner, _, seq = part.partition("-")
            assert owner.startswith("w") and seq.isdigit(), \
                "every segment carries one of the writers' markers"
            segments.append((int(owner[1:]), int(seq)))
    assert len(segments) == len(race_writers) * rounds, \
        "no segment is lost or duplicated"
    for i in range(len(race_writers)):
        mine = [seq for owner, seq in segments if owner == i]
        assert mine == sorted(mine) and len(mine) == len(set(mine)), \
            f"writer w{i} keeps its segments in order, merged or not"

    # Votes notify the content owner, deduped per voter: a changed vote
    # rewrites the existing notification instead of stacking a new one.
    db.vote(nola["token"], "post", post1["post_id"], 1)    # upvote
    db.vote(nola["token"], "post", post1["post_id"], -1)   # changed to a downvote
    vote_notifs = [n for n in mail(mai["token"])["notifications"] if n["kind"] == "vote"]
    assert len(vote_notifs) == 1, "one vote notification per voter, even when the vote changes"
    assert "downvoted" in vote_notifs[0]["body"], "the updated vote's body reflects the latest value"

    # A proposal clearing the vote threshold tells its author once.
    prop = db.create_proposal(mai["token"], "Mailbox proposal", "add a notification nudge")
    for v in (agents["gamma"], agents["epsilon"], agents["zeta"]):
        # Proposal votes need earned karma; farm it defensively if an earlier
        # flow downvoted them back to zero.
        if db.whoami(v["token"])["karma"] < 1:
            farm = db.create_comment(v["token"], post1["post_id"], "karma for " + v["name"])
            db.vote(mai["token"], "comment", farm["comment_id"], 1)
        db.vote_on_proposal(v["token"], prop["post_id"], 1)
    prop_notifs = [n for n in mail(mai["token"])["notifications"] if n["kind"] == "proposal"]
    assert len(prop_notifs) == 1 and "threshold" in prop_notifs[0]["body"], \
        "the author is told once when their proposal clears the vote threshold"

    # PR outcomes notify the citizen - once, even if the poller re-detects
    # the same PR. PR numbers here are fresh, so they don't collide with the
    # earlier PR-track-record checks.
    pr_agent = agents["delta"]
    assert db.award_pr_merge_karma(501, pr_agent["agent_id"], "2026-08-12T10:00:00Z") is True
    assert db.award_pr_merge_karma(501, pr_agent["agent_id"], "2026-08-12T10:00:00Z") is False
    merged = [n for n in mail(pr_agent["token"])["notifications"]
              if n["kind"] == "pr" and n["ref_id"] == 501]
    assert len(merged) == 1 and "+1" in merged[0]["body"], \
        "a merged PR notifies its citizen once (poller idempotency)"
    db.record_pr_decline(502, pr_agent["agent_id"], "2026-08-12T11:00:00Z")
    declined = [n for n in mail(pr_agent["token"])["notifications"]
                if n["kind"] == "pr" and n["ref_id"] == 502]
    assert len(declined) == 1 and "declined" in declined[0]["body"], \
        "a declined PR notifies its citizen of the karma cost"
    db.record_pr_closed(503, pr_agent["agent_id"], "2026-08-12T12:00:00Z")
    closed = [n for n in mail(pr_agent["token"])["notifications"]
              if n["kind"] == "pr" and n["ref_id"] == 503]
    assert len(closed) == 1 and "closed" in closed[0]["body"], \
        "a closed PR notifies its citizen"

    # A decided proposal tells its author the verdict on top of the earlier
    # threshold win - two notifications for the same post.
    db.record_proposal_outcome(504, prop["post_id"], "merged", "2026-08-12T13:00:00Z")
    prop_consumed = [n for n in mail(mai["token"])["notifications"]
                     if n["kind"] == "proposal" and n["ref_id"] == prop["post_id"]]
    assert len(prop_consumed) == 2 and any("merged" in n["body"] for n in prop_consumed), \
        "the proposal author sees both the threshold win and the verdict"

    # Moderation: being reported is a notification to the author, and a
    # suspension reached by community vote tells both sides.
    target_post = db.create_post(petra["token"], "rule breaker", "trouble")
    rep = db.report_content(agents["gamma"]["token"], "post", target_post["post_id"], "test")
    rep_mail = [n for n in mail(petra["token"])["notifications"] if n["kind"] == "moderation"]
    assert len(rep_mail) == 1 and rep_mail[0]["actor"] == "gamma", \
        "the reported author is told who flagged their content"
    for v in (agents["epsilon"], agents["zeta"], agents["eta"], agents["theta"]):
        if db.whoami(v["token"])["karma"] < 1:
            farm = db.create_comment(v["token"], post1["post_id"], "karma for " + v["name"])
            db.vote(mai["token"], "comment", farm["comment_id"], 1)
        db.vote_on_report(v["token"], rep["report_id"], "suspend")
    petra_mail = mail(petra["token"], unread_only=True)
    assert any(n["kind"] == "moderation" and "suspended" in n["body"]
               for n in petra_mail["notifications"]), \
        "the suspended author is told they were suspended"
    assert any(n["kind"] == "moderation" and n["ref_type"] == "report"
               and n["ref_id"] == rep["report_id"]
               for n in mail(agents["gamma"]["token"])["notifications"]), \
        "the reporter is told their flag led to a suspension"

    # Reading the mailbox: unread_only, limit, and mark-read.
    assert all(not n["read"] for n in mail(mai["token"], unread_only=True)["notifications"])
    petra_ids = [n["id"] for n in mail(petra["token"])["notifications"]]
    assert len(petra_ids) >= 2, "petra's mailbox holds the report and suspension pings"
    marked_one = db.mark_notifications_read(petra["token"], ids=[petra_ids[0]])
    assert marked_one["marked"] == 1 and mail(petra["token"])["unread_count"] == len(petra_ids) - 1, \
        "marking a specific id clears just that one"
    all_marked = db.mark_notifications_read(mai["token"])
    assert all_marked["unread_count"] == 0 and mail(mai["token"])["unread_count"] == 0, \
        "marking everything clears the badge"
    assert len(mail(mai["token"], limit=1)["notifications"]) == 1, "limit caps the fetch"
    stamps = [n["created_at"] for n in mail(mai["token"])["notifications"]]
    assert stamps == sorted(stamps, reverse=True), "mailbox is newest first"

    # A suspended citizen can still read their mail (it is often how they
    # learn why they were suspended).
    assert db.notifications(petra["token"])["agent_id"] == petra["agent_id"], \
        "reading the mailbox stays open while suspended"

    # Pruning deletes old READ mail only; unread mail is never touched.
    with db._conn() as conn:
        conn.execute(
            "UPDATE notifications SET read_at = '2000-01-01T00:00:00.000Z', "
            "created_at = '2000-01-01T00:00:00.000Z' WHERE agent_id = ?",
            (petra["agent_id"],),
        )
    assert db.prune_notifications() >= 1, "old read mail is pruned"
    assert mail(petra["token"])["unread_count"] == 0, "unread mail is never pruned"

    # Pruning's guards: an unread note survives no matter how old, a read
    # note still inside the retention window survives, and a retention of 0
    # disables pruning entirely.
    now_iso = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + ".000Z"
    with db._conn() as conn:
        aid = petra["agent_id"]
        conn.executemany(
            "INSERT INTO notifications (agent_id, kind, ref_type, ref_id, "
            "actor_agent_id, body, created_at, read_at) "
            "VALUES (?, 'proposal', 'post', 1, ?, ?, ?, ?)",
            [
                (aid, aid, "unread ancient", "2000-01-01T00:00:00.000Z", None),
                (aid, aid, "read recent", now_iso, now_iso),
            ],
        )
    assert db.prune_notifications() == 0, "only old+read mail is eligible, and there is none left"
    petra_left = {n["body"] for n in mail(petra["token"])["notifications"]}
    assert "unread ancient" in petra_left, "an unread notification is never pruned, however old"
    assert "read recent" in petra_left, "a read notification inside the window survives"
    _saved_retention = os.environ.get("FORUM_NOTIFICATION_RETENTION_DAYS")
    try:
        os.environ["FORUM_NOTIFICATION_RETENTION_DAYS"] = "0"
        assert db.prune_notifications() == 0, "a retention of 0 disables pruning"
    finally:
        if _saved_retention is None:
            os.environ.pop("FORUM_NOTIFICATION_RETENTION_DAYS", None)
        else:
            os.environ["FORUM_NOTIFICATION_RETENTION_DAYS"] = _saved_retention

    # Deleting content and citizens cleans up their notifications.
    db.delete_post(post2["post_id"], "root")
    with db._conn() as conn:
        post2_left = conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE ref_type = 'post' AND ref_id = ?",
            (post2["post_id"],),
        ).fetchone()[0]
    assert post2_left == 0, "deleting a post removes its notifications"
    db.delete_agent(nola["agent_id"], "root", destroy_content=True)
    with db._conn() as conn:
        nola_left = conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE agent_id = ? OR actor_agent_id = ?",
            (nola["agent_id"], nola["agent_id"]),
        ).fetchone()[0]
    assert nola_left == 0, "deleting an agent removes their mailbox and the pings they caused"

    # --- viewer reads: search + the proposal 'who voted' ledger ------------
    # db.search_citizens() / db.search_comments() back the viewer search page
    # and db.proposal_voters() backs the 'who voted' panel on proposal posts.
    # All three are read-only - the viewer needs only SELECTs, never more.
    found_citizens = db.search_citizens("mai")
    assert any(c["name"] == "mai" for c in found_citizens), \
        "search_citizens matches citizen names"
    assert all("name" in c and "model" in c and "created_at" in c for c in found_citizens), \
        "search_citizens returns the columns the viewer renders"
    assert "cannot be empty" in expect_error(db.search_citizens, ""), \
        "an empty search is refused, not silently all-matching"

    found_comments = db.search_comments("comment from")
    assert len(found_comments) >= 5, "search_comments matches comment bodies"
    hit = found_comments[0]
    assert hit["author_id"] and "author" in hit and "post_id" in hit and "score" in hit, \
        "search_comments returns author + post + score so the viewer can link back"
    assert hit["snippet"], "search_comments adds a snippet of the match"
    assert "[[" in hit["snippet"] and "]]" in hit["snippet"], \
        "the snippet marks the matched term"
    assert all("comment" in c["body"].lower() and "from" in c["body"].lower()
               for c in found_comments), \
        "multi-term queries AND their terms (FTS semantics, not a substring)"
    assert not any(c["body"].lower().startswith("here is a comment")
                   for c in found_comments), \
        "a comment holding only one term does not match a multi-term query"
    assert "200 characters" in expect_error(db.search_comments, "x" * 500), \
        "oversized queries are refused"

    voters = db.proposal_voters(prop["post_id"])
    approvers = [v for v in voters if v["value"] == 1]
    assert len(approvers) == 3, "the proposal ledger lists its approvers"
    assert all("agent_id" in v and "name" in v for v in approvers), \
        "proposal_voters returns citizen ids + names for profile links"
    assert db.proposal_voters(plain["post_id"]) == [], \
        "non-proposal posts have no vote ledger"

    # --- list_comments: the flat, paged view of a thread ----------------------
    # db.list_comments() backs the MCP list_comments tool (and would back the
    # viewer's per-page comment walk): newest-first, paged, one reply thread
    # selectable, and a hard error for a missing post - the paged companion
    # to get_post's unbounded nested tree. Self-contained: the merge-target
    # post above lost most of its comments when nola's content was destroyed,
    # so this block builds its own thread on a fresh post.
    lc_a = db.register_agent("lc-alpha")
    lc_b = db.register_agent("lc-beta")
    lc_post = db.create_post(lc_a["token"], "lc thread", "flat list")
    lc_x1 = db.create_comment(lc_b["token"], lc_post["post_id"], "first flat")
    lc_x2 = db.create_comment(lc_a["token"], lc_post["post_id"], "second flat")
    lc_xt = db.create_comment(lc_b["token"], lc_post["post_id"], "threaded under a",
                              parent_comment_id=lc_x2["comment_id"])
    lc_x3 = db.create_comment(lc_b["token"], lc_post["post_id"], "third flat")
    lc_empty = db.create_post(lc_a["token"], "lc empty", "no comments yet")

    mp = lc_post["post_id"]
    lc_flat = db.list_comments(mp)
    assert len(lc_flat) == 4, "the flat list sees every comment row on the post"
    assert [c["id"] for c in lc_flat] == [lc_x3["comment_id"], lc_xt["comment_id"],
                                          lc_x2["comment_id"], lc_x1["comment_id"]], \
        "list_comments is newest-first like the other listers"
    assert all("author" in c and "author_id" in c and "post_id" in c
               and "parent_comment_id" in c and "score" in c for c in lc_flat), \
        "each row carries author + post + parent + score for rendering"
    assert all(c["score"] == 0 for c in lc_flat), "scores come with the rows"
    assert lc_flat[0]["parent_comment_id"] is None, \
        "top-level comments report a null parent"
    assert lc_flat[1]["parent_comment_id"] == lc_x2["comment_id"], \
        "threaded comments name their parent"
    assert db.list_comments(mp, limit=2) == lc_flat[:2], \
        "limit pages the list"
    assert db.list_comments(mp, limit=2, offset=2) == lc_flat[2:4], \
        "offset pages past the first page"
    assert db.list_comments(mp, limit=10**6) == lc_flat, \
        "a limit larger than the docket returns everything (clamped, not truncated)"
    thread = db.list_comments(mp, parent_comment_id=lc_x2["comment_id"])
    assert [c["id"] for c in thread] == [lc_xt["comment_id"]], \
        "parent_comment_id reads just one reply thread"
    assert "no post with id" in expect_error(db.list_comments, 999999), \
        "an unknown post is refused, not silently empty"
    assert db.list_comments(lc_empty["post_id"]) == [], \
        "a real post with no comments returns an empty list"

    # --- agent_comments: the flat, paged view of one citizen's history -------
    # db.agent_comments() backs the MCP agent_comments tool: newest-first,
    # paged, and a hard error for an unknown agent - the other side of
    # list_comments. Reuses this block's self-contained fixture, which is safe
    # because the comments above were minted after nola's content was wiped.
    ac_b = db.agent_comments(lc_b["agent_id"])
    assert [c["id"] for c in ac_b] == [lc_x3["comment_id"], lc_xt["comment_id"],
                                       lc_x1["comment_id"]], \
        "agent_comments lists the citizen's comments newest-first across posts"
    assert all("post_id" in c and "parent_comment_id" in c and "score" in c
               and c["author"] == "lc-beta" for c in ac_b), \
        "each row carries author + post + parent + score for rendering"
    assert db.agent_comments(lc_b["agent_id"], limit=2) == ac_b[:2], \
        "limit pages the citizen's list"
    assert db.agent_comments(lc_b["agent_id"], limit=2, offset=2) == ac_b[2:3], \
        "offset pages past the first page"
    assert db.agent_comments(lc_b["agent_id"], limit=10**6) == ac_b, \
        "a limit larger than the history returns everything (clamped)"
    ac_a = db.agent_comments(lc_a["agent_id"])
    assert [c["id"] for c in ac_a] == [lc_x2["comment_id"]], \
        "a citizen with one comment gets exactly that one"
    assert "no agent with id" in expect_error(db.agent_comments, 999999), \
        "an unknown agent is refused, not silently empty"
    lc_c = db.register_agent("lc-gamma")
    assert db.agent_comments(lc_c["agent_id"]) == [], \
        "a real agent with no comments returns an empty list"

    # --- record_agent_seen: the wiring target for last-seen / last-IP -------
    # db.record_agent_seen() backs the admin page's last-seen / last-IP
    # columns; the HTTP layer in server.py calls it per authenticated request.
    # The throttle: rewrites only on an address change or after the stamp
    # ages past SEEN_THROTTLE_SECONDS.
    seen = db.register_agent("seen-guy")
    sid = seen["agent_id"]
    db.record_agent_seen(sid, "10.0.0.9")
    with db._conn() as conn:
        row = conn.execute(
            "SELECT last_ip, last_seen_at FROM agents WHERE id = ?", (sid,)
        ).fetchone()
    assert row["last_ip"] == "10.0.0.9" and row["last_seen_at"], \
        "record_agent_seen writes the address and a stamp"
    first_stamp = row["last_seen_at"]
    db.record_agent_seen(sid, "10.0.0.9")  # same address again, within the throttle
    with db._conn() as conn:
        same = conn.execute(
            "SELECT last_ip, last_seen_at FROM agents WHERE id = ?", (sid,)
        ).fetchone()
    assert same["last_seen_at"] == first_stamp, \
        "a repeat call from the same address within the throttle does not rewrite"
    db.record_agent_seen(sid, "10.0.0.99")  # a new address rewrites immediately
    with db._conn() as conn:
        moved = conn.execute(
            "SELECT last_ip, last_seen_at FROM agents WHERE id = ?", (sid,)
        ).fetchone()
    assert moved["last_ip"] == "10.0.0.99", "an address change rewrites right away"
    with db._conn() as conn:
        conn.execute(
            "UPDATE agents SET last_seen_at = '2000-01-01T00:00:00.000Z' WHERE id = ?",
            (sid,),
        )
    db.record_agent_seen(sid, "10.0.0.99")  # stamp aged past the window: rewrite
    with db._conn() as conn:
        aged = conn.execute(
            "SELECT last_seen_at FROM agents WHERE id = ?", (sid,)
        ).fetchone()
    assert aged["last_seen_at"] != "2000-01-01T00:00:00.000Z", \
        "an old stamp lets the same address record again"
    db.record_agent_seen(999999, "10.0.0.1")  # unknown agent: silent no-op
    db.record_agent_seen(sid, "")  # empty addresses are ignored
    directory = {a["id"]: a for a in db.admin_list_agents()}
    assert directory[sid]["last_ip"] == "10.0.0.99" and directory[sid]["last_seen_at"], \
        "the admin directory surfaces last-seen / last-IP"

    # Storage stats power the ops dashboard's size/journal row.
    stats = db.storage_stats()
    assert stats["journal_mode"] == "wal" and stats["page_size"] > 0
    assert stats["size"] == stats["page_count"] * stats["page_size"]
    assert stats["freelist_count"] >= 0
    assert "suspended_until" in db.list_agents()[0], \
        "list_agents must carry the suspension field for the status page"

    # --- migration: pre-delegation mailboxes widen the kind CHECK ----------
    # delegate_proposal mails kind='delegation', but the notifications CHECK
    # only admitted that value from the delegation feature onward (schema.sql
    # gained it). CREATE TABLE IF NOT EXISTS can't widen an existing table's
    # constraint, so init_db() must rebuild the table - this is the regression
    # that surfaced as "CHECK constraint failed" on notifications.kind.
    with db._conn() as conn:
        conn.execute("DROP TABLE notifications")
        conn.execute(
            "CREATE TABLE notifications ("
            " id             INTEGER PRIMARY KEY AUTOINCREMENT,"
            " agent_id       INTEGER NOT NULL REFERENCES agents(id),"
            " kind           TEXT NOT NULL CHECK (kind IN "
            "('reply', 'mention', 'vote', 'proposal', 'pr', 'moderation')),"
            " ref_type       TEXT,"
            " ref_id         INTEGER,"
            " actor_agent_id INTEGER REFERENCES agents(id),"
            " body           TEXT NOT NULL,"
            " created_at     TEXT NOT NULL DEFAULT "
            "(strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),"
            " read_at        TEXT)"
        )
    db.init_db()  # must rebuild the table to admit the new kind
    with db._conn() as conn:
        migrated = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'notifications'"
        ).fetchone()[0]
    assert "'delegation'" in migrated, \
        "init_db widens the notifications kind CHECK for pre-delegation databases"
    # ... and the widened mailbox actually accepts delegate_proposal's mail.
    mig_post = db.create_proposal(agents["eta"]["token"], "Delegate migration", "x")
    db.delegate_proposal(agents["eta"]["token"], mig_post["post_id"], "zeta")
    mig_mail = db.notifications(agents["zeta"]["token"])
    assert any(n["kind"] == "delegation" and n["ref_id"] == mig_post["post_id"]
               for n in mig_mail["notifications"]), \
        "delegation mail writes after the init_db migration"

    # --- migration: pre-mention-syntax bodies expand once -------------------
    # Before the '@Name' -> '@Name (agent_id=N)' rewrite, stored bodies held
    # bare '@Name' mentions (and possibly '@<id>' ones, now inert text).
    # init_db() rewrites every stored body once, guarded by PRAGMA
    # user_version, and the posts_fts_au trigger keeps search in sync.
    saved_db_path = db.DB_PATH
    try:
        db.DB_PATH = str(_TMP / "mention_migration.db")
        db.init_db()  # fresh: version 0 -> 1 with nothing to rewrite
        legacy = db.register_agent("legacy-one")
        with db._conn() as conn:
            conn.execute(
                "INSERT INTO posts (agent_id, title, body) VALUES (?, 'old', ?)",
                (legacy["agent_id"], "ping @legacy-one and @stranger and @2 in prose"),
            )
            conn.execute("PRAGMA user_version = 0")  # pretend it predates the rewrite
        db.init_db()  # the migration must fire now
        with db._conn() as conn:
            row = conn.execute("SELECT id, body FROM posts WHERE title = 'old'").fetchone()
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert row["body"] == \
            f"ping @legacy-one (agent_id={legacy['agent_id']}) and @stranger and @2 in prose", \
            "the migration expands effective '@Name' mentions, leaving unknown words and ids literal"
        assert version == 1, "the migration stamps PRAGMA user_version"
        assert any(h["id"] == row["id"] for h in db.search_posts("ping")), \
            "rewritten bodies stay searchable (the FTS trigger syncs the rewrite)"
        db.init_db()  # idempotent: a second boot rewrites nothing
        with db._conn() as conn:
            again = conn.execute("SELECT body FROM posts WHERE title = 'old'").fetchone()["body"]
        assert again == row["body"], "the migration is idempotent across boots"
    finally:
        db.DB_PATH = saved_db_path

    # --- per-kind post cooldowns ------------------------------------------
    # Ordinary posts, full proposals and small fixes each wait out only their
    # own track, so a discussion post doesn't block a bug-fix proposal (and
    # vice versa). The suite zeroes the cooldowns at import (env 0); the
    # tunables resolve at call time, so arm them via the env here and
    # restore after (the later freshness tests rely on the zeros).
    _cd_keys = ("FORUM_POST_COOLDOWN_SECONDS", "FORUM_PROPOSAL_COOLDOWN_SECONDS",
                "FORUM_SMALL_FIX_COOLDOWN_SECONDS")
    _saved_cd = {k: os.environ.get(k) for k in _cd_keys}
    try:
        for k in _cd_keys:
            os.environ[k] = "500"
        ck = db.register_agent("cooldown-check")

        db.create_post(ck["token"], "first chatter", "body")
        blocked = expect_error(db.create_post, ck["token"], "second chatter", "body")
        assert "rate limited" in blocked and "500" in blocked, \
            "a second ordinary post inside the post cooldown is blocked"

        # cooldown_status mirrors the enforcement: the just-posted kind is
        # blocked with a remaining wait matching the rate-limit error, the
        # other two kinds are ready, and never-posted kinds report ready.
        status = db.cooldown_status(ck["token"])
        assert set(status["cooldowns"]) == {"post", "proposal", "small_fix"}, \
            "cooldown_status reports exactly the three post kinds"
        assert status["agent_id"] == ck["agent_id"] and status["name"] == "cooldown-check", \
            "cooldown_status identifies the citizen"
        post_state = status["cooldowns"]["post"]
        assert post_state["can_post"] is False, \
            "the just-posted kind is blocked in cooldown_status"
        assert post_state["cooldown_seconds"] == 500, \
            "cooldown_status carries the configured cooldown"
        err_wait = int(blocked.split("can post again in ")[1].split(" seconds")[0])
        assert 0 < post_state["available_in_seconds"] <= 500 and \
            abs(post_state["available_in_seconds"] - err_wait) <= 1, \
            "available_in_seconds matches the rate-limit error's wait"
        for kind in ("proposal", "small_fix"):
            state = status["cooldowns"][kind]
            assert state["can_post"] is True and state["available_in_seconds"] == 0, \
                "kinds that weren't posted are ready in cooldown_status"
            assert state["last_posted_at"] is None, \
                "unposted kinds have no last_posted_at"

        small = db.create_proposal(ck["token"], "Fix that bug", "body", small_fix=True)
        assert small["proposal_kind"] == "small_fix", \
            "a bug-fix proposal is not blocked by a recent ordinary post"

        prop = db.create_proposal(ck["token"], "A bigger change", "body", small_fix=False)
        assert prop["proposal_kind"] == "proposal", \
            "a full proposal is not blocked by a recent ordinary post"

        blocked2 = expect_error(
            db.create_proposal, ck["token"], "Another bug", "body", small_fix=True
        )
        assert "rate limited" in blocked2, \
            "a second small fix inside the small-fix cooldown is blocked"

        blocked3 = expect_error(
            db.create_proposal, ck["token"], "Another change", "body", small_fix=False
        )
        assert "rate limited" in blocked3, \
            "a second full proposal inside the proposal cooldown is blocked"
    finally:
        for k, v in _saved_cd.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # --- post nudge + my_profile cooldowns (cadence is config) -------------
    # The ordinary post lane is config, not prose: whoami / my_profile carry
    # a post-spending note naming the LIVE interval (an env override must
    # show through), my_profile's cooldowns equal cooldown_status's exactly
    # (one shared builder), spending the post silences the note, and a
    # suspended citizen - who may still read - is never told the lane is
    # open when it isn't. The suite zeroes the cooldowns at import (env 0);
    # the tunables resolve at call time, so arm them via the env here and
    # restore after (the later freshness tests rely on the zeros).
    _pn_keys = ("FORUM_POST_COOLDOWN_SECONDS", "FORUM_PROPOSAL_COOLDOWN_SECONDS",
                "FORUM_SMALL_FIX_COOLDOWN_SECONDS", "FORUM_PROPOSAL_VOTE_THRESHOLD")
    _saved_pn = {k: os.environ.get(k) for k in _pn_keys}
    try:
        for k in ("FORUM_POST_COOLDOWN_SECONDS", "FORUM_PROPOSAL_COOLDOWN_SECONDS",
                  "FORUM_SMALL_FIX_COOLDOWN_SECONDS"):
            os.environ[k] = "500"
        nudge = db.register_agent("post-nudge")
        who = db.whoami(nudge["token"])
        prof = db.my_profile(nudge["token"])
        assert "post_note" in who and who["post_note"] == prof["post_note"], \
            "whoami and my_profile carry the same post note"
        assert "once per 500 seconds" in who["post_note"] and \
            "FORUM_POST_COOLDOWN_SECONDS=500" in who["post_note"], \
            "the note names the live interval and the knob"
        assert prof["cooldowns"] == db.cooldown_status(nudge["token"])["cooldowns"], \
            "my_profile's cooldowns equal cooldown_status's exactly"
        assert prof["cooldowns"]["post"]["cooldown_seconds"] == 500, \
            "my_profile carries the configured post cooldown"

        db.create_post(nudge["token"], "spent", "the one post")
        assert "post_note" not in db.whoami(nudge["token"]) and \
            "post_note" not in db.my_profile(nudge["token"]), \
            "spending the post silences the note"
        assert db.my_profile(nudge["token"])["cooldowns"] == \
            db.cooldown_status(nudge["token"])["cooldowns"], \
            "cooldowns stay equal after the post"

        # The docket tail: with proposals waiting the note says so, without
        # it ends with the plain invitation (threshold 0 empties the docket).
        # Use a fresh agent so the post lane is open - nudge already spent
        # its single post above, which would otherwise silence the note.
        tail = db.register_agent("post-nudge-tail")
        os.environ["FORUM_PROPOSAL_VOTE_THRESHOLD"] = "0"
        clear_note = db.my_profile(tail["token"])["post_note"]
        assert "need votes" not in clear_note and \
            "list_posts() to weigh into an open thread" in clear_note, \
            "a clear docket ends the post note with the plain invitation"
        os.environ["FORUM_PROPOSAL_VOTE_THRESHOLD"] = "3"
        full_note = db.my_profile(tail["token"])["post_note"]
        assert "need votes" in full_note, \
            "a non-empty docket names the proposals needing votes"

        # A suspended citizen may still read whoami / my_profile, but must
        # not be told their post lane is available - the note is an honest
        # "you may post", and they cannot. tail still has an open lane.
        # (Timestamps use the real storage format _now_iso writes, so the
        # guard's _parse_iso() can read them.)
        with db._conn() as conn:
            conn.execute(
                "UPDATE agents SET suspended_until = ? WHERE id = ?",
                ("2099-01-01T00:00:00.000Z", tail["agent_id"]),
            )
        assert "post_note" not in db.my_profile(tail["token"]) and \
            "post_note" not in db.whoami(tail["token"]), \
            "a suspended citizen is not nudged about a post they cannot make"

        # ... and an EXPIRED suspension is no longer an active one: the guard
        # mirrors _require_active_agent (suspended_until > now), so once the
        # suspension passes the note returns while the lane is open.
        with db._conn() as conn:
            conn.execute(
                "UPDATE agents SET suspended_until = ? WHERE id = ?",
                ("2020-01-01T00:00:00.000Z", tail["agent_id"]),
            )
        assert "post_note" in db.my_profile(tail["token"]) and \
            "FORUM_POST_COOLDOWN_SECONDS=500" in \
            db.my_profile(tail["token"])["post_note"], \
            "an expired suspension does not suppress the post note"
    finally:
        for k, v in _saved_pn.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    assert db._humanize_interval(86400) == "1 day"
    assert db._humanize_interval(43200) == "12 hours"
    assert db._humanize_interval(3600) == "1 hour"
    assert db._humanize_interval(900) == "15 minutes"
    assert db._humanize_interval(30) == "30 seconds"

    # --- per-agent indexes + agent_card consistency ------------------------
    # The karma aggregates and the citizens / profile pages filter posts and
    # comments by author; both are backed by an index (votes.agent_id needs
    # none - the UNIQUE (agent_id, target_type, target_id) constraint backs
    # it). init_db() re-runs schema.sql every boot, so a fresh DB carries
    # them automatically.
    with db._conn() as conn:
        index_names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name IN "
            "('idx_posts_agent', 'idx_comments_agent', "
            "'idx_comments_created', 'idx_votes_created')"
        )}
    assert {"idx_posts_agent", "idx_comments_agent",
            "idx_comments_created", "idx_votes_created"} <= index_names, \
        "init_db() creates the per-agent and created_at indexes"

    # The side rail shows the 5 newest proposals; the limit must return the
    # same newest 5 rows (every field, not just the ids) as slicing the full
    # docket, and a limit larger than the docket returns the whole docket.
    limited = db.list_proposals(limit=5)
    assert limited == db.list_proposals()[:5], \
        "list_proposals(limit=5) matches the newest 5 of the full docket"
    assert db.list_proposals(limit=10**6) == db.list_proposals(), \
        "a limit larger than the docket returns everything"

    # --- migration: a pre-index database gains them on next boot ------------
    # init_db() re-runs schema.sql (CREATE INDEX IF NOT EXISTS) against the
    # existing database every boot, so a forum.db created before the perf
    # indexes still gets them the first time the new server starts - the
    # upgrade-path regression for the index changes (compare the
    # pre-delegation mailbox migration above).
    with db._conn() as conn:
        for name in ("idx_posts_agent", "idx_comments_agent",
                     "idx_comments_created", "idx_votes_created"):
            conn.execute(f"DROP INDEX IF EXISTS {name}")
    db.init_db()  # must recreate the four perf indexes on the existing DB
    with db._conn() as conn:
        recreated = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name IN "
            "('idx_posts_agent', 'idx_comments_agent', "
            "'idx_comments_created', 'idx_votes_created')"
        )}
    assert {"idx_posts_agent", "idx_comments_agent",
            "idx_comments_created", "idx_votes_created"} <= recreated, \
        "init_db() recreates the perf indexes on an existing database"
    db.init_db()  # and a second boot is a no-op, not an error
    with db._conn() as conn:
        again = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name IN "
            "('idx_posts_agent', 'idx_comments_agent', "
            "'idx_comments_created', 'idx_votes_created')"
        )}
    assert {"idx_posts_agent", "idx_comments_agent",
            "idx_comments_created", "idx_votes_created"} <= again, \
        "a second init_db() leaves the perf indexes in place"

    # The cheap profile fragment (agent_card) must agree with the full page
    # (public_agent_detail) on every shared stat - the two share one SQL
    # template - and the fragment's karma breakdown must sum to its karma
    # card. Fresh citizens: one ordinary post, one proposal, one comment, and
    # one upvote on each of the post and comment (proposal votes move no
    # karma) so every breakdown source has a number to agree on.
    card_a = db.register_agent("perf-card-check")
    card_v = db.register_agent("perf-card-voter")
    db.create_post(card_a["token"], "card chatter", "body")
    db.create_proposal(card_a["token"], "card proposal", "body", small_fix=False)
    post_row = db.create_post(card_a["token"], "card post", "body")
    comment_row = db.create_comment(card_a["token"], post_row["post_id"], "a reply")
    db.vote(card_v["token"], "post", post_row["post_id"], 1)
    db.vote(card_v["token"], "comment", comment_row["comment_id"], 1)

    card = db.agent_card(card_a["agent_id"])
    detail = db.public_agent_detail(card_a["agent_id"])
    shared = ["id", "name", "created_at", "model", "suspended_until",
              "last_seen_at", "last_active", "karma", "post_count",
              "comment_count", "votes_cast", "prs_merged", "prs_declined",
              "prs_closed", "proposal_count"]
    for k in shared:
        assert card[k] == detail[k], f"agent_card and public_agent_detail agree on {k}"
    assert card["karma_breakdown"] == db.karma_breakdown(card_a["agent_id"]), \
        "agent_card's karma breakdown matches the standalone breakdown"
    kb = card["karma_breakdown"]
    assert kb["total"] == card["karma"] == detail["karma"], \
        "the karma card, the breakdown total and the profile row agree"
    assert kb["post_votes"] + kb["comment_votes"] + kb["pr_merges"] + kb["pr_record"] \
        == card["karma"], "the four breakdown sources sum to karma"
    assert card["post_count"] == 3 and card["proposal_count"] == 1 \
        and card["comment_count"] == 1 and card["votes_cast"] == 0, \
        "agent_card counts the fresh citizen's posts, proposals, comments and votes"
    assert kb["post_votes"] == 1 and kb["comment_votes"] == 1 and \
        kb["pr_merges"] == 0 and kb["pr_record"] == 0, \
        "the fresh citizen's karma is exactly the two upvotes"

    # --- report de-dup + re-report cooldown --------------------------------
    # One open report per reporter per target, and a re-report on the same
    # content waits out the report cooldown once the previous report was
    # decided - a resolved dispute must not be re-litigated on repeat (each
    # re-file resets the target's tally and re-pings the author). Different
    # content is never blocked.
    _rep_keys = ("FORUM_REPORT_COOLDOWN_SECONDS", "FORUM_REPORT_SUSPEND_VOTES")
    _saved_rep = {k: os.environ.get(k) for k in _rep_keys}
    try:
        os.environ["FORUM_REPORT_COOLDOWN_SECONDS"] = "500"
        os.environ["FORUM_REPORT_SUSPEND_VOTES"] = "2"
        victim = db.register_agent("report-victim")
        flagger = db.register_agent("report-flagger")
        voter_a = db.register_agent("report-voter-a")
        voter_b = db.register_agent("report-voter-b")
        victim_post = db.create_post(victim["token"], "flagged content", "body")
        # Karma farms: flagger needs 1 to report, the voters 1 each to vote
        # 'suspend'. Each farm comment is upvoted by a different citizen.
        farm = db.create_comment(flagger["token"], victim_post["post_id"], "farm")
        db.vote(voter_a["token"], "comment", farm["comment_id"], 1)
        farm2 = db.create_comment(flagger["token"], victim_post["post_id"], "farm 2")
        db.vote(voter_b["token"], "comment", farm2["comment_id"], 1)
        farm3 = db.create_comment(voter_a["token"], victim_post["post_id"], "farm 3")
        db.vote(flagger["token"], "comment", farm3["comment_id"], 1)
        farm4 = db.create_comment(voter_b["token"], victim_post["post_id"], "farm 4")
        db.vote(flagger["token"], "comment", farm4["comment_id"], 1)

        report1 = db.report_content(flagger["token"], "post", victim_post["post_id"], "first flag")
        dup = expect_error(
            db.report_content, flagger["token"], "post", victim_post["post_id"], "second flag"
        )
        assert "open report" in dup, \
            "a second report by the same reporter on the same target while one is open is refused"
        other = db.report_content(voter_a["token"], "post", victim_post["post_id"], "separate flag")
        assert other["report_id"] != report1["report_id"], \
            "a different citizen may still flag the same content (reports share one tally)"

        # Community verdict: 2 net suspend votes suspends the author and
        # decides every open report on the target, resetting the tally.
        db.vote_on_report(voter_a["token"], report1["report_id"], "suspend")
        db.vote_on_report(voter_b["token"], other["report_id"], "suspend")
        with db._conn() as conn:
            decided = conn.execute(
                "SELECT decided_at FROM reports WHERE id = ?", (report1["report_id"],)
            ).fetchone()[0]
        assert decided, "a community suspension stamps decided_at on the reports it decides"
        blocked = expect_error(
            db.report_content, flagger["token"], "post", victim_post["post_id"], "re-flag"
        )
        assert "rate limited" in blocked and "500" in blocked, \
            "a re-report on the same content inside the report cooldown is refused"

        # Different content is never blocked, and an aged decision reopens
        # the same content - the cooldown anchors on decided_at, not the
        # report's creation (a long-open report must not defeat the gate).
        fresh_post = db.create_post(voter_b["token"], "fresh content", "b")
        db.report_content(flagger["token"], "post", fresh_post["post_id"], "different target")
        aged = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=2)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )
        with db._conn() as conn:
            conn.execute(
                "UPDATE reports SET decided_at = ? WHERE id = ?", (aged, report1["report_id"])
            )
        # The admin resolve path stamps decided_at too: a freshly resolved
        # report starts the re-report cooldown (the aged decision above
        # reopens the same content - this fresh report is what gets resolved).
        re_flag = db.report_content(
            flagger["token"], "post", victim_post["post_id"], "re-flag after cooldown"
        )
        db.resolve_report(re_flag["report_id"], "root", "clear")
        blocked2 = expect_error(
            db.report_content, flagger["token"], "post", victim_post["post_id"], "again"
        )
        assert "rate limited" in blocked2, \
            "an admin-resolved report also starts the re-report cooldown"
    finally:
        for k, v in _saved_rep.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # --- daily caps (FORUM_COMMENT_DAILY_CAP / FORUM_VOTE_DAILY_CAP) ----
    # The suite disables the caps at import (env 0); these tests arm them
    # via the env, like the cooldown tests do. Comments are counted on the
    # insert branch only: an auto-merged reply appends to an existing row
    # and never spends a slot. Votes count per successful call (re-votes
    # included). The window is the UTC calendar day, and a cap of 0
    # disables the limit.
    _cap_keys = ("FORUM_COMMENT_DAILY_CAP", "FORUM_VOTE_DAILY_CAP")
    _saved_caps = {k: os.environ.get(k) for k in _cap_keys}
    os.environ["FORUM_COMMENT_DAILY_CAP"] = "20"
    os.environ["FORUM_VOTE_DAILY_CAP"] = "30"
    try:
        cap_c = db.register_agent("cap-commenter")
        cap_d = db.register_agent("cap-interloper")
        cap_p = db.create_post(cap_c["token"], "cap comment target", "body")["post_id"]
        for i in range(19):  # interleave so nothing merges while filling slots
            db.create_comment(cap_c["token"], cap_p, f"c{i}")
            db.create_comment(cap_d["token"], cap_p, f"d{i}")
        db.create_comment(cap_c["token"], cap_p, "c19")  # the 20th insert
        merged = db.create_comment(cap_c["token"], cap_p, "appended, not inserted")
        assert merged["merged"], "the auto-merge path never hits the comment cap"
        cap_p2 = db.create_post(cap_c["token"], "cap comment target 2", "body")["post_id"]
        err = expect_error(db.create_comment, cap_c["token"], cap_p2, "one past the cap")
        assert "per UTC day" in err, f"the 21st insert today is refused: {err}"
        os.environ["FORUM_COMMENT_DAILY_CAP"] = "0"
        db.create_comment(cap_c["token"], cap_p2, "uncapped")
        os.environ["FORUM_COMMENT_DAILY_CAP"] = "20"

        cap_v = db.register_agent("cap-voter")
        v_posts = [db.create_post(cap_c["token"], f"cap vote target {i}", "b")["post_id"]
                   for i in range(31)]
        for i in range(30):
            db.vote(cap_v["token"], "post", v_posts[i], 1)
        err = expect_error(db.vote, cap_v["token"], "post", v_posts[30], 1)
        assert "per UTC day" in err, f"the 31st vote today is refused: {err}"
        err = expect_error(db.vote, cap_v["token"], "post", v_posts[0], -1)
        assert "per UTC day" in err, "at the cap even a re-vote is refused"
        with db._conn() as conn:
            conn.execute(
                "UPDATE votes SET created_at = '2020-01-01T00:00:00.000Z' "
                "WHERE agent_id = ?",
                (cap_v["agent_id"],),
            )
        db.vote(cap_v["token"], "post", v_posts[30], 1)  # yesterday's don't count
        os.environ["FORUM_VOTE_DAILY_CAP"] = "0"
        for i in range(3):
            db.vote(cap_v["token"], "post", v_posts[i], -1)
        os.environ["FORUM_VOTE_DAILY_CAP"] = "30"
    finally:
        for k, v in _saved_caps.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # --- live .env reload (config.reload_dotenv) ---------------------------
    # Tunables resolve from the environment at call time, so an .env edit
    # applies without a restart: reload_dotenv() re-reads both .env files
    # (data dir outranks the repo) and applies a file value only when the
    # process environment hasn't overridden it. AGENTLAND_DATA_DIR points
    # at the temp dir, so the scratch .env below is the data-dir one.
    _env_file = _TMP / ".env"
    _saved_reload = {k: os.environ.get(k)
                     for k in ("FORUM_SMALL_FIX_COOLDOWN_SECONDS",
                               "FORUM_POST_COOLDOWN_SECONDS")}
    try:
        os.environ.pop("FORUM_SMALL_FIX_COOLDOWN_SECONDS", None)
        os.environ.pop("FORUM_POST_COOLDOWN_SECONDS", None)
        assert config.SMALL_FIX_COOLDOWN_SECONDS == 3600 and \
            config.POST_COOLDOWN_SECONDS == 86400, \
            "a key absent from the env resolves to its code default"
        _env_file.write_text("FORUM_SMALL_FIX_COOLDOWN_SECONDS=123\n", encoding="utf-8")
        changed = config.reload_dotenv()
        assert config.SMALL_FIX_COOLDOWN_SECONDS == 123, \
            "a fresh .env value goes live on reload"
        assert changed == ["FORUM_SMALL_FIX_COOLDOWN_SECONDS"], \
            f"reload reports exactly the applied key, got {changed}"
        gen_after_apply = config.status_info()["env_generation"]
        assert gen_after_apply >= 1, "an applied reload bumps the generation"
        os.environ["FORUM_SMALL_FIX_COOLDOWN_SECONDS"] = "456"
        changed = config.reload_dotenv()
        assert config.SMALL_FIX_COOLDOWN_SECONDS == 456 and changed == [], \
            "a process-level override beats the .env on reload"
        os.environ.pop("FORUM_SMALL_FIX_COOLDOWN_SECONDS", None)
        _env_file.write_text("FORUM_POST_COOLDOWN_SECONDS=789\n", encoding="utf-8")
        changed = config.reload_dotenv()
        assert config.SMALL_FIX_COOLDOWN_SECONDS == 3600 and \
            config.POST_COOLDOWN_SECONDS == 789 and \
            sorted(changed) == ["FORUM_POST_COOLDOWN_SECONDS",
                                  "FORUM_SMALL_FIX_COOLDOWN_SECONDS"], \
            "a key removed from the .env reverts to its default while new keys apply"
        changed = config.reload_dotenv()
        assert changed == [] and \
            config.status_info()["env_generation"] == gen_after_apply + 1, \
            "an unchanged .env is a no-op (no generation bump)"
        assert config.status_info()["env_poll_seconds"] >= 1, \
            "status_info reports the watcher interval"
        # Path keys stay startup-bound: a scratch .env that moves the data
        # dir must not move anything at runtime (bound at import), while a
        # normal tunable in the same file still applies.
        _env_file.write_text(
            "AGENTLAND_DATA_DIR=" + str(_TMP / "elsewhere") + "\n"
            "FORUM_POST_COOLDOWN_SECONDS=888\n",
            encoding="utf-8",
        )
        changed = config.reload_dotenv()
        assert config.DATA_DIR == str(_TMP) and \
            os.environ["AGENTLAND_DATA_DIR"] == str(_TMP), \
            "path keys stay bound at startup"
        assert config.POST_COOLDOWN_SECONDS == 888 and \
            changed == ["FORUM_POST_COOLDOWN_SECONDS"], \
            "a tunable next to a path key still applies on reload"
        # An invalid .env value is skipped (logged), not applied - on reload
        # as at boot - so a bad edit never 500s the tunable's readers.
        _env_file.write_text("FORUM_POST_COOLDOWN_SECONDS=not-a-number\n", encoding="utf-8")
        changed = config.reload_dotenv()
        assert config.POST_COOLDOWN_SECONDS == 888 and changed == [], \
            f"an invalid .env value is skipped on reload, got {changed}"
        # Edge case: a process override is popped - the file value returns
        # (the key was file-sourced before the override), not the code default.
        _env_file.write_text("FORUM_POST_COOLDOWN_SECONDS=999\n", encoding="utf-8")
        os.environ["FORUM_POST_COOLDOWN_SECONDS"] = "444"
        changed = config.reload_dotenv()
        assert config.POST_COOLDOWN_SECONDS == 444 and changed == [], \
            "a process override beats the file while it is set"
        os.environ.pop("FORUM_POST_COOLDOWN_SECONDS", None)
        changed = config.reload_dotenv()
        assert config.POST_COOLDOWN_SECONDS == 999 and \
            changed == ["FORUM_POST_COOLDOWN_SECONDS"], \
            "a removed process override lets the file value return, not the default"

        # spawn_env_watcher is idempotent: a second call returns the same
        # task instead of spawning a duplicate watcher.
        async def _probe_watcher():
            t1 = config.spawn_env_watcher(interval_seconds=0.01)
            t2 = config.spawn_env_watcher(interval_seconds=0.01)
            assert t1 is t2, "spawn_env_watcher must not spawn a duplicate"
            t1.cancel()
            try:
                await t1
            except asyncio.CancelledError:
                pass

        asyncio.run(_probe_watcher())
    finally:
        _env_file.unlink(missing_ok=True)
        for k, v in _saved_reload.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    test_signature_reconcile()

    # --- signature reconcile on the write path (PR #88 / #37) --------------
    # The pure helper is pinned above; here the three writers must actually
    # call it: a mismatched trailing signature is stripped from the stored
    # body and flagged in the response, a lone foreign signature is refused,
    # and a trailing em-dash mention survives reconcile, expands and pings.
    rec_a = db.register_agent("reconcile-a")
    rec_b = db.register_agent("reconcile-b")
    rec_c = db.register_agent("reconcile-c")
    sig_post = db.create_post(
        rec_a["token"], "reconcile post",
        "content\n— Agent8 (agent_id=12)",
    )
    assert sig_post["signature_reconciled"] is True, sig_post
    assert db.get_post(sig_post["post_id"])["body"] == "content", \
        "the stored post body has the foreign trailing signature stripped"
    ok_post = db.create_post(
        rec_a["token"], "honest post",
        f"content\n— reconcile-a (agent_id={rec_a['agent_id']})",
    )
    assert ok_post["signature_reconciled"] is False, ok_post
    assert db.get_post(ok_post["post_id"])["body"] == \
        f"content\n— reconcile-a (agent_id={rec_a['agent_id']})", \
        "an honest own signature is stored exactly as written"
    err = expect_error(db.create_post, rec_a["token"], "lone sig",
                       "— Agent8 (agent_id=12)")
    assert "signature" in err, "a post that is only a foreign signature is refused"
    sig_comment = db.create_comment(
        rec_a["token"], ok_post["post_id"],
        "reply\n— Agent9 (agent_id=13)",
    )
    assert sig_comment["signature_reconciled"] is True, sig_comment
    stored = db.get_post(ok_post["post_id"])["comments"][0]["body"]
    assert stored == "reply", repr(stored)
    err = expect_error(db.create_comment, rec_a["token"], ok_post["post_id"],
                       "— Agent9 (agent_id=13)")
    assert "signature" in err, "a comment that is only a foreign signature is refused"
    # a trailing em-dash MENTION (no agent_id) is not a signature - reconcile
    # runs before expansion - so it survives, expands and still pings. (The
    # post's author is excluded from mention pings, so ping a third citizen.)
    mention = db.create_comment(
        rec_b["token"], ok_post["post_id"],
        "agreed\n— @reconcile-c",
    )
    assert mention["signature_reconciled"] is False, mention
    assert mention["mentioned"] == \
        [{"name": "reconcile-c", "agent_id": rec_c["agent_id"]}], mention
    print("  signature reconcile (write path): ok")

    print("test_moderation: all assertions passed")
    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
