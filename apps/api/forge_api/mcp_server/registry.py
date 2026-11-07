"""Tool registry for forge's MCP server (spec I §4-§5).

A tool is a Pydantic input model plus a handler returning a Pydantic output
model, so the MCP input schema and the REST schema come from the same class.
Write tools are hidden from principals that cannot use them and refused if
called anyway. Domain failures raise :class:`ToolError` and come back as
``isError`` results; protocol failures are :class:`JsonRpcError`.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from forge_api.auth.rbac import Permission, permissions_for
from forge_api.deps import Principal
from forge_api.mcp_server.protocol import FORBIDDEN, INVALID_PARAMS, JsonRpcError
from forge_api.observability.redaction import redact_text
from forge_knowledge.redaction import redact_secrets

#: Longest string a tool result may carry; longer ones are cut and flagged.
MAX_TEXT_CHARS = 4_000

#: Roles that may call write tools (which only ever create proposals).
_WRITE_PERMISSIONS = frozenset({Permission.WRITE, Permission.RUN_AGENT})


class ToolError(Exception):
    """A domain failure (not found, conflict, invalid state) reported in-band."""


@dataclass(frozen=True)
class ToolContext:
    principal: Principal
    session: Session


@dataclass(frozen=True)
class McpTool:
    name: str
    description: str
    input_model: type[BaseModel]
    handler: Callable[[ToolContext, Any], BaseModel]
    write: bool = False


def can_write(principal: Principal) -> bool:
    return bool(permissions_for(principal.role) & _WRITE_PERMISSIONS)


def _clean(value: Any, flags: dict[str, bool]) -> Any:
    """Redact secrets and cap string length throughout a JSON-ish value."""
    if isinstance(value, str):
        # The two redactors cover different patterns (PEM/key=value vs GitHub/provider keys).
        text = redact_text(redact_secrets(value))
        if len(text) > MAX_TEXT_CHARS:
            flags["truncated"] = True
            return text[:MAX_TEXT_CHARS]
        return text
    if isinstance(value, dict):
        return {key: _clean(item, flags) for key, item in value.items()}
    if isinstance(value, list):
        return [_clean(item, flags) for item in value]
    return value


def _text_result(structured: dict[str, Any], *, is_error: bool = False) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(structured, default=str)}],
        "structuredContent": structured,
        "isError": is_error,
    }


class ToolRegistry:
    def __init__(self, tools: Iterable[McpTool]) -> None:
        self._tools: dict[str, McpTool] = {}
        for tool in tools:
            if tool.name in self._tools:
                raise ValueError(f"duplicate MCP tool name: {tool.name}")
            self._tools[tool.name] = tool

    def visible(self, principal: Principal) -> list[McpTool]:
        writer = can_write(principal)
        return [tool for tool in self._tools.values() if writer or not tool.write]

    def list_payload(self, principal: Principal) -> list[dict[str, Any]]:
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "inputSchema": tool.input_model.model_json_schema(),
            }
            for tool in self.visible(principal)
        ]

    def call(self, ctx: ToolContext, name: object, arguments: object) -> dict[str, Any]:
        """Run one tool; returns an MCP ``CallToolResult`` dict."""
        if not isinstance(name, str) or not name:
            raise JsonRpcError(INVALID_PARAMS, "tools/call requires a tool name")
        tool = self._tools.get(name)
        if tool is None:
            raise JsonRpcError(INVALID_PARAMS, f"unknown tool: {name}")
        if tool.write and not can_write(ctx.principal):
            raise JsonRpcError(FORBIDDEN, f"tool {name} is not available to this principal")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise JsonRpcError(INVALID_PARAMS, "arguments must be an object")
        try:
            parsed = tool.input_model.model_validate(arguments)
        except ValidationError as exc:
            raise JsonRpcError(INVALID_PARAMS, f"invalid arguments for {name}: {exc}") from exc
        try:
            output = tool.handler(ctx, parsed)
        except ToolError as exc:
            return _text_result({"error": str(exc)}, is_error=True)
        flags: dict[str, bool] = {}
        structured = _clean(output.model_dump(mode="json"), flags)
        if flags:
            structured["truncated"] = True
        return _text_result(structured)


__all__ = [
    "MAX_TEXT_CHARS",
    "McpTool",
    "ToolContext",
    "ToolError",
    "ToolRegistry",
    "can_write",
]
