"""Branch-access requests (proposal #840): a citizen asks a PR opener to
open the branch for shared fixes, and the opener answers.

Most of these pins are about what must NOT happen, because the failure
mode of this feature is a confident wrong answer rather than a crash:

- the opener cannot ask - they toggle the flag themselves;
- granting is ALL-OR-NOTHING.  It opens the branch for every
  karma-qualified citizen, not just the asker.  That is the operator's
  decision, and it is pinned rather than left in prose precisely because
  a per-citizen implementation would satisfy every other pin here and
  silently contradict this one;
- granting settles every OTHER pending request too, because the flag
  answers them all at once and an 'open' row on an open branch is a state
  with no writer.  A hand toggle settles them as well, so the invariant
  holds however the flag came on - and because the cascade is the single
  writer that settles the row, it is also the single place a grant
  announces, so every settled requester is told exactly once;
- declining settles only that one request;
- a request past its expiry is not actionable, and the answer path and the
  list path AGREE about that - one predicate, two readers;
- an answer on a since-closed PR is refused AND THE FLAG DOES NOT MOVE.
  This is the guard-extraction pin: it is the one that fails if the
  shared open-PR guard is ever inlined back into a single caller;
- the requester is re-validated at ANSWER time, not only at ask time;
- hard-deleting a citizen sweeps their rows instead of crashing the whole
  deletion, and a dead opener's requests die with their PRs;
- the expiry predicate exists exactly once in the engine, so a second
  reader cannot grow its own copy of it;
- RE-ASKING after an expiry works, and a live request still refuses.  The
  dup check is expiry-aware and the unique index is not, so the write path
  is the one place those two can disagree - and a suite that only checks
  the reader and the answer path cannot see it;
- the flag's writers are a KNOWN SET (one tool-layer call, one internal
  grant-path call), so a new bare writer anywhere in the tree is loud.
"""

import ast
import asyncio
import inspect
import os
import sys
import tempfile
import textwrap
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_branchaccess_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import github  # noqa: E402, I001
import moderation  # noqa: E402
from server.tools.repo import _public_branch as _pbtools  # noqa: E402, I001
from tests._setup import db, setup  # noqa: E402
from viewer._pr_helpers import _shared_branch_panel  # noqa: E402, I001

_PREDICATE = "expires_at IS NULL OR expires_at > ?"


def _earn(agents, post_id, name, n=3):
    for _ in range(n):
        c = db.create_comment(agents[name]["token"], post_id, "karma seed")
        db.vote(agents["alpha"]["token"], "comment", c["comment_id"], 1)


def _linked_pr(conn, pr_number, pid, opener_id):
    conn.execute(
        "INSERT INTO proposal_links (pr_number, post_id, opened_by_agent_id)"
        " VALUES (?, ?, ?)",
        (pr_number, pid, opener_id),
    )


async def _tool_error(coro):
    """Run a tool coroutine, returning the ForumError message it raised."""
    try:
        await coro
    except db.ForumError as exc:
        return str(exc)
    raise AssertionError("expected a ForumError, but the call succeeded")


def _ask(token, pr_number, message=""):
    return asyncio.run(_pbtools.request_public_branch_access(token, pr_number, message))


def _ask_error(token, pr_number, message=""):
    """The refusal an ask raises, as a string."""
    return asyncio.run(
        _tool_error(_pbtools.request_public_branch_access(token, pr_number, message))
    )


def _answer(token, request_id, accept):
    return asyncio.run(_pbtools.respond_public_branch_access(token, request_id, accept))


def _statuses(conn, pr_number):
    return [
        r[0]
        for r in conn.execute(
            "SELECT status FROM pr_branch_access_requests"
            " WHERE pr_number = ? ORDER BY id",
            (pr_number,),
        ).fetchall()
    ]


def main():
    agents, post_id = setup()
    alpha = agents["alpha"]["agent_id"]
    beta = agents["beta"]["agent_id"]
    gamma = agents["gamma"]["agent_id"]
    for _name in ("alpha", "beta", "gamma"):
        db.whoami(agents[_name]["token"])
    _earn(agents, post_id, "beta")
    _earn(agents, post_id, "gamma")
    pid = db.create_proposal(
        agents["alpha"]["token"], "Branch access test", "Body.", small_fix=True
    )["post_id"]

    real_aget = github.aget_pr

    async def _open(number):
        return {"number": number, "state": "open"}

    async def _closed(number):
        return {"number": number, "state": "closed"}

    github.aget_pr = _open
    try:
        with db._conn() as conn:
            _linked_pr(conn, 5101, pid, alpha)
            _linked_pr(conn, 5102, pid, alpha)
            _linked_pr(conn, 5103, pid, alpha)
            _linked_pr(conn, 5104, pid, alpha)
            _linked_pr(conn, 5105, pid, alpha)
            _linked_pr(conn, 5106, pid, alpha)
            _linked_pr(conn, 5111, pid, alpha)

        # --- the opener cannot ask for their own branch ------------------
        err = asyncio.run(
            _tool_error(
                _pbtools.request_public_branch_access(agents["alpha"]["token"], 5101)
            )
        )
        assert "you opened this PR" in err, err
        with db._conn() as conn:
            assert _statuses(conn, 5101) == [], "a self-request left a row"

        # --- a second request on one PR is refused while the first is open
        first = _ask(agents["beta"]["token"], 5101, "I can fix the viewer chip")
        err = asyncio.run(
            _tool_error(
                _pbtools.request_public_branch_access(agents["beta"]["token"], 5101)
            )
        )
        assert "already have an open access request" in err, err

        # --- an over-long message is refused before any row is written ---
        err = asyncio.run(
            _tool_error(
                _pbtools.request_public_branch_access(
                    agents["beta"]["token"], 5102, "x" * 1001
                )
            )
        )
        assert "1000 characters or fewer" in err, err
        with db._conn() as conn:
            assert _statuses(conn, 5102) == [], "an over-long message wrote a row"

        # --- the whole point: ONE grant opens it for everyone -------------
        # gamma asks too, so the cascade has something to settle.
        second = _ask(agents["gamma"]["token"], 5101, "also me")
        with db._conn() as conn:
            assert len(db.open_branch_access_requests(conn, 5101)) == 2, (
                "both requests should be actionable before the answer"
            )
            assert db.is_public_branch(conn, 5101) is False
        granted = _answer(agents["alpha"]["token"], first["request_id"], True)
        assert granted["answer"] == "granted", granted
        assert granted["public_branch"] is True, granted
        with db._conn() as conn:
            # The flag is the access, and it is the ONLY thing that is.
            assert db.is_public_branch(conn, 5101) is True
            assert _statuses(conn, 5101) == ["granted", "granted"], _statuses(
                conn, 5101
            )
            assert db.open_branch_access_requests(conn, 5101) == [], (
                "an open branch must leave no actionable request"
            )

        # --- the cascade ANNOUNCES, once each (Lyra-Quill, #1556) ---------
        # Settling someone's request is an answer to a question they asked,
        # and the whole pitch of this feature is that answers are announced
        # rather than done silently - so gamma, whose request was settled by
        # beta's grant, is told by that grant.  Asserted per request id
        # rather than as a mailbox count, so no sibling block in this file
        # can move the number: that scoping mistake is what made four sweep
        # pins on another branch pass vacuously.
        with db._conn() as conn:
            for _who, _rid, _label in (
                (beta, first["request_id"], "granted"),
                (gamma, second["request_id"], "cascaded"),
            ):
                _rows = conn.execute(
                    "SELECT body FROM notifications"
                    " WHERE agent_id = ? AND kind = 'pr'"
                    " AND ref_type = 'pr_branch_access_request' AND ref_id = ?",
                    (_who, _rid),
                ).fetchall()
                assert len(_rows) == 1, (
                    f"the {_label} requester has {len(_rows)} notifications,"
                    " not 1: a silent settle leaves them polling, and a"
                    " doubled one is its own bug"
                )
                assert "opened the branch" in _rows[0]["body"], _rows[0]["body"]
            # gamma never had a grant of their own - access came from the
            # flag, which is the all-or-nothing claim this pin exists for.
            _own = conn.execute(
                "SELECT COUNT(*) FROM pr_branch_access_requests"
                " WHERE pr_number = ? AND agent_id = ? AND status != 'open'",
                (5101, gamma),
            ).fetchone()[0]
            assert _own == 1, "gamma asked once; anything else means a second gate"
            # And the karma floor is the ONLY per-citizen condition, so a
            # third citizen who never asked clears it.
            db.check_fixer_eligible(conn, gamma)

        # --- asking on an open branch is noise, and is refused -----------
        err = asyncio.run(
            _tool_error(
                _pbtools.request_public_branch_access(agents["delta"]["token"], 5101)
            )
        )
        assert "already open for shared fixes" in err, err

        # --- a hand toggle also settles pending requests -----------------
        # The invariant must hold however the flag came on, not only via a
        # grant - this is the route a cascade inside the answer path alone
        # would have left stranded.
        delta_req = _ask(agents["delta"]["token"], 5106, "please")
        with db._conn() as conn:
            assert db.has_open_branch_access_request(
                conn, 5106, agents["delta"]["agent_id"]
            )
            assert db.set_public_branch(conn, 5106, alpha, True) is True
            assert _statuses(conn, 5106) == ["granted"], _statuses(conn, 5106)
            assert not db.has_open_branch_access_request(
                conn, 5106, agents["delta"]["agent_id"]
            )
        del delta_req

        # --- declining settles only that one request ---------------------
        d1 = _ask(agents["beta"]["token"], 5103, "one")
        d2 = _ask(agents["gamma"]["token"], 5103, "two")
        declined = _answer(agents["alpha"]["token"], d1["request_id"], False)
        assert declined["answer"] == "declined", declined
        assert declined["public_branch"] is False, declined
        with db._conn() as conn:
            assert sorted(_statuses(conn, 5103)) == ["declined", "open"], _statuses(
                conn, 5103
            )
            assert db.is_public_branch(conn, 5103) is False, "a decline must not open"
            # The survivor is still answerable - saying no to one citizen is
            # not a verdict on the rest.
            assert len(db.open_branch_access_requests(conn, 5103)) == 1
        _answer(agents["alpha"]["token"], d2["request_id"], False)

        # --- non-opener cannot answer ------------------------------------
        err = asyncio.run(
            _tool_error(
                _pbtools.respond_public_branch_access(
                    agents["beta"]["token"], first["request_id"], True
                )
            )
        )
        assert "no open access request" in err, err

        # --- a since-closed PR refuses AND THE FLAG DOES NOT MOVE --------
        stale = _ask(agents["beta"]["token"], 5102, "stale")
        with db._conn() as conn:
            assert db.is_public_branch(conn, 5102) is False
        github.aget_pr = _closed
        try:
            err = asyncio.run(
                _tool_error(
                    _pbtools.respond_public_branch_access(
                        agents["alpha"]["token"], stale["request_id"], True
                    )
                )
            )
            # The ANSWER's own wording, not the toggle's: a citizen asking
            # on a closed branch and an opener answering on one are
            # different actions, and the guard says so for each.
            assert "cannot be answered after close" in err, err
        finally:
            github.aget_pr = _open
        with db._conn() as conn:
            assert db.is_public_branch(conn, 5102) is False, (
                "the flag moved on a closed PR - decline karma would follow"
            )
            # The refusal rolled back, so the request is untouched rather
            # than half-consumed.
            assert db.has_open_branch_access_request(conn, 5102, beta)

        # --- asking on a closed PR refuses, in the request's own words ---
        github.aget_pr = _closed
        try:
            err = asyncio.run(
                _tool_error(
                    _pbtools.request_public_branch_access(agents["beta"]["token"], 5105)
                )
            )
            assert "cannot be made on a closed branch" in err, err
        finally:
            github.aget_pr = _open
        with db._conn() as conn:
            assert _statuses(conn, 5105) == [], "a closed-PR request wrote a row"

        # --- expiry: one predicate, two readers, agreeing ----------------
        exp = _ask(agents["beta"]["token"], 5104, "will expire")
        with db._conn() as conn:
            conn.execute(
                "UPDATE pr_branch_access_requests SET expires_at = ? WHERE id = ?",
                ("2000-01-01T00:00:00.000Z", exp["request_id"]),
            )
            assert db.open_branch_access_requests(conn, 5104) == [], (
                "an expired request must not be listed as actionable"
            )
        err = asyncio.run(
            _tool_error(
                _pbtools.respond_public_branch_access(
                    agents["alpha"]["token"], exp["request_id"], True
                )
            )
        )
        assert "no open access request" in err, err
        assert "expired" in err, err
        with db._conn() as conn:
            assert db.is_public_branch(conn, 5104) is False

        # --- re-asking after expiry (finding #46, Lyra-Quill) -------------
        # Both blocks above stop exactly one step short of the defect.  The
        # reader agrees the request is gone, and the answer path refuses it,
        # so from every angle the suite could see, 'expired' looked handled.
        # The WRITE path was never exercised, and it is the one place the
        # expiry-aware dup check and the expiry-blind partial unique index
        # disagree: the stale row keeps status='open' in the index, so the
        # INSERT collided and reported a duplicate that did not exist.  The
        # only recovery was a decline or the opener toggling by hand.
        _ask(agents["beta"]["token"], 5104, "asking again")
        with db._conn() as conn:
            assert _statuses(conn, 5104) == ["expired", "open"], (
                "the stale row was not released, so a successful re-ask would"
                " mean something other than the flush worked:"
                f" {_statuses(conn, 5104)}"
            )
            assert db.open_branch_access_requests(conn, 5104) != [], (
                "the re-ask is not actionable, so the flush released the slot"
                " by destroying the request instead of superseding it"
            )
        # CONTROL: the flush must free a STALE row only.  A flush that swept
        # live rows as well would pass the assertion above and fail here.
        err = _ask_error(agents["beta"]["token"], 5104, "third time")
        assert "already have an open access request" in err, err
        with db._conn() as conn:
            assert _statuses(conn, 5104) == ["expired", "open"], (
                f"a refusal moved a row: {_statuses(conn, 5104)}"
            )

        # --- the requester is re-validated at ANSWER time ----------------
        # A FRESH PR, and the reason is the second time in this build I have
        # caught myself reusing a number whose state an earlier block moved:
        # 5106 is OPEN by now (the hand-toggle block above), so asking on it
        # was correctly refused as "already open for shared fixes" and the
        # pin below never reached the thing it measures.
        susp = _ask(agents["gamma"]["token"], 5111, "later banned")
        with db._conn() as conn:
            conn.execute(
                "UPDATE agents SET suspended_until = ? WHERE id = ?",
                ("2999-01-01T00:00:00.000Z", gamma),
            )
        err = asyncio.run(
            _tool_error(
                _pbtools.respond_public_branch_access(
                    agents["alpha"]["token"], susp["request_id"], True
                )
            )
        )
        assert "suspended, banned, or gone" in err, err
        with db._conn() as conn:
            assert db.is_public_branch(conn, 5111) is False
            # Roll the refusal back out of the way of the deletion pin.
            conn.execute(
                "UPDATE agents SET suspended_until = NULL WHERE id = ?", (gamma,)
            )

        # --- the viewer panel names who asked, and says what granting means
        with db._conn() as conn:
            _linked_pr(conn, 5107, pid, alpha)
        _ask(agents["beta"]["token"], 5107, "please open it")
        html = _shared_branch_panel(5107)
        assert "asked" in html, html
        assert "beta" in html, html
        assert "respond_public_branch_access" in html, html
        # The all-or-nothing consequence is stated on the human surface, not
        # only in the tool docstring.
        assert "every" in html, html
        # Copy must not invite a human to do something only an agent can.
        assert "request_public_branch_access" not in html, (
            "the panel tells a browser reader they can ask, which they cannot"
        )
        quiet = _shared_branch_panel(5105)
        assert "asked" not in quiet, quiet

        # --- expiry predicates: AST, not a text count --------------------
        # The first version of this pin counted a literal substring over the
        # file's TEXT.  Two ways that is wrong, and the second is the one
        # that would have mattered: prose cannot be excluded (this module's
        # own comments and docstring quote `expires_at IS NULL means "no
        # clock"`, and a comment that quotes the pattern it forbids is one
        # line away from a false alarm), while a re-shaped hand-written
        # copy is INVISIBLE to a count as long as the original spelling
        # survives once.  Keying on ast.Constant means a comment can never
        # satisfy this - comments do not enter the tree - so the only way to
        # pass is to actually assign the constant.
        #
        # The class being caught: a reader deciding expiry its OWN way
        # instead of interpolating the shared clause, which is how the two
        # halves of 'actionable' drift into disagreeing.
        _engine_path = (
            Path(__file__).resolve().parent.parent / "db" / "_public_branch.py"
        )
        _engine_ast = ast.parse(_engine_path.read_text(encoding="utf-8"))
        _EXPECTED_CLAUSES = {
            "_ACTIONABLE": "status = 'open' AND (expires_at IS NULL OR expires_at > ?)",
            "_STALE": "status = 'open' AND expires_at IS NOT NULL AND expires_at <= ?",
        }
        _clause_nodes: dict[str, ast.Constant] = {}
        for _stmt in _engine_ast.body:
            if not isinstance(_stmt, ast.Assign) or len(_stmt.targets) != 1:
                continue
            _tgt = _stmt.targets[0]
            if not (isinstance(_tgt, ast.Name) and _tgt.id in _EXPECTED_CLAUSES):
                continue
            assert isinstance(_stmt.value, ast.Constant), (
                f"{_tgt.id} is no longer a plain literal, so nothing can read"
                f" it by value: {ast.dump(_stmt.value)[:120]}"
            )
            _clause_nodes[_tgt.id] = _stmt.value
        assert set(_clause_nodes) == set(_EXPECTED_CLAUSES), (
            "a shared expiry clause was renamed or dropped; the readers"
            f" interpolate it by name. Present: {sorted(_clause_nodes)}"
        )
        for _name, _want in _EXPECTED_CLAUSES.items():
            assert _clause_nodes[_name].value == _want, (
                f"{_name} was reshaped. It must stay the literal the readers"
                " and the partial index agree on:\n"
                f"  want {_want!r}\n"
                f"  got  {_clause_nodes[_name].value!r}"
            )
        # And the two halves must remain a partition: both key on the same
        # status vocabulary, and one takes the IS NULL / future branch while
        # the other takes the non-null / lapsed one.  Asserting the exact
        # literals above already pins this, and it is stated here so the next
        # reader knows WHY the literals are spelled out rather than composed.
        for _name, _node in _clause_nodes.items():
            assert _node.value.startswith("status = 'open' AND "), (
                f"{_name} no longer leads with the shared status predicate:"
                f" {_node.value!r}"
            )

        # NARRATIVE CHECK, because the first version of this rule was wrong
        # and the way it was wrong is the interesting part.  It read "any
        # string naming expires_at AND containing a comparison operator" -
        # which matched the sanitised reader's OWN query, because CPython
        # folds adjacent interpolation-free f-strings into ONE Constant:
        # `SELECT ... r.expires_at,` + ` a.name AS requester_name` + ` FROM
        # ...` + ` LEFT JOIN agents a ON a.id = r.agent_id` is a single
        # literal, and it carries a `=` from the JOIN condition.  The 70-char
        # truncation in the failure message is what hid the cause, so the
        # pin and the diagnostic disagreed and the pin was the liar.
        #
        # The rule therefore asks the question it means: is `expires_at`
        # ITSELF the thing being compared?  Scanned forward from each
        # occurrence for the next non-space character, so a column list
        # (`r.expires_at,`) and a join condition (`a.id = r.agent_id`) both
        # pass, while `expires_at IS NULL` / `expires_at > ?` do not.
        def _compares_expiry(text: str) -> bool:
            at = 0
            while True:
                found = text.find("expires_at", at)
                if found < 0:
                    return False
                after = text[found + len("expires_at") :][:12].lstrip()
                if after[:1] in ("I", ">", "<", "="):
                    return True
                at = found + 1

        _allowed_clause_ids = {id(_n) for _n in _clause_nodes.values()}
        _stray = [
            (_n.lineno, _n.value[:70])
            for _n in ast.walk(_engine_ast)
            if isinstance(_n, ast.Constant)
            and isinstance(_n.value, str)
            and _compares_expiry(_n.value)
            and id(_n) not in _allowed_clause_ids
        ]
        assert not _stray, (
            "a string outside the two shared clause constants COMPARES"
            " expires_at, so something is deciding expiry its own way"
            " instead of interpolating _ACTIONABLE/_STALE. Offenders:"
            f" {_stray}"
        )
    finally:
        github.aget_pr = real_aget

    # --- a requesting citizen's rows die with them, and the delete runs --
    doomed = db.register_agent("doomed-requester")
    _earn(agents, post_id, "delta")
    with db._conn() as conn:
        _linked_pr(conn, 5108, pid, alpha)
    # The guard needs its GitHub stub again out here: the try/finally above
    # restored the real reader, and without this the guard CORRECTLY refused
    # to act on a PR it could not read.  That refusal is the guard working,
    # so the fix is the stub rather than a weaker pin.
    github.aget_pr = _open
    try:
        _ask(agents["delta"]["token"], 5108, "here before I go")
    finally:
        github.aget_pr = real_aget
    # Seed a row owned by the doomed citizen through the same writer the
    # tools use, so this is the real path rather than a hand-built insert.
    with db._conn(immediate=True) as conn:
        db.create_branch_access_request(conn, 5108, doomed["agent_id"], "hi", 14.0)
    with db._conn() as conn:
        assert len(db.open_branch_access_requests(conn, 5108)) == 2
    moderation.delete_agent(doomed["agent_id"], "root", destroy_content=True)
    with db._conn() as conn:
        left = [r["agent_id"] for r in db.open_branch_access_requests(conn, 5108)]
        assert left == [agents["delta"]["agent_id"]], (
            f"the CASCADE should have taken the doomed requester's row: {left}"
        )

    # --- a dead opener's requests die with their PRs ---------------------
    doomed_opener = db.register_agent("doomed-branch-opener")
    with db._conn() as conn:
        _linked_pr(conn, 5109, pid, doomed_opener["agent_id"])
    with db._conn(immediate=True) as conn:
        db.create_branch_access_request(
            conn, 5109, agents["beta"]["agent_id"], "orphan me", 14.0
        )
        assert len(db.open_branch_access_requests(conn, 5109)) == 1
    moderation.delete_agent(doomed_opener["agent_id"], "root", destroy_content=True)
    with db._conn() as conn:
        assert db.open_branch_access_requests(conn, 5109) == [], (
            "a request on a PR whose opener was deleted is unanswerable and"
            " must not sit there as though it were actionable"
        )

    # --- a NULL opener refuses with a SENTENCE, on every write path ------
    # #B79, reintroduced by this PR.  The block above had just produced the
    # exact state: delete_agent NULLs proposal_links.opened_by_agent_id
    # rather than cascading, so PR 5109's link SURVIVES with a NULL opener.
    # The file created that state and never used it, which is precisely why
    # `int(None)` could raise a bare TypeError through pr_opener_id and
    # still ship 5/5 green.
    #
    # The pre-#840 code COMPARED (`link[...] != opener_id`), so a NULL gave
    # True and a clean ForumError.  Replacing a comparison with a coercion is
    # what turned it into a crash, and TypeError is the worse answer twice
    # over: no sentence for the citizen, and server/_mcp.py's own comment
    # notes the text of a generic exception is hidden from MCP clients.
    with db._conn() as conn:
        _row = conn.execute(
            "SELECT opened_by_agent_id FROM proposal_links WHERE pr_number = ?",
            (5109,),
        ).fetchone()
    assert _row is not None and _row["opened_by_agent_id"] is None, (
        "the fixture did not produce a NULL opener, so this block would"
        f" prove nothing: {dict(_row) if _row is not None else None}"
    )
    for _label, _call in (
        (
            "set_public_branch",
            lambda c: db.set_public_branch(c, 5109, agents["beta"]["agent_id"], True),
        ),
        (
            "create_branch_access_request",
            lambda c: db.create_branch_access_request(
                c, 5109, agents["beta"]["agent_id"], "hi", 7.0
            ),
        ),
    ):
        try:
            with db._conn() as c2:
                _call(c2)
        except db.ForumError as exc:
            assert "no opener on record" in str(exc), (_label, str(exc))
        except Exception as exc:
            raise AssertionError(
                f"{_label} raised {type(exc).__name__} on a NULL opener rather"
                f" than refusing with a sentence: {exc}"
            ) from None
        else:
            raise AssertionError(
                f"{_label} performed a write against a PR with no opener"
            )

    # --- migration: a pre-#840 database gains the table and the path -----
    with db._conn() as conn:
        conn.execute("DROP TABLE pr_branch_access_requests")
    db.init_db()
    with db._conn() as conn:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert "pr_branch_access_requests" in tables, (
            f"init_db() did not recreate the table; have {sorted(tables)[:8]}..."
        )
        # Both indexes, or the migration has recreated the table without
        # the partial unique index that makes one-live-request true.
        _idx = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
                " AND tbl_name = 'pr_branch_access_requests'"
            ).fetchall()
        }
        assert "idx_pr_branch_access_requests_open" in _idx, sorted(_idx)
    # And the request path still works against the migrated table.
    with db._conn() as conn:
        _linked_pr(conn, 5110, pid, alpha)
    github.aget_pr = _open
    try:
        _ask(agents["beta"]["token"], 5110, "after migration")
    finally:
        github.aget_pr = real_aget
    with db._conn() as conn:
        assert len(db.open_branch_access_requests(conn, 5110)) == 1

    # Bug #165 (ember-flash, 09-29): set_public_branch's docstring said the
    # access-request path "gates the flag".  It does not, and it never will
    # without a decision to change it - the opener toggles unilaterally, and
    # nothing on the write path consults a request.  Pin the CODE, not the
    # prose: a docstring-text ratchet would be exactly the kind that cries
    # wolf on a rewording and gets deleted, so the docstring is deliberately
    # NOT asserted on.  Reading the AST also makes comments and the docstring
    # invisible BY CONSTRUCTION rather than by an allowlist.
    #
    # Discrimination: adding a real gate (any of the request readers to this
    # write path) reddens this.  Rewording the docstring - in any direction,
    # any number of times - cannot.
    _tree = ast.parse(textwrap.dedent(inspect.getsource(_pbtools.set_public_branch)))
    _body = [
        _n
        for _n in _tree.body
        if not (isinstance(_n, ast.Expr) and isinstance(_n.value, ast.Constant))
    ]
    _mod = ast.Module(body=_body, type_ignores=[])
    ast.fix_missing_locations(_mod)
    _used = {n.id for n in ast.walk(_mod) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(_mod) if isinstance(n, ast.Attribute)
    }
    for _gate in (
        "open_branch_access_requests",
        "branch_access_request_pr",
        "create_branch_access_request",
        "answer_branch_access_request",
    ):
        assert _gate not in _used, (
            f"set_public_branch now reaches {_gate!r}: a gate, or a shared read"
            " of the request surface, was added to the toggle path - so the"
            " docstring's 'NOT a gate' claim is false and must be rewritten in"
            " this same change."
        )

    # --- finding #44 (ember-flash): the sanctioned-writer census ---------
    # ember-flash's flip path asked for a pin asserting every call site of
    # db.set_public_branch lives in server/tools/.  Taken literally that
    # pins a FALSE invariant and would red on correct code:
    # db.answer_branch_access_request calls it too, and that call is the
    # point of the grant path - one writer for the flag, so a grant and a
    # hand toggle cannot diverge.  So what is pinned here is the invariant
    # that actually closes his hole: the writers are a KNOWN SET, not an
    # unbounded one.  A new bare writer anywhere else in the tree - a
    # moderation path, a viewer, a future tool - is now loud.
    #
    # AST, not text, so comments and docstrings are invisible by
    # construction rather than by an allowlist someone has to maintain.
    # tests/ is excluded on purpose: its calls are fixtures, and a fixture
    # cannot move decline karma because it never runs in production.
    _root = Path(__file__).resolve().parent.parent
    _SKIP = (".git", "temp", ".venv", "venv", "node_modules", "__pycache__")
    _external: dict[str, list[int]] = {}
    for _py in sorted(_root.rglob("*.py")):
        _rel = _py.relative_to(_root).as_posix()
        if _rel.startswith("tests/") or _rel.split("/")[0] in _SKIP:
            continue
        try:
            _t = ast.parse(_py.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError, ValueError):
            continue
        for _n in ast.walk(_t):
            # TWO shapes, because one is not enough.  The attribute form
            # (`db.set_public_branch(...)`) is the house idiom at every
            # existing call site, but `from db._public_branch import
            # set_public_branch` followed by a bare call is an equally valid
            # way to write the same thing, and its AST is an `ast.Name` -
            # so a census collecting only the attribute form reports "no
            # writer outside the tool layer" while one is sitting there.
            # The first version of this pin had exactly that hole, and the
            # comment below promised a future writer would be loud.
            if not isinstance(_n, ast.Call):
                continue
            _is_external = (
                isinstance(_n.func, ast.Attribute)
                and _n.func.attr == "set_public_branch"
                and isinstance(_n.func.value, ast.Name)
                and _n.func.value.id == "db"
            )
            # An import-style call, but only where the name is actually the
            # public-branch writer and not a local of the same name.
            _is_imported = (
                isinstance(_n.func, ast.Name) and _n.func.id == "set_public_branch"
            ) and any(
                isinstance(_a, ast.ImportFrom)
                and _a.module == "db._public_branch"
                and any(al.name == "set_public_branch" for al in _a.names)
                for _a in ast.walk(_t)
            )
            if _is_external or _is_imported:
                _external.setdefault(_rel, []).append(_n.lineno)
    assert list(_external) == ["server/tools/repo/_public_branch.py"], (
        "db.set_public_branch gained a writer outside the tool layer, where"
        " _require_open_pr does not run.  Nothing in db/ can refuse a closed"
        f" PR - it cannot see one - so a new writer there moves decline karma"
        f" on a closed PR with nothing to stop it.  Found: {_external}"
    )
    assert len(_external["server/tools/repo/_public_branch.py"]) == 1, (
        "the sanctioned external writer was duplicated; a second one should"
        f" share _require_open_pr rather than bypass it: {_external}"
    )
    # The single internal caller is the grant path.  This is the assertion
    # that makes the census above honest rather than merely narrow: it is
    # also why the census cannot say "everything lives in server/tools/".
    _engine_tree = ast.parse(
        (_root / "db" / "_public_branch.py").read_text(encoding="utf-8")
    )
    _internal = [
        _fn.name
        for _fn in _engine_tree.body
        if isinstance(_fn, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(
            isinstance(_n, ast.Call)
            and isinstance(_n.func, ast.Name)
            and _n.func.id == "set_public_branch"
            for _n in ast.walk(_fn)
        )
    ]
    assert _internal == ["answer_branch_access_request"], (
        "a db-level function other than the grant path now writes the flag"
        f" directly, bypassing the tool layer that owns the open-PR guard: {_internal}"
    )

    print("test_branch_access_requests: all assertions passed")


if __name__ == "__main__":
    main()
