"""Single-source guard: edit_post/edit_proposal share one signature tail.

The two in-place edit tools in server/tools/forum.py ended with the same
3-line signature/reference tail, duplicated literally. The tail now lives
once in `_EDIT_REFS_TAIL` and is completed onto each docstring by the
`@_common_edit_docs` decorator (innermost, so @_logged's functools.wraps
and @mcp.tool() both see the composed text - an f-string docstring would
never become __doc__ (ruff B021), and a post-def __doc__ patch would miss
the decoration-time capture).

This test stays static (source text, no imports): importing
server.tools.forum pulls the whole app stack, and the property under
guard is textual single-sourcing anyway - the same side-effect-free
style as test_server_facade_exports.py.

Proposal #270, item 4940.
"""

import os
import re

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FORUM_PATH = os.path.join(REPO_ROOT, "server", "tools", "forum.py")

# Distinctive sentence of the shared tail: must occur exactly once in the
# file (inside _EDIT_REFS_TAIL), never pasted into a docstring literally.
TAIL_NEEDLE = "signature_reconciled`, `signature_applied`). References"


def _src():
    with open(FORUM_PATH, encoding="utf-8") as fh:
        return fh.read()


def test_tail_defined_once():
    src = _src()
    assert src.count("_EDIT_REFS_TAIL = (") == 1, (
        "expected exactly one _EDIT_REFS_TAIL definition"
    )
    assert src.count(TAIL_NEEDLE) == 1, (
        "signature/reference tail must live only in _EDIT_REFS_TAIL, "
        f"found {src.count(TAIL_NEEDLE)} copies"
    )


def test_both_tools_use_common_docs_decorator():
    src = _src()
    for name in ("edit_proposal", "edit_post"):
        m = re.search(
            r"@_logged\n@_common_edit_docs\ndef " + name + r"\(",
            src,
        )
        assert m, (
            f"{name} must carry @_common_edit_docs innermost "
            "(below @_logged) so the composed docstring reaches the registry"
        )


def main():
    test_tail_defined_once()
    test_both_tools_use_common_docs_decorator()
    print("test_forum_edit_docs: all assertions passed")


test_tail_defined_once()
test_both_tools_use_common_docs_decorator()
print("test_forum_edit_docs: all assertions passed")


if __name__ == "__main__":
    main()
