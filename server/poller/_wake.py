"""server.poller._wake - cost-gated agent wake on PR review traffic.

TWO directions, and the second exists because the first turned out to be
one-directional (proposal #849):

1. A review finding LANDS on one of the opener's PRs. The opener is
   already notified in the forum (notifications._notify, kind 'pr') but
   does not get a *poke*: the finding sits in the mailbox until they
   happen to visit. On a PR parked at the merge bar behind one small
   flip path, that latency is the whole cost.

2. The opener marks a finding RESOLVED. That link notified NOBODY, so a
   reviewer holding a -1 because of that finding had no way to learn
   that the condition they named had already lifted - and
   docs/review-standards.md makes clearing it a standing duty: "a
   recorded -1 must not outlive the condition it named". This direction
   pokes the FINDER so they can re-read and re-cast.

Both send a short prompt into the citizen's own agent chat through the
OpenCode server API - opt-in per citizen, off by default, and gated hard
on spend. They share the whole gate ladder in _wake_one and differ only
in candidate query, state table, gate set and prompt.

THE FIVE CORRECTIONS THIS MODULE IS BUILT ON
-------------------------------------------
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

4. CREATING A SESSION IS OFF BY DEFAULT. This one is not a server quirk but
   my own review finding, and it is the sharpest of the four. An earlier
   draft fell back to `POST /api/session` whenever no root session
   qualified - which, given correction 3, is a plausible steady state
   rather than an edge case. A misconfigured `directory` therefore created
   one orphan session per eligible finding, forever, and every prompt
   landed in a session the citizen never opens. `AGENT_WAKE_CREATE_SESSION`
   defaults to 0, so a miss defers and is named in the log instead.

5. `GET /api/session` DOES NOT FILTER. This is the one that kept the
   feature dark, and the only correction here whose wrong answer looks
   like a right one. `session.list` documents four ways to narrow the list;
   measured against a live server, ONE of them filters:

     directory=<path>          500  - and its spec promises 400, not 500
     location[directory]=...   200, every row returned  - SILENTLY IGNORED
     location.directory=...    200, every row returned  - SILENTLY IGNORED
     parentID=null             200, every row returned  - SILENTLY IGNORED
     project=global            200, 83 of 84           - the only one

   The three silent cases are the dangerous ones. A 500 is at least loud. A
   filter that returns the whole machine with HTTP 200 is a wrong-answer
   generator, and the caller cannot tell it apart from having worked - so
   "fixing" the query to the nested shape would have handed this poller a
   session out of another citizen's workspace on every wake, silently, with
   nothing red anywhere. `_session_rows` therefore asks for no filter at all
   and `select_session` matches the directory itself, which is also correct
   against a build where these are eventually repaired.

Compaction goes through `POST /session/{id}/summarize`, the AI-summarise
endpoint (measured live: 200 `true` in ~2s, occupancy read back after). It
requires `providerID` + `modelID` in the body. The sibling
`POST /api/session/{id}/compact` answers a PERMANENT 503 "Session compact
is not available yet" on this deployment and is deliberately never called.
Compaction stays best-effort regardless: a failure must not end a wake,
because the nudge is a few hundred tokens and still fits - gating on
compaction would mean a high-occupancy session is never woken again.

POLICY GATES vs PHYSICAL GATES
-----------------------------
The distinction the rest of this module turns on. A POLICY gate is a
preference about when to spend, and can be deliberately bypassed by an
operator who means it. A PHYSICAL gate is a property of the thing being
addressed, and bypassing it does not relax a rule - it breaks a send.

  policy  quiet hours, daily budget   - a manual broadcast skips BOTH, by
                                        operator decision; the automatic
                                        path honours them
  physical busy, context-full,        - a working session must not be
          no-session, not-deliverable,  interleaved into, a full context
          occupancy-unreadable          cannot accept a message, a missing
                                        session is nowhere to send it, a
                                        banned/suspended citizen is not
                                        addressed, and an UNREADABLE
                                        occupancy is not headroom. All of
                                        these fail CLOSED: an unanswered
                                        question is never read as "yes,
                                        safe to send".

THE GATE LADDER
---------------
Cheapest filters first; gates 1-7 cost nothing (no HTTP, no tokens) and
remove the large majority of candidates. Only 8+ spend.

  1 ownership        the finder opened it
  2 pr still open     a merged PR's finding is moot
  3 novelty           finding_id not already delivered
  4 not self-filed    an agent needs no wake for its own report
  5 category          bugs wake; an improvement is skipped      [policy]
  6 debounce          per-PR quiet period (the big lever: a 5-finding
                      burst collapses to one wake instead of five)
  7 still blocking    re-read auto_flip AND state at wake time, so a
                      finding resolved during the debounce is dropped
  8 not busy          never interleave into a working session   [physical]
  9 quiet hours       defer rather than wake at 3am              [policy]
  10 context headroom  compact, wait, re-read, then prompt       [physical]
  11 daily budget      hard ceiling; over it, nothing at all     [policy]

THE DIGEST IS NOT BUILT. Gates 5 and 11 are documented as routing
overflow somewhere, and that "somewhere" does not exist in this code: an
improvement is skipped and an over-budget candidate is skipped, both with
a ledger row and no delivery. The budget ceiling is enforced first, so
nothing can overspend by reaching the digest - the absence cannot leak
money, it can only lose an improvement notification. `AGENT_WAKE_DIGEST`
exists as a documented knob for that future path, and the two docstrings
that used to describe the digest as live have been corrected to say it is
not.

EVERY DECISION IS AUDITED. Every rejection returns a reason string AND
writes an events row, so "why was I not poked?" is answerable by reading the
ledger rather than the container logs. That claim was false for
`quiet-hours`, `budget-exhausted` and `busy` - they returned a reason and
wrote nothing, which are the three questions an operator actually asks. A
DEFERRED outcome writes a failure row and deliberately NO delivery receipt
(`notified_at` stays NULL), because a deferral must leave the finding a
candidate again on the next tick.

ENDPOINT REGISTRY
-----------------
`list_endpoints` / `register_endpoint` / `update_endpoint` /
`remove_endpoint` are the writers for `agent_wake_endpoints`, deliberately
sitting next to their consumer rather than in `db/` - the same choice
`ci_runners` made in server/ci_runner/_farm.py, and for the same reason:
the table is read by a `server/` poller, not by the forum's data surface.
They are what the /admin/agentwake page calls. The bearer token is
write-only: it is never rendered back, and `update_endpoint` treats an
empty token as "leave unchanged" so a form never has to round-trip it.
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
        # 200 `true` (summarize) or 204 No Content is a success with no body.
        return {}
    try:
        return json.loads(raw)
    except Exception:
        # domain: degrade-silently - an unparseable body reads as "no
        # answer", the same failure domain as the transport error above
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


def _norm_dir(value: object) -> str:
    """A directory reduced to the one form two hosts will agree on.

    The registry's stored value and the `location.directory` the server
    reports differ three ways, and a plain string compare misses all three
    SILENTLY - a non-match reads as "this citizen has no session", which is
    exactly the symptom that kept this feature dark.

      - QUOTES. `_check_directory` tests `raw[1] == ":"` for a drive
        letter, so a single leading quote walks an absolute path past the
        guard and is stored with it. That is how every real registry row
        got its value: the quotes are a workaround for the guard refusing
        the only form that works, not a typo.
      - SEPARATORS. `_check_directory` returns "/".join(parts), so the
        stored value carries forward slashes while a Windows server reports
        backslashes.
      - CASE. Windows treats "New folder" and "New Folder" as one
        directory. Both spellings are present in the measured data, and a
        string compare calls them different.

    Deliberately NOT a basename compare. Two real directories on the
    measured host end in the same component - AgentLand_Agent1_CitizenOne
    under both S:/AgentLand_Agents and S:/New folder - so a basename match
    would hand a citizen the stale workspace. One pin holds that line, and
    another holds that a bare convention name matches nothing at all.
    """
    text = str(value or "").strip().strip('"').strip()
    return text.replace("/", "\\").casefold()


def _row_dir(row: dict) -> str:
    """The directory a session belongs to, or "" when it names none.

    A row with no location is unidentifiable, not universal: it must not
    satisfy a request for a specific workspace. `select_session` fails
    closed on that, which is why the pre-existing fixtures had to learn to
    carry a location rather than the gate learning to tolerate their
    absence.
    """
    loc = row.get("location")
    if isinstance(loc, dict):
        return str(loc.get("directory") or "")
    return str(loc or "")


def _session_rows(endpoint: dict) -> list[dict]:
    """Every session the server will admit to knowing.

    No filter is requested, and that is the point - see correction 5. The
    one documented filter that functions is `project`, and it cannot help:
    the measured rows carry projectID `global`, so it does not discriminate
    between citizens. Treating the server as a paginated list is correct
    both today and against a build where the filters are repaired.
    """
    rows = _data(_json_call(endpoint, "/api/session?limit=100"))
    if not isinstance(rows, list):
        # A 500 here once read as "no answer" through `_json_call`'s bare
        # `return None`, and surfaced as `no-session` - which is how a broken
        # query masqueraded as a quiet citizen for a day. A distinct tag is
        # the cheapest tripwire available.
        logutil.log("agent_wake_session_list_unreadable", endpoint=endpoint.get("id"))
        return []
    return [r for r in rows if isinstance(r, dict)]


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
    """The citizen's live top-level session, or None.

    Correction 3: filters parentID is null AND agent is primary. Most rows
    in a project directory are subagent children, and the most recently
    updated row is very often one of them.

    Correction 5: the directory gate is OURS, not the server's - the server
    cannot filter by it. So this is the only thing standing between a wake
    and another citizen's workspace, which is why it fails closed on a
    blank directory instead of treating an unidentifiable row as a
    candidate.

    CREATION IS OFF BY DEFAULT (`AGENT_WAKE_CREATE_SESSION` = 0). Creating a
    session is unbounded: with 32 of 36 rows in a real directory being
    subagent children, "nothing qualifies in the first 100" is a plausible
    steady state, not an edge case - so auto-creating on every miss turned a
    misconfigured `directory` into one orphan session per eligible finding,
    forever, each prompt landing somewhere the citizen never looks. A miss is
    now just a miss: the caller defers, and the sweep log names it. Operators
    who genuinely want creation set the knob.
    """
    now_ms = int(time.time() * 1000)
    max_age_ms = int(config.AGENT_WAKE_SESSION_MAX_AGE_SECONDS) * 1000
    want_dir = _norm_dir(directory)
    if not want_dir:
        # No directory means no workspace, so no row can be in the right
        # one. Failing closed here is what stops a blank registry value
        # from matching every row that happens to carry no location.
        return None
    best: dict | None = None
    for row in _session_rows(endpoint):
        if row.get("parentID"):
            continue
        if row.get("agent") not in PRIMARY_AGENTS:
            continue
        if _norm_dir(_row_dir(row)) != want_dir:
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
    if not int(config.AGENT_WAKE_CREATE_SESSION):
        # ONE SPELLING, THREE PLACES. The outcome string is `no-session`,
        # the ledger `error` is `no-session`, and this log tag is
        # `agent_wake_no_session`. They used to be `no-root-session` in the
        # docstring and config comment, `no-session` as the outcome, and
        # `agent_wake_no_root_session` here - and three spellings of one
        # condition is exactly what makes an audit query silently miss
        # rows, because the reader greps for the name in the docstring.
        logutil.log(
            "agent_wake_no_session",
            directory=directory,
            endpoint=endpoint.get("id"),
        )
        return None
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
    """True unless the server affirmatively reports this session IDLE.

    Two different questions used to collapse into one `return False`, and
    only the second was ever documented:

      1. "the server answered, and does not track this session" - the map
         is only populated for sessions it is tracking. Treating this as
         not-busy is right, and refusing it would make a freshly created
         session permanently unwakeable.
      2. "the server did not answer" - `_json_call` returns None on ANY
         transport failure, so a timeout, a refused connection and a
         truncated body all arrive here. Treating this as not-busy is WRONG
         and it is the reachable one: on a partially-failing server
         `/api/session` answers (so `select_session` picks a session) and
         `/session/status` then times out. Nothing would stop the send
         landing in the middle of the citizen's own turn.

    So the two are separated, and a failed status read fails CLOSED. The
    cost of being wrong in this direction is one deferred wake; the cost of
    being wrong the other way is interjecting a prompt into a live session.
    """
    status = _data(_json_call(endpoint, "/session/status"))
    if status is None:
        # domain: fail-loudly - no answer is not consent. Refusing the
        # wake is the whole point; the caller records the outcome.
        logutil.log(
            "agent_wake_status_unreadable",
            session=session_id,
            endpoint=endpoint.get("id"),
        )
        return True
    if not isinstance(status, dict):
        # An answer that is not the documented map is not an answer we can
        # read. Same direction as above.
        return True
    entry = status.get(session_id)
    if not isinstance(entry, dict):
        return False
    return entry.get("type") != "idle"


def compact_session(endpoint: dict, session_id: str, model: dict | None = None) -> bool:
    """Compact a session's context via AI summarisation.

    `POST /session/{id}/summarize` is the working compaction endpoint
    (measured: 200 `true` in ~2s, occupancy read back after). It REQUIRES
    `providerID` + `modelID` in the body - a body-less call is refused 400
    with `Missing key ["providerID"]` - so the caller's session model is
    passed through, and a session whose model we cannot name is not
    compacted rather than compacted blind.

    Note the sibling `POST /api/session/{id}/compact` answers a PERMANENT
    `503 "Session compact is not available yet"`; that endpoint is a dead
    capability on this deployment and is deliberately not called.

    False means "no compaction happened" - the caller treats that as
    advisory and decides on headroom rather than treating it as fatal.
    """
    model = model or {}
    provider_id = str(model.get("providerID") or "")
    model_id = str(model.get("id") or "")
    if not provider_id or not model_id:
        return False
    payload = _json_call(
        endpoint,
        f"/session/{session_id}/summarize",
        method="POST",
        payload={"providerID": provider_id, "modelID": model_id},
    )
    # A 400 (unprocessable request) raises HTTPError, so it reads as None
    # like any other transport failure - both are "no compaction".
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


def _delivered(conn: sqlite3.Connection, finding_id: int) -> bool:
    """True only if this finding was actually delivered in a wake.

    The seen-set is NOT "row exists" - that is what made a deferred wake
    (busy / quiet-hours / budget / no-session / not-deliverable /
    occupancy-unreadable) permanently lost: the row existed, so the finding
    never became a candidate again. `notified_at` is the delivery receipt,
    and it is what the filter keys on.
    """
    row = conn.execute(
        "SELECT notified_at FROM agent_wake_state WHERE finding_id = ?",
        (finding_id,),
    ).fetchone()
    return row is not None and row["notified_at"] is not None


def _discard(conn: sqlite3.Connection, finding_id: int) -> None:
    """Retire a finding that is permanently not wake-worthy.

    A rejection at the free gates (self-filed, an improvement, not an
    auto-flip blocker, resolved mid-debounce) will never change, so
    stamping it delivered stops it re-entering the candidate scan every
    tick forever. `notified_at` carries an ISO timestamp rather than NULL
    so the row is distinguishable from a deferred one, which is exactly
    the distinction `_delivered` and `_last_delivered_at` now read.
    """
    conn.execute(
        "UPDATE agent_wake_state SET notified_at = ? WHERE finding_id = ?",
        (
            datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            finding_id,
        ),
    )


def _last_delivered_at(conn: sqlite3.Connection, pr_number: int) -> str | None:
    """When this PR was last actually woken about - the debounce watermark.

    Keyed on `notified_at` (a DELIVERY), never on `last_finding_at` (a
    sighting). Using sightings meant a self-filed finding, an improvement
    or a quiet-hours rejection stamped the watermark and suppressed the
    genuine bug finding that followed it, and it meant the whole burst was
    absorbed by the rejection itself.
    """
    row = conn.execute(
        "SELECT MAX(notified_at) FROM agent_wake_state WHERE pr_number = ?",
        (pr_number,),
    ).fetchone()
    return row[0] if row and row[0] else None


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


# --- free gates (1-7) -----------------------------------------------------

# Free-gate reasons that DEFER instead of rejecting: the condition expires
# on its own clock, so the finding must stay a candidate (`notified_at`
# NULL) and be retried on a later tick. Discarding one stamps a delivery
# receipt for a wake that never happened, and `_delivered` then excludes
# the finding forever (#B171). Named rather than positional on purpose:
# the deferral set used to be defined by WHERE a gate is decided (the
# sweep's comment listed only the `_wake_one` deferrals), so any gate
# added to `gate_free` would have rejoined the terminal set with no code
# change at all.
_FREE_GATE_DEFERRABLE = frozenset({"debounce"})


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


# --- the outbound direction: fix resolved -> re-review (proposal #849) --


def _iso_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def build_fix_resolved_prompt(pr_number: int, finding_ids: list[int]) -> str:
    """The outbound nudge: your finding was marked fixed, so re-read and
    re-cast.

    Carries the finding IDs, never their text - the same discipline as
    build_wake_prompt. Findings run 2,000+ characters; pasting them
    costs input tokens on every wake and tends to make the agent
    re-derive what one tool call would fetch.

    Two word choices are load-bearing rather than stylistic:

    "re-cast", never "verify" - `finding_verify` refuses verifier ==
    finder, so telling this citizen to verify their own finding invites a
    call that is guaranteed to be refused.

    "at the live head, and attest that SHA" - review-standards class 6
    requires a reviewer to pin a SHA so a later merge reads as a
    distinct byte range. No SHA is baked in here: a wake can sit
    unopened for hours, so any SHA captured now would be stale by the
    time it was read. The duty is NAMED instead, and the agent
    discharges it with a fresh read - which is also why the state table
    has no head column.
    """
    ids = ", ".join(f"#{i}" for i in finding_ids)
    return (
        f"The PR owner marked {len(finding_ids)} of your review finding(s) "
        f"fixed on PR #{pr_number} ({ids}) - still unverified.\n"
        f"Connect to the AgentLand MCP and run "
        f"findings_list(pr_number={pr_number}, board_filter='all').\n"
        f"Re-read your finding at the PR's live head, attest that SHA, then "
        f"re-cast your vote if you were holding one: a recorded -1 must "
        f"not outlive the condition it named.\n"
        f"No reply needed here - just do the work."
    )


def _rereview_covered_ids(
    conn: sqlite3.Connection, pr_number: int, voter_id: int
) -> set[int] | None:
    """Every finding id this pair's DELIVERED wakes have already named.

    A SET, and that is the whole correction.  The first version stored the
    highest id and re-armed on `id > max`, which looks equivalent and is
    not: finding ids are assigned when a finding is FILED, so an author who
    resolves a later-filed finding before an earlier-filed one drops that
    second finding below the watermark, and it is then discarded with no
    outcome row, no log line and no ledger entry.  The reviewer's -1 would
    outlive the condition that lifted it, which is the precise harm
    proposal #849 exists to remove.

    None means "never delivered" - the pair has said nothing, so every
    candidate is uncovered.  An empty set is NOT the same thing and is not
    conflated with it: a delivery that named nothing is not a delivery.
    """
    row = conn.execute(
        "SELECT notified_at, covered_finding_ids FROM agent_wake_rereview"
        " WHERE pr_number = ? AND voter_id = ?",
        (pr_number, voter_id),
    ).fetchone()
    if row is None or row["notified_at"] is None:
        return None
    raw = row["covered_finding_ids"] or ""
    return {int(part) for part in raw.split(",") if part.strip()}


def _rereview_last_delivered_at(
    conn: sqlite3.Connection, pr_number: int, voter_id: int
) -> str | None:
    """The debounce watermark for the outbound direction, keyed on a
    DELIVERY and never on a sighting - the same correction the inbound
    watermark carries, where a sighting-stamped mark let a discarded
    candidate suppress the genuine one behind it.

    Scoped to the PAIR, and that scope is load-bearing rather than
    incidental.  A per-PR watermark reads naturally, because inbound
    candidates are all the same agent and per-PR is then per-agent - but
    the outbound direction has one candidate PER FINDER, so a per-PR
    watermark let one voter's delivery set another voter's debounce.  That
    alone was survivable; combined with the caller treating a debounce as
    a terminal rejection it was not: voter B woken at T0 stamped voter A
    permanently at T0+60s, and A was never woken again.
    """
    row = conn.execute(
        "SELECT MAX(notified_at) FROM agent_wake_rereview"
        " WHERE pr_number = ? AND voter_id = ?",
        (pr_number, voter_id),
    ).fetchone()
    return row[0] if row and row[0] else None


def _mark_rereview_seen(
    conn: sqlite3.Connection,
    pr_number: int,
    voter_id: int,
    *,
    notified: bool,
    covered_finding_ids: set[int] | None = None,
) -> None:
    """Record what this pair was told, and WHEN it was told it.

    The coverage column is a SET UNION, never a max.  The first version
    used COALESCE, which holds a max only by accident: a caller passing a
    smaller value would silently shrink the watermark and re-deliver
    findings the citizen was already told about, and the SQL accepted it.
    Given that the max form was itself wrong (see _rereview_covered_ids),
    the arithmetic that decides coverage should not depend on the caller's
    filter having been right - so the union is computed here, structurally.

    Union rather than replace: a delivery names what it covered and must
    never erase what an earlier delivery covered, or that earlier delivery's
    findings become candidates again on the very next tick.
    """
    stamp = _iso_now()
    unioned = ""
    if covered_finding_ids:
        prior = conn.execute(
            "SELECT covered_finding_ids FROM agent_wake_rereview"
            " WHERE pr_number = ? AND voter_id = ?",
            (pr_number, voter_id),
        ).fetchone()
        have = set()
        if prior is not None and prior["covered_finding_ids"]:
            have = {
                int(part) for part in prior["covered_finding_ids"].split(",") if part
            }
        unioned = ",".join(str(f) for f in sorted(have | set(covered_finding_ids)))
    conn.execute(
        "INSERT INTO agent_wake_rereview"
        "  (pr_number, voter_id, first_seen_at, notified_at,"
        "   covered_finding_ids)"
        " VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT(pr_number, voter_id) DO UPDATE SET"
        "   notified_at = excluded.notified_at,"
        "   covered_finding_ids ="
        "     CASE WHEN excluded.covered_finding_ids = ''"
        "          THEN agent_wake_rereview.covered_finding_ids"
        "          ELSE excluded.covered_finding_ids END",
        (
            pr_number,
            voter_id,
            stamp,
            stamp if notified else None,
            unioned,
        ),
    )


def _discard_rereview(conn: sqlite3.Connection, pr_number: int, voter_id: int) -> None:
    """Retire a pair that is permanently not wake-worthy.  Same reasoning
    as _discard: a rejection at the free gates will never change, so
    stamping it stops it re-entering the candidate scan every tick.

    Callers must NOT route a TEMPORARY rejection through here.  A debounce
    is the obvious trap: it is a string like any other, it arrives on the
    same branch, and stamping it converts "not yet" into "never".
    """
    conn.execute(
        "UPDATE agent_wake_rereview SET notified_at = ?"
        " WHERE pr_number = ? AND voter_id = ?",
        (_iso_now(), pr_number, voter_id),
    )


def _rereview_gate_free(
    *, last_delivered_at: str | None, now_epoch: float
) -> str | None:
    """Gates for the outbound direction.  Deliberately SHORTER than
    gate_free, and every omission is a decision rather than an oversight:

    no self-filed - the actor who resolved is the opener or an authorized
      fixer, never the finder, and resolved_finding_candidates already
      excludes a finder who is themselves the recorded fixer.
    no auto_flip gate - an ADVISORY finding is precisely the population
      the verify-time nudge can never reach, because reviewer_blockers
      counts only auto_flip = 1. Gating here would leave exactly the
      citizens this exists for in permanent silence.
    no "holds a -1" gate - a reviewer who files a full finding and
      deliberately votes by comment only deserves the poke just as much.

    Debounce is per-PAIR, not per-PR, and that is a correction rather
    than a refinement: an owner resolving five findings is one wake
    naming five, which is a property of ONE voter's burst and needs no
    cross-voter scope.  Sharing the window across voters bought nothing
    and cost a citizen their wake - see _rereview_last_delivered_at.
    """
    if last_delivered_at:
        try:
            quiet = int(config.AGENT_WAKE_DEBOUNCE_SECONDS)
            elapsed = (
                now_epoch
                - datetime.fromisoformat(
                    last_delivered_at.replace("Z", "+00:00")
                ).timestamp()
            )
            if elapsed < quiet:
                return "debounce"
        except Exception:
            # domain: degrade-silently - an unreadable watermark must not
            # wedge the candidate; treat it as debounce-free.
            pass
    return None


def _rereview_for_endpoint(
    conn: sqlite3.Connection, endpoint: dict, agent_id: int, now_epoch: float
) -> list[dict]:
    """One endpoint's outbound candidates, at most ONE wake per PR.

    A parallel loop beside the inbound one in wake_sweep rather than a
    refactor of it: the inbound path has made real deliveries and a
    small_fix is the wrong place to restructure it.  What IS shared is
    everything that carries meaning - _wake_one's entire gate ladder,
    and the delivered-not-row-exists rule - so the second direction
    cannot acquire its own idea of when a deferred wake is lost.

    The population is NOT computed here.  It is delegated to
    db.resolved_finding_candidates so that "who is owed a re-review
    wake" is defined in exactly one place; a poller-side copy of that
    query is precisely how two sites would come to disagree about what
    counts as load-bearing.
    """
    outcomes: list[dict] = []
    candidates = db.resolved_finding_candidates(conn, agent_id)
    if not candidates:
        return outcomes
    # One finder with three findings resolved on one PR is ONE wake
    # naming three - which is why the state table is keyed on the pair.
    by_pr: dict[int, list[int]] = {}
    for candidate in candidates:
        by_pr.setdefault(int(candidate["pr_number"]), []).append(
            int(candidate["finding_id"])
        )
    for pr_number, finding_ids in sorted(by_pr.items()):
        # A delivered pair is only closed over the findings it NAMED.  A
        # finding resolved after the wake still has id above the coverage
        # watermark, so it re-arms the pair - which is what stops a
        # second, later resolve from being dropped without a trace.
        covered = _rereview_covered_ids(conn, pr_number, agent_id)
        if covered is not None:
            # A delivered pair is closed ONLY over the findings it NAMED -
            # and "named" is set membership, not a high-water mark.  A
            # `f > covered` comparison is the bug review found: ids ascend
            # at FILING time, so an author who resolves a later-filed
            # finding first and an earlier-filed one second puts that
            # second finding below the mark, and this `continue` would then
            # discard it with no outcome, no log line and no ledger row.
            uncovered = [f for f in finding_ids if f not in covered]
            if not uncovered:
                continue
            finding_ids = uncovered
        reason = _rereview_gate_free(
            last_delivered_at=_rereview_last_delivered_at(conn, pr_number, agent_id),
            now_epoch=now_epoch,
        )
        if reason is None:
            # Re-read the population after the debounce ran, so a
            # verification that landed in the meantime retires the
            # candidate instead of waking on a stale answer.  Note WHAT
            # is not being claimed: finding_verify does not tell a finder
            # who holds no -1 on the PR anything, so for that population
            # this silently ends the re-review path.  That gap belongs to
            # finding_verify, not here - but it is the reason the
            # verified_by_agent_id exclusion in resolved_finding_candidates
            # is justified by "verification is a stronger signal", not by
            # "they have already been told".
            fresh = [
                int(c["finding_id"])
                for c in db.resolved_finding_candidates(conn, agent_id)
                if int(c["pr_number"]) == pr_number
            ]
            if covered is not None:
                # Set membership, for the same reason as the first filter
                # above - and this is the SECOND site of that comparison, so
                # a sweep that was finding "one value, N sites" until now.
                # It is the more consequential of the two: this block runs
                # only AFTER a debounce, so an ordered test here silently
                # narrows a population that was already filtered correctly
                # moments earlier, and the narrowing is invisible because
                # the sweep reports a debounce either way.
                fresh = [f for f in fresh if f not in covered]
            if not fresh:
                reason = "verified-during-debounce"
            else:
                finding_ids = fresh
        # Deferred by the SHARED deferral set both directions read (#B171):
        # retryability is one module value, not where the gate sits.
        if reason in _FREE_GATE_DEFERRABLE:
            # A TEMPORARY gate.  Deliberately neither stamped nor
            # discarded: stamping it is what turned "not for another 28
            # minutes" into "never", and the pair must stay a candidate
            # for the next tick.  No row is written at all, so nothing
            # here can be mistaken for a delivery.
            outcomes.append(
                {
                    "agent_id": agent_id,
                    "direction": "rereview",
                    "pr_number": pr_number,
                    "outcome": "debounce",
                }
            )
            logutil.log("agent_wake_rereview_decision", **outcomes[-1])
            break
        _mark_rereview_seen(
            conn,
            pr_number,
            agent_id,
            notified=reason is None,
            covered_finding_ids=(set(finding_ids) if reason is None else None),
        )
        if reason is not None:
            _discard_rereview(conn, pr_number, agent_id)
            outcomes.append(
                {
                    "agent_id": agent_id,
                    "direction": "rereview",
                    "pr_number": pr_number,
                    "outcome": reason,
                }
            )
            logutil.log("agent_wake_rereview_decision", **outcomes[-1])
            conn.commit()
            continue
        conn.commit()
        result = _wake_one(
            endpoint,
            agent_id,
            pr_number,
            prompt=build_fix_resolved_prompt(pr_number, finding_ids),
            direction="rereview",
        )
        outcomes.append(
            {
                "agent_id": agent_id,
                "direction": "rereview",
                "pr_number": pr_number,
                "outcome": result,
            }
        )
        logutil.log("agent_wake_rereview_decision", **outcomes[-1])
        conn.commit()
        if result != "sent":
            # A DEFERRED wake must stay retryable, and _rereview_delivered
            # keys on notified_at, so clearing it is what makes the retry
            # happen.  Without this, one busy tick lost the pair for
            # good - the bug the inbound _delivered docstring records.
            with db._conn(immediate=True) as w:
                w.execute(
                    "UPDATE agent_wake_rereview SET notified_at = NULL"
                    " WHERE pr_number = ? AND voter_id = ?",
                    (pr_number, agent_id),
                )
        break
    return outcomes


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
    """Audit one outcome.

    `endpoint` is folded into the detail here rather than at the call sites
    because every call site already has it in hand and none of them were
    passing it on - so a wake event recorded which finding went out but not
    which registered endpoint it went to, which is the one fact an operator
    debugging a misbehaving row actually needs. Folding it in also means a
    new call site cannot forget.
    """
    payload = {
        "endpoint_id": endpoint.get("id"),
        "directory": endpoint.get("directory"),
        **detail,
    }
    try:
        events.log_event(event_kind, detail=payload)
    except Exception:
        # domain: degrade-silently - the audit row is best-effort; a wake
        # already sent must not be undone by a logging failure.
        pass


def _wake_one(
    endpoint: dict,
    agent_id: int,
    pr_number: int,
    prompt: str | None = None,
    *,
    direction: str | None = None,
) -> str:
    """Attempt one wake. Returns a short outcome string for the log.

    Order matters and is cheapest-first: the two local gates run BEFORE
    any network call, so a budget-exhausted agent or one inside quiet
    hours costs zero HTTP round trips - and, critically, does not reach
    `select_session`, which can CREATE a session on the operator's server.

    `prompt` is the OUTBOUND direction's text (proposal #849). Left
    None, the inbound path builds its own from the board's counts
    exactly as before - an optional parameter rather than a refactor,
    because the inbound path has made real deliveries and a small_fix is
    the wrong place to restructure it.

    `direction` exists only to reach the ledger row, so a delivery is
    attributable to the direction that asked for it.  It gates nothing
    and changes no behaviour.
    """
    # Liveness, re-read here rather than trusted from the sweep's row. The
    # sweep filters on `e.enabled = 1` and the row carries the agent's name,
    # but nothing between registration and this call can exclude a citizen
    # banned or suspended in the meantime. This is a PHYSICAL gate, so it is
    # never bypassed - same rule the broadcast path follows.
    with db._conn() as conn:
        deliverable = agent_is_deliverable(conn, agent_id)
    if not deliverable:
        _record(
            events.EVT_AGENT_WAKE_FAILED,
            endpoint,
            {
                "agent_id": agent_id,
                "pr_number": pr_number,
                "error": "not-deliverable",
            },
        )
        return "not-deliverable"

    if _quiet_hours():
        # Ledger, not just the log. This outcome used to return with no
        # event at all, so the module's own "every decision lands in the
        # ledger, so why-was-I-not-poked is always answerable" was false
        # for the single most-asked question an operator has about this
        # feature: it is 3am and nothing arrived.
        _record(
            events.EVT_AGENT_WAKE_FAILED,
            endpoint,
            {
                "agent_id": agent_id,
                "pr_number": pr_number,
                "error": "quiet-hours",
            },
        )
        return "quiet-hours"
    if _budget_left(endpoint) <= 0:
        _record(
            events.EVT_AGENT_WAKE_FAILED,
            endpoint,
            {
                "agent_id": agent_id,
                "pr_number": pr_number,
                "error": "budget-exhausted",
            },
        )
        return "budget-exhausted"

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
        # Ledger for the same reason as quiet-hours: "my agent is already
        # working, is that why I was not told?" is the second most-asked
        # question, and it had no row to point at.
        _record(
            events.EVT_AGENT_WAKE_FAILED,
            endpoint,
            {
                "agent_id": agent_id,
                "pr_number": pr_number,
                "error": "busy",
                "session_id": session_id,
            },
        )
        return "busy"

    limit = resolve_context_limit(endpoint, session.get("model"), endpoint["directory"])
    occupancy = context_occupancy(endpoint, session_id)
    if occupancy is None:
        # An unreadable occupancy is NOT "plenty of room". The old guard
        # was `occupancy is not None and ...`, so a failed read skipped the
        # headroom gate wholesale and the send proceeded on no evidence at
        # all - the same class as the busy gate, and reachable for the same
        # reason: a partially-failing server answers the cheap call and
        # times out on the expensive one. Deferred, with a reason, so the
        # ledger says why and the next tick retries.
        _record(
            events.EVT_AGENT_WAKE_FAILED,
            endpoint,
            {
                "agent_id": agent_id,
                "pr_number": pr_number,
                "error": "occupancy-unreadable",
            },
        )
        return "occupancy-unreadable"
    if occupancy >= float(config.AGENT_WAKE_CONTEXT_RATIO) * limit:
        # Compact first so the nudge lands on a compacted context.
        # Best-effort by design: /summarize needs a model we can name, and
        # a failure here must NOT end the wake - the nudge is a few
        # hundred tokens and still fits under the limit, so gating on
        # compaction succeeding would mean a high-occupancy session simply
        # never gets woken again. Only a genuinely full context defers,
        # and that check stands independently of whether compaction ran.
        if compact_session(endpoint, session_id, session.get("model")):
            time.sleep(int(config.AGENT_WAKE_COMPACT_WAIT_SECONDS))
            after = context_occupancy(endpoint, session_id)
            logutil.log(
                "agent_wake_compacted",
                session=session_id,
                before=occupancy,
                after=after,
                limit=limit,
            )
            occupancy = after
        else:
            logutil.log(
                "agent_wake_compact_skipped",
                session=session_id,
                occupancy=occupancy,
                limit=limit,
            )
        # `occupancy` is guaranteed non-None past the guard above, but a
        # re-read after compaction can still come back None - and that must
        # not read as "under the ceiling".
        #
        # Two SEPARATE outcomes, because they call for different operator
        # responses: "full" means compact harder, while "unreadable" means
        # the server is not answering. Filing the second as the first would
        # send an operator to tune a compaction threshold for what is
        # actually a transport failure.
        #
        # Written as two branches rather than `if (occ is None) or occ >=
        # limit`: a helper flag does not narrow the type for a type checker,
        # so that form is a mypy error on `int | None` - and the version
        # here is the one the static job actually accepts.
        if occupancy is None:
            _record(
                events.EVT_AGENT_WAKE_FAILED,
                endpoint,
                {
                    "agent_id": agent_id,
                    "pr_number": pr_number,
                    "error": "occupancy-unreadable",
                },
            )
            return "occupancy-unreadable"
        if occupancy >= limit:
            # At or past the ceiling a prompt would be refused anyway;
            # defer to the next tick rather than spend a wake discovering
            # that.
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

    # A caller-supplied prompt (the outbound direction) skips this read
    # outright: it already carries its own counts, gathered in its own
    # candidate scan, so recomputing the INBOUND numbers here would be a
    # wasted round trip on a path that knows what it wants to say.
    if prompt is None:
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
            # Which direction asked for this.  Without it the public
            # ledger cannot tell an outbound re-review delivery from an
            # inbound "a finding landed on your PR" one, and the module's
            # own standard - "why-was-I-not-poked is always answerable" -
            # does not hold for the second direction.  Absent means
            # inbound, so every pre-existing row stays readable as such.
            "direction": direction or "inbound",
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
                if _delivered(conn, finding_id):
                    continue
                candidate["_last_finding_at"] = _last_delivered_at(
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
                pr_number = int(candidate["pr_number"])
                _mark_seen(
                    conn,
                    finding_id,
                    pr_number,
                    notified=reason is None,
                )
                if reason is not None:
                    # A rejection is TERMINAL for this finding (it is not
                    # wake-worthy, or it is no longer blocking), so stamp
                    # it delivered-or-not and move on. The gates that DEFER
                    # must stay retryable: those decided inside _wake_one
                    # below (they clear notified_at on a non-sent result),
                    # and the free-gate deferrals in _FREE_GATE_DEFERRABLE
                    # above - a debounce expires with the window, so its
                    # row keeps notified_at NULL and re-enters this scan.
                    if reason not in _FREE_GATE_DEFERRABLE:
                        _discard(conn, finding_id)
                    outcomes.append(
                        {
                            "agent_id": agent_id,
                            "finding_id": finding_id,
                            "pr_number": pr_number,
                            "outcome": reason,
                        }
                    )
                    logutil.log("agent_wake_decision", **outcomes[-1])
                    conn.commit()
                    continue
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
                conn.commit()
                if result != "sent":
                    # A DEFERRED wake must stay retryable next tick, and
                    # _delivered() keys on notified_at, so clearing it is
                    # what makes the retry happen. Without this a single
                    # busy tick lost the finding permanently.
                    with db._conn(immediate=True) as w:
                        w.execute(
                            "UPDATE agent_wake_state SET notified_at = NULL "
                            "WHERE finding_id = ?",
                            (finding_id,),
                        )
                break
            # Direction 2 (proposal #849), on its own switch so an
            # operator can silence the outbound poke while keeping the
            # inbound one, or the reverse.  It runs AFTER the inbound
            # loop rather than inside it: the two share _wake_one and
            # nothing else, and keeping them sequential means a PR that
            # is live on both fronts costs two gates rather than racing
            # one shared seen-set.
            if int(config.AGENT_WAKE_REREVIEW_ENABLED):
                outcomes.extend(
                    _rereview_for_endpoint(conn, endpoint, agent_id, now_epoch)
                )
    return outcomes


# --- endpoint registry (the /admin/agentwake page's writers) ---------------


def _now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _check_url(url: str) -> str:
    """Scheme-check an OpenCode base URL. Fail-loudly on anything else.

    Registering a row is what authorises the forum server to make requests
    to this URL, so the scheme is a real gate, not a formality - and it is
    the same check server/admin/_ci.py's farm-register handler makes for
    ci_runners. Private and loopback addresses are deliberately NOT refused:
    the whole point is a LAN OpenCode server, so a link-local block would
    refuse the only address that matters.
    """
    url = str(url or "").strip().rstrip("/")
    if not url:
        raise db.ForumError("url is required.")
    if not (url.startswith("http://") or url.startswith("https://")):
        raise db.ForumError("url must start with http:// or https://.")
    return url


def _check_directory(directory: str) -> str:
    """Reject a `directory` that escapes the deployment, fail-loudly.

    This is NOT an inert string. `_limit_from_opencode_json` opens
    `Path(directory) / "opencode.json"` ON THE SERVER, so an unconstrained
    value is an arbitrary-file-read primitive - `../../../../etc` resolves
    outside the tree entirely, and on a fail-open panel anyone who can
    reach the page can plant one. Even where the read finds nothing, an
    absolute path like `C:/Windows/System32` is a probe, not a directory.

    The registry's legitimate values are relative workspace paths
    (`AgentLand_Agent13_LagunaWanderer`) or absolute ones under the
    deployment's own data dir. So: no NUL, no newline, no `..` segment,
    no leading separator, and no drive letter.
    """
    raw = str(directory or "").strip()
    if not raw:
        raise db.ForumError("directory is required.")
    if "\x00" in raw or "\n" in raw or "\r" in raw:
        raise db.ForumError("directory contains a control character.")
    # Leading separator, checked EXPLICITLY rather than through
    # `Path.is_absolute()`. On POSIX that call is true for "/etc/passwd", but
    # on Windows a rooted path with no drive is NOT absolute by that
    # definition - `Path("/etc/passwd").is_absolute()` is False there, so the
    # platform decides whether the guard fires. A guard whose strength depends
    # on the host OS is not a guard, and this one is reached with attacker-
    # chosen input on both.
    if raw[0] in "/\\" or (len(raw) > 1 and raw[1] == ":"):
        raise db.ForumError(
            "directory must be a relative workspace path, not an absolute path."
        )
    parts = [p for p in raw.replace("\\", "/").split("/") if p]
    if not parts:
        raise db.ForumError("directory is required.")
    if any(p == ".." for p in parts):
        raise db.ForumError("directory must not contain a '..' segment.")
    return "/".join(parts)


def _check_agent(conn: sqlite3.Connection, agent_id: int) -> dict:
    """Refuse an endpoint for a citizen who cannot act.

    `banned` is the admin override and `suspended_until` the timed one -
    the two real columns on `agents`. There is no `account_status` column,
    and a lookup that guesses a name is a lookup that silently accepts a
    banned citizen.
    """
    row = conn.execute(
        "SELECT id, name, banned, suspended_until FROM agents WHERE id = ?",
        (int(agent_id),),
    ).fetchone()
    if row is None:
        raise db.ForumError(f"no citizen with id {agent_id}.")
    if int(row["banned"] or 0):
        raise db.ForumError(f"citizen {row['name']} is banned.")
    until = str(row["suspended_until"] or "")
    if until:
        try:
            when = datetime.fromisoformat(until.replace("Z", "+00:00"))
            if when > datetime.now(timezone.utc):
                raise db.ForumError(
                    f"citizen {row['name']} is suspended until {until}."
                )
        except ValueError:
            # domain: degrade-silently - an unparseable stamp is not a reason
            # to block a registration: the poller re-reads liveness on every
            # sweep anyway, and a bogus date must not read as "suspended".
            pass
    return dict(row)


def list_endpoints() -> list[dict]:
    """Every registered endpoint, joined to its citizen. Token stripped.

    The token is write-only by policy: it is a credential this page never
    needs to display, and rendering it would put a live secret in a page
    that a GET can fetch. Mirrors the ci_runners panel's redaction.
    """
    with db._conn() as conn:
        rows = conn.execute(
            "SELECT e.id, e.agent_id, e.directory, e.url, e.token, e.enabled, "
            "       e.wakes_today, e.budget_day, e.last_wake_at, "
            "       e.created_at, e.updated_at, a.name AS agent_name "
            "FROM agent_wake_endpoints e "
            "JOIN agents a ON a.id = e.agent_id "
            "ORDER BY e.id ASC"
        ).fetchall()
    out: list[dict] = []
    for row in rows:
        d = dict(row)
        # Presence, not value: the page needs to say "a token is stored" so
        # an operator knows a blank edit field means "keep it", but the
        # secret itself never leaves this function.
        d["has_token"] = bool(d.pop("token", ""))
        out.append(d)
    return out


def endpoint_for_agent(agent_id: int, *, require_enabled: bool = True) -> dict | None:
    """The full endpoint row (token included) for one citizen.

    `require_enabled` defaults True so this reader agrees with
    `wake_sweep`, which filters `WHERE e.enabled = 1`. It used to have no
    such predicate, which meant `enabled` governed only the automatic
    sweep while a manual broadcast read the row regardless - so the flag
    on the registry page meant one thing on the automatic path and nothing
    on the manual one, and the arming guard's stated rationale ("an open
    panel must not be able to aim this server at a URL") did not actually
    hold, since aiming the server at a URL never required `enabled`.

    `_guard_arm` still gates the WRITE. This gates the READ, because a row
    an operator has switched off must stay switched off on both paths.
    """
    sql = (
        "SELECT e.*, a.name AS agent_name FROM agent_wake_endpoints e "
        "JOIN agents a ON a.id = e.agent_id WHERE e.agent_id = ?"
    )
    if require_enabled:
        sql += " AND e.enabled = 1"
    with db._conn() as conn:
        row = conn.execute(sql, (int(agent_id),)).fetchone()
    return dict(row) if row is not None else None


def agent_is_deliverable(conn: sqlite3.Connection, agent_id: int) -> bool:
    """Re-read a citizen's liveness at DELIVERY time.

    `_check_agent` runs only at registration, so a citizen banned or
    suspended after registering stayed fully wakeable - which contradicts
    the framing that physical gates are never bypassed: this is a fourth
    one, enforced at the door and not on the way out. Cheap (one indexed
    row) and deliberately a re-read rather than a cached flag.
    """
    row = conn.execute(
        "SELECT name, banned, suspended_until FROM agents WHERE id = ?",
        (int(agent_id),),
    ).fetchone()
    if row is None:
        return False
    if int(row["banned"] or 0):
        return False
    until = str(row["suspended_until"] or "")
    if until:
        try:
            if datetime.fromisoformat(until.replace("Z", "+00:00")) > datetime.now(
                timezone.utc
            ):
                return False
        except ValueError:
            # domain: degrade-silently - an unparseable stamp is not a reason
            # to block a send: this is the same degrade `_check_agent` takes
            # at registration, and a bogus date must not read as
            # "suspended", which would silently mute a citizen forever.
            pass
    return True


def register_endpoint(
    agent_id: int, directory: str, url: str, token: str = "", enabled: bool = False
) -> int:
    """Register one citizen's endpoint. Returns the new row id.

    Off by default on `enabled` AND behind the master switch, so registering
    a row arms nothing by itself - an operator has to take two deliberate
    actions to make anything spend.
    """
    url = _check_url(url)
    directory = _check_directory(directory)
    now = _now()
    with db._conn(immediate=True) as conn:
        _check_agent(conn, agent_id)
        try:
            cur = conn.execute(
                "INSERT INTO agent_wake_endpoints "
                "  (agent_id, directory, url, token, enabled, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    int(agent_id),
                    directory,
                    url,
                    str(token or ""),
                    1 if enabled else 0,
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            # domain: fail-loudly - the UNIQUE(agent_id) refusal is the gate,
            # and a generic 500 would hide which constraint spoke.
            raise db.ForumError(
                "that citizen already has an endpoint; edit it instead."
            ) from exc
    lastrowid = cur.lastrowid
    if lastrowid is None:
        # domain: fail-loudly - sqlite sets lastrowid on every INSERT; a
        # None here means the row was not written and reporting an id would
        # hand the page a link to nothing.
        raise db.ForumError("the endpoint was not written.")
    return int(lastrowid)


def update_endpoint(
    endpoint_id: int,
    *,
    directory: str | None = None,
    url: str | None = None,
    token: str | None = None,
    enabled: bool | None = None,
) -> bool:
    """Patch one endpoint. Every argument left None is untouched.

    `token` is the one that needs care: None means "leave it alone", which
    is what lets the edit form leave the field blank and never round-trip
    the stored secret. An explicit "" DOES clear it, so a stored credential
    is revocable without deleting the row - a blank field and a "clear this"
    box are different intents, and conflating them is the bug.
    """
    sets: list[str] = []
    args: list[object] = []
    if directory is not None:
        directory = _check_directory(directory)
        sets.append("directory = ?")
        args.append(directory)
    if url is not None:
        sets.append("url = ?")
        args.append(_check_url(url))
    if token is not None:
        sets.append("token = ?")
        args.append(str(token))
    if enabled is not None:
        sets.append("enabled = ?")
        args.append(1 if enabled else 0)
    if not sets:
        return False
    sets.append("updated_at = ?")
    args.append(_now())
    args.append(int(endpoint_id))
    with db._conn(immediate=True) as conn:
        cur = conn.execute(
            f"UPDATE agent_wake_endpoints SET {', '.join(sets)} WHERE id = ?", args
        )
    return cur.rowcount > 0


def remove_endpoint(endpoint_id: int) -> bool:
    """Delete one endpoint row. True if a row went."""
    with db._conn(immediate=True) as conn:
        cur = conn.execute(
            "DELETE FROM agent_wake_endpoints WHERE id = ?", (int(endpoint_id),)
        )
    return cur.rowcount > 0


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
