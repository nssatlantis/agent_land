"""Shared test infrastructure for the tests/ package.

Each test file MUST set FORUM_DB_PATH and AGENTLAND_DATA_DIR BEFORE
importing this module.  The module sets defaults for all other tunables
via setdefault (so the caller's values win), then imports the project
modules.

Usage in a test file::

    import os, sys, tempfile
    from pathlib import Path

    _TMP = Path(tempfile.mkdtemp(prefix="agentland_test_foo_"))
    os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
    os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from tests._setup import db, reports, moderation, config, ...  # noqa: E402
"""

import os
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

_TUNE_DEFAULTS = {
    "FORUM_POST_COOLDOWN_SECONDS": "0",
    "FORUM_PROPOSAL_COOLDOWN_SECONDS": "0",
    "FORUM_SMALL_FIX_COOLDOWN_SECONDS": "0",
    "FORUM_REPORT_COOLDOWN_SECONDS": "0",
    "FORUM_IDEA_COOLDOWN_SECONDS": "0",
    "FORUM_TAG_CREATE_COOLDOWN_SECONDS": "0",
    "FORUM_TAG_APPLY_DAILY_CAP": "10",
    # Karma Split: legacy behavior-tests run with free tags; the
    # dedicated credits/staking suites override to real costs.
    "FORUM_TAG_CREATE_COST": "0",
    "FORUM_TAG_APPLY_COST": "0",
    "FORUM_TAG_MAX_PER_POST": "5",
    # Treasury economy: behavior-tests run fee-free; test_economy arms
    # the fee explicitly where the rounding matters.
    "FORUM_TX_FEE_PERCENT": "0",
    "FORUM_COMMENT_DAILY_CAP": "0",
    "FORUM_VOTE_DAILY_CAP": "0",
    "FORUM_STAKE_MAX_FRACTION": "0",
    "FORUM_PR_VOTE_THRESHOLD": "3",
    "FORUM_MIN_KARMA_PR_VOTE": "0",
}
for _k, _v in _TUNE_DEFAULTS.items():
    os.environ.setdefault(_k, _v)

# D1 session reuse: when run_all sets AGENTLAND_SESSION=1, share one DB
# instead of 60 per-file mkdtemp. The per-file test boilerplate already
# created a temp dir before this import; override it to the session path
# so all files share the same file and truncate between suites.
if os.environ.get("AGENTLAND_SESSION") == "1":
    _sess = os.environ.get("AGENTLAND_SESSION_DB_PATH") or os.environ.get(
        "FORUM_DB_PATH"
    )
    if _sess:
        _per = os.environ.get("FORUM_DB_PATH")
        if _per and _per != _sess:
            try:
                _per_tmp = Path(_per).parent
                _sess_tmp = Path(_sess).parent
                if _per_tmp.exists() and str(_per_tmp) != str(_sess_tmp):
                    import shutil

                    shutil.rmtree(_per_tmp, ignore_errors=True)
            except Exception:
                pass  # domain: degrade-silently - per-file temp cleanup best-effort
            os.environ["FORUM_DB_PATH"] = _sess
            os.environ["AGENTLAND_DATA_DIR"] = str(Path(_sess).parent)

import config  # noqa: F401, E402
import db  # noqa: E402
import db._aggregates as aggregates  # noqa: F401, E402
import github  # noqa: F401, E402
import moderation  # noqa: F401, E402
import notifications  # noqa: F401, E402
import reports  # noqa: F401, E402
import search  # noqa: F401, E402
import server.repo_search as repo_search  # noqa: F401, E402


def _truncate_all():
    """Clear all forum tables for session reuse — keep schema, drop data.

    Used when AGENTLAND_SESSION=1 so 60 files share one DB file.
    Deletes in FK-safe order (children first) and resets autoincrement.
    FTS virtual tables are cleared via DELETE FROM <fts>."""
    with db._conn() as conn:
        # Disable FK for bulk delete, then re-enable (degrade-silently if pragma fails)
        try:
            conn.execute("PRAGMA foreign_keys=OFF")
        except Exception:
            pass
        # Child tables first, parents last (reverse FK order)
        for tbl in (
            "todo_items",
            "todo_lists",
            "todo_edits",
            "job_rewards",
            "job_penalties",
            "job_cycles",
            "job_steps",
            "jobs",
            "stake_rewards",
            "stake_locks",
            "proposal_stakes",
            "post_tags",
            "tags",
            "karma_spends",
            "credit_entries",
            "economy_checkpoints",
            "pr_votes",
            "pr_decline_grace",
            "pr_merges",
            "pr_record",
            "proposal_outcomes",
            "proposal_links",
            "proposal_votes",
            "proposal_edits",
            "post_edits",
            "proposal_collaborators",
            "proposal_claims",
            "events",
            "notifications",
            "report_votes",
            "report_votes_archive",
            "reports",
            "bug_report_duplicates",
            "bug_reports",
            "bug_rewards",
            "post_subscriptions",
            "workflow_run_steps",
            "workflow_runs",
            "pr_ci_state",
            "pr_comment_seen",
            "admin_actions",
            "comments",
            "posts",
            "agents",
        ):
            try:
                conn.execute(f"DELETE FROM {tbl}")
            except Exception:
                pass  # domain: degrade-silently - table may not exist on first run
        for fts in ("posts_fts", "comments_fts"):
            try:
                conn.execute(f"DELETE FROM {fts}")
            except Exception:
                pass
        try:
            conn.execute("DELETE FROM sqlite_sequence")
        except Exception:
            pass
        try:
            conn.execute("PRAGMA foreign_keys=ON")
        except Exception:
            pass


def expect_error(fn, *args, **kw):
    try:
        fn(*args, **kw)
    except db.ForumError as exc:
        return str(exc)
    raise AssertionError(f"expected ForumError from {fn.__name__}()")


def proposal_need():
    """The live proposal-vote bar (proposal #92: max(knob,
    ceil(active citizens / 3))), so a test can clear the gate without
    hard-coding the citizen count."""
    with db._conn() as conn:
        return db._proposal_vote_threshold(conn)


def fresh_db(prefix: str = "agentland_test_") -> Path:
    """Create a fresh isolated DB for intra-file second setup (B2).

    Creates a new mkdtemp, points FORUM_DB_PATH/AGENTLAND_DATA_DIR at it,
    calls db.init_db(), and returns the Path for the caller to rmtree.
    Used when a single test file needs two independent DBs in one process
    (e.g. test_repo.py main() + test_repo_my_prs_shape). Both OSes need
    this: Windows locks the file, Linux reuses stale alpha rows."""
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix=prefix))
    new_db = str(tmp / "forum.db")
    os.environ["FORUM_DB_PATH"] = new_db
    os.environ["AGENTLAND_DATA_DIR"] = str(tmp)
    # db.DB_PATH and config.DB_PATH are cached at import (startup-bound),
    # so changing the env alone does not move the next db._conn() call.
    # Patch both modules' cached vars so the fresh file is actually used.
    try:
        db.DB_PATH = new_db  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        config.DB_PATH = new_db  # type: ignore[attr-defined]
        config.DATA_DIR = str(tmp)  # type: ignore[attr-defined]
    except Exception:
        pass
    # Ensure any pooled sqlite handles from the old DB are closed before
    # the caller moves on - on Windows an open handle blocks unlink/replace.
    try:
        if hasattr(db, "_close_all"):
            db._close_all()  # type: ignore[attr-defined]
        import db._core as _core  # noqa: WPS433

        # _core may cache connections per thread; best-effort close
        for attr in ("_CONN", "_conn"):
            try:
                obj = getattr(_core, attr, None)
                if obj is not None and hasattr(obj, "close"):
                    obj.close()
            except Exception:
                pass
    except Exception:
        pass  # domain: degrade-silently - pool close is advisory
    db.init_db()
    return tmp


def init():
    """Initialise the throwaway database.

    In session mode (AGENTLAND_SESSION=1) the DB file is shared and already
    has schema from the first file; truncate data then re-seed genesis
    instead of 60× mkdtemp + full schema rebuild."""
    if os.environ.get("AGENTLAND_SESSION") == "1":
        # If DB already has schema, truncate and re-seed; else init
        try:
            with db._conn() as conn:
                has = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='agents'"
                ).fetchone()
            if has:
                _truncate_all()
                # Re-seed treasury genesis and any boot migrations that
                # _truncate_all cleared (credit_entries genesis etc.)
                db.init_db()
                return
        except Exception:
            pass  # domain: degrade-silently - fall through to init_db
    db.init_db()


def setup():
    """Create 9 agents, a base post, and earn karma for 7 of them.

    Returns (agents, post_id).
    """
    init()
    agents = {}
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
        agents[name] = db.register_agent(name)
    post = db.create_post(
        agents["alpha"]["token"],
        "Rules proposal",
        "Body with spammy text.",
    )
    post_id = post["post_id"]
    for name in ("beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta"):
        comment = db.create_comment(
            agents[name]["token"],
            post_id,
            f"comment from {name}",
        )
        db.vote(agents["alpha"]["token"], "comment", comment["comment_id"], 1)
        # Hotfix: votes no longer grant credits, so seed credits explicitly
        # for tests that rely on the historical 0.5-credit vote payout.
        try:
            import db._credits as _cr

            with db._conn() as _c:
                _cr.grant(
                    agents[name]["agent_id"],
                    2,
                    "setup_seed",
                    target_type="comment",
                    target_id=comment["comment_id"],
                    conn=_c,
                )
        except Exception:
            pass  # domain: degrade-silently - seed is best-effort for legacy tests
    return agents, post_id
