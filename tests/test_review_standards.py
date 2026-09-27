"""The `finding_class` vocabulary lives in docs/review-standards.md.

`db._review_findings.FINDING_CLASSES` is the set `finding_add` enforces, and
this pin is what stops the two from drifting. Drift is wrong in both
directions, and the two are not the same severity:

- A class the CODE accepts but the DOCUMENT does not list is a wire-contract
  falsehood in the citizen's favour: the resource served at
  `agentland://review-standards` claims to define the vocabulary and does not.
- A class the DOCUMENT lists but the code refuses is the worse one. An agent
  reads the authoritative source, files the class it names, and is rejected
  for using the value it was told to use.

So the assertion is equality, and the message names both sets rather than
only reporting "drift", because a bare drift message sends the next reader
hunting in the wrong direction.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import db  # noqa: E402
_DOC = "docs/review-standards.md"

# One row per legal class: | `token` | what it names |
_ROW = re.compile(r"^\|\s*`([a-z][a-z0-9-]*)`\s*\|", re.MULTILINE)


def main() -> None:
    text = (_ROOT / _DOC).read_text(encoding="utf-8")
    documented = set(_ROW.findall(text))
    enforced = set(db.FINDING_CLASSES)
    assert documented == enforced, (
        f"finding_class vocabulary drift - doc-only={sorted(documented - enforced)}"
        f" code-only={sorted(enforced - documented)}"
    )
    print(f"OK: {len(documented)} finding classes documented, enforcement in sync")


if __name__ == "__main__":
    main()
