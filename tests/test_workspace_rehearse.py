"""Snapshot a claim tree into a CI files-overlay (proposal #482, part 5)."""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_workspace_rehearse_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import github._gitops as gh  # noqa: E402
import github._workspaces as ws  # noqa: E402
from github._core import RepoError  # noqa: E402
from tests._setup import db, setup  # noqa: E402