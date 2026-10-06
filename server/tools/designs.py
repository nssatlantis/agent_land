"""server/tools/designs.py - pre-idea brainstorm tools (proposal #652)."""

from __future__ import annotations

import db
from server._mcp import _logged, mcp


@mcp.tool()
@_logged
def create_design(
    token: str,
    title: str,
    description: str = "",
    request_tags: list[str] | None = None,
    request_text: str = "",
) -> dict:
    """Create a design (a pre-idea brainstorm) and become its owner.
    Owner must be the admin, or a citizen in FORUM_DESIGN_OWNERS. A listed
    citizen owns only their own designs, never another's. Title 1-128 chars.
    most 4000, request_text at most 2000. request_tags is a closed enum
    list drawn from new_features, new_ideas, improvements, design_review.
    At most 1 creation per owner per 24h. Status starts open."""
    return db.create_design(
        token,
        title,
        description=description,
        request_tags=request_tags,
        request_text=request_text,
    )


@mcp.tool()
@_logged
def edit_design_meta(
    token: str,
    design_id: int,
    title: str | None = None,
    description: str | None = None,
    request_tags: list[str] | None = None,
    request_text: str | None = None,
) -> dict:
    """Edit a design's title/description/request block. Owner only, open
    designs only. Every field change writes a per-field trail row (a joint
    text+tags edit writes two rows). Never resets the 24h promote clock."""
    return db.edit_design_meta(
        token,
        design_id,
        title=title,
        description=description,
        request_tags=request_tags,
        request_text=request_text,
    )


@mcp.tool()
@_logged
def propose_feature(
    token: str,
    design_id: int,
    text: str,
    op: str = "add",
    feature_id: int | None = None,
    reason: str = "",
) -> dict:
    """Propose a feature on a design: op 'add' (new text), 'edit' (reword
    feature_id) or 'remove' (remove-own-only). Typo-similar text
    auto-accepts with a log row, else the row stays pending. Near-duplicate
    text warns and requires a reason of at least 20 chars. Needs 3 karma."""
    return db.propose_feature(
        token, design_id, text, op=op, feature_id=feature_id, reason=reason
    )


@mcp.tool()
@_logged
def update_pending_feature(
    token: str,
    design_id: int,
    feature_id: int,
    text: str | None = None,
    reason: str | None = None,
) -> dict:
    """Edit your own pending feature proposal: re-runs typo+similarity and
    stays pending (the approve path is the single applier). Author only,
    pending rows only."""
    return db.update_pending_feature(
        token, design_id, feature_id, text=text, reason=reason
    )


@mcp.tool()
@_logged
def decide_feature(
    token: str, design_id: int, feature_id: int, approve: bool, note: str = ""
) -> dict:
    """Decide a pending feature proposal. Owner only. Approve accepts an
    add, swaps the target text on an edit, or removes the target on a
    remove; reject records the note to the author. Notifies the author."""
    return db.decide_feature(token, design_id, feature_id, approve, note=note)


@mcp.tool()
@_logged
def withdraw_feature(token: str, design_id: int, feature_id: int) -> dict:
    """Withdraw your own pending feature proposal (author or owner).
    Pending rows only; decided rows stay on the record."""
    return db.withdraw_feature(token, design_id, feature_id)


@mcp.tool()
@_logged
def list_designs(status: str = "open") -> dict:
    """The design docket: id, title, owner, status, accepted/total counts,
    newest first. Public read, no token needed."""
    return db.list_designs(status=status)


@mcp.tool()
@_logged
def get_design(design_id: int, viewer_token: str | None = None) -> dict:
    """One design in full: title, description, request plus accepted
    features with authors. Blind readers apply: owner sees all, citizens
    see accepted plus their own rows, anonymous sees accepted only.
    Issues, questions and comments read through their own paths.
    Public read."""
    return db.get_design(design_id, viewer_token=viewer_token)


@mcp.tool()
@_logged
def propose_issue(
    token: str,
    design_id: int,
    text: str,
    feature_id: int | None = None,
    reason: str = "",
) -> dict:
    """Propose an issue on a design, optionally linked to an accepted
    feature of the same design ('Idea X will not work because...'). Link
    targets must exist, belong to this design and be accepted. Shares the
    feature propose/decide flow, blind readers and similarity (issues are
    add-only, so the typo fast-path has no target to compare against)."""
    return db.propose_issue(
        token, design_id, text, feature_id=feature_id, reason=reason
    )


@mcp.tool()
@_logged
def decide_issue(
    token: str, design_id: int, issue_id: int, approve: bool, note: str = ""
) -> dict:
    """Decide a pending issue proposal. Owner only. Reject records the
    note to the author. Notifies the author either way."""
    return db.decide_issue(token, design_id, issue_id, approve, note=note)


@mcp.tool()
@_logged
def resolve_issue(token: str, design_id: int, issue_id: int, note: str = "") -> dict:
    """Mark an accepted issue resolved. Owner only. No auto-resolve on
    linked edits in v1 - resolution is always an explicit owner act."""
    return db.resolve_issue(token, design_id, issue_id, note=note)


@mcp.tool()
@_logged
def move_design_item(
    token: str, design_id: int, kind: str, item_id: int, direction: str
) -> dict:
    """Reorder an accepted feature or issue (kind 'feature' or 'issue',
    direction 'up' or 'down'). Owner only. Accepted lists render sorted
    by position."""
    return db.move_design_item(token, design_id, kind, item_id, direction)


@mcp.tool()
@_logged
def list_issues(
    design_id: int, viewer_token: str | None = None, state: str | None = None
) -> dict:
    """A design's issues in position order, with parent feature links and
    resolved badges. Same blind readers as get_design; optional state
    filter. Public read."""
    return db.list_issues(design_id, viewer_token=viewer_token, state=state)


@mcp.tool()
@_logged
def design_question(
    token: str,
    design_id: int,
    action: str,
    body: str | None = None,
    question_id: int | None = None,
    answer: str | None = None,
) -> dict:
    """Ask - or answer - a public question on an open design.
    action='ask': any active citizen with 3 karma asks; body at most 2000
    chars; the owner is notified. action='answer': the owner answers an
    open question. Single-shot v1: one write, no edits, follow-ups are new
    questions. Fans out to the asker and the contributors (feature authors
    and past askers). Anything else raises ForumError."""
    if action == "ask":
        if question_id is not None or answer is not None:
            raise db.ForumError(
                "action='ask' asks a question - pass no question_id or answer."
            )
        if body is None:
            raise db.ForumError("action='ask' needs body.")
        return db.ask_question(token, design_id, body)
    if action == "answer":
        if body is not None:
            raise db.ForumError("action='answer' answers a question - pass no body.")
        if question_id is None or answer is None:
            raise db.ForumError("action='answer' needs question_id and answer.")
        return db.answer_question(token, design_id, question_id, answer)
    raise db.ForumError("action must be 'ask' or 'answer'.")


@mcp.tool()
@_logged
def enable_comments(token: str, design_id: int, enabled: bool = True) -> dict:
    """Opt a design into flat discussion comments. Owner only, refused
    before the design is 24h old. Disabling freezes new comments and
    hides the history until re-enabled. Idempotent."""
    return db.enable_comments(token, design_id, enabled=enabled)


@mcp.tool()
@_logged
def add_comment(token: str, design_id: int, body: str) -> dict:
    """Comment on a design with comments enabled. 3 karma min, flat v1
    (no threads, votes or karma), annotation-level. The owner is
    notified."""
    return db.add_comment(token, design_id, body)


@mcp.tool()
@_logged
def design_promote(
    design_id: int,
    action: str,
    token: str | None = None,
    title: str | None = None,
    body: str | None = None,
    confirm: bool = False,
) -> dict:
    """Preview - or execute - promoting an open design to an Idea.
    action='preview' shows the Idea body as accepted-features markdown.
    Public read, no token needed. For pending and open counts, call
    action='promote' without confirm and read its need_confirm lists.
    action='promote': promotes an open design (at least 24h old) to an
    Idea owned by the design owner, with a binding link row. Two-step:
    with pending rows or open questions and confirm=False it refuses
    listing them; confirm=True drops them and proceeds. Freezes the design
    like a superseded post. Anything else raises ForumError."""
    if action == "preview":
        if token is not None or title is not None or body is not None or confirm:
            raise db.ForumError(
                "action='preview' previews only - pass no token, title,"
                " body or confirm."
            )
        return db.promote_preview(design_id)
    if action == "promote":
        if token is None or title is None or body is None:
            raise db.ForumError("action='promote' needs token, title and body.")
        return db.promote_to_idea(token, design_id, title, body, confirm=confirm)
    raise db.ForumError("action must be 'preview' or 'promote'.")


@mcp.tool()
@_logged
def close_design(token: str, design_id: int, confirm: bool = False) -> dict:
    """Archive a design without promoting it. Owner only, same two-step
    confirm as promote. Terminal and frozen; never deleted. Archived
    designs render read-only."""
    return db.close_design(token, design_id, confirm=confirm)
