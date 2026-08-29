"""Session DB helpers for run_all D1.

When AGENTLAND_SESSION=1, all test files share one DB file
(`AGENTLAND_SESSION_DB_PATH`) instead of 60 mkdtemp DBs. This cuts
60× mkdtemp + init_db overhead. Each file still calls setup() which
truncates before seeding, so isolation is preserved.

Standalone `python tests/test_foo.py` still works: it creates its own
temp DB as before when AGENTLAND_SESSION is not set.
"""

import os
import tempfile
from pathlib import Path


def session_db_path() -> str | None:
    """Return the shared session DB path when session mode is active."""
    if os.environ.get("AGENTLAND_SESSION") == "1":
        p = os.environ.get("AGENTLAND_SESSION_DB_PATH")
        if p:
            return p
    return None


def ensure_session_dir() -> Path:
    """Create (or reuse) the session temp dir and return its Path."""
    p = os.environ.get("AGENTLAND_SESSION_DB_PATH")
    if p:
        d = Path(p).parent
        d.mkdir(parents=True, exist_ok=True)
        return d
    # run_all creates this; fallback for ad-hoc use
    tmp = Path(tempfile.mkdtemp(prefix="agentland_session_"))
    os.environ["AGENTLAND_SESSION"] = "1"
    os.environ["AGENTLAND_SESSION_DB_PATH"] = str(tmp / "forum.db")
    os.environ["AGENTLAND_DATA_DIR"] = str(tmp)
    # Also set FORUM_DB_PATH so child imports see it before _setup
    os.environ["FORUM_DB_PATH"] = str(tmp / "forum.db")
    return tmp
