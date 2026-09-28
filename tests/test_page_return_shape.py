"""Ratchet: no viewer page re-wraps the response _page() already built (#816).

/findings shipped merged, 8 approvals, CI 5/5 green - and returned HTTP
500 for its entire life.  The handler was

    return HTMLResponse(_page("Open findings", _findings_body()))

and _page() already returns an HTMLResponse, so Starlette's render() was
handed a Response and raised

    AttributeError: 'HTMLResponse' object has no attribute 'encode'

It was the ONLY instance of that shape in the viewer, and every pin in
tests/test_findings_page.py called _findings_body() rather than the
handler, so no test ever executed the line that was broken.

This is the class-closer.  Four properties make it survive contact with
a real codebase:

* AST, not regex.  A docstring or comment quoting the pattern cannot
  false-alarm, because prose is a string constant and comments never
  enter the tree.  A ratchet that cries wolf gets deleted, and deleting
  it hands the real defect back unguarded.
* A narrow predicate: the wrapped argument must BE a call to _page, so
  the legitimate HTMLResponse("<p>literal</p>") elsewhere in the viewer
  cannot turn this red.
* Its own controls - a synthetic bad snippet it MUST flag, a synthetic
  good one it must not, and a prose-only snippet.  A ratchet that has
  only ever reported zero offenders is indistinguishable from one whose
  glob silently matches nothing, which is exactly the failure I filed as
  bug #B137 against the exception-domain FILE_LIST.
* It fails CLOSED on its own scope: an empty file set is not a pass, so
  the scanned count is asserted rather than assumed.

Pure source analysis - no database, no network, no repo imports.
"""

from __future__ import annotations

import ast
import shutil
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

# viewer/ is ~25 modules today. A floor this far under the real count
# catches a moved or renamed directory while leaving room for the tree to
# shrink a little; the point is that an EMPTY or near-empty scan is a
# failure, not a pass.
_MIN_VIEWER_FILES = 20


def _called_name(node: ast.AST) -> str:
    """The trailing name of a call target: _page, resp._page, HTMLResponse."""
    func = getattr(node, "func", None)
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _offenders(path: Path) -> list[int]:
    """Line numbers of Returns that re-wrap a _page() response."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Return) or node.value is None:
            continue
        value = node.value
        if not isinstance(value, ast.Call):
            continue
        if _called_name(value) != "HTMLResponse":
            continue
        if len(value.args) != 1:
            continue
        inner = value.args[0]
        if isinstance(inner, ast.Call) and _called_name(inner) == "_page":
            out.append(node.lineno)
    return out


_BAD = (
    "from starlette.responses import HTMLResponse\n"
    "from viewer._layout import _page\n"
    "\n"
    "def findings_page(request):\n"
    "    return HTMLResponse(_page('Open findings', 'body'))\n"
)

_GOOD = (
    "from starlette.responses import HTMLResponse\n"
    "from viewer._layout import _page\n"
    "\n"
    "def findings_page(request):\n"
    "    return _page('Open findings', 'body')\n"
    "\n"
    "def literal(request):\n"
    "    return HTMLResponse('<p>hello</p>')\n"
)

_PROSE = (
    "from viewer._layout import _page\n"
    "\n"
    "def note(request):\n"
    '    """Never write HTMLResponse(_page(...)) - _page is already a'
    ' response."""\n'
    "    return _page('x', 'y')\n"
    "# HTMLResponse(_page(...)) in a comment must not trip this either\n"
)


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="agentland_test_page_return_shape_"))
    try:
        # --- control 1: it MUST fire on the shipped defect ---------------
        bad = tmp / "ctl_bad.py"
        bad.write_text(_BAD, encoding="utf-8")
        assert _offenders(bad) == [5], _offenders(bad)

        # --- control 2: the correct shape, and a legitimate literal ------
        good = tmp / "ctl_good.py"
        good.write_text(_GOOD, encoding="utf-8")
        assert _offenders(good) == [], _offenders(good)

        # --- control 3: prose quoting the pattern is invisible ------------
        prose = tmp / "ctl_prose.py"
        prose.write_text(_PROSE, encoding="utf-8")
        assert _offenders(prose) == [], _offenders(prose)
        print("  controls: fires on the defect, silent on good + prose: ok")

        # --- the real scan, failing closed on its own scope --------------
        viewer = _ROOT / "viewer"
        assert viewer.is_dir(), f"no viewer/ directory at {viewer}"
        files = sorted(viewer.rglob("*.py"))
        assert len(files) >= _MIN_VIEWER_FILES, (
            f"viewer/ glob matched only {len(files)} files, below the"
            f" floor of {_MIN_VIEWER_FILES} - a ratchet whose scope went"
            " stale would pass vacuously, and an empty set is not a pass"
        )
        hits = [
            f"{f.relative_to(_ROOT)}:{lineno}"
            for f in files
            for lineno in _offenders(f)
        ]
        assert not hits, (
            "a viewer page re-wraps the response _page() already returned;"
            " return _page(...) directly: " + ", ".join(hits)
        )
        print(f"  viewer/ scanned: {len(files)} files, 0 offenders")

        print("test_page_return_shape: all assertions passed")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
