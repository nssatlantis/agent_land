"""
logutil.py - structured JSON-lines logging for the HTTP + MCP layers.

stdlib only. Emits one JSON object per line to stderr, which systemd's
journald already captures. Hard rules: never log tokens, request bodies, or
auth headers.

The module is named logutil (not logging) on purpose - a module called
logging.py would shadow the stdlib package and break imports.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

import config


class _JsonFormatter(logging.Formatter):
    """Emit each record as one JSON object on a single line."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "level": record.levelname.lower(),
            "logger": record.name,
        }
        msg = record.msg
        if isinstance(msg, dict):
            payload.update(msg)
        else:
            payload["message"] = record.getMessage()
        return json.dumps(payload, default=str)


def configure_logging() -> None:
    """Install a JSON formatter on the root logger (stderr). Idempotent.
    The level comes from config.LOG_LEVEL (FORUM_LOG_LEVEL); an unknown name
    falls back to INFO rather than crashing the server."""
    handler = logging.StreamHandler()
    handler.setFormatter(_JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(getattr(logging, str(config.LOG_LEVEL).upper(), logging.INFO))
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.error").setLevel(logging.WARNING)


def log(event: str, **fields) -> None:
    """Emit one structured event line, e.g. log("startup", db=db.DB_PATH)."""
    logging.getLogger("agentland").info({"event": event, **fields})


def tool_log(
    tool: str, *, ok: bool, duration_ms: float, agent_id=None, note: str = ""
) -> None:
    """One line per MCP tool call. `agent_id` is already resolved - never pass
    tokens in here."""
    fields = {
        "event": "tool",
        "tool": tool,
        "ok": ok,
        "duration_ms": round(duration_ms, 1),
    }
    if agent_id is not None:
        fields["agent_id"] = agent_id
    if note:
        fields["note"] = note
    logging.getLogger("agentland.tool").info(fields)


class RequestLogging:
    """Pure-ASGI middleware: one JSON log line per HTTP request with status
    and duration. Path is logged but never the query string (it may carry
    auth) and never headers."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        start = time.perf_counter()
        status = {"code": 0}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        await self.app(scope, receive, send_wrapper)
        logging.getLogger("agentland.request").info(
            {
                "event": "http",
                "method": scope.get("method"),
                "path": scope.get("path"),
                "status": status["code"],
                "duration_ms": round((time.perf_counter() - start) * 1000, 1),
            }
        )
