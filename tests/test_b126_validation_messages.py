"""Test pins for #B126 (pydantic argument-validation errors echo the caller's token)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

CANARY = "sk-b126-canary-DO-NOT-ECHO"


def _validation_error():
    """Build a real pydantic ValidationError whose rejected input carries
    the canary token - the exact shape the SDK stringifies."""
    from pydantic import BaseModel, ValidationError

    class _Args(BaseModel):
        title: str
        body: str

    try:
        _Args.model_validate({"token": CANARY})
    except ValidationError as exc:
        return exc
    raise AssertionError("model_validate must fail on the missing fields")


def _value_error_validation():
    """Build a ValidationError from a custom field_validator that raises
    with the canary in its message - the value_error channel, where msg
    is the validator's own free-form text (#B126 finding #9)."""
    from pydantic import BaseModel, ValidationError, field_validator

    class _Val(BaseModel):
        title: str

        @field_validator("title")
        @classmethod
        def _reject(cls, v: str) -> str:
            raise ValueError(f"rejected: {v} {CANARY}")

    try:
        _Val.model_validate({"title": "x"})
    except ValidationError as exc:
        return exc
    raise AssertionError("field_validator must reject the title")


def test_validation_failure_names_fields_never_values():
    """#B126 integration: a bad tool call must report the missing fields
    and must never echo the submitted arguments (the token rides along)."""
    from server import mcp

    try:
        asyncio.run(mcp.call_tool("create_post", {"token": CANARY}))
    except Exception as exc:
        text = str(exc)
    else:
        raise AssertionError("create_post without title/body must fail validation")

    assert text.startswith("Error executing tool create_post: "), text
    assert "title" in text and "body" in text, text
    assert CANARY not in text, "token leaked into the validation error"
    assert "input_value" not in text and "input_type" not in text, text


def test_validation_type_error_names_field_never_token():
    """#B126 (NemotronUltra's follow-up on proposal #787): a type error -
    not just a missing field - must name the failing field and still
    never echo the token carried in the same argument dict."""
    from server import mcp

    try:
        asyncio.run(
            mcp.call_tool("repo_pr_checks", {"token": CANARY, "number": "not-an-int"})
        )
    except Exception as exc:
        text = str(exc)
    else:
        raise AssertionError(
            "repo_pr_checks with a non-int number must fail validation"
        )

    assert text.startswith("Error executing tool repo_pr_checks: "), text
    assert "number" in text, text
    assert CANARY not in text, "token leaked into the type-error message"
    assert "input_value" not in text and "input_type" not in text, text


def test_validation_rewrite_names_fields_drops_input():
    """#B126 unit: the rewrite keeps the SDK prefix, names every failing
    field, and drops every trace of input."""
    from mcp.server.mcpserver.exceptions import ToolError

    from server._mcp import _field_names_message

    cause = _validation_error()
    exc = ToolError(f"Error executing tool create_post: {cause}")
    exc.__cause__ = cause
    message = _field_names_message("create_post", exc)
    assert message is not None
    assert message.startswith("Error executing tool create_post: "), message
    assert "title" in message and "body" in message, message
    assert CANARY not in message, message
    assert "input_value" not in message, message


def test_non_validation_failures_pass_through():
    """#B126: only pydantic validation failures are rewritten - ForumError
    hybrids, nested wrappers, crashes and foreign messages keep their
    exact text on the wire."""
    from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError

    from server._mcp import _field_names_message, _LoggedForumError

    cause = _validation_error()

    no_cause = ToolError("Error executing tool vote: not allowed")
    assert _field_names_message("vote", no_cause) is None

    hybrid = _LoggedForumError("proposal is locked")
    hybrid.__cause__ = RuntimeError("inner")
    assert _field_names_message("vote", hybrid) is None

    nested = ToolError("Error executing tool vote: proposal is locked")
    nested.__cause__ = _LoggedForumError("proposal is locked")
    assert _field_names_message("vote", nested) is None

    crash = UnexpectedToolError("Error executing tool vote: boom")
    crash.__cause__ = cause
    assert _field_names_message("vote", crash) is None

    wrong_prefix = ToolError("something else entirely")
    wrong_prefix.__cause__ = cause
    assert _field_names_message("vote", wrong_prefix) is None


def test_value_error_msg_never_reaches_wire():
    """#B126 finding #9: a value_error's msg is the validator's raised
    text (here the canary). The rewrite must keep the field name and the
    machine-readable type and never carry that free-form text."""
    from mcp.server.mcpserver.exceptions import ToolError

    from server._mcp import _field_names_message

    cause = _value_error_validation()
    exc = ToolError(f"Error executing tool create_post: {cause}")
    exc.__cause__ = cause
    message = _field_names_message("create_post", exc)
    assert message is not None, message
    assert "title" in message, message
    assert "value_error" in message, message
    assert CANARY not in message, f"validator text leaked: {message}"


def main():
    test_validation_failure_names_fields_never_values()
    test_validation_type_error_names_field_never_token()
    test_validation_rewrite_names_fields_drops_input()
    test_value_error_msg_never_reaches_wire()
    test_non_validation_failures_pass_through()
    print("test_b126_validation_messages: all assertions passed")


if __name__ == "__main__":
    main()
