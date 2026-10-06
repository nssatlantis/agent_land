"""The workspace claim dispatcher replaces four removed tools (proposal #919).

`claim_workspace`, `release_workspace`, `workspace_fetch_ticket` and
`workspace_upload_ticket` are hard-removed and collapsed into the single
`workspace_claim(token, action, ...)` tool. This pins the removal rather
than the call: a name that survives on ANY of the three surfaces a tool can
leak from is still callable by an agent that reads the tool list, which is
the exact dead-end the collapse exists to remove.

The three surfaces are the module that defines the name, the
`server.tools.repo` package facade it is re-exported through, and the
top-level `server` package. The first two are checked per name because the
two ticket tools were defined in `_transfer` while claim/release lived in
`_workspace`.
"""

import importlib
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_workspace_claim_dispatch_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import setup  # noqa: E402

# Each removed tool name mapped to the module that used to define it.
_REMOVED = {
    "claim_workspace": "server.tools.repo._workspace",
    "release_workspace": "server.tools.repo._workspace",
    "workspace_fetch_ticket": "server.tools.repo._transfer",
    "workspace_upload_ticket": "server.tools.repo._transfer",
}


def test_removed_workspace_tools_are_absent():
    """None of the four names reachable from any of the three surfaces,
    and the dispatcher that replaced them reachable from all three."""
    import server
    import server.tools.repo as repo_pkg

    for name, module_path in _REMOVED.items():
        defining = importlib.import_module(module_path)
        assert not hasattr(defining, name), (
            f"{module_path} still exposes the removed tool {name}"
        )
        assert not hasattr(repo_pkg, name), (
            f"server.tools.repo still re-exports the removed tool {name}"
        )
        assert not hasattr(server, name), (
            f"server still re-exports the removed tool {name}"
        )

    import server.tools.repo._workspace as workspace_mod

    for surface, module in (
        ("server.tools.repo._workspace", workspace_mod),
        ("server.tools.repo", repo_pkg),
        ("server", server),
    ):
        assert hasattr(module, "workspace_claim"), (
            f"{surface} is missing the workspace_claim dispatcher"
        )
    print("  removed workspace tools absent; workspace_claim on 3 surfaces: ok")


def main():
    setup()
    test_removed_workspace_tools_are_absent()
    print("test_workspace_claim_dispatch: all assertions passed")


if __name__ == "__main__":
    main()
