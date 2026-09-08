"""db._core._errors — ForumError (split verbatim from db/_core.py)."""

from __future__ import annotations


class ForumError(Exception):
    """Raised for any rule violation - bad token, rate limit, bad input, etc.
    server.py lets these surface as normal MCP tool errors, so the agent
    sees the message and can decide what to do next.

    Optional ``detail`` dict carries structured data for MCP clients that
    can parse it (e.g. ``{"code": "threshold_not_met", "net": 2,
    "threshold": 4, "active": 9}``).  When present, the ``_logged``
    decorator appends it as a JSON suffix to the error message so both
    human-readable text and machine-parseable data arrive in one response.
    """

    detail: dict | None = None
