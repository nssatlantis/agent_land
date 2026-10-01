"""Pins for CI-farm legibility (proposal #907): the skip ledger, the no-pick
classifier, the read-only panel probe, and the rejected-vs-may-have-run split.

HTTP is mocked - no network, no docker, no runner. The skip rows are asserted
on the payload handed to the ledger, and the no-pick classifier is driven
against real registry rows, so every population under test is one the code can
actually reach rather than a hand-built fixture.

Split into its own file for the usual reason (a shared-DB file hands its
residue to later siblings) and because every arm here is cheap.
"""

import faulthandler
import json
import os
import sys
import tempfile
import urllib.error
from pathlib import Path
from unittest import mock

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_ci_farm_obs_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

faulthandler.dump_traceback_later(110, exit=True)

import config  # noqa: E402
import db  # noqa: E402
import server.ci_runner._farm as farm  # noqa: E402
import server.ci_runner._trees as trees_mod  # noqa: E402

# The farm is the subject of every test in this file, and CI_FARM_ENABLED
# defaults to 0 - try_dispatch returns None on it before dispatching anything.
# Armed once here, for the whole file, rather than per test: patching it
# inside each test would have meant re-indenting bodies for no extra coverage,
# and the first version of this file shipped two pins that passed only
# because they never reached the code they claimed to test.
config.CI_FARM_ENABLED = True


def _captured(fn, *args, **kwargs):
    """Run fn with the ledger stubbed; return (skips, failures) payloads."""
    skips: list = []
    failures: list = []

    def _log(kind, **kw):
        (skips if kind == farm.events.EVT_CI_FARM_SKIPPED else failures).append(kw)

    with mock.patch.object(farm.events, "log_event", _log):
        fn(*args, **kwargs)
    return skips, failures


def _captured_allowing_retry(fn, *args, **kwargs):
    """Like _captured, but a _FarmRetryLocal is swallowed and returned.

    _FarmRetryLocal is the overflow lane's "run it locally instead" signal, so
    for a declined run it is the CORRECT outcome, not a test failure. Swallowing
    it is what lets the row assertions run at all - and asserting on it is the
    stronger half, because a None return on that lane means "no runner" and
    becomes a BUSY error for the agent instead.
    """
    skips: list = []
    failures: list = []
    retried: list = []

    def _log(kind, **kw):
        (skips if kind == farm.events.EVT_CI_FARM_SKIPPED else failures).append(kw)

    with mock.patch.object(farm.events, "log_event", _log):
        try:
            fn(*args, **kwargs)
        except farm._FarmRetryLocal as exc:
            retried.append(str(exc))
    return skips, failures, retried


def test_skip_reason_vocabulary_is_closed():
    """log_skip writes a ci_farm_skipped row, and an out-of-vocabulary reason
    is coerced rather than passed through.

    The coercion arm is load-bearing: an unrecognised reason reaching the
    ledger verbatim would make the row unqueryable by anyone filtering on
    SKIP_REASONS, which is the entire reason the vocabulary is closed.
    """
    skips, failures = _captured(
        farm.log_skip, "busy", "overflow", "tests", 7, "citizen-four"
    )
    assert not failures, "log_skip must not write a dispatch-failure row"
    assert len(skips) == 1, f"expected exactly one skip row, got {skips}"
    detail = skips[0]["detail"]
    assert detail["reason"] == "busy"
    assert detail["lane"] == "overflow"
    assert detail["checks"] == "tests"
    for key in ("reason", "lane", "checks", "runner", "runner_id", "url"):
        assert key in detail, f"skip detail must carry {key!r}: {detail}"

    bad, _ = _captured(farm.log_skip, "something_new", "overflow", "tests", 7, "x")
    assert bad[0]["detail"]["reason"] in farm.SKIP_REASONS, (
        f"out-of-vocabulary reason leaked: {bad[0]['detail']['reason']!r}"
    )


def test_classify_no_pick_tells_five_populations_apart():
    """An empty pick must be attributable: no runner, dispatch disabled, all
    unhealthy, all at the server cap, or all busy - decided from the registry
    state the failed attempt already wrote.

    Each arm builds a real row, so none is a population the code cannot reach.
    The precedence arm matters too: with two runners, one at cap and one
    recorded busy, at_capacity must win - a farm that is full AND busy is
    full, and reporting "busy" would send an operator to look at the wrong
    thing.
    """
    db.init_db()

    for row in farm.list_runners():
        farm.remove_runner(row["id"])
    assert farm.classify_no_pick() == ("no_runner_registered", 0)

    a = farm.register_runner("cap-runner", "http://a")
    b = farm.register_runner("busy-runner", "http://b")
    try:
        farm._mark(a["id"], "stale")
        farm._mark(b["id"], "stale")
        assert farm.classify_no_pick()[0] == "unhealthy"

        farm._mark(b["id"], "busy")
        assert farm.classify_no_pick()[0] == "busy"

        with mock.patch.object(config, "CI_FARM_RUNNER_MAX_ACTIVE", 1):
            with farm._ACTIVE_LOCK:
                farm._ACTIVE_RUNS[a["id"]] = 1
            try:
                reason, count = farm.classify_no_pick()
                assert reason == "at_capacity", f"got {reason}"
                assert count == 2, f"both runners must be counted: {count}"
            finally:
                with farm._ACTIVE_LOCK:
                    farm._ACTIVE_RUNS.pop(a["id"], None)

        # A cap of zero admits nothing, so "at capacity" cannot exist.
        # pick_runner returns at that knob BEFORE it reads the registry, so
        # the recorded state says nothing about the reason and the only honest
        # answer is that dispatch is switched off.
        #
        # Both runners are marked healthy first. The previous version of this
        # arm left runner b marked busy from the arm above, so it asserted
        # "busy" and was satisfied by the status branch - it never reached the
        # fall-through it claimed to test, and would have passed against code
        # that reported a switched-off farm as unhealthy. The arm has to build
        # the population it names.
        farm._mark(b["id"], "healthy")
        farm._mark(a["id"], "healthy")
        with mock.patch.object(config, "CI_FARM_RUNNER_MAX_ACTIVE", 0):
            with farm._ACTIVE_LOCK:
                farm._ACTIVE_RUNS[a["id"]] = 99
            try:
                reason, count = farm.classify_no_pick()
                assert reason == "disabled", (
                    f"a zero cap is the operator switching dispatch off, not "
                    f"runners being down; got {reason!r}"
                )
                assert reason != "unhealthy", (
                    "reporting 'unhealthy' for a deliberately disabled farm is "
                    "the exact confusion this vocabulary exists to remove"
                )
                assert count == 2, f"got {count}"
            finally:
                with farm._ACTIVE_LOCK:
                    farm._ACTIVE_RUNS.pop(a["id"], None)
        assert "disabled" in farm.SKIP_REASONS, (
            "a reason the classifier can return must be in the closed "
            "vocabulary, or log_skip coerces it to 'unhealthy' on the way out"
        )
    finally:
        farm.remove_runner(a["id"])
        farm.remove_runner(b["id"])


def test_probe_runners_is_read_only():
    """The panel probe must not consume a slot or stamp a heartbeat.

    This is the assertion that catches the tempting refactor - reusing
    pick_runner for the panel, which would make rendering the page both eat a
    dispatch slot and overwrite the one column whose meaning is "when a
    dispatch last happened". Snapshot the table before and after and compare.
    """
    db.init_db()
    row = farm.register_runner("probe-runner", "http://127.0.0.1:1", token="t")

    def _snap():
        with db._conn() as conn:
            return [
                dict(r)
                for r in conn.execute("SELECT * FROM ci_runners ORDER BY id").fetchall()
            ]

    try:
        with mock.patch.object(farm, "_ping", lambda *a, **k: {"ok": True}):
            before = _snap()
            out = farm.probe_runners()
            after = _snap()
            assert before == after, "probe_runners must not write the registry"
            assert not farm._ACTIVE_RUNS, (
                f"probe_runners reserved a dispatch slot: {farm._ACTIVE_RUNS}"
            )
            mine = [p for p in out if p["id"] == row["id"]]
            assert len(mine) == 1, f"probe must report the runner: {out}"
            assert mine[0]["reachable"] is True
            assert mine[0]["busy"] is False

            # A dead runner must read unreachable, not silently absent - the
            # point of the column is that off and broken differ.
            with mock.patch.object(farm, "_ping", lambda *a, **k: None):
                dead = farm.probe_runners()
            dmine = [p for p in dead if p["id"] == row["id"]]
            assert dmine and dmine[0]["reachable"] is False, (
                f"a failed ping must read unreachable: {dmine}"
            )
    finally:
        farm.remove_runner(row["id"])


def _http_error(code):
    return urllib.error.HTTPError(
        "http://runner/run",
        code,
        "boom",
        {},
        None,  # py311: hdrs=None is fine
    )


class _Resp:
    payload = b"[]"

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _OkResp(_Resp):
    payload = b'{"ok": true, "exit_code": 0}'


def _dispatch(side_effect=None, responder=None):
    """One dispatch against a FRESH runner dict; return (result, reason).

    Fresh per call on purpose: the reason rides on the runner dict, so reusing
    one would let a stale reason satisfy the next arm - the same defect as a
    pin asserting on state it did not produce.
    """
    runner = {"id": 1, "url": "http://runner"}
    with mock.patch.object(farm.config, "CI_FARM_DISPATCH_TIMEOUT", 5):
        if responder is not None:
            with mock.patch.object(
                farm.urllib.request, "urlopen", lambda *a, **k: responder
            ):
                res = farm.dispatch_to_runner(runner, {"checks": "tests"})
        else:
            with mock.patch.object(
                farm.urllib.request, "urlopen", side_effect=side_effect
            ):
                res = farm.dispatch_to_runner(runner, {"checks": "tests"})
    return res, str(runner.get("_dispatch_reason") or "")


def test_dispatch_to_runner_splits_rejected_from_may_have_run():
    """A runner that DECLINED must never be recorded as may-have-executed.

    This is the arm the whole change exists for, and the one pre-fix code
    could not express at all: urlopen raises HTTPError on any 4xx, so the 409
    "runner busy" at ci_farm/runner.py:519 fell into a bare `except
    Exception` and was reported as the same string a mid-run socket reset
    produced. On the old bytes the runner dict carries no _dispatch_reason at
    all, so every assertion below fails - and none of them can be satisfied by
    a return value, because there is no return value to read.
    """
    # 409 -> rejected: no work was done, so a local retry is the only execution
    # and nothing ran twice.
    res, why = _dispatch(side_effect=_http_error(409))
    assert res is None
    assert why == farm.DISPATCH_REJECTED, f"409 must be rejected, got {why}"
    assert why != farm.DISPATCH_MAY_HAVE_RUN

    # Other 4xx are equally declined; 5xx is NOT, because it may have started.
    for code in (400, 404):
        _, why = _dispatch(side_effect=_http_error(code))
        assert why == farm.DISPATCH_REJECTED, f"{code} must be rejected: {why}"
    _, why = _dispatch(side_effect=_http_error(500))
    assert why == farm.DISPATCH_MAY_HAVE_RUN, f"5xx must be unknown: {why}"

    # A timeout is precisely the case where the work may already be running.
    _, why = _dispatch(side_effect=TimeoutError())
    assert why == farm.DISPATCH_MAY_HAVE_RUN, f"timeout must be unknown: {why}"

    # A 2xx whose body is not a dict is unreadable, not rejected.
    _, why = _dispatch(responder=_Resp())
    assert why == "unreadable_body", f"got {why}"

    # Success leaves no reason at all, so no caller can read one as a fault.
    res, why = _dispatch(responder=_OkResp())
    assert res == {"ok": True, "exit_code": 0}, f"result must round-trip: {res}"
    assert why == "", f"success must carry no reason, got {why!r}"


def test_try_dispatch_409_is_a_skip_and_not_a_failure():
    """The behavioural change: a declined run becomes a `busy` skip, writes no
    dispatch-failure row, and still takes the local-retry path.

    All three halves matter. The skip row is what makes a full farm legible;
    the ABSENCE of a failure row is what stops a full farm being reported as a
    broken one; and the raise is what stops this change turning "run locally"
    into "busy, retry later" for the agent. Pre-fix, the 409 produced a
    ci_farm_dispatch_failed row whose error string was "runner reply
    unreadable".
    """
    db.init_db()
    runner = {"id": 4242, "url": "http://runner", "name": "full-farm"}

    with mock.patch.object(farm, "pick_runner", return_value=runner):
        with mock.patch.object(
            farm.urllib.request, "urlopen", side_effect=_http_error(409)
        ):
            skips, failures, retried = _captured_allowing_retry(
                farm.try_dispatch,
                checks="tests",
                local_mode=True,
                branch_mode=False,
                is_bench=False,
                pr_number=None,
                files=[{"path": "a.py", "content": "x\n"}],
                tree=None,
                quiet=None,
                base_ref=None,
                agent_id=7,
                name="citizen-four",
                kind_event="ci_local_run",
                run_id=None,
            )

    assert not failures, (
        f"a declined run is a capacity skip, not a dispatch failure: {failures}"
    )
    assert retried, (
        "a declined run must still raise the local-retry signal: returning None "
        "on this lane means 'no runner' and becomes a busy error for the agent"
    )
    assert len(skips) == 1, f"expected one skip row, got {skips}"
    assert skips[0]["detail"]["reason"] == "busy"
    assert skips[0]["detail"]["lane"] == "overflow"
    assert skips[0]["detail"]["runner"] == "full-farm", (
        f"the skip must name the runner that declined: {skips[0]['detail']}"
    )


def test_try_dispatch_no_runner_logs_a_skip():
    """An empty pick is attributable. Pre-fix this returned None silently - a
    farm could be switched off entirely and the ledger stayed quiet, which is
    how "idle" and "broken" came to read the same."""
    db.init_db()
    for row in farm.list_runners():
        farm.remove_runner(row["id"])

    with mock.patch.object(farm, "pick_runner", return_value=None):
        with mock.patch.object(farm, "classify_no_pick", return_value=("busy", 1)):
            skips, failures = _captured(
                farm.try_dispatch,
                checks="tests",
                local_mode=True,
                branch_mode=False,
                is_bench=False,
                pr_number=None,
                files=[{"path": "a.py", "content": "x\n"}],
                tree=None,
                quiet=None,
                base_ref=None,
                agent_id=7,
                name="citizen-four",
                kind_event="ci_local_run",
                run_id=None,
            )
    assert not failures, "an empty pick must not write a failure row"
    assert len(skips) == 1, f"an empty pick must log a skip, got {skips}"
    assert skips[0]["detail"]["reason"] == "busy"
    assert skips[0]["detail"]["candidates"] == 1


def test_tree_payload_ships_the_union_not_just_the_newest_delta():
    """The parity-critical arm, and the one P2 exists for.

    The local path resets the tree to current origin/<base> and replays the
    stored deltas and THEN the incoming one. A payload carrying only the
    incoming delta would therefore describe a DIFFERENT tree from the one the
    local path runs - which is the failure the farm gate's own comment names,
    and the one the whole design refuses rather than risks. So order and
    membership are both load-bearing: stored first, incoming last.
    """
    db.init_db()
    stored_a = [{"path": "one.py", "content": "A = 1\n"}]
    stored_b = [{"path": "two.py", "content": "B = 2\n"}]
    incoming = [{"path": "three.py", "content": "C = 3\n"}]
    with mock.patch.object(
        trees_mod, "named_tree_deltas", return_value=[stored_a, stored_b]
    ):
        payload, reason = farm.tree_payload(7, "feat", incoming, "main")
    assert reason == "", f"a reproducible tree must be eligible: {reason}"
    assert payload is not None
    paths = [e["path"] for e in payload]
    assert paths == ["one.py", "two.py", "three.py"], (
        f"payload must be stored-then-incoming, got {paths}"
    )


def test_tree_payload_refuses_rather_than_ships_a_partial_tree():
    """Every refusal arm, each a population the code can actually reach.

    A cold tree is eligible and ships exactly the incoming delta - the case a
    cold-only design would have handled. The rest are the refusals, and each
    one must stay local rather than guess: a wrong tree that reports green is
    the worst outcome this subsystem can produce.
    """
    db.init_db()
    incoming = [{"path": "a.py", "content": "x\n"}]

    with mock.patch.object(trees_mod, "named_tree_deltas", return_value=[]):
        payload, reason = farm.tree_payload(7, "feat", incoming, "main")
    assert payload == incoming, f"a cold tree ships the incoming delta: {payload}"
    assert reason == ""

    # Nothing to ship: a warm re-run with no new delta stays local, because
    # the run would measure the stored deltas and nothing else.
    with mock.patch.object(trees_mod, "named_tree_deltas", return_value=[]):
        payload, reason = farm.tree_payload(7, "feat", [], "main")
    assert payload is None, "a tree with no incoming delta must stay local"
    assert reason == "ineligible_tree"

    # No base to reconstruct against.
    with mock.patch.object(trees_mod, "named_tree_deltas", return_value=[]):
        payload, reason = farm.tree_payload(7, "feat", incoming, "")
    assert payload is None
    assert reason == "ineligible_tree"

    # Ceilings, count first then bytes: the runner's own limits, mirrored so a
    # payload it would reject is refused BEFORE a multi-MB upload. The gate's
    # comment records paying that upload "to learn the cap" as owed.
    many = [{"path": f"f{i}.py", "content": "x\n"} for i in range(51)]
    with mock.patch.object(trees_mod, "named_tree_deltas", return_value=[]):
        payload, reason = farm.tree_payload(7, "feat", many, "main")
    assert payload is None, f"{len(many)} files must be refused"
    assert reason == "ineligible_tree"

    fat = [{"path": "big.bin", "content": "x" * (farm.FARM_MAX_FILES_BYTES + 1)}]
    with mock.patch.object(trees_mod, "named_tree_deltas", return_value=[]):
        payload, reason = farm.tree_payload(7, "feat", fat, "main")
    assert payload is None, "an oversize payload must be refused before upload"
    assert reason == "ineligible_tree"


def test_named_tree_deltas_is_read_only_and_absent_safe():
    """A predicate that mutated the tree it judges would be a nasty bug, and a
    tree that does not exist must read as 'no stored deltas' rather than
    raising - otherwise the farm gate crashes the builder lane it was meant to
    offload."""
    db.init_db()
    assert trees_mod.named_tree_deltas(7, "never-made-this-tree") == []
    # A name that fails validation must not escape the tree root.
    assert trees_mod.named_tree_deltas(7, "../escape") == []


def test_the_test_lane_consults_the_payload_decision():
    """The gate consults tree_payload and the payload's eligibility.

    A source-shape pin, declared as one: driving run_checks end to end for
    the farm path means standing up a registry, a runner and a sandbox, which
    is the whole rehearsal rather than a unit test. So this pins the three
    expressions that decide it - all absent from the pre-#907 text, so it reds
    on the bytes it replaces - and deliberately asserts nothing about the rest
    of that function.
    """
    import inspect

    import server.ci_runner._runs as runs_mod

    src = inspect.getsource(runs_mod.run_checks)
    assert "_farm_mod.tree_payload" in src, "the gate must build the union payload"
    assert "farm_files is not None" in src, (
        "the gate must gate on the payload decision, not on the tree name"
    )
    assert "tree=str(tree)" in src, (
        "a refusal must name the tree, or the skip row is unattributable"
    )


def test_a_corrupt_stored_delta_makes_the_tree_indeterminate():
    """The population the truncation defect actually lives in.

    _stored_deltas stops scanning at a corrupt blob and returns the PREFIX it
    managed to read. A prefix is indistinguishable from a short tree, so a
    warm tree whose second blob is unreadable would ship only the incoming
    delta: the runner resets to base, applies a SUBSET, and returns green -
    a receipt for a tree nobody tested. named_tree_deltas must return None
    (indeterminate) rather than [] ("genuinely empty"), and tree_payload must
    refuse.

    The earlier `refuses_rather_than_ships_a_partial_tree` arm covered only the
    COUNT and BYTE ceilings, never an unreadable store, which is why the
    truncation shipped past it. This is the census lesson: the arm you add
    passes on the case you just fixed, so the census is the thing nobody
    re-reads.
    """
    name = "corrupt-delta-tree"
    tree = trees_mod._named_dir(7, name)
    os.makedirs(os.path.join(tree, ".git"), exist_ok=True)
    trees_mod._write_manifest(
        tree, {"agent_id": 7, "base_sha": "a" * 40, "base_ref": "main"}
    )
    store = os.path.join(tree, ".ci-deltas")
    os.makedirs(store, exist_ok=True)
    incoming = [{"path": "incoming.py", "content": "two"}]
    try:
        with open(os.path.join(store, "0000.json"), "w", encoding="utf-8") as fh:
            json.dump([{"path": "first.py", "content": "one"}], fh)
        with open(os.path.join(store, "0001.json"), "w", encoding="utf-8") as fh:
            fh.write("{ this is not json")

        assert trees_mod.named_tree_deltas(7, name) is None, (
            "a store that cannot be read whole must read as None (indeterminate), "
            "never as a truncated list - [] means 'no deltas' and a prefix means "
            "'some deltas are unreadable', and only the second may refuse"
        )
        payload, reason = farm.tree_payload(7, name, incoming, "main")
        assert payload is None and reason == "ineligible_tree", (
            "tree_payload must refuse a tree whose stored deltas cannot be read "
            f"whole, got payload={payload!r} reason={reason!r}"
        )

        # POSITIVE CONTROL, directly under the absence arm: the very same tree
        # with a readable store must dispatch, in stored-then-incoming order.
        # Without this the arm above would also pass if tree_payload refused
        # everything.
        with open(os.path.join(store, "0001.json"), "w", encoding="utf-8") as fh:
            json.dump([{"path": "second.py", "content": "three"}], fh)
        stored = trees_mod.named_tree_deltas(7, name)
        assert stored is not None and len(stored) == 2, (
            f"a fully readable store must read as both blobs, got {stored!r}"
        )
        payload, reason = farm.tree_payload(7, name, incoming, "main")
        assert payload is not None and reason == "", (
            f"a readable tree must still dispatch, got {reason!r}"
        )
        paths = [f["path"] for f in payload]
        assert paths == ["first.py", "second.py", "incoming.py"], (
            f"stored blobs then the incoming delta, in that order, got {paths}"
        )
    finally:
        trees_mod._retire_dir(tree)


def test_a_tree_with_no_store_at_all_is_empty_not_indeterminate():
    """The other half of the sentinel: absent must stay [].

    Pinning only the None arm would let a future edit collapse the two and
    make every cold tree look indeterminate - which would silently push all
    cold-tree rehearsals back to local.
    """
    name = "no-store-tree"
    tree = trees_mod._named_dir(7, name)
    os.makedirs(os.path.join(tree, ".git"), exist_ok=True)
    trees_mod._write_manifest(
        tree, {"agent_id": 7, "base_sha": "a" * 40, "base_ref": "main"}
    )
    try:
        assert trees_mod.named_tree_deltas(7, name) == [], (
            "a tree whose .ci-deltas directory does not exist holds no deltas; "
            "that is [] (empty), not None (indeterminate)"
        )
        payload, reason = farm.tree_payload(
            7, name, [{"path": "only.py", "content": "x"}], "main"
        )
        assert payload is not None and reason == "", (
            f"a cold tree must dispatch on its incoming delta alone, got {reason!r}"
        )
        assert [f["path"] for f in payload] == ["only.py"]
    finally:
        trees_mod._retire_dir(tree)


def test_the_tree_payload_and_its_skip_are_gated_on_farm_eligibility():
    """Declared SOURCE-SHAPE pin, because driving run_checks end-to-end is not
    cheap: it acquires slots and can execute a suite. Everything it asserts is
    a decision run_checks itself makes before touching the farm.

    The block that built the tree payload and logged `ineligible_tree` used to
    sit ABOVE the farm gate. With CI_FARM_ENABLED at its default of 0 that
    wrote a public ci_farm_skipped row on every named-tree run - asserting the
    farm declined a dispatch it was never offered, which is the precise
    contract SKIP_REASONS' own comment forbids ("never a shape the gate refused
    by design"). It also paid the whole payload read to produce that row.

    Two arms, because the ordering alone is not the property: the gate must be
    evaluated first AND the tree block must be conditioned on it.
    """
    import inspect

    import server.ci_runner._runs as runs_mod

    src = inspect.getsource(runs_mod.run_checks)
    gate_at = src.index("farm_eligible = (")
    tree_at = src.index("_farm_mod.tree_payload(")
    assert gate_at < tree_at, (
        "eligibility must be decided before the payload is built; with the "
        "farm off, an ungated build writes a skip row claiming a farm "
        "interaction that never happened"
    )
    assert "if farm_eligible and tree is not None:" in src, (
        "the tree payload block must be gated on farm_eligible, not on `tree` "
        "alone - `tree is not None` is true for every named rehearsal"
    )
    assert "if farm_eligible and (tree is None or farm_files is not None):" in src, (
        "the dispatch guard must carry the same predicate, or a refused tree "
        "still reaches try_dispatch"
    )


def test_a_runner_without_a_url_releases_the_slot_pick_runner_took():
    """Latent-leak guard. pick_runner reserves the active-run slot BEFORE
    dispatch, and dispatch_to_runner's rid/url guard used to return before the
    try/finally that releases it - so a runner dict that got past registration
    with no url would sit at its cap until remove_runner cleared it, the one
    failure in this module with no self-clearing path.

    Unreachable through register_runner (it refuses an empty or non-http url),
    which is exactly why it needs a pin: the state cannot be produced by the
    public surface, so nothing else would notice the guard going away.
    """
    released: list = []
    runner = {"id": 7, "url": "", "name": "no-url"}
    with mock.patch.object(farm, "_release", side_effect=released.append):
        assert (
            farm.dispatch_to_runner(runner, {"checks": "tests", "mode": "main"}) is None
        )
    assert released == [7], (
        f"the slot pick_runner reserved must be released on this path, got {released}"
    )
    assert runner["_dispatch_reason"] == farm.RUNNER_UNCONFIGURED, (
        "a runner with no url was never sent a request, so it is refused - "
        "not may_have_executed - and it is NOT a capacity event, so it must "
        "not borrow the value both lanes file as a 'busy' skip"
    )


def test_a_refusal_that_is_not_a_capacity_event_keeps_its_own_label():
    """A picked-but-unusable runner must not be filed as a capacity skip.

    Both lanes sent DISPATCH_REJECTED to a "busy" skip, because a 4xx really
    is the farm being full. Two refusals are not 4xx - the row is not routable,
    and we could not encode the payload - and they used to share that value, so
    the ledger would have reported a saturated farm for a farm that was never
    saturated. That is a false all-clear about capacity, which is the one
    direction this whole module exists to make legible (finding #118).

    The census arm is load-bearing, and it is a proof rather than a proxy: the
    routing passes `skip_reason=why`, and log_skip coerces only what is NOT in
    SKIP_REASONS - so every routing value being a member is exactly the
    condition under which the ledger records the reason verbatim.
    """
    assert farm._REJECTED_NOT_A_CAPACITY_EVENT, "the routing tuple is empty"
    for value in farm._REJECTED_NOT_A_CAPACITY_EVENT:
        assert value in farm.SKIP_REASONS, (
            f"{value!r} would be coerced to 'unhealthy' on the way to the "
            f"ledger; SKIP_REASONS is {list(farm.SKIP_REASONS)}"
        )
    # Control: the value that DOES mean capacity keeps the capacity routing.
    assert farm.DISPATCH_REJECTED not in farm._REJECTED_NOT_A_CAPACITY_EVENT

    # Drive the real guard, so the census cannot pass while the arm still
    # stamps the shared value.
    runner = {"id": 11, "url": "", "name": "no-url"}
    with mock.patch.object(farm, "_release"):
        assert farm.dispatch_to_runner(runner, {"checks": "tests"}) is None
    why = str(runner.get("_dispatch_reason") or "")
    assert why == farm.RUNNER_UNCONFIGURED, f"guard stamped {why!r}"

    # The sibling refusal: same shape, same requirement, reached by handing
    # json.dumps something it genuinely cannot encode.
    unserialisable = {"id": 12, "url": "http://r/", "name": "r"}
    with mock.patch.object(farm, "_release"):
        assert farm.dispatch_to_runner(unserialisable, {"checks": object()}) is None
    stamped = str(unserialisable.get("_dispatch_reason") or "")
    assert stamped == farm.PAYLOAD_UNSERIALISABLE, f"serialisation stamped {stamped!r}"


def main() -> None:
    test_a_refusal_that_is_not_a_capacity_event_keeps_its_own_label()
    test_classify_no_pick_tells_five_populations_apart()
    test_the_tree_payload_and_its_skip_are_gated_on_farm_eligibility()
    test_a_runner_without_a_url_releases_the_slot_pick_runner_took()
    test_tree_payload_ships_the_union_not_just_the_newest_delta()
    test_a_corrupt_stored_delta_makes_the_tree_indeterminate()
    test_a_tree_with_no_store_at_all_is_empty_not_indeterminate()
    test_tree_payload_refuses_rather_than_ships_a_partial_tree()
    test_named_tree_deltas_is_read_only_and_absent_safe()
    test_the_test_lane_consults_the_payload_decision()
    test_skip_reason_vocabulary_is_closed()
    test_probe_runners_is_read_only()
    test_dispatch_to_runner_splits_rejected_from_may_have_run()
    test_try_dispatch_409_is_a_skip_and_not_a_failure()
    test_try_dispatch_no_runner_logs_a_skip()
    print("test_ci_farm_observability: all ok")


if __name__ == "__main__":
    main()
