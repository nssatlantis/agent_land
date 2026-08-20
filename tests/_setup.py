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
    "FORUM_TAG_CREATE_COOLDOWN_SECONDS": "0",
    "FORUM_TAG_APPLY_DAILY_CAP": "10",
    "FORUM_TAG_MAX_PER_POST": "5",
    "FORUM_COMMENT_DAILY_CAP": "0",
    "FORUM_VOTE_DAILY_CAP": "0",
    "FORUM_BOUNTY_MAX_STAKE_FRACTION": "0",
}
for _k, _v in _TUNE_DEFAULTS.items():
    os.environ.setdefault(_k, _v)

import db  # noqa: E402
import reports  # noqa: F401, E402
import moderation  # noqa: F401, E402
import config  # noqa: F401, E402
import db._aggregates as aggregates  # noqa: F401, E402
import github  # noqa: F401, E402
import notifications  # noqa: F401, E402
import server.repo_search as repo_search  # noqa: F401, E402
import search  # noqa: F401, E402


def expect_error(fn, *args, **kw):
    try:
        fn(*args, **kw)
    except db.ForumError as exc:
        return str(exc)
    raise AssertionError(f"expected ForumError from {fn.__name__}()")


def init():
    """Initialise the throwaway database."""
    db.init_db()


def setup():
    """Create 9 agents, a base post, and earn karma for 7 of them.

    Returns (agents, post_id).
    """
    init()
    agents = {}
    for name in ("alpha", "beta", "gamma", "delta", "epsilon", "zeta",
                 "eta", "theta", "fresh"):
        agents[name] = db.register_agent(name)
    post = db.create_post(
        agents["alpha"]["token"], "Rules proposal", "Body with spammy text.",
    )
    post_id = post["post_id"]
    for name in ("beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta"):
        comment = db.create_comment(
            agents[name]["token"], post_id, f"comment from {name}",
        )
        db.vote(agents["alpha"]["token"], "comment", comment["comment_id"], 1)
    return agents, post_id
