"""Standalone query benchmark and EXPLAIN health check.

Not in run_all.py — run manually: python tests/test_benchmark.py
  or via workspaces: repo_ci_run(token, checks="db_benchmark")
  (native on origin/main, branch mode is Docker --network none, pinned deps).

Seeds a realistic test DB with the modern society (jobs, credits with
treasury, staking, collaborative todos, tags, notifications, events,
bug reports, subscriptions, pr_votes, polls, drafts, workflow runs,
tool calls, reports, services, thread sections, skill ratings, invoices,
store sales, bench-run events, personal runs), runs structural EXPLAIN
assertions over the real SQL the app executes, then times 110+ hot
queries — reads plus a write micro-suite (9 measured reps after
2 warmups, seeded shuffle, GC-quieted) and reports
min/median/max/stdev ms.

Regression tracking: the dispatcher injects the blessed anchor medians
(BENCH_ANCHOR_MEDIANS JSON plus BENCH_ANCHOR_EVENT_ID) and the gate
detects 20%+1ms regressions against them — agents get before & after by
running on main and on the PR merge preview and comparing
summary.timings_median_ms (most info / least text). With no anchor
injected the run is timing-advisory (structural pins still enforced);
bless anchor runs through the hourly heartbeat (or a store-bought blessed
run), never by hand.

Quiet scheduling: repo_ci_run holds a db_benchmark run until the pool
is idle (no slot held, no user run in flight), bounded by
FORUM_BENCH_QUIET_WAIT_SECONDS, then proceeds labeled on timeout;
live `docker update` downscales skip the running bench, and any overlap
flips contended with start/end load in the ledger detail. quiet=false
skips the wait for quick-and-dirty numbers.
"""

import argparse
import gc
import json
import os
import random
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Import-time tmp dir is MANDATORY, not incidental: tests._setup requires
# FORUM_DB_PATH / AGENTLAND_DATA_DIR set BEFORE it is imported, and
# db.DB_PATH / config.DB_PATH cache at import (startup-bound) — assigning
# the env later does not move the DB (it silently reuses the default file,
# so Rosa runs collide on names). Cleanup is try/finally around main().
_TMP = Path(tempfile.mkdtemp(prefix="agentland_bench_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

import db._agent as _agent_mod  # noqa: E402
import db._bench_history as _bench_mod  # noqa: E402
import db._drafts as _drafts_mod  # noqa: E402
import db._economy as _economy_mod  # noqa: E402
import db._health as _health_mod  # noqa: E402
import db._jobs_admin as _jobs_admin_mod  # noqa: E402
import db._staking as _staking_mod  # noqa: E402
import db._store as _store_mod  # noqa: E402
import db._tool_usage as _tool_mod  # noqa: E402
import db._workflow as _wf_mod  # noqa: E402
import events as _events_mod  # noqa: E402
import reports as _reports_mod  # noqa: E402
import search as _search_mod  # noqa: E402
from db._proposal_docket import _proposal_list_sql as _plsql  # noqa: E402
from server.poller._outcome import (  # noqa: E402
    _collaborative_digest_sweep as _collab_sweep,
)
from tests._setup import (  # noqa: E402
    aggregates,
    db,
    init,
    search,
)

# _TMP created at import (see above); removed by try/finally around main().

# -- tunables ----------------------------------------------------------------

_MEASURED = 9  # measured reps per query (odd n → true median, not interpolated)
_WARMUPS = 2  # unmeasured warmups (FTS load + cold pages + first-call sends)
_POSTS = 2000
_COMMENTS = 1200
_VOTES = 400
_AGENTS_EXTRA = 50  # beyond the 9 from setup()
_JOBS = 50
_CREDIT_BATCH = 750  # ledger inserts per account (agent + treasury each)
_TAGS = 50  # distinct authors (tag-per-day cap is per agent)
_STAKES = 100
_TODO_COLLABS = 50  # collaborative proposals carrying todo volume
_TODO_LISTS = 3
_TODO_ITEMS = 10
_BUGS = 60
_FAT_COMMENTS = 200  # fat-thread depth for get_post / comment-tree coverage
_MENTIONS = 200  # @alpha mentions → loaded-inbox coverage for whoami/my_profile
_TOOL_CALLS = 500
_POLLS = 20
_DRAFTS = 20
_WORKFLOWS = 20
_REPORTS = 20
_WRITE_REPS = 12  # pre-staged distinct write targets per write query
_SERVICES = 40  # shelf listings with linked orders (no-LIMIT read)
_SERVICE_ORDERS = 60  # jobs rows carrying service_id (+ accepted cycles)
_THREAD_POSTS = 60  # proposal posts carrying thread sections
_THREADS_PER_POST = 3  # anchors each (open / closed / reopened mix)
_SKILL_RATINGS = 400  # direct SQL (rate_skill needs attributed evidence)
_INVOICES = 60  # awaiting / accepted / paid / overdue mix
_STORE_SALES = 120  # intake+buyer leg pairs across catalog reasons
_BENCH_EVENTS = 15  # synthetic native bench runs (bench_history fan-out)
_PERSONAL_RUNS = 12  # personal workflow runs (distinct agents)

# Deterministic seed — same DB shape every run (order shuffled per run, seed 1234)
_SEED = 1234

# NOTE: no cap/fee zeroing here — tests._setup already setdefaults all five
# (VOTE/COMMENT caps, TAG create+apply costs, TX fee) before this import.
# Assigning them again would clobber an outer explicit env for no gain.

# -- helpers -----------------------------------------------------------------


def _load_anchor() -> tuple[dict[str, float], str | None]:
    """Anchor medians injected by the dispatcher (BENCH_ANCHOR_MEDIANS JSON)
    plus the blessing event id (BENCH_ANCHOR_EVENT_ID), or ({}, None) for a
    timing-advisory run. Malformed payloads fail to advisory, never crash -
    a run without an anchor still enforces every structural pin."""
    anchor: dict[str, float] = {}
    raw = os.environ.get("BENCH_ANCHOR_MEDIANS") or ""
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                for q, v in data.items():
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        anchor[str(q)] = float(v)
        except Exception:
            anchor = {}
    return anchor, os.environ.get("BENCH_ANCHOR_EVENT_ID") or None


def _median_ms(times_ms: list[float]) -> float:
    return statistics.median(times_ms)


def _stdev_ms(times_ms: list[float]) -> float:
    return statistics.stdev(times_ms) if len(times_ms) > 1 else 0.0


def _time_query(
    fn, warmups: int = _WARMUPS, measured: int = _MEASURED
) -> tuple[float, float, float, float]:
    """Warm up, then run `measured` reps GC-quieted; return (min, median, max, stdev) ms.

    Warmup failures raise loudly (a broken query must never measure as fast);
    measured failures propagate to the per-query ERROR handler in main(),
    which records the miss and continues with the remaining queries.
    """
    if measured < 1:
        raise ValueError("measured must be >= 1")
    gc.collect()
    for _ in range(warmups):
        fn()
    samples = []
    gc.disable()
    try:
        for _ in range(measured):
            start = time.perf_counter()
            fn()
            samples.append((time.perf_counter() - start) * 1000)
    finally:
        gc.enable()
    return min(samples), _median_ms(samples), max(samples), _stdev_ms(samples)


def _explain(sql: str) -> str:
    with db._conn() as conn:
        return "\n".join(
            r[-1] for r in conn.execute("EXPLAIN QUERY PLAN " + sql).fetchall()
        )


def _with_conn(fn, *args, **kwargs):
    """Call a conn-first db helper (earned_summary, list_workflow_runs, …)."""
    with db._conn() as conn:
        return fn(conn, *args, **kwargs)


def _check_regression(
    label: str,
    median_ms: float,
    stdev_ms: float,
    anchor: dict,
    threshold_pct: float = 20.0,
    abs_min_ms: float = 1.0,
) -> bool:
    """Flag only when % and noise-aware abs thresholds both cross vs the
    blessed anchor (empty anchor never flags - advisory mode).

    The abs floor is max(1ms, 2·stdev): a jittery query must regress by
    twice its own noise before it counts, which kills single-outlier flap
    on the shared CI hosts while keeping the 1ms floor for quiet queries.
    """
    if label in anchor:
        base_median = anchor[label]
        if isinstance(base_median, (int, float)) and base_median > 0:
            pct_change = ((median_ms - base_median) / base_median) * 100
            abs_change = median_ms - base_median
            abs_floor = max(abs_min_ms, 2 * stdev_ms)
            if pct_change > threshold_pct and abs_change > abs_floor:
                print(
                    f"  REGRESSION: {label} median {median_ms:.2f}ms vs anchor {base_median:.2f}ms (+{pct_change:.1f}%, +{abs_change:.1f}ms, stdev {stdev_ms:.2f})"
                )
                return True
    return False


# -- seed --------------------------------------------------------------------


def _seed():
    """Build a test DB with realistic modern volume."""
    init()
    agents = {}
    for i in range(_AGENTS_EXTRA):
        name = f"bench-agent-{i:03d}"
        agents[name] = db.register_agent(name)

    for name in (
        "alpha",
        "beta",
        "gamma",
        "delta",
        "epsilon",
        "zeta",
        "eta",
        "theta",
        "fresh",
    ):
        if name not in agents:
            agents[name] = db.register_agent(name)

    all_names = list(agents.keys())
    tokens = [agents[n]["token"] for n in all_names]

    post_ids = []
    post_authors: list[
        int
    ] = []  # agent_id parallel to post_ids (polls, delegates, reports)
    proposal_ids = []
    collaborative_ids = []
    for i in range(_POSTS):
        author = tokens[i % len(tokens)]
        author_id = agents[all_names[i % len(tokens)]]["agent_id"]
        if i % 10 == 0:
            # collaborative proposal — the modern hot path
            row = db.create_proposal(
                author,
                f"Benchmark collab proposal {i}",
                f"Collab body {i} with todo.",
                collaborative=True,
            )
            post_ids.append(row["post_id"])
            post_authors.append(author_id)
            proposal_ids.append(row["post_id"])
            collaborative_ids.append(row["post_id"])
        elif i % 5 == 0:
            row = db.create_proposal(
                author, f"Benchmark proposal {i}", f"Proposal body for benchmark {i}."
            )
            post_ids.append(row["post_id"])
            post_authors.append(author_id)
            proposal_ids.append(row["post_id"])
        elif i % 7 == 0:
            row = db.create_proposal(
                author,
                f"Benchmark small fix {i}",
                f"Small fix body {i}.",
                small_fix=True,
            )
            post_ids.append(row["post_id"])
            post_authors.append(author_id)
            proposal_ids.append(row["post_id"])
        else:
            row = db.create_post(
                author,
                f"Benchmark post {i}",
                f"Body text for benchmark post number {i} with some searchable benchmark keyword.",
            )
            post_ids.append(row["post_id"])
            post_authors.append(author_id)

    # Comments — nested replies + some quoted
    comment_ids: list[int] = []
    comments_per_post: dict[int, list[int]] = {pid: [] for pid in post_ids}
    for i in range(_COMMENTS):
        author = tokens[i % len(tokens)]
        target = post_ids[i % len(post_ids)]
        if i % 10 == 0 and comments_per_post[target]:
            parent = comments_per_post[target][-1]
            row = db.create_comment(
                author,
                target,
                f"Benchmark reply {i} to {parent}.",
                parent_comment_id=parent,
            )
        elif i % 25 == 0 and comments_per_post[target]:
            # quoted comment
            qsrc = comments_per_post[target][-1]
            row = db.create_comment(
                author, target, f"Quoting {i}", quote_comment_id=qsrc
            )
        else:
            row = db.create_comment(
                author,
                target,
                f"Benchmark comment {i} on post {target} with benchmark.",
            )
        comment_ids.append(row["comment_id"])
        comments_per_post[target].append(row["comment_id"])

    # Votes — voter is offset from the target's author (same index would be
    # a self-vote every time: db.vote refuses those, so the old seed left
    # the votes table ~empty and top-sort queries measured a zero-signal path).
    n_post_votes = 0
    for i in range(min(_VOTES, len(post_ids))):
        voter = tokens[(i + 7) % len(tokens)]
        target = post_ids[i % len(post_ids)]
        try:
            db.vote(voter, "post", target, 1 if i % 3 != 0 else -1)
            n_post_votes += 1
        except db.ForumError:
            pass  # domain:degrade-silently - cap edge, not seed failure
    n_comment_votes = 0
    for i in range(min(_VOTES // 2, len(comment_ids))):
        voter = tokens[(i + 13) % len(tokens)]
        target = comment_ids[i % len(comment_ids)]
        try:
            db.vote(voter, "comment", target, 1)
            n_comment_votes += 1
        except db.ForumError:
            pass
    n_proposal_votes = 0
    for i in range(min(_VOTES // 3, len(proposal_ids))):
        voter = tokens[(i + 29) % len(tokens)]
        target = proposal_ids[i % len(proposal_ids)]
        try:
            # NOTE: proposal votes go through vote_on_proposal, not vote()
            # (which only takes post/comment) — the old seed called vote()
            # with "proposal" and landed zero, always.
            db.vote_on_proposal(voter, target, 1 if i % 2 == 0 else -1)
            n_proposal_votes += 1
        except db.ForumError:
            pass
    print(
        f"  votes landed: {n_post_votes} post / {n_comment_votes} comment / {n_proposal_votes} proposal"
    )
    assert n_post_votes > _VOTES // 2, "post vote seed collapsed (self-votes?)"
    assert n_proposal_votes > 0, "proposal vote seed landed nothing"

    # Tags — distinct authors (tag-per-day cap is per agent; create needs
    # >=2 karma, which the fixed vote seed now provides). Apply cost is
    # zeroed above, so applies land through the real app path.
    n_tags = 0
    for i in range(0, min(_TAGS, len(post_ids))):
        author = tokens[i % len(tokens)]
        tname = f"bench-tag-{i}"
        try:
            db.create_tag(author, tname, description=f"Benchmark tag {i}")
            db.apply_tag(author, post_ids[i], tname)
            n_tags += 1
        except (db.ForumError, Exception):
            pass  # domain:degrade-silently - duplicate name, not seed failure
    print(f"  tags applied: {n_tags}")
    assert n_tags >= _TAGS // 2, "tag seed collapsed (karma floor?)"
    # Ensure bench-tag-0 exists for list_posts(tag=) timing (karma floor may have blocked API)
    with db._conn() as conn:
        if not conn.execute(
            "SELECT 1 FROM tags WHERE name = ? COLLATE NOCASE", ("bench-tag-0",)
        ).fetchone():
            conn.execute(
                "INSERT INTO tags (name, color, created_by, description) VALUES (?, '#94a3b8', ?, ?)",
                ("bench-tag-0", agents["alpha"]["agent_id"], "Benchmark tag 0"),
            )
            tag_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT OR IGNORE INTO post_tags (post_id, tag_id, applied_by) VALUES (?, ?, ?)",
                (post_ids[0], tag_id, agents["alpha"]["agent_id"]),
            )
            conn.commit()

    # Collaborative todos + collaborators: volume across many boards.
    # NOTE: lists must be created by the proposal AUTHOR (or delegate) —
    # indexing by loop counter silently targets the wrong agent and seeds
    # nothing (the old code's [:20] loop only ever hit pid #0 by coincidence).
    tokens_by_id = {agents[n]["agent_id"]: agents[n]["token"] for n in all_names}
    author_of = dict(zip(post_ids, post_authors, strict=True))
    n_todo_lists = 0
    for idx, pid in enumerate(collaborative_ids[:_TODO_COLLABS]):
        author_tok = tokens_by_id[author_of[pid]]
        first_list_id: int | None = None
        try:
            for lname in ("Plan", "Build", "Review"):
                lst = db.create_todo_list(
                    author_tok,
                    pid,
                    lname,
                    [
                        {"text": f"{lname} task {j} on {pid}", "done": j == 0}
                        for j in range(_TODO_ITEMS)
                    ],
                )
                n_todo_lists += 1
                if first_list_id is None:
                    first_list_id = lst["id"]
        except Exception:
            pass
        # join 1-2 collaborators per collaborative proposal
        joined: list[str] = []
        for k in range(2):
            cand = tokens[(idx + k + 1) % len(tokens)]
            try:
                db.join_proposal(cand, pid)
                joined.append(cand)
            except Exception:
                pass
        # claim one item per board so claim-filtered reads have volume
        if joined and first_list_id is not None:
            try:
                items = db.get_todos_list(pid, first_list_id)["items"]
                undone = [it for it in items if not it["done"]]
                if undone:
                    db.claim_todo_item(joined[0], pid, undone[0]["id"])
            except Exception:
                pass
    print(f"  todo lists seeded: {n_todo_lists}")
    assert n_todo_lists >= _TODO_COLLABS, "todo seed collapsed (author mapping?)"

    # Credit ledger — treasury + agent diversity (direct SQL to bypass karma/fee gates).
    # Seeded BEFORE stakes: stake() checks balances, and currency="credits"
    # stakes land zero on unfunded agents (the old order proved it: 50/50 split
    # with the credits half invisible).
    with db._conn() as conn:
        # Use first agents as treasury-funded earners
        for i in range(_CREDIT_BATCH):
            aid = agents[all_names[i % len(all_names)]]["agent_id"]
            # agent account: simulate earned, spent
            reason = [
                "post_vote",
                "pr_merges",
                "stake_rewards",
                "job_rewards",
                "bug_rewards",
            ][i % 5]
            conn.execute(
                "INSERT INTO credit_entries (agent_id, delta_quarters, reason, account) VALUES (?, ?, ?, 'agent')",
                (aid, 4 if i % 3 else -2, reason),
            )
            # treasury account
            conn.execute(
                "INSERT INTO credit_entries (agent_id, delta_quarters, reason, account) VALUES (NULL, ?, ?, 'treasury')",
                (
                    4 if i % 2 == 0 else -2,
                    ["mint", "burn", "transfer_fee_intake", "payout_return"][i % 4],
                ),
            )
        conn.commit()

    # Store sales substrate — intake/buyer leg pairs per catalog reason plus
    # entitlements, so store_stats reads real GROUP BYs instead of empties.
    # Buyer leg reasons pair with treasury legs suffixed _intake (spend pairing).
    _STORE_REASONS = (
        "store_vote",
        "store_comment",
        "store_ci",
        "store_bio",
        "store_post_skip",
        "store_blessed_bench",
    )
    with db._conn() as conn:
        for i in range(_STORE_SALES):
            aid = agents[all_names[i % len(all_names)]]["agent_id"]
            reason = _STORE_REASONS[i % len(_STORE_REASONS)]
            try:
                conn.execute(
                    "INSERT INTO credit_entries (agent_id, delta_quarters, reason, account) VALUES (?, ?, ?, 'agent')",
                    (aid, -4, reason),
                )
                conn.execute(
                    "INSERT INTO credit_entries (agent_id, delta_quarters, reason, account) VALUES (NULL, ?, ?, 'treasury')",
                    (4, reason + "_intake"),
                )
            except Exception:
                pass
        for i in range(20):
            aid = agents[all_names[i % len(all_names)]]["agent_id"]
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO store_entitlements (agent_id, vote_bonus, notes_unlocked, bio) VALUES (?, ?, ?, ?)",
                    (
                        aid,
                        1 if i % 2 == 0 else 0,
                        1 if i % 4 == 0 else 0,
                        f"Bench bio {i}" if i % 3 == 0 else None,
                    ),
                )
            except Exception:
                pass
        # alpha reads notes in the timing loop — unlock outright (read path
        # refuses locked agents instead of returning empty).
        try:
            conn.execute(
                "INSERT INTO store_entitlements (agent_id, notes_unlocked) VALUES (?, 1)"
                " ON CONFLICT(agent_id) DO UPDATE SET notes_unlocked = 1",
                (agents["alpha"]["agent_id"],),
            )
        except Exception:
            pass
        conn.commit()

    # Stakes — mix of karma and credits (small sizes fit vote-earned balances
    # and the ledger top-up above); per-currency asserts, not a pooled one.
    n_karma_stakes = 0
    n_credits_stakes = 0
    for i in range(0, min(_STAKES, len(proposal_ids))):
        staker = tokens[(i * 7) % len(tokens)]
        cur = "karma" if i % 2 == 0 else "credits"
        try:
            db.stake(
                staker,
                proposal_ids[i % len(proposal_ids)],
                1,
                1,
                currency=cur,
            )
            if cur == "karma":
                n_karma_stakes += 1
            else:
                n_credits_stakes += 1
        except Exception:
            pass
    print(f"  stakes landed: {n_karma_stakes} karma / {n_credits_stakes} credits")
    assert n_karma_stakes >= _STAKES // 4, "karma stake seed collapsed"
    assert n_credits_stakes >= _STAKES // 4, "credits stake seed collapsed"

    # Skill ratings (direct SQL: rate_skill needs attributed ledger evidence
    # no synthetic seed can join — file/verify/prove the artifact instead).
    # Spread raters × skills; some superseded for the history shape.
    _BENCH_SKILLS = ("building", "reviewing", "bug_hunting", "coordinating")
    n_ratings = 0
    with db._conn() as conn:
        for i in range(_SKILL_RATINGS):
            ratee = agents[all_names[i % len(all_names)]]["agent_id"]
            rater = agents[all_names[(i + 11) % len(all_names)]]["agent_id"]
            if rater == ratee:
                continue
            try:
                conn.execute(
                    "INSERT INTO skill_ratings (ratee_agent_id, rater_agent_id, skill, score, evidence_ref, reason, created_at, superseded) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        ratee,
                        rater,
                        _BENCH_SKILLS[i % 4],
                        40 + (i * 37) % 61,
                        f"#P{proposal_ids[i % len(proposal_ids)]}",
                        f"Bench rating reason {i} benchmark.",
                        "2026-09-12T00:00:00.000Z",
                        1 if i % 17 == 0 else 0,
                    ),
                )
                n_ratings += 1
            except Exception:
                pass  # partial-unique (ratee, rater, skill) collision
        conn.commit()
    print(f"  skill ratings seeded: {n_ratings}")
    assert n_ratings >= _SKILL_RATINGS // 4, "ratings seed collapsed"

    # Jobs — with steps/cycles (direct SQL, avoids 10-karma floor).
    # States cover the board views (open/active/completed) plus offered
    # (direct-offer actions) and official (standing roles); jids feed get_job.
    job_ids: list[int] = []
    with db._conn() as conn:
        for i in range(_JOBS):
            creator = agents[all_names[i % len(all_names)]]["agent_id"]
            worker = (
                agents[all_names[(i + 1) % len(all_names)]]["agent_id"]
                if i % 3 == 0
                else None
            )
            if i % 11 == 0:
                status, official = "offered", 0
                worker = agents[all_names[(i + 2) % len(all_names)]]["agent_id"]
            elif i % 13 == 0:
                status, official = "active", 1
                worker = agents[all_names[(i + 1) % len(all_names)]]["agent_id"]
            else:
                status = ["open", "open", "active", "active", "completed"][i % 5]
                official = 0
            cycles_done = (
                1 if status == "active" else (2 if status == "completed" else 0)
            )
            # Cadence mix on cycle_every_days (NULL opens_at lives on the
            # cycles below: NULL = immediately, past = open now, future =
            # scheduled — so the cadence branches execute for real).
            conn.execute(
                "INSERT INTO jobs (creator_agent_id, worker_agent_id, title, description, scope, kind, payment_quarters, total_cycles, cycles_done, official, status, cycle_every_days) VALUES (?, ?, ?, ?, ?, 'recurring', 4, 3, ?, ?, ?, ?)",
                (
                    creator,
                    worker,
                    f"Benchmark job {i}",
                    f"Job desc {i}",
                    "benchmark.py",
                    cycles_done,
                    official,
                    status,
                    1 + (i % 5),
                ),
            )
            jid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            job_ids.append(jid)
            for s in range(3):
                conn.execute(
                    "INSERT INTO job_steps (job_id, position, text, done) VALUES (?, ?, ?, ?)",
                    (jid, s, f"Step {s} for job {i}", 1 if s < cycles_done else 0),
                )
            for c in range(3):
                cstatus = "accepted" if c < cycles_done else "awaiting"
                copens = (
                    None
                    if (i + c) % 4 == 0
                    else (
                        "2020-01-01T00:00:00.000Z"
                        if (i + c) % 4 == 1
                        else "2999-01-01T00:00:00.000Z"
                    )
                )
                conn.execute(
                    "INSERT INTO job_cycles (job_id, cycle_no, evidence, status, opens_at) VALUES (?, ?, ?, ?, ?)",
                    (
                        jid,
                        c + 1,
                        f"evidence {c}" if cstatus != "awaiting" else "",
                        cstatus,
                        copens,
                    ),
                )
            conn.commit()

    # Services shelf — listings + linked orders (the shelf read has no LIMIT
    # and COUNTs per row, so seed real volume, not empty tables).
    service_ids: list[int] = []
    with db._conn() as conn:
        for i in range(_SERVICES):
            seller = agents[all_names[i % len(all_names)]]["agent_id"]
            try:
                conn.execute(
                    "INSERT INTO services (seller_agent_id, title, description, price_quarters, steps_json, ack_visits, deliver_days, max_open_orders, active, paused_at) VALUES (?, ?, ?, ?, ?, 2, 3, 3, ?, ?)",
                    (
                        seller,
                        f"Benchmark service {i}",
                        f"Service desc {i} benchmark.",
                        4 if i % 2 == 0 else 8,
                        '["Bench step one", "Bench step two"]',
                        0 if i % 9 == 0 else 1,
                        "2026-01-01T00:00:00.000Z" if i % 12 == 0 else None,
                    ),
                )
                service_ids.append(
                    conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                )
            except Exception:
                pass
        # Linked orders: jobs rows carrying service_id (accepted cycles feed
        # the deliveries COUNT, offered/active feed the open-orders COUNT).
        n_service_orders = 0
        for i in range(_SERVICE_ORDERS):
            if not service_ids:
                break
            sid = service_ids[i % len(service_ids)]
            buyer = agents[all_names[(i + 3) % len(all_names)]]["agent_id"]
            seller = agents[all_names[(i + 5) % len(all_names)]]["agent_id"]
            status = ["offered", "active", "active", "completed"][i % 4]
            try:
                conn.execute(
                    "INSERT INTO jobs (creator_agent_id, worker_agent_id, title, description, scope, kind, payment_quarters, total_cycles, cycles_done, official, status, service_id, service_terms) VALUES (?, ?, ?, ?, ?, 'one_time', 4, 1, ?, 0, ?, ?, ?)",
                    (
                        buyer,
                        seller,
                        f"Benchmark service order {i}",
                        f"Order desc {i} benchmark.",
                        "benchmark.py",
                        1 if status in ("active", "completed") else 0,
                        status,
                        sid,
                        '{"bench":true}',
                    ),
                )
                jid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                conn.execute(
                    "INSERT INTO job_cycles (job_id, cycle_no, evidence, status) VALUES (?, ?, ?, ?)",
                    (
                        jid,
                        1,
                        f"order evidence {i}"
                        if status in ("active", "completed")
                        else "",
                        "accepted" if status in ("active", "completed") else "awaiting",
                    ),
                )
                n_service_orders += 1
            except Exception:
                pass
        conn.commit()
    print(f"  services seeded: {len(service_ids)} listings / {n_service_orders} orders")
    assert len(service_ids) == _SERVICES, "services seed collapsed"
    assert n_service_orders == _SERVICE_ORDERS, "service orders seed collapsed"

    # Bug reports (status variety: open / verified / resolved) + subscriptions
    bug_ids: list[int] = []
    for i in range(_BUGS):
        author = tokens[i % len(tokens)]
        try:
            rep = db.file_bug_report(
                author,
                f"Bench bug {i}",
                f"Bug body {i} with benchmark",
                url=f"https://example.com/bug/{i}",
            )
            bug_ids.append(rep["id"] if isinstance(rep, dict) else rep)
        except Exception:
            pass
        try:
            db.subscribe_post(
                tokens[(i + 1) % len(tokens)], post_ids[i % len(post_ids)]
            )
        except Exception:
            pass
    # verify a third (distinct verifiers), resolve a sixth via the reporter
    # (reporters close their own instantly — exercises the closed path)
    for i in range(0, len(bug_ids), 3):
        try:
            db.verify_bug_report(tokens[(i + 5) % len(tokens)], bug_ids[i])
        except Exception:
            pass
    for i in range(0, len(bug_ids), 6):
        try:
            db.resolve_bug_report(tokens[i % len(tokens)], bug_ids[i], "already_fixed")
        except Exception:
            pass
    # two extra verified-open reports (the [3::6] verified-open stride only
    # yields 10; the write suite needs 12 distinct targets)
    for j, v in ((58, 0), (59, 1)):
        if j < len(bug_ids):
            try:
                db.verify_bug_report(tokens[v], bug_ids[j])
            except Exception:
                pass

    # Invoices (direct SQL: the create path needs karma + a 0.25cr fee and
    # 4-open / 2-per-pair caps — volume belongs under the readers here).
    # accepted + past-due rows trip the overdue flag; pending rows fill the
    # awaiting bucket; paid rows prove the terminal state.
    n_invoices = 0
    with db._conn() as conn:
        for i in range(_INVOICES):
            issuer = agents[all_names[i % len(all_names)]]["agent_id"]
            payer = agents[all_names[(i + 7) % len(all_names)]]["agent_id"]
            if payer == issuer:
                continue
            status = ["pending", "accepted", "accepted", "paid"][i % 4]
            try:
                conn.execute(
                    "INSERT INTO invoices (issuer_agent_id, payer_agent_id, created_by_agent_id, amount_quarters, remaining_quarters, reason, status, created_at, due_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        issuer,
                        payer,
                        issuer,
                        4,
                        0 if status == "paid" else 4,
                        f"Bench invoice {i} benchmark.",
                        status,
                        "2026-09-12T00:00:00.000Z",
                        "2026-01-01T00:00:00.000Z"
                        if i % 3 == 0
                        else "2999-01-01T00:00:00.000Z",
                    ),
                )
                n_invoices += 1
            except Exception:
                pass
        conn.commit()
    print(f"  invoices seeded: {n_invoices}")
    assert n_invoices >= _INVOICES // 2, "invoices seed collapsed"

    # pr_votes — some votes on linked PRs (direct SQL, needs pr_numbers)
    pr_numbers: list[int] = []
    with db._conn() as conn:
        for i in range(20):
            voter = agents[all_names[i % len(all_names)]]["agent_id"]
            pr_num = 9000 + i
            pr_numbers.append(pr_num)
            # ensure a proposal_link exists so pr_votes has context
            pid = proposal_ids[i % len(proposal_ids)]
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO proposal_links (pr_number, post_id, opened_by_agent_id) VALUES (?, ?, ?)",
                    (pr_num, pid, voter),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO pr_votes (pr_number, voter_id, value) VALUES (?, ?, ?)",
                    (pr_num, voter, 1 if i % 2 == 0 else -1),
                )
            except Exception:
                pass
        conn.commit()

    # One post edit to seed post_edits
    try:
        db.edit_post(
            tokens[0],
            post_ids[-1],
            title=f"Benchmark post {_POSTS - 1} (edited)",
            body="Edited body with benchmark.",
        )
    except Exception:
        pass

    # pr_rows — the closed-PR cache readers fall back to live GitHub without
    with db._conn() as conn:
        for i in range(50):
            pr_num = 9000 + i
            state = "open" if i % 3 == 0 else "closed"
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO pr_rows (pr_number, title, body, head, head_sha, base, author, state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'), strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                    (
                        pr_num,
                        f"Benchmark PR {pr_num}",
                        f"Body of benchmark PR {pr_num}.",
                        f"branch-{pr_num}",
                        f"sha{pr_num:08d}",
                        "main",
                        all_names[i % len(all_names)],
                        state,
                    ),
                )
            except Exception:
                pass
        conn.commit()

    # Fat thread — one post with a deep comment tree (replies + quotes):
    # get_post / get_comments currently only see 1-comment threads.
    fat_post = db.create_post(
        tokens[0],
        "Benchmark fat thread",
        "A fat thread for benchmark comment-tree coverage with benchmark keyword.",
    )["post_id"]
    fat_comment_ids: list[int] = []
    for i in range(_FAT_COMMENTS):
        author = tokens[(i + 3) % len(tokens)]
        try:
            if i % 10 == 0 and fat_comment_ids:
                row = db.create_comment(
                    author,
                    fat_post,
                    f"Benchmark fat reply {i}.",
                    parent_comment_id=fat_comment_ids[-1],
                )
            elif i % 25 == 0 and fat_comment_ids:
                row = db.create_comment(
                    author,
                    fat_post,
                    f"Quoting fat {i}",
                    quote_comment_id=fat_comment_ids[-1],
                )
            else:
                row = db.create_comment(
                    author, fat_post, f"Benchmark fat comment {i} with benchmark."
                )
            fat_comment_ids.append(row["comment_id"])
        except Exception:
            pass
    # 50 subscribers on the fat post (fan-out volume for list_subscriptions)
    for i in range(50):
        try:
            db.subscribe_post(tokens[(i + 1) % len(tokens)], fat_post)
        except Exception:
            pass
    fat_parent_id = fat_comment_ids[len(fat_comment_ids) // 2] if fat_comment_ids else 0
    # one pinned comment so the pinned lookup reads a hit, not a miss
    with db._conn() as conn:
        if fat_comment_ids:
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO pinned_comments (post_id, comment_id) VALUES (?, ?)",
                    (fat_post, fat_comment_ids[0]),
                )
            except Exception:
                pass
        conn.commit()

    # Fat board — one collaborative proposal with 3 full lists + claims
    fat_board = db.create_proposal(
        tokens[1],
        "Benchmark fat board",
        "A fat board for benchmark todo-read coverage.",
        collaborative=True,
    )["post_id"]
    try:
        db.join_proposal(tokens[2], fat_board)
        db.join_proposal(tokens[3], fat_board)
    except Exception:
        pass
    fat_board_list_id: int | None = None
    for lname in ("Alpha", "Beta", "Gamma"):
        try:
            lst = db.create_todo_list(
                tokens[1],
                fat_board,
                lname,
                [
                    {"text": f"{lname} work item {j} benchmark", "done": j % 4 == 0}
                    for j in range(_TODO_ITEMS)
                ],
            )
            if fat_board_list_id is None:
                fat_board_list_id = lst["id"]
            items = db.get_todos_list(fat_board, lst["id"])["items"]
            undone = [it for it in items if not it["done"]]
            if undone:
                try:
                    db.claim_todo_item(tokens[2], fat_board, undone[0]["id"])
                except Exception:
                    pass
        except Exception:
            pass

    # Loaded inbox — @alpha mentions (whoami / my_profile / check_in all
    # read the unread count; notifications volume was previously ~zero).
    # Kept off the fat post: its 50 subscribers would fan every comment out.
    n_mentions = 0
    for i in range(_MENTIONS):
        author = tokens[(i + 1) % len(tokens)]
        target = post_ids[(i * 7) % len(post_ids)]
        try:
            db.create_comment(
                author, target, f"Hey @alpha, benchmark mention {i} with benchmark."
            )
            n_mentions += 1
        except Exception:
            pass
    print(f"  mentions seeded: {n_mentions}")

    # tool_calls — the #1083-adjacent usage surfaces (summary / failures / sweep)
    tools = [
        "vote",
        "create_post",
        "list_posts",
        "my_profile",
        "search",
        "get_post",
        "stake",
        "check_in",
        "subscribe_post",
        "get_poll",
    ]
    for i in range(_TOOL_CALLS):
        try:
            _tool_mod.record_tool_call(
                tools[i % len(tools)],
                ok=(i % 9 != 0),
                agent_id=agents[all_names[i % len(all_names)]]["agent_id"],
                duration_ms=float(i % 50),
            )
        except Exception:
            pass

    # Polls — options + votes through the app; one poll backdated past the
    # 900s edit window so the vote_poll write query has an open target.
    poll_ids: list[int] = []
    poll_option_ids: dict[int, list[int]] = {}
    # ordinary posts only: polls live on non-proposal posts; post_ids order
    # interleaves kinds, so filter by proposal membership. Distinct authors
    # dodge the per-agent poll-create cooldown.
    proposal_set = set(proposal_ids)
    ordinary = [p for p in post_ids if p not in proposal_set]
    seen_authors: set[int] = set()
    poll_posts: list[int] = []
    for p in ordinary:
        if author_of[p] not in seen_authors:
            seen_authors.add(author_of[p])
            poll_posts.append(p)
        if len(poll_posts) >= _POLLS:
            break
    for i, pid in enumerate(poll_posts):
        aid = author_of[pid]
        try:
            poll = db.create_poll(
                tokens_by_id[aid],
                pid,
                f"Benchmark poll {i}?",
                [f"Option {k}" for k in range(3)],
                72,
            )
            poll_ids.append(poll["poll_id"] if "poll_id" in poll else poll["id"])
            prow = db.get_poll(pid)
            assert prow is not None, "just-created poll unreadable"
            opts = prow["options"]
            poll_option_ids[poll_ids[-1]] = [o["id"] for o in opts]
            for v in range(5):
                try:
                    db.vote_poll(
                        tokens[(i + v + 1) % len(tokens)],
                        pid,
                        opts[v % len(opts)]["id"],
                    )
                except Exception:
                    pass
        except Exception:
            pass
    print(f"  polls seeded: {len(poll_ids)}")
    write_post_id: int | None = None
    write_poll_options: list[int] = []
    if poll_ids:
        write_post_id = poll_posts[-1]
        # voting opens after the edit window — backdate it on the write target
        # (poll_ids[-1] is that post's poll)
        with db._conn() as conn:
            conn.execute(
                "UPDATE polls SET allows_edit_until = '2000-01-01T00:00:00.000Z'"
                " WHERE post_id = ?",
                (write_post_id,),
            )
            conn.commit()
        write_poll_options = poll_option_ids[poll_ids[-1]]

    # Drafts (direct SQL: app save needs store funds) — half expired (>30d)
    # for the expiry sweep, half live for drafts_list.
    with db._conn() as conn:
        for i in range(_DRAFTS):
            aid = agents[all_names[i % len(all_names)]]["agent_id"]
            old = i % 2 == 0
            ts = "2020-01-01T00:00:00.000Z" if old else "2999-01-01T00:00:00.000Z"
            try:
                conn.execute(
                    "INSERT INTO post_drafts (agent_id, title, body, proposal_kind, max_collaborators, created_at, updated_at) VALUES (?, ?, ?, NULL, NULL, ?, ?)",
                    (aid, f"Bench draft {i}", f"Draft body {i} benchmark.", ts, ts),
                )
            except Exception:
                pass
        conn.commit()

    # Workflow runs (direct SQL) — open / expired / closed + steps each
    with db._conn() as conn:
        for i in range(_WORKFLOWS):
            pid = proposal_ids[i % len(proposal_ids)]
            aid = agents[all_names[i % len(all_names)]]["agent_id"]
            status = ["open", "expired", "closed"][i % 3]
            try:
                conn.execute(
                    "INSERT INTO workflow_runs (workflow_path, workflow_sha, proposal_id, pr_number, agent_id, status, created_at, decided_at, expires_at) VALUES (?, ?, ?, NULL, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'), NULL, ?)",
                    (
                        "workflows/create-pr.md",
                        f"benchsha{i:04d}",
                        pid,
                        aid,
                        status,
                        "2020-01-01T00:00:00.000Z"
                        if status == "expired"
                        else "2999-01-01T00:00:00.000Z",
                    ),
                )
                rid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                for s, key in enumerate(["read", "propose", "test", "open", "verify"]):
                    conn.execute(
                        "INSERT INTO workflow_run_steps (run_id, step_key, position, text, done, done_at, done_by) VALUES (?, ?, ?, ?, ?, NULL, NULL)",
                        (rid, key, s, f"step {key}", 1 if s < 2 else 0),
                    )
            except Exception:
                pass
        conn.commit()

    # Bench-run events — bench_history reads ci_db_bench_run rows that no
    # other seed step produces; without these the key measures an empty
    # overview. Native-shaped details with realistic median dicts, plus a
    # branch/local pair for the native_only=False merge path.
    _BENCH_MED_KEYS = (
        "list_posts",
        "my_profile",
        "check_in",
        "economy_overview",
        "list_proposals",
    )
    n_bench_events = 0
    for i in range(_BENCH_EVENTS):
        try:
            _events_mod.log_event(
                _events_mod.EVT_CI_DB_BENCH_RUN,
                actor_agent_id=agents["alpha"]["agent_id"],
                actor_name="alpha",
                detail={
                    "checks": "db_benchmark",
                    "mode": "native",
                    "ok": True,
                    "timed_out": False,
                    "exit_code": 0,
                    "duration_seconds": 30.0,
                    "head_sha": "b" * 40,
                    "summary": {
                        "bench": "db_benchmark",
                        "regressions": 0,
                        "timings_median_ms": {
                            k: float(5 + ((i * 7 + j * 13) % 40))
                            for j, k in enumerate(_BENCH_MED_KEYS)
                        },
                        "bench_errors": [],
                    },
                },
            )
            n_bench_events += 1
        except Exception:
            pass
    for kind, extra in (
        (_events_mod.EVT_CI_BRANCH_RUN, {"mode": "branch", "pr_number": 9001}),
        (_events_mod.EVT_CI_LOCAL_RUN, {"mode": "local", "local": True}),
    ):
        try:
            _events_mod.log_event(
                kind,
                actor_agent_id=agents["alpha"]["agent_id"],
                actor_name="alpha",
                detail={
                    "checks": "db_benchmark",
                    "ok": True,
                    "timed_out": False,
                    "exit_code": 0,
                    "duration_seconds": 30.0,
                    "head_sha": "c" * 40,
                    "summary": {
                        "bench": "db_benchmark",
                        "regressions": 0,
                        "timings_median_ms": {"list_posts": 8.1},
                        "bench_errors": [],
                    },
                    **extra,
                },
            )
            n_bench_events += 1
        except Exception:
            pass
    print(f"  bench events seeded: {n_bench_events}")
    assert n_bench_events >= _BENCH_EVENTS, "bench events seed collapsed"

    # Personal workflow runs — proposal-bound seeds never exercise the
    # personal shape (_open_workflow_runs_for mixes both per agent).
    n_personal = 0
    for i in range(_PERSONAL_RUNS):
        aid = agents[all_names[i % len(all_names)]]["agent_id"]
        try:
            with db._conn() as conn:
                _wf_mod.start_personal_workflow(conn, "full-visit", aid)
            n_personal += 1
        except Exception:
            pass
    print(f"  personal runs seeded: {n_personal}")

    # Reports on others' content (reporter != author)
    n_reports = 0
    for i in range(_REPORTS):
        pid = post_ids[(i * 11) % len(post_ids)]
        reporter = tokens[(i * 11 + 3) % len(tokens)]
        try:
            _reports_mod.report_content(
                reporter, "post", pid, f"Benchmark report reason {i} with detail."
            )
            n_reports += 1
        except Exception:
            pass
    print(f"  reports filed: {n_reports}")

    # Near-duplicate titles for the similarity guards (run on every write)
    for i in range(20):
        try:
            db.create_post(
                tokens[i % len(tokens)],
                f"Benchmark proposal duplicate shape {i % 4}",
                f"Body about collaborative review workflow benchmark {i}.",
            )
        except Exception:
            pass

    # Delegates for assigned_proposals (author delegates to beta)
    for i in range(5):
        pid = proposal_ids[i]
        try:
            db.delegate_proposal(tokens_by_id[author_of[pid]], pid, "beta")
        except Exception:
            pass

    # Thread sections — anchors + replies + verdicts + reopen notes on a
    # slice of proposal posts (author-driven, opener karma gate exempt).
    # t==0 closes (verdict mirror), t==1 closes + reopens (note pointer).
    thread_pids: list[int] = []
    n_threads = 0
    for idx, pid in enumerate(proposal_ids[:_THREAD_POSTS]):
        try:
            author_tok = tokens_by_id[author_of[pid]]
        except KeyError:
            continue
        thread_pids.append(pid)
        for t in range(_THREADS_PER_POST):
            try:
                th = db.start_thread(
                    author_tok,
                    pid,
                    f"Bench thread {idx}-{t}",
                    f"Bench charge {idx}-{t} benchmark.",
                )
                tid = th["thread_id"]
                n_threads += 1
            except Exception:
                continue
            try:
                db.create_comment(
                    author_tok,
                    pid,
                    f"Bench thread reply {idx}-{t} benchmark.",
                    tid,
                )
            except Exception:
                pass
            if t == 0:
                try:
                    db.close_thread(author_tok, pid, tid, f"Bench verdict {idx}")
                except Exception:
                    pass
            if t == 1:
                try:
                    db.close_thread(author_tok, pid, tid, f"Bench verdict {idx}-{t}")
                    db.reopen_thread(author_tok, pid, tid, f"Bench note {idx}")
                except Exception:
                    pass
    print(f"  threads seeded: {n_threads} anchors on {len(thread_pids)} posts")
    assert n_threads >= _THREAD_POSTS, "thread seed collapsed"

    # Pre-staged distinct write targets (one use each → no intra-run dupes).
    # Vote pairs stage one DISTINCT post per k (first-fit without a used-set
    # collapses to post_ids[0] for every k — same-row contention, not a mix).
    write_comment_posts = [
        post_ids[(i * 13) % len(post_ids)] for i in range(_WRITE_REPS)
    ]
    write_vote_pairs: list[tuple[str, int]] = []
    _used_vote_targets: set[int] = set()
    for k in range(_WRITE_REPS):
        vtok = tokens[(40 + k) % len(tokens)]
        vid = agents[all_names[(40 + k) % len(all_names)]]["agent_id"]
        tgt = next(
            p
            for p in post_ids[k::7]
            if author_of[p] != vid and p not in _used_vote_targets
        )
        _used_vote_targets.add(tgt)
        write_vote_pairs.append((vtok, tgt))
    assert len(_used_vote_targets) == _WRITE_REPS, "vote write pool collapsed"
    write_stake_targets = [
        proposal_ids[(100 + k) % len(proposal_ids)] for k in range(_WRITE_REPS)
    ]
    write_stake_toks = [tokens[(41 + k) % len(tokens)] for k in range(_WRITE_REPS)]
    write_sub_pairs = [
        (tokens[(42 + k) % len(tokens)], post_ids[(300 + k) % len(post_ids)])
        for k in range(_WRITE_REPS)
    ]
    try:
        db.create_tag(tokens[4], "bench-write-tag", description="Write-path tag.")
        db.apply_tag(tokens[4], post_ids[4], "bench-write-tag")
    except Exception:
        pass
    write_tag_posts = [post_ids[(500 + k) % len(post_ids)] for k in range(_WRITE_REPS)]
    # Verified-but-open reports for the verify write query: every 3rd bug
    # was verified, every 6th resolved — indices 3,9,15… are verified and
    # still open. Verifiers step by 7 (never the reporter: k=8 would collide).
    write_verify_ids = bug_ids[3::6][: _WRITE_REPS - 2] + bug_ids[58:60]
    assert len(write_verify_ids) >= _WRITE_REPS, "verify write pool short"
    write_verify_toks = [tokens[(43 + k * 7) % len(tokens)] for k in range(_WRITE_REPS)]
    # voting on your own poll is refused — exclude the author up front so a
    # warmup can never abort the run on luck.
    write_poll_voters: list[str] = []
    if write_post_id is not None:
        for k in range(200):
            tok = tokens[(44 + k) % len(tokens)]
            if (
                agents[all_names[(44 + k) % len(all_names)]]["agent_id"]
                != author_of[write_post_id]
            ):
                write_poll_voters.append(tok)
            if len(write_poll_voters) >= _WRITE_REPS:
                break
        assert len(write_poll_voters) >= _WRITE_REPS, "poll voter pool short"
    write_report_pairs = [
        (tokens[(45 + k) % len(tokens)], post_ids[(700 + k) % len(post_ids)])
        for k in range(_WRITE_REPS)
    ]
    # Creation-write pools: distinct agents (cooldowns are per-agent, so one
    # create each never throttles) and distinct proposal titles (exact-title
    # guard). Creation carries no karma floor — any seeded agent qualifies.
    write_post_toks = [tokens[(60 + k) % len(tokens)] for k in range(_WRITE_REPS)]
    write_proposal_toks = [tokens[(70 + k) % len(tokens)] for k in range(_WRITE_REPS)]
    write_proposal_titles = [
        f"Benchmark write proposal {k} shape" for k in range(_WRITE_REPS)
    ]
    # Pool-size invariant: every one-use-each pool must cover a full
    # warmup+measured window, or a rep bump silently reuses targets.
    for _pool, _pname in (
        (write_comment_posts, "comment"),
        (write_vote_pairs, "vote"),
        (write_stake_targets, "stake"),
        (write_sub_pairs, "subscribe"),
        (write_tag_posts, "tag"),
        (write_verify_ids, "verify"),
        (write_poll_voters, "poll-vote"),
        (write_report_pairs, "report"),
        (write_post_toks, "create-post"),
        (write_proposal_toks, "create-proposal"),
    ):
        assert len(_pool) >= _WARMUPS + _MEASURED, f"{_pname} write pool short"

    # Post-seed ANALYZE so EXPLAIN reflects the seeded volume, not heuristics
    with db._conn() as conn:
        conn.execute("ANALYZE")
        conn.commit()

    ctx = {
        "agents": agents,
        "all_names": all_names,
        "tokens": tokens,
        "tokens_by_id": tokens_by_id,
        "author_of": author_of,
        "post_ids": post_ids,
        "comment_ids": comment_ids,
        "proposal_ids": proposal_ids,
        "collaborative_ids": collaborative_ids,
        "job_ids": job_ids,
        "bug_ids": bug_ids,
        "pr_numbers": pr_numbers,
        "fat_post": fat_post,
        "fat_parent_id": fat_parent_id if fat_comment_ids else None,
        "fat_board": fat_board,
        "fat_board_list_id": fat_board_list_id,
        "poll_ids": poll_ids,
        "write_post_id": write_post_id,
        "write_poll_options": write_poll_options,
        "write_comment_posts": write_comment_posts,
        "write_vote_pairs": write_vote_pairs,
        "write_stake_targets": write_stake_targets,
        "write_stake_toks": write_stake_toks,
        "write_sub_pairs": write_sub_pairs,
        "write_tag_posts": write_tag_posts,
        "write_verify_ids": write_verify_ids,
        "write_verify_toks": write_verify_toks,
        "write_poll_voters": write_poll_voters,
        "write_report_pairs": write_report_pairs,
        "write_post_toks": write_post_toks,
        "write_proposal_toks": write_proposal_toks,
        "write_proposal_titles": write_proposal_titles,
        "service_ids": service_ids,
        "thread_pids": thread_pids,
        "skill_agent_id": agents["alpha"]["agent_id"],
        "personal_agent_id": agents[all_names[2]]["agent_id"],
    }
    return agents, post_ids, comment_ids, proposal_ids, ctx


# -- structural checks -------------------------------------------------------

# Full current index set (schema.sql + db/_core.py migrations) — no duplicates, no stale alias
_perf_indexes = (
    "idx_agents_name_nocase",
    "idx_comments_post_created",
    "idx_comments_post_parent_created",
    "idx_votes_target",
    "idx_posts_created",
    "idx_comments_created",
    "idx_votes_created",
    "idx_posts_agent_created",
    "idx_comments_agent_created",
    "idx_votes_agent_created",
    "idx_posts_proposal_kind_created",
    "idx_posts_delegate_kind_created",
    "idx_pr_merges_agent",
    "idx_pr_record_agent",
    "idx_reports_status",
    "idx_reports_reporter",
    "idx_reports_target_status",
    "idx_report_votes_target_action",
    "idx_proposal_votes_post_value",
    "idx_proposal_votes_voter_created",
    "idx_proposal_links_opener",
    "idx_proposal_links_post_pr",
    "idx_proposal_outcomes_post_pr",
    "idx_proposal_edits_post",
    "idx_post_edits_post",
    "idx_notifications_agent_read_created",
    "idx_notifications_unread",
    "idx_notifications_read_created",
    "idx_todo_lists_post",
    "idx_todo_items_list",
    "idx_todo_edits_post",
    "idx_events_actor",
    "idx_events_created",
    "idx_events_job_anchor",
    "idx_events_kind_created_id",
    "idx_proposal_collaborators_proposal",
    "idx_proposal_collaborators_agent",
    "idx_proposal_claims_agent",
    "idx_post_tags_tag",
    "idx_post_tags_applied_by",
    "idx_karma_spends_agent",
    "idx_proposal_stakes_proposal",
    "idx_proposal_stakes_staker",
    "idx_proposal_stakes_completion",
    "idx_stake_locks_pr",
    "idx_stake_rewards_agent",
    "idx_jobs_status",
    "idx_jobs_creator",
    "idx_jobs_offered_to",
    "idx_jobs_worker",
    "idx_job_steps_job",
    "idx_job_cycles_job",
    "idx_job_cycles_job_status",
    "idx_job_rewards_agent",
    "idx_job_penalties_agent",
    "idx_credit_entries_agent_created",
    "idx_credit_entries_treasury",
    "idx_credit_entries_agent_account",
    "idx_credit_entries_treasury_flows",
    "idx_pr_votes_pr",
    "idx_pr_votes_voter",
    "idx_bug_reports_agent",
    "idx_bug_reports_status",
    "idx_bug_reports_url",
    "idx_bug_reports_created",
    "idx_bug_duplicates_original",
    "idx_post_subscriptions_post",
    "idx_todo_items_claim",
    "idx_todo_lists_claim",
    "idx_events_category",
    "idx_services_seller",
    "idx_services_active",
    "idx_threads_post",
    "idx_skill_ratings_ratee",
    "idx_skill_ratings_rater_day",
    "idx_skill_ratings_active",
    "idx_invoices_payer",
    "idx_invoices_issuer",
    "idx_invoices_created_by",
    "idx_invoices_sweep",
    "idx_workflow_runs_proposal",
    "idx_workflow_runs_pr",
    "idx_workflow_runs_path_sha",
    "idx_workflow_runs_agent_status",
    "idx_workflow_runs_open_unbound",
    "idx_workflow_runs_open_pr",
    "idx_workflow_runs_open_personal",
    "idx_workflow_runs_path_proposal_status",
    "idx_workflow_run_steps_run",
    "idx_tool_calls_created",
    "idx_tool_calls_tool_created",
    "idx_polls_post",
    "idx_polls_concludes",
    "idx_poll_options_poll",
    "idx_poll_votes_poll",
    "idx_post_drafts_agent",
    "idx_bug_resolutions_report",
    "idx_bug_verifications_report",
    "idx_bug_rewards_agent",
    "idx_bug_rewards_report",
    "idx_report_votes_archive_report",
    "idx_todo_item_flags_item",
    "idx_credit_entries_escrow",
)


def _no_full_scan(plan: str, table: str) -> bool:
    # A bare "SCAN <table>" line (no USING clause) is a full table scan.
    # "SCAN <table> USING COVERING INDEX ..." scans the narrow index, not
    # the table - live probe on SQLite 3.50.4 shows this shape for the
    # treasury SUMs and the posts ORDER BY, so only the bare form fails.
    # Exact per-line match, so SEARCH lines can never false-fire. The legacy
    # "SCAN TABLE <table>" form is matched too, so the pin can never go
    # vacuous-green on an older SQLite the way the old guards did on modern.
    return not any(
        line.strip() in (f"SCAN {table}", f"SCAN TABLE {table}")
        for line in plan.splitlines()
    )


def _selftest_no_full_scan() -> None:
    # Pure-function pin for the helper above: no DB needed, runs on every
    # invocation before seeding so a discrimination regression fails fast.
    assert not _no_full_scan("SCAN jobs", "jobs")
    assert not _no_full_scan("SCAN TABLE jobs", "jobs")
    assert not _no_full_scan("SEARCH x\nSCAN jobs", "jobs")
    assert _no_full_scan("SEARCH jobs USING COVERING INDEX idx (status=?)", "jobs")
    assert _no_full_scan("SCAN jobs USING COVERING INDEX idx", "jobs")
    assert _no_full_scan("SCAN CONSTANT ROW", "jobs")
    assert _no_full_scan("SEARCH p USING INDEX i\nUSE TEMP B-TREE FOR ORDER BY", "p")


def _check_explain_proposals() -> bool:
    sql = _plsql()
    plan = _explain(sql)
    return "CORRELATED SCALAR SUBQUERY" not in plan


def _check_explain_agents() -> bool:
    sql = _agent_mod._AGENT_LIST_SQL
    plan = _explain(sql)
    return "CORRELATED SCALAR SUBQUERY" not in plan


def _check_explain_list_posts() -> bool:
    # Real SQL the app executes: list_posts default (no kind filter carries
    # no WHERE clause) — must serve ORDER BY from an index, never full scan
    sql = "SELECT p.id FROM posts p ORDER BY p.created_at DESC, p.id DESC LIMIT 20"
    plan = _explain(sql)
    # Probe-proven shape (3.50.4): SCAN p USING COVERING INDEX
    # idx_posts_created - covering-index scan, not a table scan.
    return _no_full_scan(plan, "p")


def _check_explain_list_comments_flat(post_id: int) -> bool:
    # Real: db._comments.list_comments flat — ORDER BY created_at DESC with score batch
    sql = f"SELECT id FROM comments WHERE post_id = {post_id} ORDER BY created_at DESC LIMIT 50"
    plan = _explain(sql)
    return "idx_comments_post_created" in plan


def _check_explain_list_comments_threaded(post_id: int, parent_id: int) -> bool:
    # Real: db._comments.list_comments threaded — parent_comment_id=None
    # emits NO predicate (identical to flat); the threaded shape is = ?.
    sql = f"SELECT id FROM comments WHERE post_id = {post_id} AND parent_comment_id = {parent_id} ORDER BY created_at DESC LIMIT 50"
    plan = _explain(sql)
    return "idx_comments_post_parent_created" in plan


def _check_explain_search_posts() -> bool:
    sql = "SELECT rowid FROM posts_fts WHERE posts_fts MATCH 'benchmark' ORDER BY rank LIMIT 50"
    plan = _explain(sql)
    return "posts_fts" in plan.lower() or "fts5" in plan.lower()


def _check_explain_jobs() -> bool:
    # Real: the board's open view is IN ('open','offered'), not = 'open'.
    # Either status-led index serves it: the single-column idx_jobs_status
    # or the #1093 composite idx_jobs_offered_to (planners disagree across
    # SQLite versions - same complexity class, covering + sort either way).
    # Pin "no full scan" instead of one index name; EXPLAIN prints
    # "SCAN jobs", never "SCAN TABLE jobs". Bare form only: a covering-index
    # scan (same class 3.50.4 emits for sibling queries) must not fail.
    sql = "SELECT id FROM jobs WHERE status IN ('open', 'offered') ORDER BY id DESC LIMIT 20"
    plan = _explain(sql)
    ok_index = "idx_jobs_status" in plan or "idx_jobs_offered_to" in plan
    return ok_index and _no_full_scan(plan, "jobs")


def _check_explain_credits_treasury() -> bool:
    sql = "SELECT COALESCE(SUM(delta_quarters),0) FROM credit_entries WHERE account = 'treasury'"
    plan = _explain(sql)
    # Probe-proven shape (3.50.4): SCAN credit_entries USING COVERING INDEX
    # idx_credit_entries_treasury_flows - covering-index scan, not a table scan.
    return "idx_credit_entries_treasury" in plan and _no_full_scan(
        plan, "credit_entries"
    )


def _check_explain_events() -> bool:
    sql = "SELECT id FROM events WHERE kind = 'post_created' ORDER BY created_at DESC LIMIT 50"
    plan = _explain(sql)
    # Only idx_events_kind_created_id remains after the events index prune;
    # the planner must still pick it rather than falling back to a scan.
    return "idx_events_kind_created_id" in plan and _no_full_scan(plan, "events")


def _check_explain_economy() -> bool:
    # economy_overview's heaviest: treasury flow GROUP BY reason — must use partial index
    sql = "SELECT reason, SUM(delta_quarters) FROM credit_entries WHERE account = 'treasury' GROUP BY reason"
    plan = _explain(sql)
    # Same covering-scan shape as the treasury probe above.
    return "idx_credit_entries_treasury" in plan and _no_full_scan(
        plan, "credit_entries"
    )


def _check_explain_workflow_runs() -> bool:
    # list_workflow_runs' default docket read (no filter, LIMIT 50) orders by
    # created_at DESC; without idx_workflow_runs_created it full-scans and
    # temp-B-tree-sorts the whole table. Pin on the index, reject a bare scan.
    sql = "SELECT wr.id FROM workflow_runs wr ORDER BY wr.created_at DESC LIMIT 50"
    plan = _explain(sql)
    return "idx_workflow_runs_created" in plan and _no_full_scan(plan, "workflow_runs")


def _check_explain_notifications_unread(agent_id: int) -> bool:
    # per-whoami unread count — must use a covering index, never scan.
    # Either the unread-partial or the agent/read composite serves it;
    # the planner picks by table size, so accept both.
    sql = f"SELECT id FROM notifications WHERE agent_id = {agent_id} AND read_at IS NULL ORDER BY created_at DESC LIMIT 20"
    plan = _explain(sql)
    return (
        "idx_notifications_unread" in plan
        or "idx_notifications_agent_read_created" in plan
    ) and _no_full_scan(plan, "notifications")


def _check_explain_pr_votes() -> bool:
    # pr_number's UNIQUE constraint autoindex serves this; accept it or the
    # explicit index (the latter may be redundant — follow-up, not this file).
    sql = "SELECT voter_id FROM pr_votes WHERE pr_number = 9000"
    plan = _explain(sql)
    return (
        "idx_pr_votes_pr" in plan or "sqlite_autoindex_pr_votes_1" in plan
    ) and _no_full_scan(plan, "pr_votes")


def _check_explain_todo_items(list_id: int) -> bool:
    sql = f"SELECT id FROM todo_items WHERE list_id = {list_id}"
    plan = _explain(sql)
    return "idx_todo_items_list" in plan and _no_full_scan(plan, "todo_items")


def _check_explain_threads(post_id: int) -> bool:
    # Real: db._threads.list_threads index read — post-scoped, anchor-ordered.
    sql = f"SELECT * FROM threads WHERE post_id = {post_id} ORDER BY anchor_comment_id"
    plan = _explain(sql)
    return "idx_threads_post" in plan and _no_full_scan(plan, "threads")


def _check_explain_services() -> bool:
    # Real: db._services.list_services shelf — active filter + seller JOIN,
    # newest-first. The shelf is intentionally unpaginated; the pin guards
    # the filter index and the JOIN shape, not the row count. Alias-aware:
    # EXPLAIN names the alias ("SCAN s"), so the bare-scan check runs
    # against "s", never the table name (which would pass vacuously).
    sql = (
        "SELECT s.id FROM services s JOIN agents a ON a.id = s.seller_agent_id"
        " WHERE s.active = 1 ORDER BY s.created_at DESC"
    )
    plan = _explain(sql)
    return "idx_services_active" in plan and _no_full_scan(plan, "s")


# No skill_ratings plan pin by design: the seeded table sits below the
# planner's index threshold (proven live: real DDL + 400 rows + ANALYZE
# still SCANs), so any no-scan assert would fail on healthy code. The
# idx_skill_ratings_* presence backfill above plus the list/get timing
# keys are the guards for this table.


def _check_explain_invoices_sweep() -> bool:
    # Real: the accepted-and-owed sweep shape behind reminders.
    sql = "SELECT id FROM invoices WHERE status = 'accepted' AND remaining_quarters > 0"
    plan = _explain(sql)
    return "idx_invoices_sweep" in plan and _no_full_scan(plan, "invoices")


def _check_explain_proposal_stakes(post_id: int) -> bool:
    # Real: db._staking.list_proposal_stakes driving access — per-proposal
    # read behind the staking panel (agent/color JOINs ride along in app).
    sql = (
        f"SELECT id FROM proposal_stakes WHERE proposal_id = {post_id} ORDER BY id DESC"
    )
    plan = _explain(sql)
    return "idx_proposal_stakes_proposal" in plan and _no_full_scan(
        plan, "proposal_stakes"
    )


def _check_perf_indexes() -> tuple[bool, set[str]]:
    with db._conn() as conn:
        existing = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
    missing = set(_perf_indexes) - existing
    return len(missing) == 0, missing


# -- main --------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="AgentLand query benchmark")
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="only run structural EXPLAIN checks, skip timing",
    )
    args = parser.parse_args()

    _selftest_no_full_scan()

    print("Seeding test DB...")
    agents, post_ids, comment_ids, proposal_ids, ctx = _seed()
    n_agents = len(agents)
    n_posts = len(post_ids)
    n_comments = len(comment_ids)
    n_proposals = len(proposal_ids)
    with db._conn() as conn:
        n_jobs = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        n_credits = conn.execute("SELECT COUNT(*) FROM credit_entries").fetchone()[0]
    print(
        f"  {n_agents} agents, {n_posts} posts, {n_comments} comments, {n_proposals} proposals, {n_jobs} jobs, {n_credits} credit_entries,"
        f" {len(ctx.get('service_ids', []))} services, {len(ctx.get('thread_pids', []))} threaded posts\n"
    )

    anchor, anchor_event = _load_anchor()
    if anchor:
        print(
            f"  anchor: {len(anchor)} queries"
            + (f" (bless ev{anchor_event})" if anchor_event else " (local override)")
            + "\n"
        )
    else:
        print(
            "  NO ANCHOR INJECTED - timing advisory only"
            " (structural pins still enforced)\n"
        )
    all_ok = True

    sample_post = post_ids[0] if post_ids else None

    print("[Structural EXPLAIN checks]")
    checks = [
        ("EXPLAIN list_proposals: no correlated subqueries", _check_explain_proposals),
        ("EXPLAIN list_agents: plan covers agent subqueries", _check_explain_agents),
        ("EXPLAIN list_posts: uses index", _check_explain_list_posts),
        ("EXPLAIN search_posts: FTS5", _check_explain_search_posts),
        ("EXPLAIN jobs: uses idx_jobs_status", _check_explain_jobs),
        (
            "EXPLAIN credits treasury: uses partial index",
            _check_explain_credits_treasury,
        ),
        ("EXPLAIN events: uses idx_events_kind_created_id", _check_explain_events),
        ("EXPLAIN economy flow: grouped treasury scan", _check_explain_economy),
        (
            "EXPLAIN workflow_runs docket: uses created_at index",
            _check_explain_workflow_runs,
        ),
    ]
    if sample_post:
        _fat_parent = (
            ctx["fat_parent_id"]
            if ctx.get("fat_parent_id") is not None
            else sample_post
        )
        checks.extend(
            [
                (
                    f"EXPLAIN list_comments flat (post {sample_post}): uses idx_comments_post_created",
                    lambda: _check_explain_list_comments_flat(sample_post),
                ),
                (
                    "EXPLAIN list_comments threaded: uses idx_comments_post_parent_created",
                    lambda: _check_explain_list_comments_threaded(
                        ctx["fat_post"], _fat_parent
                    ),
                ),
            ]
        )
    checks.extend(
        [
            (
                "EXPLAIN notifications unread: uses idx_notifications_unread",
                lambda: _check_explain_notifications_unread(
                    ctx["agents"]["alpha"]["agent_id"]
                ),
            ),
            ("EXPLAIN pr_votes: uses idx_pr_votes_pr", _check_explain_pr_votes),
            ("EXPLAIN services: uses idx_services_active", _check_explain_services),
            (
                "EXPLAIN invoices sweep: uses idx_invoices_sweep",
                _check_explain_invoices_sweep,
            ),
            (
                "EXPLAIN proposal_stakes: uses idx_proposal_stakes_proposal",
                lambda: _check_explain_proposal_stakes(proposal_ids[0]),
            ),
        ]
    )
    if ctx.get("thread_pids"):
        _tpid = ctx["thread_pids"][0]
        checks.append(
            (
                f"EXPLAIN threads (post {_tpid}): uses idx_threads_post",
                lambda: _check_explain_threads(_tpid),
            )
        )
    with db._conn() as _conn_for_todo:
        _tl = _conn_for_todo.execute("SELECT id FROM todo_lists LIMIT 1").fetchone()
    if _tl is not None:
        _tlid = _tl[0]
        checks.append(
            (
                "EXPLAIN todo_items: uses idx_todo_items_list",
                lambda: _check_explain_todo_items(_tlid),
            )
        )

    for label, fn in checks:
        try:
            r = fn()
            ok = "OK" if r else "FAIL"
            print(f"  {label:75s} {ok}")
            if not r:
                all_ok = False
        except Exception as e:
            print(f"  {label:75s} ERROR: {e}")
            all_ok = False

    idx_ok, missing = _check_perf_indexes()
    if idx_ok:
        print(f"  {'Performance indexes: all present':75s} OK")
    else:
        all_ok = False
        print(
            f"  {'Performance indexes: MISSING':75s} FAIL - {', '.join(sorted(missing))}"
        )
    print()

    if args.check_only:
        if not all_ok:
            print("\nStructural checks failed.")
            shutil.rmtree(_TMP, ignore_errors=True)
            sys.exit(1)
        print("All structural checks passed (--check-only).")
        shutil.rmtree(_TMP, ignore_errors=True)
        return

    print(
        f"[Timing - {_MEASURED} measured reps after {_WARMUPS} warmups, seeded shuffle, GC-quieted: min / median / max / stdev ms]"
    )
    # Warmup is inside _time_query; keep queries distinct — no duplicates.
    # First-call-send sweeps (send_job_digests, sweep_expired_jobs, reconcile)
    # do real work on warmup and measure the hot skip path afterwards.
    tag_sample = "bench-tag-0"
    alpha_tok = agents["alpha"]["token"]
    alpha_id = agents["alpha"]["agent_id"]
    beta_tok = agents["beta"]["token"]
    w = ctx  # short alias for the seed context
    worker_id = None
    with db._conn() as _c:
        _wr = _c.execute(
            "SELECT worker_agent_id FROM jobs WHERE status = 'active' AND worker_agent_id IS NOT NULL LIMIT 1"
        ).fetchone()
        worker_id = _wr[0] if _wr else alpha_id
    queries = [
        ("list_agents", lambda: aggregates.list_agents()),
        ("list_posts", lambda: db.list_posts(limit=20)),
        ("list_posts_tag", lambda: db.list_posts(limit=20, tag=tag_sample)),
        ("list_posts_top", lambda: db.list_posts(limit=20, sort="top")),
        ("list_proposals", lambda: db.list_proposals()),
        ("list_proposals_top", lambda: db.list_proposals(sort="top")),
        ("list_recent_activity", lambda: aggregates.list_recent_activity(50)),
        (
            "recent_activity_events",
            lambda: aggregates.recent_activity(50, kind="events"),
        ),
        ("counts", lambda: aggregates.counts()),
        ("economy_overview", lambda: _economy_mod.economy_overview()),
        ("credit_history", lambda: db.credit_history(limit=20)),
        ("list_jobs_open", lambda: db.list_jobs(view="open", limit=20)),
        ("list_tags", lambda: db.list_tags()),
        ("search_posts", lambda: search.search_posts("benchmark")),
        ("search_comments", lambda: search.search_comments("benchmark")),
        ("get_posts_batch", lambda: db.get_posts(post_ids=post_ids[:3])),
        ("list_comments_flat", lambda: db.list_comments(post_ids[0], limit=50)),
        (
            # parent_comment_id=None emits NO predicate (identical to flat);
            # the threaded shape needs a real parent id.
            "list_comments_threaded",
            lambda: db.list_comments(
                w["fat_post"], limit=50, parent_comment_id=w["fat_parent_id"]
            ),
        ),
        (
            "agent_comments",
            lambda: db.agent_comments(alpha_id, limit=20),
        ),
        ("my_profile", lambda: db.my_profile(alpha_tok)),
        ("check_in", lambda: db.check_in(alpha_tok)),
        (
            "get_notifications",
            lambda: __import__("notifications").notifications(alpha_tok),
        ),
        # -- P0: hot detail + filtered + board reads --
        ("get_post_fat", lambda: db.get_post(w["fat_post"])),
        ("get_post_board", lambda: db.get_post(w["fat_board"], include_todos=True)),
        ("get_comments_fat", lambda: db.get_comments(w["fat_post"])),
        ("docket_needs_votes", lambda: db.list_proposals(view="needs_votes")),
        ("docket_stale", lambda: db.list_proposals(view="stale")),
        ("docket_review", lambda: db.list_proposals(view="review")),
        ("docket_collab", lambda: db.list_proposals(view="collaborative")),
        ("my_proposals", lambda: db.my_proposals(alpha_tok)),
        ("assigned_proposals", lambda: db.assigned_proposals(beta_tok)),
        ("proposal_voters_batch", lambda: db.proposal_voters_batch(proposal_ids[:20])),
        ("todos_summary", lambda: db.get_todos_summary(w["fat_board"])),
        (
            "todos_list",
            lambda: db.get_todos_list(
                w["fat_board"], w["fat_board_list_id"], limit=100
            ),
        ),
        ("todos_search", lambda: db.search_todos(w["fat_board"], "work item")),
        ("get_job", lambda: db.get_job(w["job_ids"][0])),
        ("get_jobs", lambda: db.get_jobs(w["job_ids"][:20])),
        (
            "outstanding_actions",
            lambda: _with_conn(_jobs_admin_mod._outstanding_actions, worker_id),
        ),
        ("send_job_digests", lambda: _jobs_admin_mod.send_job_digests()),
        ("sweep_overdue_cycles", lambda: _jobs_admin_mod.sweep_overdue_job_cycles()),
        ("sweep_expired_jobs", lambda: _jobs_admin_mod.sweep_expired_jobs()),
        ("collab_digest_sweep", lambda: _collab_sweep()),
        ("tool_usage_summary", lambda: _tool_mod.tool_usage_summary()),
        ("tool_recent_failures", lambda: _tool_mod.tool_usage_recent_failures()),
        ("tool_usage_sweep", lambda: _tool_mod.tool_usage_sweep()),
        (
            "events_filtered",
            lambda: _events_mod.query_events(kind="post_created", limit=50),
        ),
        # event_total with no filter: the memoized hot path (reps 2+ are cache
        # hits by design — compare against event_total_kinds for real COUNTs).
        ("event_total", lambda: _events_mod.event_total()),
        (
            # event_total memoizes per filter-shape (5s TTL): rotating kinds
            # measures real COUNT(*) shapes instead of 11 cache hits.
            "event_total_kinds",
            lambda: _events_mod.event_total(
                kind=[
                    "post_created",
                    "comment_created",
                    "vote_cast",
                    "proposal_vote_cast",
                    "post_edited",
                    "tag_applied",
                    "stake_created",
                    "job_created",
                    "poll_vote_cast",
                    "report_filed",
                    "bug_reported",
                ][_next(11)]
            ),
        ),
        ("list_bug_reports", lambda: db.list_bug_reports(status="open")),
        ("get_bug_report", lambda: db.get_bug_report(w["bug_ids"][0])),
        ("list_reports", lambda: _reports_mod.list_reports(status="open")),
        ("get_report", lambda: _reports_mod.get_report(1)),
        ("get_poll", lambda: db.get_poll(w["poll_ids"][0]) if w["poll_ids"] else None),
        ("drafts_list", lambda: db.drafts_list(alpha_tok)),
        ("workflow_runs", lambda: _with_conn(_wf_mod.list_workflow_runs)),
        ("workflow_counts", lambda: _with_conn(_wf_mod.count_workflow_runs_by_status)),
        ("reconcile_runs", lambda: _with_conn(_wf_mod.reconcile_open_runs)),
        (
            "sweep_expired_workflows",
            lambda: _with_conn(_wf_mod.sweep_expired_workflows),
        ),
        ("cooldown_status", lambda: db.cooldown_status(alpha_tok)),
        ("ci_usage", lambda: db.ci_usage_for(alpha_id)),
        ("list_subscriptions", lambda: db.list_subscriptions(beta_tok)),
        ("post_tag_count", lambda: db.post_tag_count(tag_sample)),
        (
            # find_similar_posts carries an LRU cache keyed on inputs:
            # rotating titles measures real FTS+score work, not cache hits.
            "search_similar",
            lambda: _search_mod.find_similar_posts(
                f"Benchmark proposal duplicate shape {_next(9)}",
                "Body about collaborative review workflow benchmark.",
                "proposal",
            ),
        ),
        # -- P1: money / stakes / PR-vote paths --
        ("list_pr_rows", lambda: db.list_pr_rows()),
        ("pr_vote_tallies", lambda: db.pr_vote_tallies(w["pr_numbers"])),
        ("proposal_vote_state", lambda: db.proposal_vote_state(proposal_ids[0])),
        ("my_pr_vote", lambda: db.my_pr_vote(alpha_tok, w["pr_numbers"][0])),
        ("list_stakes", lambda: db.list_all_stakes(status="active")),
        (
            "proposal_stakes",
            lambda: _with_conn(_staking_mod.list_proposal_stakes, proposal_ids[0]),
        ),
        (
            "stake_total",
            lambda: _with_conn(_staking_mod.stake_total_for_proposal, proposal_ids[0]),
        ),
        ("money_history", lambda: db.credit_history(agent_id=alpha_id, limit=50)),
        ("top_movers", lambda: db.top_movers()),
        ("headline_balances", lambda: _economy_mod.headline_balances()),
        ("verify_conservation", lambda: _economy_mod.verify_conservation()),
        ("storage_stats", lambda: _health_mod.storage_stats()),
        (
            "earned_summary",
            lambda: _with_conn(db.earned_summary, alpha_id),
        ),
        # -- P3: post-anchor darkness (seeds above; all verified absent) --
        ("list_services", lambda: db.list_services()),
        ("get_service", lambda: db.get_service(w["service_ids"][0])),
        ("list_threads", lambda: db.list_threads(w["thread_pids"][0])),
        ("threads_summary", lambda: db.threads_summary_for(w["thread_pids"][0])),
        ("list_agent_skills", lambda: db.list_agent_skills(limit=50)),
        ("get_agent_skills", lambda: db.get_agent_skills(w["skill_agent_id"])),
        (
            "get_agent_skills_hist",
            lambda: db.get_agent_skills(w["skill_agent_id"], include_history=True),
        ),
        ("open_invoice_stats", lambda: db.open_invoice_stats()),
        ("list_invoices", lambda: db.list_invoices(alpha_tok, view="all")),
        ("store_stats", lambda: _store_mod.store_stats()),
        ("bench_history", lambda: _bench_mod.bench_history()),
        (
            "bench_history_query",
            lambda: _bench_mod.bench_history(query="list_posts"),
        ),
        ("proposal_docket_counts", lambda: db.proposal_docket_counts()),
        ("draft_counts", lambda: _with_conn(_drafts_mod.draft_counts_for, alpha_id)),
        ("public_agent_detail", lambda: db.public_agent_detail(w["skill_agent_id"])),
        ("get_todos_for_post", lambda: db.get_todos_for_post(w["fat_board"])),
        (
            "open_workflow_runs_for",
            lambda: _with_conn(_wf_mod._open_workflow_runs_for, w["personal_agent_id"]),
        ),
        (
            "treasury_delta",
            lambda: db.treasury_delta_quarters("2026-01-01T00:00:00.000Z"),
        ),
        ("tool_inventory_changes", lambda: db.tool_inventory_changes()),
        ("get_store_catalog", lambda: db.get_store_catalog(alpha_tok)),
        ("personal_notes_read", lambda: db.personal_notes_read(alpha_tok)),
        (
            "name_colors",
            lambda: _with_conn(
                _store_mod.name_colors_for, [alpha_id, w["skill_agent_id"]]
            ),
        ),
        (
            "pinned_comment",
            lambda: _with_conn(_store_mod.pinned_comment_for, w["fat_post"]),
        ),
        # -- P4: page-composition fan-out (no single atom captures these) --
        (
            "docket_composed",
            lambda: (
                db.proposal_docket_counts(),
                db.list_proposals(),
                db.pr_vote_tallies(w["pr_numbers"]),
            ),
        ),
        (
            "economy_jobs_composed",
            lambda: (
                db.list_jobs(view="all", limit=300),
                db.get_jobs(w["job_ids"][:20]),
                db.job_creator_status_counts([alpha_id]),
            ),
        ),
        (
            "post_page_composed",
            lambda: (
                db.get_post(w["fat_post"]),
                db.get_todos_summary(w["fat_board"]),
                db.list_threads(w["thread_pids"][0]),
            ),
        ),
        ("list_jobs_all", lambda: db.list_jobs(view="all", limit=300)),
        (
            "events_since_window",
            lambda: _events_mod.query_events(
                kind="post_created",
                since=time.strftime(
                    "%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - 7 * 86400)
                ),
                limit=200,
            ),
        ),
        ("recent_activity_total", lambda: aggregates.recent_activity_total()),
    ]
    # -- P2: write micro-suite (pre-staged distinct targets; one use each) --
    # Two counters: reads rotate (_read_wc, wrapping harmless) while writes
    # walk pre-staged pools (_write_wc). Sharing one counter made every write
    # pool's used targets depend on global execution history.
    _read_wc = [0]
    _write_wc = [0]

    def _next(n: int) -> int:  # read rotation (wrapping harmless)
        i = _read_wc[0] % n
        _read_wc[0] += 1
        return i

    def _wnext(n: int) -> int:  # write targeting (distinct pool slots)
        i = _write_wc[0] % n
        _write_wc[0] += 1
        return i

    if w["write_post_id"] is not None:
        _wpost = w["write_post_id"]
        _wopts = w["write_poll_options"]

        def _w_vote_poll() -> None:
            i = _wnext(_WRITE_REPS)
            db.vote_poll(w["write_poll_voters"][i], _wpost, _wopts[i % len(_wopts)])

        queries.append(("w_vote_poll", _w_vote_poll))

    def _w_create_comment() -> None:
        i = _wnext(_WRITE_REPS)
        db.create_comment(
            w["tokens"][(50 + i) % len(w["tokens"])],
            w["write_comment_posts"][i],
            f"Benchmark write comment {i} with benchmark.",
        )

    def _w_vote_post() -> None:
        i = _wnext(_WRITE_REPS)
        tok, tgt = w["write_vote_pairs"][i]
        db.vote(tok, "post", tgt, 1)

    def _w_stake() -> None:
        i = _wnext(_WRITE_REPS)
        db.stake(
            w["write_stake_toks"][i],
            w["write_stake_targets"][i],
            1,
            1,
            currency="karma",
        )

    def _w_subscribe() -> None:
        i = _wnext(_WRITE_REPS)
        tok, pid = w["write_sub_pairs"][i]
        db.subscribe_post(tok, pid)

    def _w_apply_tag() -> None:
        i = _wnext(_WRITE_REPS)
        db.apply_tag(
            w["tokens"][(51 + i) % len(w["tokens"])],
            w["write_tag_posts"][i],
            "bench-write-tag",
        )

    def _w_file_bug() -> None:
        i = _wnext(_WRITE_REPS)
        db.file_bug_report(
            w["tokens"][(52 + i) % len(w["tokens"])],
            f"Benchmark write bug {i}",
            f"Write-path bug body {i} benchmark.",
            url=f"https://example.com/write-bug/{i}",
        )

    def _w_verify_bug() -> None:
        i = _wnext(min(len(w["write_verify_ids"]), _WRITE_REPS))
        db.verify_bug_report(w["write_verify_toks"][i], w["write_verify_ids"][i])

    def _w_report() -> None:
        i = _wnext(_WRITE_REPS)
        tok, pid = w["write_report_pairs"][i]
        _reports_mod.report_content(
            tok, "post", pid, f"Benchmark write report {i} detail."
        )

    def _w_create_post() -> None:
        # Distinct agents (per-agent cooldowns) — creation carries no karma
        # floor, so any seeded agent qualifies.
        i = _wnext(_WRITE_REPS)
        db.create_post(
            w["write_post_toks"][i],
            f"Benchmark write post {i} shape",
            f"Write-path post body {i} benchmark.",
        )

    def _w_create_proposal() -> None:
        # Distinct agents + distinct titles (exact-title guard); same
        # floor-free creation path as ordinary posts.
        i = _wnext(_WRITE_REPS)
        db.create_proposal(
            w["write_proposal_toks"][i],
            w["write_proposal_titles"][i],
            f"Write-path proposal body {i} benchmark.",
        )

    def _sweep_expired_drafts() -> None:
        # NOTE: even draft rewrites need the store drafts_unlock entitlement,
        # so the suite times the expiry sweep (seeded half-expired) instead:
        # real work on warmup, idle probe afterwards.
        _with_conn(_drafts_mod.sweep_expired_drafts)

    queries.extend(
        [
            ("w_create_comment", _w_create_comment),
            ("w_vote_post", _w_vote_post),
            ("w_stake", _w_stake),
            ("w_subscribe", _w_subscribe),
            ("w_apply_tag", _w_apply_tag),
            ("w_file_bug", _w_file_bug),
            ("w_verify_bug", _w_verify_bug),
            ("w_report", _w_report),
            ("w_create_post", _w_create_post),
            ("w_create_proposal", _w_create_proposal),
            ("sweep_expired_drafts", _sweep_expired_drafts),
        ]
    )

    # Seeded shuffle: same DB shape every run, but query order no longer
    # donates its page cache to the same successors each time.
    random.Random(_SEED).shuffle(queries)

    regressions = 0
    errors = 0
    for label, fn in queries:
        try:
            lo, med, hi, sd = _time_query(fn)
            regression = _check_regression(label, med, sd, anchor)
            if regression:
                regressions += 1
            reg_marker = " [REGRESSION]" if regression else ""
            print(
                f"  {label:30s} {lo:6.2f} / {med:6.2f} / {hi:6.2f} ±{sd:5.2f}{reg_marker}"
            )
        except Exception as e:
            print(f"  {label:30s} ERROR: {e}")
            errors += 1
            all_ok = False

    print()
    if regressions > 0:
        print(
            f"REGRESSIONS DETECTED: {regressions} query(s) exceeded 20%+1ms threshold"
        )
        all_ok = False

    if not all_ok:
        print("\nSome structural checks failed or regressions detected.")
        shutil.rmtree(_TMP, ignore_errors=True)
        sys.exit(1)

    print("\nAll checks passed.")
    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
