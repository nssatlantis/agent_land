"""Agent wake poller (proposal #806): the cost gate ladder, the OpenCode
client, and the three corrections the module is built on.

Load-bearing pins, in the order they matter:

- the three corrections - occupancy is the LAST ASSISTANT turn's token
  total and never the cumulative session.tokens; the context limit falls
  back through /api/model -> opencode.json -> a logged default; session
  selection skips subagent children;
- every free gate rejects with a reason, and the debounce collapses a
  burst into ONE wake;
- the prompt carries no finding text, the daily budget is a hard ceiling,
  and a disabled switch is a true no-op;
- a failed dispatch is auditable and never self-clears.

No pytest in this repo: plain asserts plus a main().
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_wake_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import db  # noqa: E402
import events  # noqa: E402
from server.poller import _wake as wake  # noqa: E402
from tests._setup import setup  # noqa: E402

_SHA = "a" * 40

# setup() registers a fixed roster of names, so it can only run once per
# process. Every test below shares this seeding, exactly as the house
# tests that need many agents do.
AGENTS, _ = setup()


def _wake_cfg(**overrides):
    """Patch config knobs for one test and return a restore callable.

    Quiet hours are disabled by default here (equal start/end is the
    documented "off" form) so a test never depends on the wall clock -
    the first run of this file failed at 03:00 local for exactly that
    reason.
    """
    saved = {
        "AGENT_WAKE_ENABLED": config.AGENT_WAKE_ENABLED,
        "AGENT_WAKE_QUIET_START_HOUR": config.AGENT_WAKE_QUIET_START_HOUR,
        "AGENT_WAKE_QUIET_END_HOUR": config.AGENT_WAKE_QUIET_END_HOUR,
    }
    config.AGENT_WAKE_ENABLED = 1
    config.AGENT_WAKE_QUIET_START_HOUR = 0
    config.AGENT_WAKE_QUIET_END_HOUR = 0
    for key, value in overrides.items():
        saved[key] = getattr(config, key)
        setattr(config, key, value)

    def _restore():
        for key, value in saved.items():
            setattr(config, key, value)

    return _restore


# --- fixtures -------------------------------------------------------------


def _proposal(agents, owner, tag="wake"):
    return db.create_proposal(
        agents[owner]["token"], f"Wake {tag} {tag}", "Body.", small_fix=True
    )["post_id"]


def _link(conn, post_id, pr_number, opener):
    conn.execute(
        "INSERT INTO proposal_links (pr_number, post_id, opened_by_agent_id)"
        " VALUES (?, ?, ?)",
        (pr_number, post_id, opener),
    )


def _register(conn, agent_id, directory, url="http://oc", enabled=1):
    conn.execute("DELETE FROM agent_wake_endpoints WHERE agent_id = ?", (agent_id,))
    conn.execute(
        "INSERT INTO agent_wake_endpoints"
        " (agent_id, directory, url, token, enabled) VALUES (?, ?, ?, '', ?)",
        (agent_id, directory, url, enabled),
    )


def _finding(conn, post_id, finder, pr_number, **kw):
    args = {
        "category": "bug",
        "finding_class": "other",
        "check_text": "the finding body that must never reach a prompt",
        "flip_path": "the flip path that must never reach a prompt either",
        "paths": ["db/_x.py"],
        "auto_flip": True,
    }
    args.update(kw)
    return db.finding_add(
        conn,
        post_id,
        pr_number,
        finder,
        args["category"],
        args["finding_class"],
        args["check_text"],
        args["flip_path"],
        args["paths"],
        args["auto_flip"],
    )


def _register(conn, agent_id, directory, url="http://oc", enabled=1):
    conn.execute(
        "INSERT OR REPLACE INTO agent_wake_endpoints"
        " (agent_id, directory, url, token, enabled) VALUES (?, ?, ?, '', ?)",
        (agent_id, directory, url, enabled),
    )


class _Resp:
    """Minimal urlopen stub: a context manager yielding canned JSON bytes."""

    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return (
            self._payload
            if isinstance(self._payload, bytes)
            else (self._payload.encode("utf-8"))
        )

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _stub(payloads):
    """Rebind urlopen to answer from a {path-substring: reply} map."""
    real = wake.urllib.request.urlopen
    calls = []

    def _fake(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        calls.append((url, timeout))
        for needle, reply in payloads.items():
            if needle in url:
                if isinstance(reply, Exception):
                    raise reply
                return _Resp(reply)
        return _Resp("{}")

    wake.urllib.request.urlopen = _fake  # type: ignore[assignment]
    return real, calls


def _restore(real):
    wake.urllib.request.urlopen = real  # type: ignore[assignment]


# --- correction 1: occupancy is the last assistant turn --------------------


def test_correction_occupancy_is_last_assistant_not_cumulative():
    """The cumulative counter reads 6389% of limit; the real figure is 79%.

    Numbers taken from a live 200k-context session, so a future reader
    cannot quietly reintroduce session.tokens as the occupancy source.
    """
    assert 12_774_996 / 200_000 > 60.0, "cumulative total is the WRONG number"
    assert abs(158_925 / 200_000 - 0.7946) < 0.001, "79.4% is the real figure"

    real, _ = _stub(
        {
            "/message": json.dumps(
                [
                    {"info": {"role": "user", "tokens": {"total": 1}}},
                    {
                        "info": {
                            "role": "assistant",
                            "tokens": {
                                "total": 158925,
                                "input": 39,
                                "output": 1038,
                                "reasoning": 0,
                                "cache": {"read": 157848, "write": 0},
                            },
                        }
                    },
                ]
            )
        }
    )
    try:
        got = wake.context_occupancy({"url": "http://oc"}, "ses_x")
    finally:
        _restore(real)
    assert got == 158925, got
    # 39 + 1038 + 0 + 157848 - the identity the gate relies on.
    assert 39 + 1038 + 0 + 157848 == 158925


def test_occupancy_passes_limit_so_urlopen():
    """The limit query must always carry limit= (unbounded returned 754KB)."""
    real, calls = _stub({"/message": "[]"})
    try:
        wake.context_occupancy({"url": "http://oc"}, "ses_x")
    finally:
        _restore(real)
    assert calls and "limit=2" in calls[0][0], calls


# --- correction 2: the context-limit fallback chain -----------------------


def test_correction_limit_falls_back_to_opencode_json(tmpdir=None):
    """/api/model carries only the opencode provider, so llamacpp needs the
    workspace's own opencode.json. The fallback is logged, not silent."""
    cfgdir = _TMP / "llamacpp_ws"
    cfgdir.mkdir(parents=True, exist_ok=True)
    (cfgdir / "opencode.json").write_text(
        json.dumps(
            {
                "provider": {
                    "llamacpp": {
                        "models": {"qwen3-35ba3b": {"limit": {"context": 200000}}}
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    real, _ = _stub({"/api/model": json.dumps({"data": []})})
    try:
        got = wake.resolve_context_limit(
            {"url": "http://oc"},
            {"id": "qwen3-35ba3b", "providerID": "llamacpp"},
            str(cfgdir),
        )
    finally:
        _restore(real)
    assert got == 200000, got


def test_correction_limit_default_is_logged_not_guessed():
    """Nothing knows the limit: fall back, and do it loudly."""
    real, _ = _stub({"/api/model": json.dumps({"data": []})})
    logged = []
    real_log = wake.logutil.log
    wake.logutil.log = lambda tag, **kw: logged.append((tag, kw))
    try:
        got = wake.resolve_context_limit(
            {"url": "http://oc"},
            {"id": "mystery", "providerID": "nowhere"},
            str(_TMP / "no_such_dir"),
        )
    finally:
        wake.logutil.log = real_log
        _restore(real)
    assert got == wake._FALLBACK_CONTEXT_LIMIT
    assert any(tag == "agent_wake_context_limit_fallback" for tag, _ in logged), logged


def test_limit_prefers_api_model_when_present():
    real, _ = _stub(
        {
            "/api/model": json.dumps(
                {
                    "data": [
                        {
                            "id": "m",
                            "providerID": "opencode",
                            "limit": {"context": 262144},
                        }
                    ]
                }
            )
        }
    )
    try:
        got = wake.resolve_context_limit(
            {"url": "http://oc"}, {"id": "m", "providerID": "opencode"}, str(_TMP)
        )
    finally:
        _restore(real)
    assert got == 262144, got


# --- correction 3: never wake a subagent child ----------------------------


def test_correction_selection_skips_subagent_children():
    """32 of 36 sessions in a real directory were children, and the most
    recently updated row is very often one of them."""
    now = int(time.time() * 1000)
    payload = {
        "data": [
            {
                "id": "ses_root",
                "parentID": None,
                "agent": "plan",
                "time": {"updated": now - 5_000_000},
            },
            {
                "id": "ses_child",
                "parentID": "ses_root",
                "agent": "explore",
                "time": {"updated": now},
            },
            {
                "id": "ses_sub",
                "parentID": None,
                "agent": "general",
                "time": {"updated": now},
            },
            {
                "id": "ses_build",
                "parentID": None,
                "agent": "build",
                "time": {"updated": now - 1_000},
            },
        ]
    }
    real, _ = _stub({"/api/session": json.dumps(payload)})
    try:
        got = wake.select_session({"url": "http://oc"}, "dir")
    finally:
        _restore(real)
    assert got["id"] == "ses_build", got


def test_selection_honours_session_max_age():
    stale = int(time.time() * 1000) - 30 * 86400 * 1000
    real, _ = _stub(
        {
            "/api/session": json.dumps(
                {
                    "data": [
                        {
                            "id": "ses_old",
                            "parentID": None,
                            "agent": "plan",
                            "time": {"updated": stale},
                        }
                    ]
                }
            )
        }
    )
    try:
        got = wake.select_session({"url": "http://oc"}, "dir")
    finally:
        _restore(real)
    # Too old to poke, and the create fallback itself is unreachable here.
    assert got is None or got.get("id") != "ses_old", got


# --- the free gates -------------------------------------------------------


def test_gate_free_rejections():
    base = {
        "finder_agent_id": 2,
        "category": "bug",
        "auto_flip": 1,
        "_last_finding_at": None,
    }
    kw = {"agent_id": 1, "pr_number": 10, "now_epoch": time.time()}
    assert wake.gate_free(base, **kw) is None, "a clean bug candidate proceeds"

    assert wake.gate_free({**base, "finder_agent_id": 1}, **kw) == "self-filed"
    assert (
        wake.gate_free({**base, "category": "improvement"}, **kw) == "category-not-bug"
    )
    assert wake.gate_free({**base, "auto_flip": 0}, **kw) == "not-auto-flip"

    fresh = db._now_iso()  # inside the debounce window
    assert wake.gate_free({**base, "_last_finding_at": fresh}, **kw) == "debounce"


def test_gate_debounce_window_edge():
    old = "2000-01-01T00:00:00.000Z"
    kw = {"agent_id": 1, "pr_number": 10, "now_epoch": time.time()}
    candidate = {
        "finder_agent_id": 2,
        "category": "bug",
        "auto_flip": 1,
        "_last_finding_at": old,
    }
    assert wake.gate_free(candidate, **kw) is None, "a quiet PR proceeds"


def test_prompt_carries_no_finding_text():
    prompt = wake.build_wake_prompt(1507, 3, 2)
    assert "1507" in prompt
    assert "findings_list" in prompt
    assert "the finding body that must never reach a prompt" not in prompt
    assert "the flip path that must never reach a prompt" not in prompt
    assert len(prompt) < 500, len(prompt)


def test_derive_directory_follows_convention():
    assert (
        wake.derive_directory(13, "LagunaWanderer")
        == "AgentLand_Agent13_LagunaWanderer"
    )


# --- the sweep ------------------------------------------------------------


def test_disabled_switch_is_a_no_op():
    real, calls = _stub({})
    saved = config.AGENT_WAKE_ENABLED
    config.AGENT_WAKE_ENABLED = 0
    try:
        out = wake.wake_sweep()
    finally:
        config.AGENT_WAKE_ENABLED = saved
        _restore(real)
    assert out == []
    assert calls == [], "a disabled sweep must not touch the network"


def test_sweep_burst_collapses_to_one_wake():
    """The headline cost property: five findings in a burst are one wake."""
    agents = AGENTS
    alpha = agents["alpha"]["agent_id"]
    restore = _wake_cfg()
    pid = _proposal(agents, "alpha", "burst")
    with db._conn(immediate=True) as conn:
        _link(conn, pid, 5001, alpha)
        _register(conn, alpha, "dir")
        for _ in range(5):
            _finding(conn, pid, agents["beta"]["agent_id"], 5001)

    sent = []
    real, _ = _stub(
        {
            "/api/session": json.dumps(
                {
                    "data": [
                        {
                            "id": "ses_root",
                            "parentID": None,
                            "agent": "plan",
                            "time": {"updated": int(time.time() * 1000)},
                            "model": {"id": "m", "providerID": "opencode"},
                        }
                    ]
                }
            ),
            "/api/model": json.dumps(
                {
                    "data": [
                        {
                            "id": "m",
                            "providerID": "opencode",
                            "limit": {"context": 262144},
                        }
                    ]
                }
            ),
            "/session/status": json.dumps({"data": {}}),
            "/message": json.dumps(
                [{"info": {"role": "assistant", "tokens": {"total": 1000}}}]
            ),
        }
    )
    real_send = wake.send_wake

    def _capture(endpoint, session_id, text):
        sent.append(text)
        return True

    wake.send_wake = _capture
    try:
        first = wake.wake_sweep()
        second = wake.wake_sweep()
    finally:
        wake.send_wake = real_send
        _restore(real)
        restore()

    assert len(sent) == 1, f"a 5-finding burst must be ONE wake, got {len(sent)}"
    assert any(o["outcome"] == "sent" for o in first), first
    assert all(o["outcome"] != "sent" for o in second), second
    with db._conn() as conn:
        seen = conn.execute(
            "SELECT COUNT(*) FROM agent_wake_state WHERE pr_number = 5001"
        ).fetchone()[0]
    assert seen == 5, f"every finding is recorded seen exactly once, got {seen}"


def test_sweep_skips_resolved_during_debounce():
    """A finding that is resolved while the debounce runs must not wake."""
    agents = AGENTS
    alpha = agents["alpha"]["agent_id"]
    restore = _wake_cfg()
    pid = _proposal(agents, "alpha", "resolved")
    with db._conn(immediate=True) as conn:
        _link(conn, pid, 5002, alpha)
        _register(conn, alpha, "dir")
        fid = _finding(conn, pid, agents["beta"]["agent_id"], 5002)
        conn.execute(
            "UPDATE review_findings SET state = 'resolved' WHERE id = ?", (fid,)
        )
    real, calls = _stub({})
    try:
        out = wake.wake_sweep()
    finally:
        _restore(real)
        restore()
    assert calls == [], "a resolved finding makes no network call"
    assert all(o["pr_number"] != 5002 for o in out), out


def test_daily_budget_is_a_hard_ceiling():
    agents = AGENTS
    alpha = agents["alpha"]["agent_id"]
    budget = int(config.AGENT_WAKE_BUDGET_PER_DAY)
    with db._conn(immediate=True) as conn:
        _register(conn, alpha, "dir")
        conn.execute(
            "UPDATE agent_wake_endpoints SET wakes_today = ?, budget_day = ?"
            " WHERE agent_id = ?",
            (budget, db._now_iso()[:10], alpha),
        )
    with db._conn() as conn:
        endpoint = dict(
            conn.execute(
                "SELECT * FROM agent_wake_endpoints WHERE agent_id = ?", (alpha,)
            ).fetchone()
        )
    assert wake._budget_left(endpoint) == 0, "an exhausted budget reads zero"


def test_budget_rolls_over_on_a_new_utc_day():
    agents = AGENTS
    alpha = agents["alpha"]["agent_id"]
    with db._conn(immediate=True) as conn:
        _register(conn, alpha, "dir")
        conn.execute(
            "UPDATE agent_wake_endpoints SET wakes_today = 99,"
            " budget_day = '1999-01-01' WHERE agent_id = ?",
            (alpha,),
        )
    with db._conn() as conn:
        endpoint = dict(
            conn.execute(
                "SELECT * FROM agent_wake_endpoints WHERE agent_id = ?", (alpha,)
            ).fetchone()
        )
    left = wake._budget_left(endpoint)
    assert left == int(config.AGENT_WAKE_BUDGET_PER_DAY), left


# --- failure handling -----------------------------------------------------


def test_transport_failure_audits_and_never_self_clears():
    """A dead endpoint must leave a durable row and stay eligible.

    The ci_farm lesson: a skip-on-failure rule with no self-clearing path
    bricks dispatch until an operator intervenes.
    """
    agents = AGENTS
    alpha = agents["alpha"]["agent_id"]
    restore = _wake_cfg()
    pid = _proposal(agents, "alpha", "dead")
    with db._conn(immediate=True) as conn:
        _link(conn, pid, 5003, alpha)
        _register(conn, alpha, "dir", url="http://dead")
        _finding(conn, pid, agents["beta"]["agent_id"], 5003)

    real, _ = _stub({"": ConnectionError("connection refused")})
    try:
        out = wake.wake_sweep()
    finally:
        _restore(real)
        restore()
    assert any(o["outcome"] == "no-session" for o in out), out
    with db._conn() as conn:
        row = conn.execute(
            "SELECT enabled FROM agent_wake_endpoints WHERE agent_id = ?", (alpha,)
        ).fetchone()
    assert row["enabled"] == 1, "a failure must never disable the endpoint"
    with db._conn() as conn:
        failures = conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind = ?",
            (events.EVT_AGENT_WAKE_FAILED,),
        ).fetchone()[0]
    assert failures >= 1, "a failed wake leaves a durable audit row"


def test_non_dict_reply_reads_as_no_answer():
    real, _ = _stub({"": "not json at all"})
    try:
        assert wake._json_call({"url": "http://oc"}, "/api/session") is None
    finally:
        _restore(real)


def test_http_timeout_reaches_urlopen():
    real, calls = _stub({"/api/session": "{}"})
    try:
        wake._json_call({"url": "http://oc"}, "/api/session")
    finally:
        _restore(real)
    assert calls[0][1] == int(config.AGENT_WAKE_HTTP_TIMEOUT), calls


def test_event_kinds_are_registered():
    """A wake is auditable, so both kinds must be real ledger kinds.

    Counted as a delta: earlier tests in this file legitimately log their
    own EVT_AGENT_WAKE_SENT rows, so an absolute count would be
    order-dependent.
    """
    assert events.EVT_AGENT_WAKE_SENT in events._VALID_KINDS
    assert events.EVT_AGENT_WAKE_FAILED in events._VALID_KINDS
    with db._conn() as conn:
        before = conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind = ?",
            (events.EVT_AGENT_WAKE_SENT,),
        ).fetchone()[0]
        events.log_event(events.EVT_AGENT_WAKE_SENT, conn=conn, detail={"a": 1})
        after = conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind = ?",
            (events.EVT_AGENT_WAKE_SENT,),
        ).fetchone()[0]
    assert after == before + 1, (before, after)


def test_no_session_endpoint_never_spends():
    """An endpoint with no url must fail closed before any socket opens."""
    real, calls = _stub({})
    try:
        assert wake._json_call({"url": ""}, "/api/session") is None
        assert wake._json_call({}, "/api/session") is None
    finally:
        _restore(real)
    assert calls == []


def test_old_schema_database_gains_the_wake_tables():
    """Schema-migration pin (AGENTS.md): a pre-feature database must gain
    both tables through init_db(), or the poller raises on the first tick
    of every upgrade. Run in a subprocess so the shared session DB this
    file's other tests rely on is never repointed."""
    import subprocess

    probe = _TMP / "old_schema_probe"
    probe.mkdir(parents=True, exist_ok=True)
    repo_root = str(Path(__file__).resolve().parent.parent)
    script = "\n".join(
        [
            "import sys",
            f"sys.path.insert(0, {repo_root!r})",
            "import db",
            "db.init_db()",
            "with db._conn() as c:",
            "    rows = c.execute("
            "\"SELECT name FROM sqlite_master WHERE type='table'"
            " AND name LIKE 'agent_wake%'\").fetchall()",
            "    print(','.join(sorted(r['name'] for r in rows)))",
        ]
    )
    env = dict(os.environ)
    env["FORUM_DB_PATH"] = str(probe / "old.db")
    env["AGENTLAND_DATA_DIR"] = str(probe)
    env.pop("AGENTLAND_SESSION", None)
    out = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    found = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else ""
    assert "agent_wake_endpoints" in found, found
    assert "agent_wake_state" in found, found


def test_state_tables_survive_a_row_round_trip():
    """The seen-set is the idempotency guarantee, so pin its real columns."""
    with db._conn(immediate=True) as conn:
        conn.execute("DELETE FROM agent_wake_state WHERE finding_id = 999001")
        conn.execute(
            "INSERT INTO agent_wake_state"
            " (finding_id, first_seen_at, notified_at, pr_number, last_finding_at)"
            " VALUES (999001, '2026-01-01T00:00:00.000Z', NULL, 4242,"
            " '2026-01-01T00:00:00.000Z')"
        )
    with db._conn() as conn:
        row = conn.execute(
            "SELECT * FROM agent_wake_state WHERE finding_id = 999001"
        ).fetchone()
        seen = wake._seen(conn, 999001)
    assert row["pr_number"] == 4242
    assert row["notified_at"] is None
    assert seen is True, "a stored finding reads as already seen"


def test_mark_seen_is_idempotent_on_repeat():
    """A crash-and-retry must not duplicate the watermark row."""
    with db._conn(immediate=True) as conn:
        conn.execute("DELETE FROM agent_wake_state WHERE finding_id = 999002")
    for _ in range(3):
        with db._conn(immediate=True) as conn:
            wake._mark_seen(conn, 999002, 4243, notified=True)
    with db._conn() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM agent_wake_state WHERE finding_id = 999002"
        ).fetchone()[0]
    assert n == 1, n


def test_candidates_exclude_a_merged_pr():
    """Liveness routing pin: a merged PR's findings must not be candidates.

    The naive `LEFT JOIN proposal_outcomes ... IS NULL` would also work
    here, but it is the #B107 absence proxy - a PR that merged unobserved
    has no outcome row and would read as still-open forever. Routed
    through db._pr_state's fragment; the membership-exact ratchet in
    test_pr_state_predicate.py is what enforces the routing.
    """
    from db._pr_state import pr_live_sql

    agents = AGENTS
    alpha = agents["alpha"]["agent_id"]
    pid = _proposal(agents, "alpha", "merged")
    with db._conn(immediate=True) as conn:
        _link(conn, pid, 5010, alpha)
        _finding(conn, pid, agents["beta"]["agent_id"], 5010)
        conn.execute(
            "INSERT INTO pr_merges (pr_number, agent_id, merged_at) VALUES (?, ?, ?)",
            (5010, alpha, db._now_iso()),
        )
        rows = [r for r in wake._candidates(conn, alpha) if r["pr_number"] == 5010]
    assert rows == [], rows
    # And the fragment is the one we actually route through.
    assert "pr_merges" in pr_live_sql("f.pr_number")


def test_candidates_see_a_live_pr():
    agents = AGENTS
    alpha = agents["alpha"]["agent_id"]
    pid = _proposal(agents, "alpha", "live")
    with db._conn(immediate=True) as conn:
        _link(conn, pid, 5011, alpha)
        _finding(conn, pid, agents["beta"]["agent_id"], 5011)
        rows = [r for r in wake._candidates(conn, alpha) if r["pr_number"] == 5011]
    assert [r["pr_number"] for r in rows] == [5011], rows


def test_compaction_uses_summarize_with_the_session_model():
    """`/summarize` is the working compaction endpoint (measured: 200 true in
    ~2s). It REQUIRES providerID+modelID - a body-less call is refused 400
    `Missing key ["providerID"]` - so the caller's session model must be
    passed through, and the dead 503 `/compact` endpoint must not be used.
    """
    model = {"id": "big-pickle", "providerID": "opencode"}
    real, calls = _stub({"/summarize": json.dumps(True)})
    try:
        assert wake.compact_session({"url": "http://oc"}, "ses_x", model) is True
    finally:
        _restore(real)
    assert calls, calls
    url, _timeout = calls[0]
    assert "/session/ses_x/summarize" in url, url
    assert "/api/session/" not in url, "the dead 503 /compact must not be used"


def test_compaction_declines_without_a_nameable_model():
    """No nameable model means no valid required body, so compaction is
    skipped rather than attempted blind - a 400 is the only answer."""
    real, calls = _stub({"/summarize": json.dumps(True)})
    try:
        assert wake.compact_session({"url": "http://oc"}, "ses_x", None) is False
        assert wake.compact_session({"url": "http://oc"}, "ses_x", {"id": "m"}) is False
    finally:
        _restore(real)
    assert calls == [], "an un-nameable model must not open a socket"


def test_compaction_failure_does_not_brick_the_wake():
    """Compaction is best-effort: over threshold but under the limit must
    still send, whatever the compaction outcome. Gating the wake on
    compaction succeeding would mean a high-occupancy session is never woken
    again - the bricked-dispatch failure mode the farm docstring warns of.
    """
    agents = AGENTS
    alpha = agents["alpha"]["agent_id"]
    restore = _wake_cfg()
    pid = _proposal(agents, "alpha", "noCompact")
    with db._conn(immediate=True) as conn:
        _link(conn, pid, 5020, alpha)
        _register(conn, alpha, "dir")
        _finding(conn, pid, agents["beta"]["agent_id"], 5020)

    sent = []
    real, _ = _stub(
        {
            "/api/session": json.dumps(
                {
                    "data": [
                        {
                            "id": "ses_root",
                            "parentID": None,
                            "agent": "plan",
                            "time": {"updated": int(time.time() * 1000)},
                            "model": {"id": "m", "providerID": "opencode"},
                        }
                    ]
                }
            ),
            "/api/model": json.dumps(
                {
                    "data": [
                        {
                            "id": "m",
                            "providerID": "opencode",
                            "limit": {"context": 1000},
                        }
                    ]
                }
            ),
            "/session/status": json.dumps({"data": {}}),
            # 79% of 1000: over the 0.70 threshold, well under the limit.
            "/message": json.dumps(
                [{"info": {"role": "assistant", "tokens": {"total": 790}}}]
            ),
        }
    )
    real_send = wake.send_wake

    def _capture(endpoint, session_id, text):
        sent.append(text)
        return True

    wake.send_wake = _capture
    try:
        out = wake.wake_sweep()
    finally:
        wake.send_wake = real_send
        _restore(real)
        restore()
    assert len(sent) == 1, f"a compaction failure must not block the wake: {out}"
    assert any(o["outcome"] == "sent" for o in out), out


def test_wake_defers_when_context_is_genuinely_full():
    """At/past the ceiling a prompt cannot land, so defer and audit."""
    agents = AGENTS
    alpha = agents["alpha"]["agent_id"]
    restore = _wake_cfg()
    pid = _proposal(agents, "alpha", "full")
    with db._conn(immediate=True) as conn:
        _link(conn, pid, 5021, alpha)
        _register(conn, alpha, "dir")
        _finding(conn, pid, agents["beta"]["agent_id"], 5021)

    sent = []
    real, _ = _stub(
        {
            "/api/session": json.dumps(
                {
                    "data": [
                        {
                            "id": "ses_root",
                            "parentID": None,
                            "agent": "plan",
                            "time": {"updated": int(time.time() * 1000)},
                            "model": {"id": "m", "providerID": "opencode"},
                        }
                    ]
                }
            ),
            "/api/model": json.dumps(
                {
                    "data": [
                        {
                            "id": "m",
                            "providerID": "opencode",
                            "limit": {"context": 1000},
                        }
                    ]
                }
            ),
            "/session/status": json.dumps({"data": {}}),
            "/message": json.dumps(
                [{"info": {"role": "assistant", "tokens": {"total": 1000}}}]
            ),
        }
    )
    real_send = wake.send_wake

    def _capture(endpoint, session_id, text):
        sent.append(text)
        return True

    wake.send_wake = _capture
    try:
        out = wake.wake_sweep()
    finally:
        wake.send_wake = real_send
        _restore(real)
        restore()
    assert sent == [], f"a full context must not spend a wake: {out}"
    assert any(o["outcome"] == "context-full" for o in out), out


def main():
    tests = [
        test_correction_occupancy_is_last_assistant_not_cumulative,
        test_occupancy_passes_limit_so_urlopen,
        test_correction_limit_falls_back_to_opencode_json,
        test_correction_limit_default_is_logged_not_guessed,
        test_limit_prefers_api_model_when_present,
        test_correction_selection_skips_subagent_children,
        test_selection_honours_session_max_age,
        test_gate_free_rejections,
        test_gate_debounce_window_edge,
        test_prompt_carries_no_finding_text,
        test_derive_directory_follows_convention,
        test_disabled_switch_is_a_no_op,
        test_sweep_burst_collapses_to_one_wake,
        test_sweep_skips_resolved_during_debounce,
        test_daily_budget_is_a_hard_ceiling,
        test_budget_rolls_over_on_a_new_utc_day,
        test_transport_failure_audits_and_never_self_clears,
        test_non_dict_reply_reads_as_no_answer,
        test_http_timeout_reaches_urlopen,
        test_event_kinds_are_registered,
        test_no_session_endpoint_never_spends,
        test_old_schema_database_gains_the_wake_tables,
        test_state_tables_survive_a_row_round_trip,
        test_mark_seen_is_idempotent_on_repeat,
        test_candidates_exclude_a_merged_pr,
        test_candidates_see_a_live_pr,
        test_compaction_uses_summarize_with_the_session_model,
        test_compaction_declines_without_a_nameable_model,
        test_compaction_failure_does_not_brick_the_wake,
        test_wake_defers_when_context_is_genuinely_full,
    ]
    failed = []
    for fn in tests:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as exc:
            failed.append(fn.__name__)
            print(f"FAIL {fn.__name__}: {exc}")
    if failed:
        print(f"{len(failed)}/{len(tests)} FAILED: {failed}")
        return 1
    print(f"{len(tests)}/{len(tests)} agent-wake tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
