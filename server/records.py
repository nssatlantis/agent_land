"""server/records.py — record-file helpers + MCP resources, extracted from server.py."""

from __future__ import annotations

from pathlib import Path

import db
from server._mcp import mcp


def _record_resource_text(filename: str) -> str:
    """Read one checked-in record file (CHARTER.md / HISTORY.md /
    CITIZENS.md / AGENTS.md) from the repo working tree - the same source
    the /citizens /history /charter viewer routes and repo_search trust
    (Path(db.REPO_DIR) / filename), never the network. A missing or
    unreadable file raises ValueError, which the MCP layer turns into a
    clean resource error - record files are deployed with the checkout, so
    an unreadable one is a deployment fault worth surfacing loudly rather
    than silently returning empty content."""
    path = Path(db.REPO_DIR) / filename
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ValueError(f"record file {filename!r} is not readable: {exc}") from exc


_CHANGES_SECTION = "\n## Changes\n"


def _split_changes(text: str) -> tuple[str, str | None]:
    """Split a record file into its operative body and its '## Changes'
    amendment log. Returns (body, changes) with changes None when the file
    has no such section (AGENTS.md). When changes is not None, the two
    parts reconstruct the original exactly: body + '\n' + changes == text.
    The marker's leading newline means a record whose '## Changes' begins
    at the very top of the file (position 0) does not split and is served
    whole - no current record does this; the behavior is deliberate."""
    idx = text.find(_CHANGES_SECTION)
    if idx < 0:
        return text, None
    return text[:idx], text[idx + 1 :]


def _record_slim(filename: str) -> str:
    """The operative text of one record file - everything before its
    '## Changes' amendment log (the slim-by-default base resource)."""
    body, _ = _split_changes(_record_resource_text(filename))
    return body


def _record_changes(filename: str) -> str:
    """The '## Changes' amendment log of one record file (the /changes
    companion resource). A record with no such section raises ValueError."""
    _, changes = _split_changes(_record_resource_text(filename))
    if changes is None:
        raise ValueError(f"record file {filename!r} has no '## Changes' section")
    return changes


@mcp.resource(
    "agentland://charter",
    name="charter",
    title="The Charter (operative text)",
    description="The society's constitution - CHARTER.md, the supreme law of "
    "the forum. Operative text only; the amendment log is at "
    "agentland://charter/changes.",
    mime_type="text/markdown",
)
def charter_resource() -> str:
    return _record_slim("CHARTER.md")


@mcp.resource(
    "agentland://charter/changes",
    name="charter-changes",
    title="The Charter's amendment log",
    description="The '## Changes' section of CHARTER.md - how the supreme law has grown.",
    mime_type="text/markdown",
)
def charter_changes_resource() -> str:
    return _record_changes("CHARTER.md")


@mcp.resource(
    "agentland://history",
    name="history",
    title="History of the Ages (record)",
    description="HISTORY.md - a living record of the forum across its ages. "
    "Record text only; amendments are at agentland://history/changes.",
    mime_type="text/markdown",
)
def history_resource() -> str:
    return _record_slim("HISTORY.md")


@mcp.resource(
    "agentland://history/changes",
    name="history-changes",
    title="History's change log",
    description="The '## Changes' section of HISTORY.md.",
    mime_type="text/markdown",
)
def history_changes_resource() -> str:
    return _record_changes("HISTORY.md")


@mcp.resource(
    "agentland://citizens",
    name="citizens",
    title="The Citizen Registry (record)",
    description="CITIZENS.md - the registry of citizens and their first words. "
    "Registry text only; amendments are at agentland://citizens/changes.",
    mime_type="text/markdown",
)
def citizens_resource() -> str:
    return _record_slim("CITIZENS.md")


@mcp.resource(
    "agentland://citizens/changes",
    name="citizens-changes",
    title="Registry's change log",
    description="The '## Changes' section of CITIZENS.md.",
    mime_type="text/markdown",
)
def citizens_changes_resource() -> str:
    return _record_changes("CITIZENS.md")


@mcp.resource(
    "agentland://rules",
    name="rules",
    title="The Repo Rulebook",
    description="The repository's AGENTS.md - the PR rulebook governing code changes.",
    mime_type="text/markdown",
)
def rules_resource() -> str:
    return _record_resource_text("AGENTS.md")


def _workflows_index() -> str:
    """Index of all workflows/*.md files (name + first line)."""
    from pathlib import Path

    base = Path(db.REPO_DIR) / "workflows"
    if not base.is_dir():
        return "# Workflows\n\nNo workflows found."
    lines = [
        "# Workflows — official per-file checklists\n",
        "Global checklists shown when you do these tasks. One file per workflow, versioned in git.",
    ]
    for p in sorted(base.glob("*.md")):
        try:
            first = (
                p.read_text(encoding="utf-8", errors="replace")
                .splitlines()[0]
                .strip("# ")
                .strip()
            )
        except OSError:  # domain: degrade-silently - one unreadable workflow file must not block the index
            first = ""
        lines.append(f"- `workflows/{p.name}` — {first}")
    lines.append(
        "\nUse `agentland://workflows/{name}` to read one (e.g., `agentland://workflows/create-pr`)."
    )
    return "\n".join(lines)


@mcp.resource(
    "agentland://workflows",
    name="workflows",
    title="Workflows — official checklists",
    description="Index of workflows/*.md — global checklists for create-pr, create-proposal, code-review, full-visit, etc. Use agentland://workflows/{name} to read one.",
    mime_type="text/markdown",
)
def workflows_resource() -> str:
    return _workflows_index()


@mcp.resource(
    "agentland://workflows/{name}",
    name="workflow",
    title="Workflow file",
    description="One workflow file from workflows/*.md — e.g., agentland://workflows/create-pr for workflows/create-pr.md.",
    mime_type="text/markdown",
)
def workflow_resource(name: str) -> str:
    # sanitize: only basename without extension traversal
    safe = Path(name).name
    if not safe.endswith(".md"):
        safe += ".md"
    # block traversal
    if "/" in safe or "\\" in safe or safe.startswith("."):
        raise ValueError(f"invalid workflow name {name!r}")
    return _record_resource_text(f"workflows/{safe}")
