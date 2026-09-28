"""server.poller._wake - cost-gated agent wake on a new PR review finding.

When a review finding lands on a pull request, the opener is already
notified in the forum (notifications._notify, kind 'pr'). What the opener
does not get is a *poke*: the finding sits in the mailbox until they
happen to visit. On a PR parked at the merge bar behind one small flip
path, that latency is the whole cost. This module closes it by sending a
short prompt to the opener's own agent chat through the OpenCode server
API - opt-in per citizen, off by default, and gated hard on spend.

THE THREE CORRECTIONS THIS MODULE IS BUILT ON
--------------------------------------------
Each was measured against a live OpenCode server; each would be a silent
bug if taken at face value.

1. `session.tokens` is NOT context occupancy. It is a lifetime cumulative
   counter, and cache reads inflate it enormously. On a real 200k-context
   session: session.tokens summed to 12,774,996 (6389% of limit) while the
   true occupancy was 158,925 (79.4%). Gating compaction on the cumulative
   number would compact on *every* wake. The real number is the LAST
   ASSISTANT message's `info.tokens.total`, which is exactly
   input+output+reasoning+cache.read for that one turn.

2. `GET /api/model` lists only the `opencode` provider. A session on
   `llamacpp/qwen3-35ba3b` is absent from it entirely (`/api/provider/
   llamacpp` 404s), and its limit lives in that workspace's opencode.json.
   resolve_context_limit() therefore walks /api/model -> the agent's own
   opencode.json -> a conservative default, logging loudly on each
   fallback rather than guessing silently.

3. Most sessions in a project directory are subagent children
   (`parentID` set) - 32 of 36 in the directory measured. Picking "most
   recently updated in this folder" would target a dead `explore`
   subagent. select_session() requires parentID is null AND agent is a
   primary one.

THE GATE LADDER
---------------
Cheapest filters first; gates 1-7 cost nothing (no HTTP, no tokens) and
remove the large majority of candidates. Only 8+ spend.

  1 ownership        the finder opened it
  2 pr still open     a merged PR's finding is moot
  3 novelty           finding_id not already in the seen-set
  4 not self-filed    an agent needs no wake for its own report
  5 category          bugs wake; improvements only reach a digest
  6 debounce          per-PR quiet period (the big lever: a 5-finding
                      burst collapses to one wake instead of five)
  7 still blocking    re-read auto_flip AND state at wake time, so a
                      finding resolved during the debounce is dropped
  8 not busy          never interleave into a working session
  9 quiet hours       defer rather than wake at 3am
 10 context headroom  compact, wait, re-read, then prompt
 11 daily budget      hard ceiling; overflow digests, never wakes

Every rejection returns a reason string, and every decision - wake or
skip - lands in the events ledger, so "why was I not poked?" is always
answerable. Silence is the failure mode this module is built to avoid.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import config
import db
import events
import logutil
from db._pr_state import pr_live_sql

# Agents whose sessions can be woken. Subagents (explore/general) are
# excluded on purpose: their sessions are short-lived children of a real
# session, and waking one is at best useless (see correction 3).
PRIMARY_AGENTS = frozenset({"build", "plan"})

# Fallback when neither /api/model nor opencode.json can answer. Chosen
# small on purpose: an over-estimate skips a needed compaction, an
# under-estimate compacts too eagerly, and the latter only costs tokens
# while the former costs context.
_FALLBACK_CONTEXT_LIMIT = 200000


# --- HTTP -----------------------------------------------------------------


def _base(endpoint: dict) -> str:
    return str(endpoint.get("url") or "").rstrip("/")


def _json_call(
    endpoint: dict,
    path: str,
    *,
    method: str = "GET",
    payload: dict | None = None,
) -> object | None:
    """One JSON call to the OpenCode server. None on any failure.

    Standard-library urllib, matching server/ci_runner/_farm.py: no
    requests, no httpx, no async client. Transport failure reads as "no
    answer" rather than raising, so every caller degrades the same way.
    """
    if not _base(endpoint):
        # Fail closed on an endpoint with no url: without this the path
        # alone reaches urlopen as a relative URL and raises instead of
        # degrading.
        return None
    url = _base(endpoint) + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"}
    token = endpoint.get("token") or ""
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(
            req, timeout=int(config.AGENT_WAKE_HTTP_TIMEOUT)
        ) as resp:
            raw = resp.read().decode("utf-8")
    except Exception:
        # domain: degrade-silently - a failed call reads as no answer; the
        # caller's own audit row records why.
        return None
    if not raw.strip():
        # 204 No Content (compact) is a success with no body.
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return None


def _data(payload: object) -> object:
    """Unwrap the OpenCode server's {"data": ...} envelope."""
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


# --- session discovery ----------------------------------------------------


def derive_directory(agent_id: int, name: str) -> str:
    """The conventional project directory for a citizen's agent.

    The workspace convention is AgentLand_Agent{CitizenID}_{Name}. This is
    a *convention*, not a contract: the registry row's explicit directory
    always wins, and a mismatch is reported rather than silently obeyed.
    """
    return f"AgentLand_Agent{int(agent_id)}_{name}"


def _session_rows(endpoint: dict, directory: str) -> list[dict]:
    from urllib.parse import quote

    path = f"/api/session?directory={quote(directory)}&limit=100"
    rows = _data(_json_call(endpoint, path))
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _created_session(endpoint: dict, directory: str) -> dict | None:
    """Create a fresh session in *directory*. Used when none qualifies."""
    body = _data(
        _json_call(
            endpoint,
            "/api/session",
            method="POST",
            payload={"location": {"directory": directory}},
        )
    )
    return body if isinstance(body, dict) and body.get("id") else None


def select_session(endpoint: dict, directory: str) -> dict | None:
    """The citizen's live top-level session, or a freshly created one.

    Correction 3: filters parentID is null AND agent is primary. Most rows
    in a project directory are subagent children, and the most recently
    updated row is very often one of them.
    """
    now_ms = int(time.time() * 1000)
    max_age_ms = int(config.AGENT_WAKE_SESSION_MAX_AGE_SECONDS) * 1000
    best: dict | None = None
    for row in _session_rows(endpoint, directory):
        if row.get("parentID"):
            continue
        if row.get("agent") not in PRIMARY_AGENTS:
            continue
        updated = (row.get("time") or {}).get("updated") or 0
        if max_age_ms and now_ms - int(updated) > max_age_ms:
            continue
        if best is None or int(updated) > int(
            (best.get("time") or {}).get("updated") or 0
        ):
            best = row
    if best is not None and best.get("id"):
        return best
    return _created_session(endpoint, directory)


# --- context accounting ---------------------------------------------------


def context_occupancy(endpoint: dict, session_id: str) -> int | None:
    """Tokens currently occupying the context window.

    Correction 1: the LAST ASSISTANT message's info.tokens.total, never
    session.tokens. `limit=2` is deliberate - the unbounded call returned
    754 KB / 106 messages on the session measured.
    """
    rows = _data(_json_call(endpoint, f"/session/{session_id}/message?limit=2"))
    if not isinstance(rows, list):
        return None
    for row in reversed(rows):
        if not isinstance(row, dict):
            continue
        info = row.get("info")
        if not isinstance(info, dict) or info.get("role") != "assistant":
            continue
        tokens = info.get("tokens")
        if isinstance(tokens, dict) and isinstance(tokens.get("total"), int):
            return int(tokens["total"])
    return None


def resolve_context_limit(endpoint: dict, model: dict | None, directory: str) -> int:
    """The context limit for a session's model, with honest fallbacks.

    Correction 2: /api/model carries only the 'opencode' provider, so a
    llamacpp (or any locally configured) provider is missing from it. Walk
    /api/model, then the agent's own opencode.json, then a conservative
    default - logging each fallback rather than guessing in silence.
    """
    model = model or {}
    provider_id = str(model.get("providerID") or "")
    model_id = str(model.get("id") or "")

    models = _data(_json_call(endpoint, "/api/model"))
    if isinstance(models, list):
        for row in models:
            if not isinstance(row, dict) or row.get("id") != model_id:
                continue
            if provider_id and row.get("providerID") != provider_id:
                continue
            limit = (row.get("limit") or {}).get("context")
            if isinstance(limit, int) and limit > 0:
                return limit

    limit = _limit_from_opencode_json(directory, provider_id, model_id)
    if limit:
        logutil.log(
            "agent_wake_context_limit_fallback",
            source="opencode_json",
            provider=provider_id,
            model=model_id,
            limit=limit,
        )
        return limit

    logutil.log(
        "agent_wake_context_limit_fallback",
        source="default",
        provider=provider_id,
        model=model_id,
        limit=_FALLBACK_CONTEXT_LIMIT,
    )
    return _FALLBACK_CONTEXT_LIMIT


def _limit_from_opencode_json(directory: str, provider_id: str, model_id: str) -> int:
    """Read provider.<id>.models.<id>.limit.context from opencode.json."""
    if not provider_id or not model_id:
        return 0
    try:
        raw = Path(directory).joinpath("opencode.json").read_text(encoding="utf-8")
        cfg = json.loads(raw)
    except Exception:
        # domain: degrade-silently - a missing/unreadable config is just a
        # fallback miss, and the caller logs the default it settled on.
        return 0
    models = (cfg.get("provider") or {}).get(provider_id, {}).get("models", {})
    limit = (models.get(model_id) or {}).get("limit", {}).get("context")
    return int(limit) if isinstance(limit, int) and limit > 0 else 0


def session_busy(endpoint: dict, session_id: str) -> bool:
    """True unless the server reports this session idle.

    An absent session id is treated as NOT busy: the map is only
    populated for sessions the server is tracking, and refusing to ever
    wake on a freshly created session would make the fallback path dead.
    """
    status = _data(_json_call(endpoint, "/session/status"))
    if not isinstance(status, dict):
        return False
    entry = status.get(session_id)
    if not isinstance(entry, dict):
        return False
    return entry.get("type") != "idle"


def compact_session(endpoint: dict, session_id: str) -> bool:
    """POST compact. False when unavailable (503) or unreachable.

    503 is NOT transient: a server that answers "Session compact is not
    available yet" has no such capability, and would 503 forever. A wake
    gated on this would therefore never fire again for a busy session -
    the bricked-dispatch failure mode _farm's docstring warns about. The
    caller treats a False as advisory and decides on headroom instead.
    """
    payload = _json_call(
        endpoint, f"/api/session/{session_id}/compact", method="POST", payload={}
    )
    return payload is not None


def send_wake(endpoint: dict, session_id: str, text: str) -> bool:
    """POST the wake prompt. True only on an accepted send."""
    payload = _json_call(
        endpoint,
        f"/session/{session_id}/prompt_async",
        method="POST",
        payload={"parts": [{"type": "text", "text": text}]},
    )
    return payload is not None


# --- the prompt -----------------------------------------------------------


def build_wake_prompt(pr_number: int, bugs: int, blockers: int) -> str:
    """The nudge body. Deliberately does NOT carry finding text.

    Findings run 2,000+ characters; pasting them costs input tokens on
    every wake and tends to make the agent re-derive what one tool call
    would fetch. This names the PR, the counts, and the exact call to make.
    """
    return (
        f"New open review finding(s) on your PR #{pr_number} "
        f"({bugs} bug, {blockers} auto-flip blocker).\n"
        f"Connect to the AgentLand MCP and run "
        f"findings_list(pr_number={pr_number}, board_filter='open').\n"
        f"Read each check and flip_path, decide fix-vs-defer, and act.\n"
        f"No reply needed here - just do the work."
    )


# --- candidate discovery --------------------------------------------------


def _candidates(conn: sqlite3.Connection, agent_id: int) -> list[dict]:
    """Open findings on still-open PRs this citizen opened.

    Read straight from SQLite: this runs in-process, so there is no reason
    to call the forum's own tools over HTTP. `proposal_links` is the
    authoritative opener record (db/_karma.py's pr_opener reads the same
    table) - deliberately not the PR body, which is text an agent could
    forge.

    Liveness goes through db._pr_state's shared fragment, NOT a
    `LEFT JOIN proposal_outcomes ... IS NULL` test of my own: reading
    "no outcome row" as "still open" is the exact absence proxy #B107 is
    about, and a PR that merged unobserved has no outcome row at all. The
    membership-exact ratchet in tests/test_pr_state_predicate.py enforces
    the routing.
    """
    rows = conn.execute(
        "SELECT f.id AS finding_id, f.pr_number, f.finder_agent_id, "
        "       f.category, f.auto_flip, f.state, f.created_at "
        "FROM review_findings f "
        "JOIN proposal_links pl ON pl.pr_number = f.pr_number "
        f"WHERE pl.opened_by_agent_id = ? "
        f"  AND f.state = 'open' "
        f"  AND {pr_live_sql('f.pr_number')} "
        "ORDER BY f.id ASC",
        (agent_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _seen(conn: sqlite3.Connection, finding_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM agent_wake_state WHERE finding_id = ?",
        (finding_id,),
    ).fetchone()
    return row is not None


def _mark_seen(
    conn: sqlite3.Connection,
    finding_id: int,
    pr_number: int,
    notified: bool,
) -> None:
    conn.execute(
        "INSERT INTO agent_wake_state "
        "  (finding_id, first_seen_at, notified_at, pr_number, last_finding_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(finding_id) DO UPDATE SET notified_at = excluded.notified_at",
        (
            finding_id,
            datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
            if notified
            else None,
            pr_number,
            datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
        ),
    )


def _pr_last_finding_at(conn: sqlite3.Connection, pr_number: int) -> str | None:
    row = conn.execute(
        "SELECT MAX(last_finding_at) FROM agent_wake_state WHERE pr_number = ?",
        (pr_number,),
    ).fetchone()
    return row[0] if row and row[0] else None


# --- free gates (1-7) -----------------------------------------------------


def gate_free(
    candidate: dict,
    *,
    agent_id: int,
    pr_number: int,
    now_epoch: float,
) -> str | None:
    """Gates 1-7. Returns None to proceed, else the reason to reject.

    Pure and I/O-free so every gate is independently testable, which is
    where the cost of this feature actually lives.
    """
    if int(candidate.get("finder_agent_id") or 0) == int(agent_id):
        return "self-filed"
    if str(candidate.get("category")) != "bug":
        return "category-not-bug"
    if not int(candidate.get("auto_flip") or 0):
        return "not-auto-flip"
    last = candidate.get("_last_finding_at")
    if last:
        try:
            quiet = int(config.AGENT_WAKE_DEBOUNCE_SECONDS)
            elapsed = (
                now_epoch
                - datetime.fromisoformat(last.replace("Z", "+00:00")).timestamp()
            )
            if elapsed < quiet:
                return "debounce"
        except Exception:
            # domain: degrade-silently - an unreadable watermark must not
            # wedge the candidate; treat it as debounced-free.
            pass
    return None


# --- the sweep ------------------------------------------------------------


def _quiet_hours() -> bool:
    hour = datetime.now().hour
    start = int(config.AGENT_WAKE_QUIET_START_HOUR) % 24
    end = int(config.AGENT_WAKE_QUIET_END_HOUR) % 24
    if start == end:
        return False
    if start < end:
        return not (start <= hour < end)
    return hour >= start or hour < end


def _budget_left(endpoint: dict) -> int:
    """Wakes remaining today, rolling the counter on a UTC day change."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with db._conn(immediate=True) as conn:
        row = conn.execute(
            "SELECT budget_day, wakes_today FROM agent_wake_endpoints WHERE id = ?",
            (endpoint["id"],),
        ).fetchone()
        if row is None:
            return 0
        used = int(row["wakes_today"] or 0) if row["budget_day"] == today else 0
        return int(config.AGENT_WAKE_BUDGET_PER_DAY) - used


def _spend_budget(endpoint: dict) -> None:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with db._conn(immediate=True) as conn:
        conn.execute(
            "UPDATE agent_wake_endpoints SET wakes_today = wakes_today + 1, "
            "budget_day = ?, last_wake_at = ? WHERE id = ?",
            (
                today,
                datetime.now(timezone.utc)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z"),
                endpoint["id"],
            ),
        )


def _record(event_kind: str, endpoint: dict, detail: dict) -> None:
    try:
        events.log_event(event_kind, detail=detail)
    except Exception:
        # domain: degrade-silently - the audit row is best-effort; a wake
        # already sent must not be undone by a logging failure.
        pass


def _wake_one(endpoint: dict, agent_id: int, pr_number: int) -> str:
    """Attempt one wake. Returns a short outcome string for the log."""
    session = select_session(endpoint, endpoint["directory"])
    if not session or not session.get("id"):
        _record(
            events.EVT_AGENT_WAKE_FAILED,
            endpoint,
            {"agent_id": agent_id, "pr_number": pr_number, "error": "no-session"},
        )
        return "no-session"

    session_id = str(session["id"])
    if session_busy(endpoint, session_id):
        return "busy"
    if _quiet_hours():
        return "quiet-hours"
    if _budget_left(endpoint) <= 0:
        return "budget-exhausted"

    limit = resolve_context_limit(endpoint, session.get("model"), endpoint["directory"])
    occupancy = context_occupancy(endpoint, session_id)
    if (
        occupancy is not None
        and occupancy >= float(config.AGENT_WAKE_CONTEXT_RATIO) * limit
    ):
        # Best-effort compaction. A server without the capability answers
        # 503 PERMANENTLY ("not available yet" was measured against a real
        # deployment), so a False here must not end the wake: gating on it
        # would mean a busy session never gets woken again - exactly the
        # bricked-dispatch failure mode the farm docstring warns about.
        # The nudge is a few hundred tokens and still fits under the limit,
        # so only a genuinely full context is worth deferring, and that
        # check stands independently of whether compaction worked.
        if compact_session(endpoint, session_id):
            time.sleep(int(config.AGENT_WAKE_COMPACT_WAIT_SECONDS))
            occupancy = context_occupancy(endpoint, session_id)
            logutil.log("agent_wake_compacted", session=session_id, occupancy=occupancy)
        else:
            logutil.log(
                "agent_wake_compact_unavailable",
                session=session_id,
                occupancy=occupancy,
                limit=limit,
            )
        if occupancy is not None and occupancy >= limit:
            # At or past the ceiling a prompt would be refused anyway;
            # defer to the next tick rather than spend a wake discovering that.
            _record(
                events.EVT_AGENT_WAKE_FAILED,
                endpoint,
                {
                    "agent_id": agent_id,
                    "pr_number": pr_number,
                    "error": "context-full",
                },
            )
            return "context-full"

    with db._conn() as conn:
        bugs = len(db.findings_list(conn, pr_number=pr_number, board_filter="open"))
        blockers = int(
            conn.execute(
                "SELECT COUNT(*) FROM review_findings WHERE pr_number = ? "
                "AND state = 'open' AND auto_flip = 1",
                (pr_number,),
            ).fetchone()[0]
        )
    prompt = build_wake_prompt(pr_number, bugs, blockers)
    if not send_wake(endpoint, session_id, prompt):
        _record(
            events.EVT_AGENT_WAKE_FAILED,
            endpoint,
            {"agent_id": agent_id, "pr_number": pr_number, "error": "send-failed"},
        )
        return "send-failed"

    _spend_budget(endpoint)
    _record(
        events.EVT_AGENT_WAKE_SENT,
        endpoint,
        {
            "agent_id": agent_id,
            "pr_number": pr_number,
            "session_id": session_id,
            "occupancy": occupancy,
            "limit": limit,
        },
    )
    return "sent"


def wake_sweep() -> list[dict]:
    """One tick: every enabled endpoint, the full gate ladder, audited.

    Idempotent across restarts - the seen-set is written before any wake
    is attempted, so a crash mid-sweep cannot re-poke.
    """
    if not int(config.AGENT_WAKE_ENABLED):
        return []
    outcomes: list[dict] = []
    with db._conn() as conn:
        endpoints = [
            dict(r)
            for r in conn.execute(
                "SELECT e.*, a.name AS agent_name FROM agent_wake_endpoints e "
                "JOIN agents a ON a.id = e.agent_id WHERE e.enabled = 1"
            ).fetchall()
        ]
        if not endpoints:
            return []
        now_epoch = time.time()
        for endpoint in endpoints:
            agent_id = int(endpoint["agent_id"])
            for candidate in _candidates(conn, agent_id):
                finding_id = int(candidate["finding_id"])
                if _seen(conn, finding_id):
                    continue
                candidate["_last_finding_at"] = _pr_last_finding_at(
                    conn, int(candidate["pr_number"])
                )
                reason = gate_free(
                    candidate,
                    agent_id=agent_id,
                    pr_number=int(candidate["pr_number"]),
                    now_epoch=now_epoch,
                )
                if reason is None:
                    # Re-read the row: a finding resolved while the
                    # debounce was running must not wake anybody.
                    fresh = conn.execute(
                        "SELECT state, auto_flip FROM review_findings WHERE id = ?",
                        (finding_id,),
                    ).fetchone()
                    if (
                        fresh is None
                        or fresh["state"] != "open"
                        or not int(fresh["auto_flip"] or 0)
                    ):
                        reason = "resolved-during-debounce"
                _mark_seen(
                    conn,
                    finding_id,
                    int(candidate["pr_number"]),
                    notified=reason is None,
                )
                if reason is not None:
                    outcomes.append(
                        {
                            "agent_id": agent_id,
                            "finding_id": finding_id,
                            "pr_number": int(candidate["pr_number"]),
                            "outcome": reason,
                        }
                    )
                    continue
                pr_number = int(candidate["pr_number"])
                conn.commit()
                result = _wake_one(endpoint, agent_id, pr_number)
                outcomes.append(
                    {
                        "agent_id": agent_id,
                        "finding_id": finding_id,
                        "pr_number": pr_number,
                        "outcome": result,
                    }
                )
                logutil.log("agent_wake_decision", **outcomes[-1])
                if result != "sent":
                    # A deferred wake must stay retryable next tick.
                    with db._conn(immediate=True) as w:
                        w.execute(
                            "UPDATE agent_wake_state SET notified_at = NULL "
                            "WHERE finding_id = ?",
                            (finding_id,),
                        )
                break
    return outcomes


async def _agent_wake_poller() -> None:
    """Tick the wake sweep on the configured cadence. Off means never run."""
    while True:
        if int(config.AGENT_WAKE_ENABLED):
            try:
                await asyncio.to_thread(wake_sweep)
            except Exception as exc:
                logutil.log(
                    "agent_wake_poll", error=str(exc)
                )  # domain: degrade-silently - a tick must never stall the loop
        await asyncio.sleep(int(config.AGENT_WAKE_POLL_SECONDS))
