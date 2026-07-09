"""Fixture-backed fakes for MCP tests (plan Task 1.12: "live transport mocked").

These let the SDK, gateway, and API layers be exercised end-to-end without any
network traffic. :class:`FakeTransport` records tool calls so tests can assert on
them; :func:`sample_transport` / :func:`sample_connection` build a realistic
Confluence-style fixture with multiple namespaces and a read/write tool pair.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from forge_contracts import (
    MCPAuth,
    MCPCapabilities,
    MCPConnection,
    MCPResource,
    MCPResourceContent,
)
from forge_mcp.transport import PromptMessage, PromptSpec, ToolSpec

# A secret deliberately embedded in fixture content to exercise redaction.
_SECRET_IN_CONTENT = "Authorization: Bearer sk-fixture-secret-123"

_DEFAULT_RESOURCES = [
    MCPResource(
        uri="confluence://engineering/page-1",
        name="Vault Rotation Runbook",
        namespace="engineering",
    ),
    MCPResource(
        uri="confluence://engineering/page-2", name="Service Page", namespace="engineering"
    ),
    MCPResource(uri="confluence://architecture/adr-7", name="ADR 7", namespace="architecture"),
    MCPResource(uri="confluence://finance/budget", name="Budget", namespace="finance"),
]

_DEFAULT_CONTENTS = {
    "confluence://engineering/page-1": f"How to rotate the vault token. {_SECRET_IN_CONTENT}",
    "confluence://engineering/page-2": "A general engineering page about services.",
    "confluence://architecture/adr-7": "Architecture decision record number seven.",
    "confluence://finance/budget": "Quarterly budget figures.",
}

_DEFAULT_TOOLS = [
    ToolSpec(name="search_pages", description="Search pages", read_only=True),
    ToolSpec(name="get_document", description="Read a document", read_only=True),
    ToolSpec(name="create_page", description="Create a page", read_only=False),
]

_DEFAULT_PROMPTS = [
    PromptSpec(
        name="summarize_page",
        description="Summarize a Confluence page",
        arguments=[{"name": "uri", "description": "resource uri", "required": True}],
    ),
    PromptSpec(name="triage_incident", description="Draft an incident triage"),
]

_DEFAULT_PROMPT_MESSAGES = {
    # A secret is planted in a rendered message to exercise redaction (rule 6).
    "summarize_page": [
        PromptMessage(role="system", content="You summarize pages."),
        PromptMessage(
            role="user",
            content=f"Summarize the page. {_SECRET_IN_CONTENT}",
        ),
    ],
    "triage_incident": [PromptMessage(role="user", content="Triage the incident.")],
}


class FakeTransport:
    """In-memory :class:`~forge_mcp.transport.Transport` for tests/fixtures."""

    def __init__(
        self,
        *,
        resources: list[MCPResource] | None = None,
        contents: dict[str, str] | None = None,
        tools: list[ToolSpec] | None = None,
        tool_results: dict[str, Any] | None = None,
        prompts: list[PromptSpec] | None = None,
        prompt_messages: dict[str, list[PromptMessage]] | None = None,
        elicitations: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self._resources = list(resources if resources is not None else _DEFAULT_RESOURCES)
        self._contents = dict(contents if contents is not None else _DEFAULT_CONTENTS)
        self._tools = list(tools if tools is not None else _DEFAULT_TOOLS)
        self._tool_results = dict(tool_results or {})
        self._prompts = list(prompts if prompts is not None else _DEFAULT_PROMPTS)
        self._prompt_messages = dict(
            prompt_messages if prompt_messages is not None else _DEFAULT_PROMPT_MESSAGES
        )
        # Map of tool name -> elicitation request the server returns for that call.
        self._elicitations = dict(elicitations or {})
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def list_resources(self) -> list[MCPResource]:
        return list(self._resources)

    def read_resource(self, uri: str) -> MCPResourceContent:
        if uri not in self._contents:
            raise KeyError(f"unknown resource uri: {uri}")
        return MCPResourceContent(uri=uri, content=self._contents[uri], mime_type="text/plain")

    def list_tools(self) -> list[ToolSpec]:
        return list(self._tools)

    def call_tool(self, name: str, arguments: Mapping[str, Any]) -> Any:
        self.calls.append((name, dict(arguments)))
        if name in self._elicitations:
            return {"elicitation": self._elicitations[name]}
        return self._tool_results.get(name, {"ok": True, "tool": name})

    def list_prompts(self) -> list[PromptSpec]:
        return list(self._prompts)

    def get_prompt(
        self, name: str, arguments: Mapping[str, Any] | None = None
    ) -> list[PromptMessage]:
        if name not in self._prompt_messages:
            raise KeyError(f"unknown prompt: {name}")
        return list(self._prompt_messages[name])


def sample_transport(**overrides: Any) -> FakeTransport:
    """Build a :class:`FakeTransport` seeded with the standard fixture."""
    return FakeTransport(**overrides)


def sample_connection(**overrides: Any) -> MCPConnection:
    """Build a read-only Confluence-style :class:`MCPConnection` fixture."""
    defaults: dict[str, Any] = {
        "id": "confluence-engineering",
        "name": "Engineering Confluence",
        "endpoint": "https://mcp.test/confluence",
        "auth": MCPAuth(),
        "capabilities": MCPCapabilities(resources=True, tools=True),
        "allow_write": False,
        "allowed_namespaces": ["engineering", "architecture"],
    }
    defaults.update(overrides)
    return MCPConnection(**defaults)


__all__ = ["FakeTransport", "sample_connection", "sample_transport"]
