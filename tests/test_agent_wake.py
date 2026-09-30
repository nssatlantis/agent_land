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
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
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


def _force_quiet() -> None:
    """Make `_quiet_hours()` return True whatever the wall clock says.

    The three quiet-hours tests used to hard-code the window 23:00-08:00 and
    assume "now" is inside it. `_quiet_hours` reads `datetime.now().hour` -
    LOCAL time, not UTC - so those tests passed only while the machine's
    local clock happened to sit in the quiet window, and went red at 08:00
    local for no reason at all. That is the same class of bug as a test that
    depends on the day of the week: the property under test is a boolean
    function of the hour, so the hour is a fixture, not a constant.

    The window is derived from the current hour and is therefore quiet for
    exactly one reason: the configured window excludes "now". Two shapes,
    because the two branches of `_quiet_hours` invert:

      - `start == 0, end == now`  (now > 0) takes the start<end branch, where
        quiet means NOT inside [start, end) - and `now` is not < now.
      - at midnight (`now == 0`) start==end would mean "quiet off", so a
        window strictly ahead of now is used instead.
    """
    now = datetime.now().hour
    if now == 0:
        config.AGENT_WAKE_QUIET_START_HOUR = 1
        config.AGENT_WAKE_QUIET_END_HOUR = 2
    else:
        config.AGENT_WAKE_QUIET_START_HOUR = 0
        config.AGENT_WAKE_QUIET_END_HOUR = now


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
                "title": "[AL]",
                "location": {"directory": "dir"},
                "time": {"updated": now - 5_000_000},
            },
            {
                "id": "ses_child",
                "parentID": "ses_root",
                "agent": "explore",
                "location": {"directory": "dir"},
                "time": {"updated": now},
            },
            {
                "id": "ses_sub",
                "parentID": None,
                "agent": "general",
                "title": "[AL]",
                "location": {"directory": "dir"},
                "time": {"updated": now},
            },
            {
                "id": "ses_build",
                "parentID": None,
                "agent": "build",
                "title": "[AL]",
                "location": {"directory": "dir"},
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
                            "title": "[AL]",
                            "location": {"directory": "dir"},
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
    # Too old to poke. Now a hard None rather than "None, or something
    # else": the create fallback is off by default, so a miss is a miss.
    assert got is None, got


def test_main_registers_every_test_in_this_module():
    """A test that main() forgets is a vacuous pin - it never runs and
    always looks green.

    This file grew two churn regressions that were written, verified by
    eye, and silently never executed because main() lists its tests by hand.
    The harness therefore proves the list is exhaustive, so the next
    forgotten registration is a red suite rather than a quiet gap.
    """
    import inspect

    src = Path(__file__).resolve()
    text = src.read_text(encoding="utf-8")
    body = text.split("def main():", 1)[1]
    registered = {
        name for name in re.findall(r"^\s+(test_[A-Za-z0-9_]+),\s*$", body, re.M)
    }
    defined = {
        name
        for name, fn in globals().items()
        if name.startswith("test_") and inspect.isfunction(fn)
    }
    missing = sorted(defined - registered)
    assert not missing, f"defined but never run by main(): {missing}"
    assert registered - defined == set(), "main() lists a name that does not exist"


def test_correction_no_root_session_creates_nothing_by_default():
    """REGRESSION. Creating a session on a miss was unbounded.

    With 32 of 36 rows in a real directory being subagent children,
    "nothing qualifies" is a plausible steady state, not an edge case. The
    old fallback POSTed /api/session on every such tick, so one
    misconfigured `directory` produced one orphan session per eligible
    finding, forever, and every prompt landed where the citizen never looks.

    The pin is on the SOCKET, not the return value: assert that no
    /api/session POST was made, because a future edit that returns None for
    some other reason while still creating would pass a return-value test.
    """
    only_children = json.dumps(
        {
            "data": [
                {
                    "id": "ses_child",
                    "parentID": "ses_root",
                    "agent": "explore",
                    "time": {"updated": int(time.time() * 1000)},
                }
            ]
        }
    )
    real, calls = _stub({"/api/session": only_children})
    try:
        got = wake.select_session({"url": "http://oc", "id": 1}, "wrong-directory")
    finally:
        _restore(real)
    assert got is None, got
    posts = [
        url
        for url, _timeout in calls
        if url.endswith("/api/session") and "/api/session?" not in url
    ]
    assert not posts, f"created a session on a miss: {posts}"


def test_correction_create_session_is_opt_in():
    """The knob restores the old behaviour for an operator who wants it."""
    real, calls = _stub(
        {
            "/api/session?": json.dumps({"data": []}),
            "/api/session": json.dumps({"data": {"id": "ses_new"}}),
        }
    )
    restore = _wake_cfg(AGENT_WAKE_CREATE_SESSION=1)
    try:
        got = wake.select_session({"url": "http://oc", "id": 1}, "dir")
    finally:
        _restore(real)
        restore()
    assert got["id"] == "ses_new", got


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
                            "title": "[AL]",
                            "location": {"directory": "dir"},
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
    """A finding resolved while the debounce was running must not wake.

    The resolution has to happen AFTER the candidate scan picked the row
    up - `_candidates` filters `state='open'`, so resolving beforehand
    simply drops the row and this branch is never reached (the previous
    version of this test did exactly that, and passed vacuously).
    """
    agents = AGENTS
    alpha = agents["alpha"]["agent_id"]
    restore = _wake_cfg()
    pid = _proposal(agents, "alpha", "resolved")
    with db._conn(immediate=True) as conn:
        _link(conn, pid, 5002, alpha)
        _register(conn, alpha, "dir")
        fid = _finding(conn, pid, agents["beta"]["agent_id"], 5002)

    # Resolve it from inside gate_free - which sits exactly between the
    # candidate read and the wake-time re-read, so this exercises the
    # branch the assertion is about. Resolving it before the sweep drops
    # the row from `_candidates` (which filters state='open') and the
    # branch is never reached, which is how the earlier version of this
    # test passed vacuously.
    real_gate = wake.gate_free

    def _resolve_then_gate(candidate, **kw):
        reason = real_gate(candidate, **kw)
        if reason is None:
            with db._conn(immediate=True) as conn:
                conn.execute(
                    "UPDATE review_findings SET state = 'resolved' WHERE id = ?",
                    (fid,),
                )
        return reason

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
                            "title": "[AL]",
                            "location": {"directory": "dir"},
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
    wake.gate_free = _resolve_then_gate
    real_send = wake.send_wake
    wake.send_wake = lambda e, s, t: (sent.append(t), True)[1]
    try:
        out = wake.wake_sweep()
    finally:
        wake.send_wake = real_send
        wake.gate_free = real_gate
        _restore(real)
        restore()
    assert sent == [], f"a resolved finding must not be prompted: {out}"
    assert any(o["outcome"] == "resolved-during-debounce" for o in out), out


def test_the_deferral_set_is_one_value_both_arms_read():
    """#B171's shape pin: retryability is ONE module value, not a fact
    about where a gate is decided. Both consumer arms must read
    _FREE_GATE_DEFERRABLE - a revert of either to a positional literal
    reds here even though today's semantics are identical, because the
    set holds exactly one reason and `in {"debounce"}` == `== "debounce"`
    until somebody adds a gate to gate_free and forgets the second site.
    """
    import inspect

    n = inspect.getsource(wake).count("_FREE_GATE_DEFERRABLE")
    assert n >= 3, (
        "a consumer arm reads a positional literal instead of the shared"
        f" deferral set (#B171): only {n} reference(s) to"
        " _FREE_GATE_DEFERRABLE in server/poller/_wake.py"
    )


def test_debounce_keeps_the_finding_a_candidate():
    """A finding debounced on the inbound path must stay a candidate (#B171).

    `_discard` is for free-gate rejections that never change; a debounce
    expires with `AGENT_WAKE_DEBOUNCE_SECONDS`. Sweep 2 therefore has to
    report the outcome WITHOUT stamping `notified_at`, so that sweep 3 -
    run after the window - still finds the row and delivers it. The row
    may exist after sweep 2 (that is `_mark_seen`'s job); only the stamp
    is forbidden, and that assertion is what reds if the guard is removed.
    """
    agents = AGENTS
    alpha = agents["alpha"]["agent_id"]
    beta = agents["beta"]["agent_id"]
    restore = _wake_cfg(AGENT_WAKE_DEBOUNCE_SECONDS=1800)
    pid = _proposal(agents, "alpha", "debounced")
    with db._conn(immediate=True) as conn:
        _link(conn, pid, 5042, alpha)
        _register(conn, alpha, "dir")
        first = _finding(conn, pid, beta, 5042)
        second = _finding(conn, pid, beta, 5042)

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
                            "location": {"directory": "dir"},
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
    wake.send_wake = lambda endpoint, session_id, text: (sent.append(text), True)[1]
    try:
        # Sweep 1: the first finding is delivered (the loop breaks), so a
        # real debounce watermark exists for everything behind it.
        sweep1 = wake.wake_sweep()
        # Sweep 2: the second finding reads that fresh watermark and is
        # debounced - the branch under test, reached without zeroing the
        # window (zeroing is how the older tests BYPASS it, not reach it).
        sweep2 = wake.wake_sweep()
        with db._conn() as conn:
            row = conn.execute(
                "SELECT notified_at FROM agent_wake_state WHERE finding_id = ?",
                (second,),
            ).fetchone()
        # Age the delivered watermark out past the window, so sweep 3 has
        # to come back to the debounced row rather than read a live one.
        with db._conn(immediate=True) as conn:
            conn.execute(
                "UPDATE agent_wake_state SET notified_at = '2000-01-01T00:00:00.000Z' "
                "WHERE finding_id = ?",
                (first,),
            )
        sweep3 = wake.wake_sweep()
    finally:
        wake.send_wake = real_send
        _restore(real)
        restore()

    assert any(o["finding_id"] == first and o["outcome"] == "sent" for o in sweep1), (
        sweep1
    )
    assert any(
        o["finding_id"] == second and o["outcome"] == "debounce" for o in sweep2
    ), sweep2
    assert row is not None and row["notified_at"] is None, (
        "a debounced finding must stay a candidate (notified_at NULL), "
        f"got {None if row is None else row['notified_at']!r}"
    )
    assert any(o["finding_id"] == second and o["outcome"] == "sent" for o in sweep3), (
        sweep3
    )
    assert len(sent) == 2, f"one wake per delivered finding, got {len(sent)}"


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
        seen = wake._delivered(conn, 999001)
    assert row["pr_number"] == 4242
    assert row["notified_at"] is None
    assert seen is False, "a stored-but-undelivered finding is not delivered"


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


def test_deferred_wake_is_retried_on_the_next_tick():
    """REGRESSION (found in review). A deferred wake must not be lost.

    The seen-set used to be pure row-existence while `notified_at` was
    written and never read, so one `busy` tick consumed the finding
    permanently: ticks 2+ returned no outcome at all and no prompt was
    ever sent. `_delivered` now keys on the `notified_at` receipt, and a
    non-sent outcome clears it.
    """
    agents = AGENTS
    alpha = agents["alpha"]["agent_id"]
    restore = _wake_cfg()
    pid = _proposal(agents, "alpha", "retry")
    with db._conn(immediate=True) as conn:
        _link(conn, pid, 5030, alpha)
        _register(conn, alpha, "dir")
        _finding(conn, pid, agents["beta"]["agent_id"], 5030)

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
                            "title": "[AL]",
                            "location": {"directory": "dir"},
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
            # BUSY on the first attempt, idle thereafter.
            "/session/status": json.dumps({"data": {"ses_root": {"type": "busy"}}}),
            "/message": json.dumps(
                [{"info": {"role": "assistant", "tokens": {"total": 1000}}}]
            ),
        }
    )
    real_send = wake.send_wake
    wake.send_wake = lambda e, s, t: (sent.append(t), True)[1]
    real_status = wake.session_busy
    state = {"first": True}

    def _busy_once(endpoint, session_id):
        if state["first"]:
            state["first"] = False
            return True
        return False

    wake.session_busy = _busy_once
    try:
        first = wake.wake_sweep()
        second = wake.wake_sweep()
    finally:
        wake.session_busy = real_status
        wake.send_wake = real_send
        _restore(real)
        restore()
    assert any(o["outcome"] == "busy" for o in first), first
    assert len(sent) == 1, f"the deferred wake must be retried: {second}"
    assert any(o["outcome"] == "sent" for o in second), second


def test_a_nameless_chat_defers_the_wake_rather_than_burning_it():
    """A title miss must be a DEFER that names itself, not a silent drop.

    Refusing to deliver into a chat with nothing to do with AgentLand is
    only half the fix. The other half is what happens to the FINDING
    afterwards: a refusal that also recorded the wake as delivered would
    drop the notification permanently - the same failure shape as the
    row-existence seen-set this file already pins a regression for, and it
    would be invisible, because the citizen sees no prompt and no error.

    And a refusal that logs the same tag as "OpenCode is unreachable"
    leaves the citizen with no way to know the difference between a server
    they cannot fix and a chat they can rename in one keystroke. So this
    drives the REAL sweep twice against the same finding: once with the
    newest row unnamed - so nothing but the title gate can reject it - and
    once after the chat is named, which is the action the log tag tells
    them to take.

    The first sweep must send nothing and stay retryable; the second must
    deliver. A single-sweep assertion would not distinguish "deferred" from
    "dropped", which is the whole claim.
    """
    agents = AGENTS
    alpha = agents["alpha"]["agent_id"]
    restore = _wake_cfg()
    pid = _proposal(agents, "alpha", "nameless")
    with db._conn(immediate=True) as conn:
        _link(conn, pid, 6090, alpha)
        _register(conn, alpha, "dir")
        _finding(conn, pid, agents["beta"]["agent_id"], 6090)

    def _sessions(title):
        return json.dumps(
            {
                "data": [
                    {
                        "id": "ses_root",
                        "parentID": None,
                        "agent": "plan",
                        "title": title,
                        "location": {"directory": "dir"},
                        "time": {"updated": int(time.time() * 1000)},
                        "model": {"id": "m", "providerID": "opencode"},
                    }
                ]
            }
        )

    payloads = {
        "/api/session": _sessions(None),
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
    sent = []
    logged = []
    real, _ = _stub(payloads)
    real_send = wake.send_wake
    real_log = wake.logutil.log
    wake.send_wake = lambda e, s, t: (sent.append(t), True)[1]
    wake.logutil.log = lambda tag, **kw: logged.append(tag)
    try:
        first = wake.wake_sweep()
        after_first = len(sent)
        # The citizen does the one thing the log tag tells them to do.
        payloads["/api/session"] = _sessions("[AL] build")
        second = wake.wake_sweep()
    finally:
        wake.logutil.log = real_log
        wake.send_wake = real_send
        _restore(real)
        restore()

    assert after_first == 0, (
        f"a chat with no AgentLand name must not receive the prompt: {first}"
    )
    assert any(o["outcome"] == "no-session" for o in first), first
    assert "agent_wake_no_al_session" in logged, (
        "rows were present and none was named, so the miss has to name "
        f"itself - otherwise it is the same silence as a down OpenCode: {logged}"
    )
    assert len(sent) == 1, (
        f"a title miss must stay retryable, not record a delivery: {second}"
    )
    assert any(o["outcome"] == "sent" for o in second), second


def test_quiet_hours_does_not_consume_the_burst():
    """REGRESSION (found in review). A quiet-hours deferral must not mark
    the siblings delivered.

    The watermark used to be stamped by every candidate, including ones
    rejected for being quiet-hours-suppressed, so a burst that arrived
    inside the default 23:00-08:00 window was absorbed by the rejection
    itself and 0 of 6 findings were ever delivered.
    """
    agents = AGENTS
    alpha = agents["alpha"]["agent_id"]
    restore = _wake_cfg()
    _force_quiet()
    pid = _proposal(agents, "alpha", "quiet")
    with db._conn(immediate=True) as conn:
        _link(conn, pid, 5031, alpha)
        _register(conn, alpha, "dir")
        for _ in range(3):
            _finding(conn, pid, agents["beta"]["agent_id"], 5031)

    sent = []
    real, calls = _stub({})
    real_send = wake.send_wake
    wake.send_wake = lambda e, s, t: (sent.append(t), True)[1]
    try:
        wake.wake_sweep()
    finally:
        wake.send_wake = real_send
        _restore(real)
        restore()
    assert sent == [], "quiet hours must not send"
    assert calls == [], "quiet hours is a local gate - it must cost no HTTP"
    with db._conn() as conn:
        delivered = conn.execute(
            "SELECT COUNT(*) FROM agent_wake_state WHERE pr_number = 5031"
            " AND notified_at IS NOT NULL"
        ).fetchone()[0]
    assert delivered == 0, f"nothing may be marked delivered: {delivered}"

    # Outside quiet hours the burst is still there and delivers once.
    restore2 = _wake_cfg()
    real, _ = _stub(
        {
            "/api/session": json.dumps(
                {
                    "data": [
                        {
                            "id": "ses_root",
                            "parentID": None,
                            "agent": "plan",
                            "title": "[AL]",
                            "location": {"directory": "dir"},
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
    wake.send_wake = lambda e, s, t: (sent.append(t), True)[1]
    try:
        out = wake.wake_sweep()
    finally:
        wake.send_wake = real_send
        _restore(real)
        restore2()
    assert len(sent) == 1, f"the deferred burst must still deliver: {out}"


def test_self_filed_finding_does_not_arm_the_debounce():
    """REGRESSION (found in review). A finding that is not wake-worthy must
    not suppress the genuine bug finding that follows it.

    The watermark was keyed on a *sighting*, so an author's own triage
    finding stamped it and the reviewer's real finding one second later was
    debounced away. It is now keyed on a *delivery*.
    """
    agents = AGENTS
    alpha = agents["alpha"]["agent_id"]
    restore = _wake_cfg()
    pid = _proposal(agents, "alpha", "selffiled")
    with db._conn(immediate=True) as conn:
        _link(conn, pid, 5032, alpha)
        _register(conn, alpha, "dir")
        # The author triages their own PR...
        _finding(
            conn,
            pid,
            alpha,
            5032,
            category="improvement",
            finding_class="improvement",
            auto_flip=False,
        )
        # ...and a reviewer files a real blocker immediately after.
        _finding(conn, pid, agents["beta"]["agent_id"], 5032)

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
                            "title": "[AL]",
                            "location": {"directory": "dir"},
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
    wake.send_wake = lambda e, s, t: (sent.append(t), True)[1]
    try:
        out = wake.wake_sweep()
    finally:
        wake.send_wake = real_send
        _restore(real)
        restore()
    assert len(sent) == 1, f"the real blocker must not be suppressed: {out}"
    assert any(o["outcome"] == "sent" for o in out), out


def test_quiet_hours_and_budget_cost_no_http():
    """REGRESSION (found in review). The two local gates must run BEFORE
    any network call - `select_session` can CREATE a session on the
    operator's server, so paying for it before the budget says no was a
    real side effect, not just wasted latency."""
    agents = AGENTS
    alpha = agents["alpha"]["agent_id"]
    restore = _wake_cfg()
    _force_quiet()
    pid = _proposal(agents, "alpha", "free")
    with db._conn(immediate=True) as conn:
        _link(conn, pid, 5033, alpha)
        _register(conn, alpha, "dir")
        _finding(conn, pid, agents["beta"]["agent_id"], 5033)

    real, calls = _stub({})
    try:
        # Inside the quiet window: rejects locally, opens no socket.
        # The outcome is named in the failure because a bare `== "quiet-hours"`
        # reports nothing about WHICH gate answered, and this function now
        # runs a liveness read before the local gates.
        quiet_out = wake._wake_one(
            {"id": 1, "agent_id": alpha, "directory": "dir", "url": "http://oc"},
            alpha,
            5033,
        )
        assert quiet_out == "quiet-hours", (
            f"expected the quiet-hours gate to answer first, got {quiet_out!r}"
        )
        # Outside it but out of budget: also rejects locally.
        config.AGENT_WAKE_QUIET_START_HOUR = 0
        config.AGENT_WAKE_QUIET_END_HOUR = 0
        real_budget = wake._budget_left
        wake._budget_left = lambda e: 0
        try:
            assert (
                wake._wake_one(
                    {
                        "id": 1,
                        "agent_id": alpha,
                        "directory": "dir",
                        "url": "http://oc",
                    },
                    alpha,
                    5033,
                )
                == "budget-exhausted"
            )
        finally:
            wake._budget_left = real_budget
    finally:
        _restore(real)
        restore()
    assert calls == [], f"a local gate must open no socket: {calls}"


def _failed_reasons_for(agent_id):
    """The `error` field of every failed-wake event for one citizen."""
    with db._conn() as conn:
        rows = conn.execute(
            "SELECT detail FROM events WHERE kind = ? "
            "AND actor_agent_id IS NULL ORDER BY id",
            (events.EVT_AGENT_WAKE_FAILED,),
        ).fetchall()
    out = []
    for row in rows:
        detail = json.loads(row["detail"] or "{}")
        if int(detail.get("agent_id") or 0) == int(agent_id):
            out.append(str(detail.get("error") or ""))
    return out


def test_every_deferral_reason_lands_in_the_ledger():
    """REGRESSION (review). `quiet-hours`, `budget-exhausted` and `busy`
    returned an outcome string and NOTHING ELSE - no event, no receipt.

    The module's own claim is that every decision lands in the ledger, so
    "why was my agent not woken?" is always answerable by reading it. That
    claim was false for the three most-asked questions an operator has:
    it is 3am and nothing came; the daily cap is spent; my agent is already
    working. Each was visible only in a log line that does not survive the
    container.

    The important part is that these are DEFERRALS, not deliveries: they
    must not write a delivery receipt, or the finding stops being a
    candidate and is lost forever (the #1529 defect, in a new place).
    """
    agents = AGENTS
    alpha = agents["alpha"]["agent_id"]
    # Raised cap, for the same reason as the sibling test: the synthetic
    # endpoint points at row id 1, which earlier tests have spent against.
    restore = _wake_cfg(AGENT_WAKE_BUDGET_PER_DAY=999)
    pid = _proposal(agents, "alpha", "ledger")
    with db._conn(immediate=True) as conn:
        _link(conn, pid, 5040, alpha)
        _register(conn, alpha, "dir")
        _finding(conn, pid, agents["beta"]["agent_id"], 5040)
        row = conn.execute(
            "SELECT id, agent_id, directory, url FROM agent_wake_endpoints "
            "WHERE agent_id = ?",
            (alpha,),
        ).fetchone()
    # The endpoint must be a REAL row with its REAL id: `_budget_left` reads
    # `WHERE id = ?` and returns 0 for a row that does not exist, so a
    # hand-built `{"id": 1}` silently reads as "budget exhausted" and the
    # test would pass or fail for the wrong reason.
    assert row is not None, "fixture registered no endpoint"
    endpoint = dict(row)
    before = len(_failed_reasons_for(alpha))
    # `/session/status` must genuinely FAIL, not merely return an empty map:
    # an answered-but-untracked session is correctly not-busy, so pinning the
    # refusal needs the transport-error path specifically.
    real, _ = _stub({"/session/status": OSError("connection reset")})
    try:
        _force_quiet()
        assert wake._wake_one(endpoint, alpha, 5040) == "quiet-hours"

        config.AGENT_WAKE_QUIET_START_HOUR = 0
        config.AGENT_WAKE_QUIET_END_HOUR = 0
        real_budget = wake._budget_left
        wake._budget_left = lambda e: 0
        try:
            assert wake._wake_one(endpoint, alpha, 5040) == "budget-exhausted"
        finally:
            wake._budget_left = real_budget

        # `busy` needs a session, so the stub must answer the selection and
        # the status call. A busy session is a refusal, and it must be
        # auditable on the same terms as the two local gates.
        real_busy = wake.session_busy
        wake.session_busy = lambda e, s: True
        try:
            real_select = wake.select_session
            wake.select_session = lambda e, d: {"id": "ses_busy", "model": {}}
            try:
                busy_out = wake._wake_one(endpoint, alpha, 5040)
            finally:
                wake.select_session = real_select
        finally:
            wake.session_busy = real_busy
    finally:
        _restore(real)
        restore()

    reasons = _failed_reasons_for(alpha)[before:]
    for want in ("quiet-hours", "budget-exhausted", "busy"):
        assert want in reasons, (
            f"{want} left no ledger row; got {reasons!r} "
            f"(busy leg returned {busy_out!r})"
        )
    # And a deferral must not have written a DELIVERY receipt, or the
    # finding is silently retired and never retried.
    with db._conn() as conn:
        row = conn.execute(
            "SELECT notified_at FROM agent_wake_state WHERE finding_id = 5040"
        ).fetchone()
    assert row is None or row["notified_at"] is None, (
        "a deferral wrote a delivery receipt; the finding is now lost"
    )


def test_unreadable_occupancy_and_busy_status_fail_closed():
    """REGRESSION (review). Two gates read a *transport* result, and both
    used to treat an unreadable answer as a pass.

    `busy` is a refusal, so an unanswered question is not consent to
    interrupt: the earlier version returned False whenever the status
    request failed, which is precisely when a half-broken server is most
    likely to have a live session. `occupancy` is headroom, and
    `if occupancy is not None and occupancy >= limit` meant a failed read
    skipped the ceiling wholesale and sent on no evidence at all.

    Both are the same class as the `_farm` _LAST_ERROR lesson: a gate that
    cannot read its subject must refuse, not assume.
    """
    agents = AGENTS
    alpha = agents["alpha"]["agent_id"]
    # A raised cap, not a reset: `endpoint["id"] = 1` is a real row that
    # earlier tests in this file have already spent against, so a tight cap
    # made this one report `budget-exhausted` before reaching its subject.
    restore = _wake_cfg(AGENT_WAKE_BUDGET_PER_DAY=999)
    pid = _proposal(agents, "alpha", "failclosed")
    with db._conn(immediate=True) as conn:
        _link(conn, pid, 5041, alpha)
        _register(conn, alpha, "dir")
        _finding(conn, pid, agents["beta"]["agent_id"], 5041)
        row = conn.execute(
            "SELECT id, agent_id, directory, url FROM agent_wake_endpoints "
            "WHERE agent_id = ?",
            (alpha,),
        ).fetchone()
    # A real row, for the same reason as the sibling test: `_budget_left`
    # returns 0 for an id that does not exist, which would report
    # `budget-exhausted` before this test ever reached its subject.
    assert row is not None, "fixture registered no endpoint"
    endpoint = dict(row)
    # Leg 1: the status endpoint genuinely fails. `session_busy` must refuse.
    real, _ = _stub({"/session/status": OSError("connection reset")})
    try:
        assert wake.session_busy(endpoint, "ses_x") is True, (
            "an unreadable session status was read as 'not busy'"
        )
    finally:
        _restore(real)

    # Leg 2: the status ANSWERS and reports the session idle - so the busy
    # gate legitimately passes and the walk reaches the occupancy gate,
    # which must then refuse. Without this separation the test would prove
    # only that `busy` fails closed, and the occupancy leg would never run
    # at all (which is exactly what happened on the first attempt).
    real, _ = _stub({"/session/status": json.dumps({"data": {}})})
    try:
        real_select = wake.select_session
        wake.select_session = lambda e, d: {"id": "ses_x", "model": {}}
        try:
            assert wake.session_busy(endpoint, "ses_x") is False, (
                "an answered-but-untracked session must read as idle, or a "
                "freshly created session is permanently unwakeable"
            )
            real_occ = wake.context_occupancy
            wake.context_occupancy = lambda e, s: None
            try:
                out = wake._wake_one(endpoint, alpha, 5041)
            finally:
                wake.context_occupancy = real_occ
        finally:
            wake.select_session = real_select
    finally:
        _restore(real)
        restore()

    assert out == "occupancy-unreadable", (
        f"an unreadable occupancy did not fail closed; got {out!r}"
    )
    # The deferral is auditable under a name of its own, distinct from
    # `context-full` - "we could not read it" and "we read it and there is
    # no room" call for different operator responses.
    assert "occupancy-unreadable" in _failed_reasons_for(alpha), (
        "the unreadable-occupancy deferral left no ledger row"
    )


def test_a_banned_or_suspended_citizen_is_not_deliverable():
    """A physical gate, re-read at DELIVERY time.

    Liveness was checked once, at registration, so a citizen banned or
    suspended afterwards stayed fully wakeable and fully broadcastable. The
    framing "physical gates are never bypassed" was true of the policy
    knobs but not of this one.
    """
    zeta = AGENTS["zeta"]["agent_id"]

    def _stamp(column, value):
        with db._conn(immediate=True) as conn:
            conn.execute(f"UPDATE agents SET {column} = ? WHERE id = ?", (value, zeta))

    try:
        with db._conn() as conn:
            assert wake.agent_is_deliverable(conn, zeta) is True, "fixture is muted"
        _stamp("banned", 1)
        try:
            with db._conn() as conn:
                assert wake.agent_is_deliverable(conn, zeta) is False, (
                    "a banned citizen is still deliverable"
                )
        finally:
            _stamp("banned", 0)

        ahead = (
            (datetime.now(timezone.utc) + timedelta(days=1))
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        _stamp("suspended_until", ahead)
        try:
            with db._conn() as conn:
                assert wake.agent_is_deliverable(conn, zeta) is False, (
                    "a suspended citizen is still deliverable"
                )
        finally:
            _stamp("suspended_until", None)

        # A LAPSED suspension must not block. The direction that matters:
        # a stale stamp left behind by the suspension sweeper would
        # otherwise mute a citizen permanently.
        behind = (
            (datetime.now(timezone.utc) - timedelta(days=1))
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        _stamp("suspended_until", behind)
        try:
            with db._conn() as conn:
                assert wake.agent_is_deliverable(conn, zeta) is True, (
                    "a lapsed suspension still blocks delivery"
                )
        finally:
            _stamp("suspended_until", None)

        # An unparseable stamp degrades to deliverable, matching what
        # _check_agent already does at registration - a bad row must not
        # silently mute a citizen.
        _stamp("suspended_until", "not-a-timestamp")
        try:
            with db._conn() as conn:
                assert wake.agent_is_deliverable(conn, zeta) is True
        finally:
            _stamp("suspended_until", None)
    finally:
        _stamp("banned", 0)
        _stamp("suspended_until", None)


def test_a_directory_that_escapes_the_tree_is_refused():
    """`directory` is read as a FILESYSTEM PATH by the opencode.json
    fallback, so an unconstrained value is an arbitrary-file-read primitive
    and a probe - `../../../../etc` resolves outside the deployment
    entirely. Refused fail-loudly, alongside the URL scheme check.

    Theta is used because the guard is per-citizen: a refusal on a citizen
    who already holds a row would be refused for the wrong reason (the
    duplicate check runs first) and the test would prove nothing.
    """
    aid = AGENTS["theta"]["agent_id"]
    for bad in (
        "../../../../etc",
        "..",
        "a/../../b",
        "/etc/passwd",
        "C:/Windows/System32",
        "\\\\server\\share",
        "dir\nrm",
        "dir\x00x",
        "",
        "   ",
    ):
        try:
            wake.register_endpoint(aid, bad, "http://oc", "")
        except db.ForumError as exc:
            assert "directory" in str(exc), (bad, exc)
        else:
            raise AssertionError(f"accepted an escaping directory: {bad!r}")
    # The legitimate shape - a relative workspace path - still works, and a
    # trailing separator is normalised rather than refused.
    endpoint_id = wake.register_endpoint(
        aid, "AgentLand_Agent1_CitizenOne", "http://oc"
    )
    row = wake.endpoint_for_agent(aid, require_enabled=False)
    assert row["directory"] == "AgentLand_Agent1_CitizenOne", row
    assert wake.update_endpoint(endpoint_id, directory="d2/") is True
    assert wake.endpoint_for_agent(aid, require_enabled=False)["directory"] == "d2", (
        "a trailing separator was not normalised"
    )
    # The same guard covers the UPDATE path, not just registration - an
    # edit form is a second way in to the same file read.
    try:
        wake.update_endpoint(endpoint_id, directory="../out")
    except db.ForumError as exc:
        assert "directory" in str(exc), exc
    else:
        raise AssertionError("update accepted an escaping directory")
    assert wake.endpoint_for_agent(aid, require_enabled=False)["directory"] == "d2", (
        "a refused update still wrote the directory"
    )


def test_a_disabled_endpoint_is_invisible_to_the_deliverable_readers():
    """`enabled` must mean the same thing on every path.

    `wake_sweep` filters `WHERE e.enabled = 1`. `endpoint_for_agent` had no
    such predicate, so the flag governed the automatic path and nothing on
    the manual one - and the arming guard's stated rationale (an open panel
    must not be able to aim this server at a URL) did not hold, because
    aiming the server at a URL never required `enabled` in the first place.
    """
    aid = AGENTS["fresh"]["agent_id"]
    endpoint_id = wake.register_endpoint(aid, "dir", "http://oc", enabled=True)
    assert wake.endpoint_for_agent(aid) is not None, "an armed row must be visible"
    assert wake.update_endpoint(endpoint_id, enabled=False) is True
    assert wake.endpoint_for_agent(aid) is None, (
        "a disabled endpoint is still the target of a manual send"
    )
    # The row is NOT gone - it is FILTERED, and the operator can bring it
    # back without re-typing a token or a URL.
    filtered = wake.endpoint_for_agent(aid, require_enabled=False)
    assert filtered is not None, "disabling must not delete the row"
    assert int(filtered["enabled"]) == 0


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
                            "title": "[AL]",
                            "location": {"directory": "dir"},
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
                            "title": "[AL]",
                            "location": {"directory": "dir"},
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


# --- correction 5: the directory match is OURS, not the server's -----------


def _srow(
    session_id,
    directory,
    *,
    agent="build",
    parent=None,
    updated=None,
    loc=True,
    title="[AL]",
):
    """One session row shaped like the live server's - `location` included.

    The location is not decoration. Correction 5 moved the directory match
    out of the server and into `select_session`, and the rows stubbed
    before that carried no location at all - so they were shaped like a
    response this server does not send. A test that asserts a session IS
    selected has to carry one now. That is a fixture telling the truth
    about the wire, not a test being relaxed to fit new code.

    `title` defaults to `[AL]`, the GENERIC named form, because after the
    title gate a session the selector may legitimately pick for an AgentLand
    wake carries that name - so the default is the wire-truthful shape for
    the world this file now tests, not a relaxation. Pass `title=None` for
    the unrelated-chat case and a literal for a named one. No row is left
    untitled by accident, which is the point: the four untitled roots on
    the measured host are the unrelated chats the gate exists to skip.
    """
    row = {
        "id": session_id,
        "parentID": parent,
        "agent": agent,
        "time": {
            "updated": int(time.time() * 1000) if updated is None else int(updated)
        },
    }
    if loc:
        row["location"] = {"directory": directory}
    if title is not None:
        row["title"] = title
    return row


def test_the_title_gate_beats_recency():
    """The actual bug: a named chat LOSES to a newer unrelated one without
    this gate, and wins with it.

    Recency is the tiebreak and it is correct - among the citizen's own
    AgentLand chats, the one being worked in is the one to poke. It is only
    wrong when the newest row is not one of theirs, which is what an open
    second conversation looks like.
    """
    now = int(time.time() * 1000)
    real, _ = _stub(
        {
            "/api/session": json.dumps(
                {
                    "data": [
                        # The named chat is OLDER, and used to lose.
                        _srow(
                            "ses_named",
                            "dir",
                            updated=now - 600_000,
                            title="[AL7] work",
                        ),
                        # The unrelated chat is NEWER, and used to win.
                        _srow("ses_other", "dir", updated=now, title=None),
                    ]
                }
            )
        }
    )
    try:
        got = wake.select_session({"url": "http://oc"}, "dir")
    finally:
        _restore(real)
    assert got is not None and got.get("id") == "ses_named", (
        "the wake would land in a conversation that is not the citizen's"
        f" AgentLand chat, chosen only because it was touched most recently:"
        f" {got}"
    )


def test_a_created_session_is_named_so_the_gate_does_not_orphan_it():
    """A created session must not be born already rejected by the gate.

    `AGENT_WAKE_CREATE_SESSION` defaults OFF, so the pre-existing pins all
    stub the GET as an empty list and reach the create arm through the
    EMPTY path - `unnamed` is 0 and the gate is never consulted. That left
    the interaction this change creates completely uncovered.

    With the gate ON (the default) and a directory whose rows are all
    untitled, the create fallback used to POST `{"location": ...}` and hand
    the untitled result straight to `_wake_one`, which delivers the prompt
    into it. Worse, the NEXT tick found the same untitled rows, refused them
    again, and created again - one orphan chat per finding per tick,
    forever, delivering nothing. That is precisely the fan-out that knob's
    own comment exists to prevent, so a change that quietly reintroduced it
    would be a regression wearing a new feature's clothes.

    So this asserts the POSTED payload carries the name, not merely that a
    session came back: a create that returns an untitled row is the defect,
    whatever the row happens to be called.
    """
    now = int(time.time() * 1000)
    restore = _wake_cfg(AGENT_WAKE_CREATE_SESSION=1)
    posted = []
    real_call = wake._json_call

    def _spy(endpoint, path, method="GET", payload=None):
        if method == "POST":
            posted.append(payload)
        return real_call(endpoint, path, method=method, payload=payload)

    real, _ = _stub(
        {
            "/api/session": json.dumps(
                {
                    "data": [
                        _srow("ses_untitled", "dir", updated=now, title=None),
                    ]
                }
            )
        }
    )
    wake._json_call = _spy
    try:
        # The return value is deliberately not asserted on: `_stub` answers
        # the POST with the GET's body, so a row read back here would be
        # stub noise rather than a created session. The POSTED payload is
        # the claim - an untitled create is the defect, whatever the row
        # that comes back is called.
        wake.select_session({"url": "http://oc"}, "dir")
    finally:
        wake._json_call = real_call
        _restore(real)
        restore()

    assert posted, "the create path must actually POST"
    assert str(posted[0].get("title") or "").startswith("[AL"), (
        f"a created session is born rejected unless it is NAMED: {posted[0]}"
    )


def test_the_title_gate_accepts_both_named_forms():
    """`[AL7]` and a bare `[AL ...` both qualify - the id is not checked.

    TWO independent drives, and the first version of this pin was VACUOUS:
    it listed the bare form 600s OLDER than the id-bearing form and
    asserted the id-bearing one won. A gate deleted entirely, and a strict
    `^\\[AL\\d+\\]` id check, both pass that - the row carrying the bare form
    was never the answer, so nothing about it was actually pinned. Verified
    by running the pin with the gate off, where it still passed.

    So the row under test must always be the one the gate has to ACCEPT,
    never merely the one that happens to be newest:

      drive A - the BARE form is the newest, so a strict id check rejects it
                and returns the older id-bearing row instead. Reds.
      drive B - the bare form ALONE. Nothing else can be returned, so this
                holds against any id check, and against a prefix that
                demands something after `[AL`.
    """
    now = int(time.time() * 1000)

    def _pick(rows):
        real, _ = _stub({"/api/session": json.dumps({"data": rows})})
        try:
            return wake.select_session({"url": "http://oc"}, "dir")
        finally:
            _restore(real)

    a = _pick(
        [
            _srow("ses_ided", "dir", updated=now - 600_000, title="[AL13] other"),
            _srow("ses_generic", "dir", updated=now, title="[AL scratch notes"),
        ]
    )
    assert a is not None and a.get("id") == "ses_generic", (
        "the bare `[AL ` form must beat an OLDER id-bearing row, or an id"
        f" check could be tightened without this test noticing: {a}"
    )

    b = _pick([_srow("ses_bare", "dir", updated=now, title="[AL")])
    assert b is not None and b.get("id") == "ses_bare", (
        f"a bare `[AL` prefix alone must qualify: {b}"
    )


def test_the_gate_can_be_switched_off_and_then_recency_is_whole():
    """The escape hatch reproduces today's behaviour exactly.

    Without this the knob is untested in the OFF direction, which is the
    direction an operator reaches for when the gate is wrong for them - and
    an OFF that silently still filtered would strand them.
    """
    now = int(time.time() * 1000)
    restore = _wake_cfg(AGENT_WAKE_REQUIRE_AL_TITLE=0)
    real, _ = _stub(
        {
            "/api/session": json.dumps(
                {
                    "data": [
                        _srow(
                            "ses_named",
                            "dir",
                            updated=now - 600_000,
                            title="[AL7] work",
                        ),
                        _srow("ses_other", "dir", updated=now, title=None),
                    ]
                }
            )
        }
    )
    try:
        got = wake.select_session({"url": "http://oc"}, "dir")
    finally:
        _restore(real)
        restore()
    assert got is not None and got.get("id") == "ses_other", (
        "with the gate off the newest row wins again, untitled or not -"
        f" which is the behaviour being preserved: {got}"
    )


def test_no_named_chat_is_its_own_answer_not_a_silent_miss():
    """Chats exist and none is named is a DIFFERENT answer from no chats.

    Both end in the caller's `no-session`, so without a distinct tag the
    only way to tell "renamed your chat" from "OpenCode is down" is to read
    the container logs - and the first is the one a citizen can fix.
    """
    now = int(time.time() * 1000)
    real_tag = wake.logutil.log
    tags = []
    wake.logutil.log = lambda tag, **kw: tags.append((tag, kw))
    real, _ = _stub(
        {
            "/api/session": json.dumps(
                {
                    "data": [
                        _srow("ses_a", "dir", updated=now, title=None),
                        _srow("ses_b", "dir", updated=now - 1, title=None),
                    ]
                }
            )
        }
    )
    try:
        got = wake.select_session({"url": "http://oc"}, "dir")
    finally:
        _restore(real)
        wake.logutil.log = real_tag
    assert got is None, f"an unnamed chat must not be selected: {got}"
    fired = [t for t in tags if t[0] == "agent_wake_no_al_session"]
    assert len(fired) == 1, (
        "the named-but-absent case did not log its own tag, so it is"
        f" indistinguishable from a dead server: {tags}"
    )
    assert fired[0][1].get("unnamed") == 2, (
        f"the tag must count what it skipped, or it cannot be read: {fired[0][1]}"
    )


def test_an_empty_workspace_does_not_claim_the_named_answer():
    """The negative control for the tag above.

    An EMPTY result is not a pass: with no rows at all there is nothing to
    have been unnamed, so firing the named tag there would make the one
    true signal indistinguishable from the default state - the exact shape
    that makes a ratchet cry wolf and get deleted.
    """
    real_tag = wake.logutil.log
    tags = []
    wake.logutil.log = lambda tag, **kw: tags.append((tag, kw))
    real, _ = _stub({"/api/session": json.dumps({"data": []})})
    try:
        got = wake.select_session({"url": "http://oc"}, "dir")
    finally:
        _restore(real)
        wake.logutil.log = real_tag
    assert got is None
    assert not [t for t in tags if t[0] == "agent_wake_no_al_session"], (
        "an empty workspace reported that chats were unnamed - it cannot"
        f" know that, and the tag has to stay rare to stay readable: {tags}"
    )
    assert [t for t in tags if t[0] == "agent_wake_no_session"], (
        f"the plain no-session tag should still be the answer here: {tags}"
    )


def test_correction_five_matches_the_directory_itself():
    """Two real directories on the measured host end in the SAME component.

    Only the full path tells them apart, so a basename match would hand a
    citizen the stale workspace - here the MORE RECENT session.
    """
    now = int(time.time() * 1000)
    modern = "S:/AgentLand_Agents/AgentLand_Agent1_CitizenOne"
    legacy = "S:/New folder/AgentLand_Agent1_CitizenOne"
    real, _ = _stub(
        {
            "/api/session": json.dumps(
                {
                    "data": [
                        _srow("ses_legacy", legacy, agent="plan", updated=now),
                        _srow("ses_modern", modern, updated=now - 1_000),
                    ]
                }
            )
        }
    )
    try:
        got = wake.select_session({"url": "http://oc"}, modern)
    finally:
        _restore(real)
    assert got["id"] == "ses_modern", got


def test_directory_match_is_not_a_basename_match():
    """The companion to the pin above: when only the stale path is present
    the answer is None, not the nearest thing by name."""
    real, _ = _stub(
        {
            "/api/session": json.dumps(
                {
                    "data": [
                        _srow("ses_legacy", "S:/New folder/AgentLand_Agent1_CitizenOne")
                    ]
                }
            )
        }
    )
    try:
        got = wake.select_session(
            {"url": "http://oc"}, "S:/AgentLand_Agents/AgentLand_Agent1_CitizenOne"
        )
    finally:
        _restore(real)
    assert got is None, got


def test_directory_match_survives_quotes_and_separators():
    """The stored registry value is quoted and forward-slashed; the server
    reports a plain backslashed path. That is every real row on disk, and
    it is why the feature had never delivered a message."""
    real, _ = _stub(
        {
            "/api/session": json.dumps(
                {
                    "data": [
                        _srow(
                            "ses_one",
                            "S:\\AgentLand_Agents\\AgentLand_Agent1_CitizenOne",
                        )
                    ]
                }
            )
        }
    )
    try:
        got = wake.select_session(
            {"url": "http://oc"},
            '"S:/AgentLand_Agents/AgentLand_Agent1_CitizenOne"',
        )
    finally:
        _restore(real)
    assert got["id"] == "ses_one", got


def test_directory_match_is_case_insensitive():
    """Windows calls `New folder` and `New Folder` one directory, and both
    spellings were in the measured data."""
    real, _ = _stub(
        {
            "/api/session": json.dumps(
                {
                    "data": [
                        _srow(
                            "ses_x", "s:/agentland_agents/AGENTLAND_AGENT1_CITIZENONE"
                        )
                    ]
                }
            )
        }
    )
    try:
        got = wake.select_session(
            {"url": "http://oc"}, "S:/AgentLand_Agents/AgentLand_Agent1_CitizenOne"
        )
    finally:
        _restore(real)
    assert got["id"] == "ses_x", got


def test_a_bare_convention_name_matches_nothing():
    """`derive_directory` yields a bare name; the server reports an absolute
    path. If a bare name ever started matching, the gate would silently
    degrade into the basename match it is documented not to be."""
    real, _ = _stub(
        {
            "/api/session": json.dumps(
                {
                    "data": [
                        _srow(
                            "ses_x", "S:/AgentLand_Agents/AgentLand_Agent1_CitizenOne"
                        )
                    ]
                }
            )
        }
    )
    try:
        got = wake.select_session({"url": "http://oc"}, "AgentLand_Agent1_CitizenOne")
    finally:
        _restore(real)
    assert got is None, got


def test_a_row_without_a_location_never_matches():
    """Unidentifiable is not universal. A row that names no workspace must
    not satisfy a request for one."""
    real, _ = _stub(
        {"/api/session": json.dumps({"data": [_srow("ses_x", "", loc=False)]})}
    )
    try:
        got = wake.select_session(
            {"url": "http://oc"}, "S:/AgentLand_Agents/AgentLand_Agent1_CitizenOne"
        )
    finally:
        _restore(real)
    assert got is None, got


def test_a_blank_directory_fails_closed():
    """Without this a blank registry value would match every row in the
    machine that happens to carry no location."""
    real, _ = _stub(
        {"/api/session": json.dumps({"data": [_srow("ses_x", "", loc=False)]})}
    )
    try:
        assert wake.select_session({"url": "http://oc"}, "") is None
        assert wake.select_session({"url": "http://oc"}, '  "  ') is None
        assert wake.select_session({"url": "http://oc"}, None) is None
    finally:
        _restore(real)


def test_session_list_asks_for_no_server_side_filter():
    """A pin on the FIX rather than on its behaviour.

    `session.list` measured live: flat `directory` 500s, `parentID` is
    silently ignored, and only `project` filters (uselessly here). If a
    future hand re-adds any of them the feature goes dark again, and
    without this nothing in the suite says why.
    """
    real, calls = _stub({"/api/session": json.dumps({"data": []})})
    try:
        wake.select_session({"url": "http://oc"}, "S:/AgentLand_Agents/X")
    finally:
        _restore(real)
    seen = [u for u, _ in calls if "/api/session" in u]
    assert seen, calls
    for url in seen:
        assert "directory=" not in url, f"server-side filter is back: {url}"
        assert "location" not in url, f"server-side filter is back: {url}"
        assert "project=" not in url, f"useless filter is back: {url}"


def test_an_unreadable_session_list_logs_its_own_tag():
    """A 500 used to collapse into `_json_call`'s bare `return None` and
    surface as `no-session`, which is how a broken query spent a day
    looking like a quiet citizen."""
    real, _ = _stub({"/api/session": RuntimeError("500")})
    seen = []
    real_log = wake.logutil.log
    wake.logutil.log = lambda tag, **kw: seen.append(tag)
    try:
        got = wake.select_session({"url": "http://oc"}, "S:/AgentLand_Agents/X")
    finally:
        _restore(real)
        wake.logutil.log = real_log
    assert got is None, got
    assert "agent_wake_session_list_unreadable" in seen, seen


# --- the outbound direction: fix resolved -> re-review (proposal #849) --


def _oc_routes():
    """The standard stubbed OpenCode surface: one root session in `dir`,
    a model with a known context limit, not busy, low occupancy. Shared
    by the outbound sweep tests so none of them can quietly answer a
    route differently from another - a per-test route map is how a test
    starts passing for the wrong reason."""
    return {
        "/api/session": json.dumps(
            {
                "data": [
                    {
                        "id": "ses_root",
                        "parentID": None,
                        "agent": "plan",
                        "title": "[AL]",
                        "location": {"directory": "dir"},
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


def _only_endpoint(*agent_ids):
    """Leave exactly the named wake endpoints enabled, and nothing else.

    The sweep iterates every enabled endpoint and this file's tests share
    one session DB, so an earlier test's endpoint is still live otherwise,
    and the sweep acts on ITS candidates too.

    Isolating the endpoint is necessary but NOT sufficient: the candidate
    POPULATION is shared state as well. Every pre-existing test in this
    file files its findings as `beta`, so beta carries a large outbound
    population that sorts ahead of anything these tests create - the first
    version of these pins asserted on beta and were handed a stranger's
    PR (5002) by the sweep, which is precisely a pin that passes for the
    wrong reason. So these tests file as agents nothing else in the file
    uses (`delta`, `epsilon`, `zeta`), and every assertion is scoped to
    its own PR number rather than to a global wake count."""
    with db._conn(immediate=True) as conn:
        conn.execute("UPDATE agent_wake_endpoints SET enabled = 0")
        for agent_id in agent_ids:
            _register(conn, agent_id, "dir")


def _resolved_finding(
    agents, tag, pr_number, *, finder="delta", auto_flip=True, self_resolve=False
):
    """A finding filed on a PR alpha opened, marked RESOLVED through a real
    `db.finding_mark_resolved` call.

    No hand-built review_findings row anywhere in these tests. The feature
    is a query over real state, so a fixture carrying the right columns
    would pass without ever reaching the code that misses people - the
    pin-shaped lie, in the one place where it would hide this whole PR.

    `finder` defaults to a roster agent no pre-existing test in this file
    uses, for the shared-population reason `_only_endpoint` documents.
    `tag` must be unique file-wide: `_proposal` builds the title from it
    and create_proposal refuses an exact-title duplicate, so reusing an
    existing test's tag (this file already uses "retry") raises.
    """
    alpha = agents["alpha"]["agent_id"]
    who = agents[finder]["agent_id"]
    pid = _proposal(agents, "alpha", tag)
    with db._conn(immediate=True) as conn:
        _link(conn, pid, pr_number, alpha)
        fid = _finding(conn, pid, who, pr_number, auto_flip=auto_flip)
        if self_resolve:
            # The finder is also an authorized fixer resolving their OWN
            # finding - the shape a public-branch fixer can reach.
            db.finding_mark_resolved(conn, fid, who, "self", (who,))
        else:
            db.finding_mark_resolved(conn, fid, alpha, "shipped", ())
    return pid, fid


def _candidate_ids(agent_id):
    with db._conn() as conn:
        return [f["finding_id"] for f in db.resolved_finding_candidates(conn, agent_id)]


def test_rereview_candidate_is_the_finder_after_a_real_resolve():
    """The population pin, and the fail-before: delete the query in
    db.resolved_finding_candidates and this reds, because the set is
    asserted NON-EMPTY for a resolve driven through the real writers."""
    delta = AGENTS["delta"]["agent_id"]
    _pid, fid = _resolved_finding(AGENTS, "rrcand", 6101, finder="delta")
    ids = _candidate_ids(delta)
    assert fid in ids, f"the finder is not a candidate after a real resolve: {ids}"
    with db._conn() as conn:
        row = conn.execute(
            "SELECT pr_number FROM review_findings WHERE id = ?", (fid,)
        ).fetchone()
    assert row["pr_number"] == 6101, row["pr_number"]


def test_rereview_skips_a_verified_finding():
    """Once a third party verifies, finding_verify already told the finder
    - by auto-flip if they consented, by advisory nudge if not. A second
    notification for a delivered fact is noise."""
    delta = AGENTS["delta"]["agent_id"]
    gamma = AGENTS["gamma"]["agent_id"]
    _pid, fid = _resolved_finding(AGENTS, "rrver", 6102, finder="delta")
    with db._conn(immediate=True) as conn:
        db.finding_verify(conn, fid, gamma, _SHA)
    assert fid not in _candidate_ids(delta), "a verified finding must not re-wake"


def test_rereview_skips_a_finder_already_at_plus_one():
    _pid, fid = _resolved_finding(AGENTS, "rrplus1", 6103, finder="delta")
    db.vote_on_pr(AGENTS["delta"]["token"], 6103, 1)
    assert fid not in _candidate_ids(AGENTS["delta"]["agent_id"]), (
        "a finder who has already said yes needs no poke"
    )


def test_rereview_skips_a_finder_who_resolved_their_own_finding():
    _pid, fid = _resolved_finding(
        AGENTS, "rrself", 6104, finder="delta", self_resolve=True
    )
    assert fid not in _candidate_ids(AGENTS["delta"]["agent_id"]), (
        "they fixed it themselves, they know"
    )


def test_rereview_advisory_finding_also_arms():
    """The population decision, pinned. reviewer_blockers counts only
    auto_flip = 1, so an advisory finding is the one the verify-time
    nudge can NEVER reach. Gating the wake on auto_flip as well would
    leave exactly these reviewers in permanent silence - which is the
    bug this PR exists to close, repeated in new code."""
    _pid, fid = _resolved_finding(
        AGENTS, "rradv", 6105, finder="delta", auto_flip=False
    )
    assert fid in _candidate_ids(AGENTS["delta"]["agent_id"]), (
        "an advisory finding must still arm the re-review wake"
    )


def test_rereview_prompt_never_invites_the_refused_call():
    """`finding_verify` refuses verifier == finder, so telling this
    citizen to verify their own finding invites a call guaranteed to be
    refused. The prompt says re-cast, and carries no finding text."""
    prompt = wake.build_fix_resolved_prompt(6106, [7, 9])
    assert "finding_verify" not in prompt, prompt
    assert "re-cast" in prompt, prompt
    assert "6106" in prompt and "#7" in prompt and "#9" in prompt, prompt
    assert len(prompt) < 500, len(prompt)
    # An earlier version of this block also asserted
    # `"must never reach a prompt" not in prompt`.  That phrase is fixture
    # text from a docstring in THIS file; it appears in no implementation
    # of the prompt, so the assertion was satisfied by every string the
    # function could ever return and could not fail.  A negative assertion
    # is worth exactly the set of strings it rules out, and that set was
    # empty.  The two negatives kept above are real: an implementation
    # that told the finder to run finding_verify, or that named a
    # verifier, would red them.


def test_rereview_wakes_the_finder_through_the_sweep():
    agents = AGENTS
    epsilon = agents["epsilon"]["agent_id"]
    _pid, fid = _resolved_finding(agents, "rrsweep", 6201, finder="epsilon")
    _only_endpoint(epsilon)
    restore = _wake_cfg()
    real, _ = _stub(_oc_routes())
    real_send = wake.send_wake
    sent = []

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
    mine = [s for s in sent if "6201" in s]
    assert len(mine) == 1, f"exactly one wake for PR 6201, got {sent}"
    assert f"#{fid}" in mine[0], mine[0]
    assert any(
        o.get("direction") == "rereview"
        and o["pr_number"] == 6201
        and o["outcome"] == "sent"
        for o in out
    ), out


def test_rereview_burst_collapses_into_one_wake_naming_all():
    """Three findings resolved on one PR is ONE wake naming three - which
    is why the state table is keyed on the (pr, voter) pair rather than
    the finding, and why a poll-time grouping has to exist at all."""
    agents = AGENTS
    alpha = agents["alpha"]["agent_id"]
    zeta = agents["zeta"]["agent_id"]
    pid = _proposal(agents, "alpha", "rrburst")
    with db._conn(immediate=True) as conn:
        _link(conn, pid, 6202, alpha)
        fids = [_finding(conn, pid, zeta, 6202) for _ in range(3)]
        for f in fids:
            db.finding_mark_resolved(conn, f, alpha, "shipped", ())
    _only_endpoint(zeta)
    restore = _wake_cfg()
    real, _ = _stub(_oc_routes())
    real_send = wake.send_wake
    sent = []

    def _capture(endpoint, session_id, text):
        sent.append(text)
        return True

    wake.send_wake = _capture
    try:
        wake.wake_sweep()
    finally:
        wake.send_wake = real_send
        _restore(real)
        restore()
    mine = [s for s in sent if "6202" in s]
    assert len(mine) == 1, f"a 3-finding resolve burst must be ONE wake, got {sent}"
    for f in fids:
        assert f"#{f}" in mine[0], f"finding {f} unnamed in the wake: {mine[0]}"


def test_rereview_wakes_again_for_a_finding_resolved_after_the_first_wake():
    """A delivered pair is closed over the findings it NAMED, not forever.

    The state recorded only "this voter was told", never "told about
    WHICH findings", so a finding resolved AFTER a wake was discarded at
    the caller's `continue` with nothing anywhere recording the loss.  The
    burst pin could not see this, because it resolves every finding
    BEFORE the sweep and so only ever exercises the pre-wake burst.

    The debounce is zeroed here deliberately, and for a reason worth
    naming: with it live, the second wake is correctly DEFERRED for the
    quiet window, which is the designed behaviour and would mask the
    coverage defect behind a debounce arm.  Zeroing it isolates the
    question being asked, which is "is this pair still a candidate at
    all", and the deferral is pinned separately by the retry test.
    """
    agents = AGENTS
    alpha = agents["alpha"]["agent_id"]
    zeta = agents["zeta"]["agent_id"]
    pid = _proposal(agents, "alpha", "rrafter")
    with db._conn(immediate=True) as conn:
        _link(conn, pid, 6301, alpha)
        # BOTH findings exist before the first sweep, and the LATER-FILED one
        # (the higher id) is resolved FIRST.  That order is the whole point.
        #
        # The first version of this test created the second finding AFTER the
        # first sweep, so `second.id > first.id` held by construction - it
        # only ever exercised ascending order, which is the ONE order in which
        # a high-water mark is a valid stand-in for a set.  Finding ids are
        # assigned when a finding is FILED, so an author who fixes a
        # later-filed finding before an earlier-filed one is ordinary, and
        # under `id > covered` the second resolve is discarded with no
        # outcome row, no log line and no ledger entry.  64 tests and a 5/5
        # green CI all agreed the old shape was fine.
        low = _finding(conn, pid, zeta, 6301)
        high = _finding(conn, pid, zeta, 6301)
        assert high > low, f"the fixture did not produce two ordered ids: {low}, {high}"
        db.finding_mark_resolved(conn, high, alpha, "shipped", ())
    _only_endpoint(zeta)
    restore = _wake_cfg(AGENT_WAKE_DEBOUNCE_SECONDS=0)
    real, _ = _stub(_oc_routes())
    real_send = wake.send_wake
    sent = []

    def _capture(endpoint, session_id, text):
        sent.append(text)
        return True

    wake.send_wake = _capture
    try:
        wake.wake_sweep()
        first_wake = [s for s in sent if "6301" in s]
        assert len(first_wake) == 1, sent
        assert f"#{high}" in first_wake[0], first_wake[0]
        assert f"#{low}" not in first_wake[0], (
            "the first wake named a finding that had not been resolved yet:"
            f" {first_wake[0]}"
        )
        with db._conn(immediate=True) as conn:
            db.finding_mark_resolved(conn, low, alpha, "shipped", ())
        wake.wake_sweep()
    finally:
        wake.send_wake = real_send
        _restore(real)
        restore()
    later = [s for s in sent if "6301" in s]
    assert len(later) == 2, (
        "a finding resolved after the first wake earned no second wake, so"
        " the blocker it names would sit unnoticed - and the drop is silent,"
        f" with no outcome and no ledger row. Sent: {sent}"
    )
    assert f"#{low}" in later[1], (
        f"the second wake did not name the finding that was newly resolved: {later[1]}"
    )
    assert f"#{high}" not in later[1], (
        f"the second wake re-names an already-covered finding: {later[1]}"
    )


def test_rereview_one_finders_wake_does_not_silence_another_on_the_same_pr():
    """The debounce watermark is per PAIR, so two finders on one PR are two
    wakes - and a debounce is never stamped as a terminal rejection.

    Both halves were wrong and together they were a silent loss.  The
    watermark was `MAX(notified_at) ... WHERE pr_number = ?`, scoped
    across every voter, so within a SINGLE sweep voter A's delivery set
    voter B's debounce; and the caller treated the resulting "debounce"
    like any other rejection and called _discard_rereview, whose own
    docstring says it is only for rejections "that will never change".  A
    debounce is the opposite.  So B was retired on a condition that
    expires in 28 minutes, and never woken afterwards.
    """
    agents = AGENTS
    alpha = agents["alpha"]["agent_id"]
    eta = agents["eta"]["agent_id"]
    theta = agents["theta"]["agent_id"]
    pid = _proposal(agents, "alpha", "rrtwofinders")
    with db._conn(immediate=True) as conn:
        _link(conn, pid, 6302, alpha)
        for _who in (eta, theta):
            db.finding_mark_resolved(
                conn, _finding(conn, pid, _who, 6302), alpha, "shipped", ()
            )
    _only_endpoint(eta, theta)
    restore = _wake_cfg(AGENT_WAKE_DEBOUNCE_SECONDS=1800)
    real, _ = _stub(_oc_routes())
    real_send = wake.send_wake
    sent = []

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
    mine = [s for s in sent if "6302" in s]
    assert len(mine) == 2, (
        "two finders with a resolved finding on one PR, one sweep, and only"
        f" {len(mine)} wake(s) - one voter's delivery debounced the other"
        f" out permanently: {sent}"
    )
    assert not any(
        o.get("direction") == "rereview" and o.get("outcome") == "debounce" for o in out
    ), (
        "a candidate hit the debounce inside its own first sweep, which can"
        f" only mean another voter's delivery set its watermark: {out}"
    )


def test_rereview_deferred_wake_stays_retryable():
    """A busy session must not lose the pair. The seen-set is 'was
    DELIVERED', never 'row exists' - the exact bug the inbound
    _delivered docstring records, which a fresh table would repeat."""
    agents = AGENTS
    gamma = agents["gamma"]["agent_id"]
    _pid, fid = _resolved_finding(agents, "rrretry", 6203, finder="gamma")
    _only_endpoint(gamma)
    restore = _wake_cfg()
    real, _ = _stub(_oc_routes())
    real_busy = wake.session_busy
    wake.session_busy = lambda e, s: True
    try:
        out = wake.wake_sweep()
    finally:
        wake.session_busy = real_busy
        _restore(real)
        restore()
    assert any(
        o.get("pr_number") == 6203 and o.get("outcome") == "busy" for o in out
    ), f"the sweep did not act on PR 6203 at all: {out}"
    with db._conn() as conn:
        row = conn.execute(
            "SELECT notified_at FROM agent_wake_rereview"
            " WHERE pr_number = 6203 AND voter_id = ?",
            (gamma,),
        ).fetchone()
    assert row is not None, "no watermark row was written at all"
    assert row["notified_at"] is None, (
        "a deferred wake was marked delivered and is now lost forever"
    )


def test_rereview_debounce_defers_without_stamping_the_pair():
    """A debounce is a TEMPORARY gate, so it must not write `notified_at`.

    MiMo's finding #51, and the mutation is the whole receipt: changing
    the branch condition to a False-arm - `if False and reason ==
    "debounce":` on the literal of the day, `if False and reason in
    _FREE_GATE_DEFERRABLE:` now - lets a debounce fall through to
    `_mark_rereview_seen(notified=False)`
    plus `_discard_rereview` - and 268/268 files still passed,
    `test_agent_wake.py` included. Every debounce assertion in the suite
    proved the branch did NOT fire (`assert not any(outcome ==
    "debounce")`) and the one nonzero-window test zeroed the window to get
    past it, so no test in the file ever REACHED the branch.

    Three arms, because each alone is satisfiable by the wrong thing:

    1. the branch FIRES - a debounced sweep reports `outcome == "debounce"`
       for the PR, so the `if False` mutation is caught at the seam rather
       than inferred from its side effects;
    2. it does not STAMP - `notified_at` is byte-identical to the delivery
       that preceded it. This is the arm that catches the discard: the
       discard's whole documented job is to write that column, so if the
       value is unchanged, nothing recorded the deferral as terminal;
    3. it RECOVERS - with the window opened again the pair delivers the
       very finding it deferred on, and names it. So arm 2 is not satisfied
       by the pair having been retired outright.
    """
    agents = AGENTS
    zeta = agents["zeta"]["agent_id"]
    pid = _proposal(agents, "alpha", "rrdeb")
    with db._conn(immediate=True) as conn:
        _link(conn, pid, 6204, agents["alpha"]["agent_id"])
        first = _finding(conn, pid, zeta, 6204)
        db.finding_mark_resolved(
            conn, first, agents["alpha"]["agent_id"], "shipped", ()
        )
    _only_endpoint(zeta)
    # Delivery one: window open, so this is a real send and a real stamp.
    open_window = _wake_cfg(AGENT_WAKE_DEBOUNCE_SECONDS=0)
    real, _ = _stub(_oc_routes())
    try:
        wake.wake_sweep()
        with db._conn() as conn:
            delivered_at = conn.execute(
                "SELECT notified_at FROM agent_wake_rereview"
                " WHERE pr_number = 6204 AND voter_id = ?",
                (zeta,),
            ).fetchone()
        assert delivered_at is not None and delivered_at["notified_at"], (
            "the first delivery did not stamp the pair, so the debounce arm"
            " below would pass for the wrong reason"
        )
        first_stamp = delivered_at["notified_at"]
    finally:
        _restore(real)
        open_window()
    # Now a SECOND finding lands, and the window is shut.
    with db._conn(immediate=True) as conn:
        second = _finding(conn, pid, zeta, 6204)
        db.finding_mark_resolved(
            conn, second, agents["alpha"]["agent_id"], "shipped", ()
        )
    shut = _wake_cfg(AGENT_WAKE_DEBOUNCE_SECONDS=1800)
    real, _ = _stub(_oc_routes())
    try:
        held = wake.wake_sweep()
    finally:
        _restore(real)
        shut()
    assert any(
        o.get("pr_number") == 6204 and o.get("outcome") == "debounce" for o in held
    ), (
        "the debounce branch did not fire, so a sweep that discards instead"
        f" of deferring is indistinguishable here: {held}"
    )
    with db._conn() as conn:
        after = conn.execute(
            "SELECT notified_at FROM agent_wake_rereview"
            " WHERE pr_number = ? AND voter_id = ?",
            (6204, zeta),
        ).fetchone()
    assert after["notified_at"] == first_stamp, (
        "a debounce rewrote notified_at ("
        f"{first_stamp} -> {after['notified_at']}), which is the terminal"
        " stamp _discard_rereview exists to write. A deferral recorded as a"
        " delivery is a lost wake."
    )
    # Arm 3: it recovers, rather than having been retired outright.
    reopened = _wake_cfg(AGENT_WAKE_DEBOUNCE_SECONDS=0)
    real, _ = _stub(_oc_routes())
    sent = []
    real_send = wake.send_wake

    def _capture(endpoint, session_id, text):
        sent.append(text)
        return True

    wake.send_wake = _capture
    try:
        wake.wake_sweep()
    finally:
        wake.send_wake = real_send
        _restore(real)
        reopened()
    later = [s for s in sent if "6204" in s]
    assert later, f"the deferred pair never recovered: {sent}"
    assert f"#{second}" in later[0], (
        f"the recovering wake did not name the deferred finding: {later[0]}"
    )


def test_rereview_switch_off_silences_only_that_direction():
    """A per-direction switch must not become a poller-wide mute: the
    inbound 'you have new feedback' wake has to keep working."""
    agents = AGENTS
    beta = agents["beta"]["agent_id"]
    delta = agents["delta"]["agent_id"]
    pid = _proposal(agents, "alpha", "rrsw")
    with db._conn(immediate=True) as conn:
        # delta OPENS this PR, so delta is the inbound candidate, and
        # beta's resolved finding on it is beta's OUTBOUND candidate.
        _link(conn, pid, 6110, delta)
        _finding(conn, pid, agents["gamma"]["agent_id"], 6110)
        fid = _finding(conn, pid, beta, 6110)
        db.finding_mark_resolved(conn, fid, delta, "shipped", ())
    # Both endpoints are enabled on purpose: beta carries a large dirty
    # outbound population from the pre-existing tests, so with the switch
    # off the re-review direction has real candidates available and must
    # still send nothing. Registering a clean agent here instead would
    # make the assertion true for the wrong reason.
    _only_endpoint(delta, beta)
    restore = _wake_cfg()
    saved = config.AGENT_WAKE_REREVIEW_ENABLED
    config.AGENT_WAKE_REREVIEW_ENABLED = 0
    real, _ = _stub(_oc_routes())
    real_send = wake.send_wake
    sent = []

    def _capture(endpoint, session_id, text):
        sent.append(text)
        return True

    wake.send_wake = _capture
    try:
        out = wake.wake_sweep()
    finally:
        wake.send_wake = real_send
        _restore(real)
        config.AGENT_WAKE_REREVIEW_ENABLED = saved
        restore()
    assert not any(o.get("direction") == "rereview" for o in out), out
    assert not any("finding resolved" in s for s in sent), sent
    assert any(o["outcome"] == "sent" for o in out), (
        f"the inbound direction was silenced too: {out}"
    )
    # Names PR 6110 specifically, because the assertion above only proves
    # that SOME inbound wake went out and a different PR would satisfy it.
    # The earlier form filtered the list on "6110" and then asserted 6110
    # was in the filtered list - a tautology that raised IndexError instead
    # of failing with its own message when nothing named 6110.
    assert any("6110" in s for s in sent), sent


def test_old_schema_database_regains_the_rereview_table():
    """Class-2 migration pin, and a real UPGRADE rather than a fresh
    build: a database that already exists WITHOUT the table must regain
    it through init_db(). db/_core/_init.py:40 runs
    conn.executescript(schema.sql) on every boot, so
    CREATE TABLE IF NOT EXISTS is the whole migration - but that is a
    claim about a line number, and this is the check that would catch the
    line moving. Subprocess, so the shared session DB this file's other
    tests rely on is never repointed."""
    import subprocess

    probe = _TMP / "old_schema_rereview"
    probe.mkdir(parents=True, exist_ok=True)
    repo_root = str(Path(__file__).resolve().parent.parent)
    script = "\n".join(
        [
            "import sys",
            f"sys.path.insert(0, {repo_root!r})",
            "import db",
            "db.init_db()",
            "with db._conn() as c:",
            "    c.execute('DROP TABLE agent_wake_rereview')",
            "db.init_db()",
            "with db._conn() as c:",
            "    rows = c.execute("
            "\"SELECT name FROM sqlite_master WHERE type='table'"
            " AND name = 'agent_wake_rereview'\").fetchall()",
            "    print('REGAINED' if rows else 'MISSING')",
        ]
    )
    env = dict(os.environ)
    env["FORUM_DB_PATH"] = str(probe / "old.db")
    env["AGENTLAND_DATA_DIR"] = str(probe)
    env.pop("FORUM_AGENTLAND_SESSION", None)
    out = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    assert "REGAINED" in out.stdout, out.stdout + out.stderr[-2000:]


def main():
    tests = [
        test_correction_occupancy_is_last_assistant_not_cumulative,
        test_occupancy_passes_limit_so_urlopen,
        test_correction_limit_falls_back_to_opencode_json,
        test_correction_limit_default_is_logged_not_guessed,
        test_limit_prefers_api_model_when_present,
        test_correction_selection_skips_subagent_children,
        test_the_title_gate_beats_recency,
        test_a_created_session_is_named_so_the_gate_does_not_orphan_it,
        test_the_title_gate_accepts_both_named_forms,
        test_the_gate_can_be_switched_off_and_then_recency_is_whole,
        test_no_named_chat_is_its_own_answer_not_a_silent_miss,
        test_an_empty_workspace_does_not_claim_the_named_answer,
        test_a_nameless_chat_defers_the_wake_rather_than_burning_it,
        test_selection_honours_session_max_age,
        test_gate_free_rejections,
        test_gate_debounce_window_edge,
        test_prompt_carries_no_finding_text,
        test_derive_directory_follows_convention,
        test_disabled_switch_is_a_no_op,
        test_sweep_burst_collapses_to_one_wake,
        test_sweep_skips_resolved_during_debounce,
        test_the_deferral_set_is_one_value_both_arms_read,
        test_debounce_keeps_the_finding_a_candidate,
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
        test_deferred_wake_is_retried_on_the_next_tick,
        test_quiet_hours_does_not_consume_the_burst,
        test_self_filed_finding_does_not_arm_the_debounce,
        test_quiet_hours_and_budget_cost_no_http,
        test_wake_defers_when_context_is_genuinely_full,
        test_every_deferral_reason_lands_in_the_ledger,
        test_unreadable_occupancy_and_busy_status_fail_closed,
        test_a_banned_or_suspended_citizen_is_not_deliverable,
        test_a_directory_that_escapes_the_tree_is_refused,
        test_a_disabled_endpoint_is_invisible_to_the_deliverable_readers,
        test_correction_no_root_session_creates_nothing_by_default,
        test_correction_create_session_is_opt_in,
        test_correction_five_matches_the_directory_itself,
        test_directory_match_is_not_a_basename_match,
        test_directory_match_survives_quotes_and_separators,
        test_directory_match_is_case_insensitive,
        test_a_bare_convention_name_matches_nothing,
        test_a_row_without_a_location_never_matches,
        test_a_blank_directory_fails_closed,
        test_session_list_asks_for_no_server_side_filter,
        test_an_unreadable_session_list_logs_its_own_tag,
        test_rereview_candidate_is_the_finder_after_a_real_resolve,
        test_rereview_skips_a_verified_finding,
        test_rereview_skips_a_finder_already_at_plus_one,
        test_rereview_skips_a_finder_who_resolved_their_own_finding,
        test_rereview_advisory_finding_also_arms,
        test_rereview_prompt_never_invites_the_refused_call,
        test_rereview_wakes_the_finder_through_the_sweep,
        test_rereview_burst_collapses_into_one_wake_naming_all,
        test_rereview_wakes_again_for_a_finding_resolved_after_the_first_wake,
        test_rereview_one_finders_wake_does_not_silence_another_on_the_same_pr,
        test_rereview_deferred_wake_stays_retryable,
        test_rereview_debounce_defers_without_stamping_the_pair,
        test_rereview_switch_off_silences_only_that_direction,
        test_old_schema_database_regains_the_rereview_table,
        test_main_registers_every_test_in_this_module,
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
