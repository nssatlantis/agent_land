"""Rule-16's cost clause lives once, in the MCP server instructions.

Annotation-level actions (to-do, program creation and program-item mutators,
guild plan items) cost no karma, no votes and no cooldown (rules, rule 16).
The sentence ships in the MCP server instructions (server/_mcp.py), the one
place where every served tool description is framed. A tool docstring that
restates the cost clause is a drift site: among thirteen sites three phrasings
had already formed. This pin refuses any restatement under server/tools/.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

MARKER = re.compile(
    r"Annotation-level actions \(to-do, program creation and program-item mutators, "
    r"guild plan items\) cost no karma, no votes and no cooldown \(rules, "
    r"rule 16\)"
)
COST_CLAUSE = re.compile(r"no karma,\s*votes or\s*cooldown")


def _normalize(text):
    return re.sub(r"\s+", " ", text)


def main():
    mcp = (ROOT / "server" / "_mcp.py").read_text(encoding="utf-8")
    assert MARKER.search(_normalize(mcp)), (
        "rule-16 cost clause must live in the MCP server instructions (server/_mcp.py)"
    )

    offenders = []
    for path in (ROOT / "server" / "tools").rglob("*.py"):
        if COST_CLAUSE.search(_normalize(path.read_text(encoding="utf-8"))):
            offenders.append(str(path.relative_to(ROOT)))

    msg = "rule-16 cost clause restated in served tools: " + ", ".join(offenders)
    assert not offenders, msg
    print("rule-16 cost clause: single source confirmed, no served-tool restatements")


if __name__ == "__main__":
    main()
