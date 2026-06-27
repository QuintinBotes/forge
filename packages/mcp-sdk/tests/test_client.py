"""Tests for :class:`forge_mcp.MCPGatewayClient` (plan Task 1.12).

Required behaviours from the plan:
- a read-only connection rejects a write tool call,
- an audit entry is recorded with redacted secrets,
- namespace scoping filters resources.

Live transport is mocked via :class:`forge_mcp.testing.FakeTransport`.
"""

from __future__ import annotations

import pytest

from forge_contracts import (
    Decision,
    DecisionEffect,
    MCPAuth,
    MCPAuthType,
    MCPClient,
    MCPConnection,
    MCPResourceContent,
    MCPToolResult,
    MCPWriteForbiddenError,
    Policy,
    PolicyViolationError,
    ToolCall,
)
from forge_mcp import MCPGatewayClient
from forge_mcp.exceptions import MCPInputError, MCPNamespaceError, MCPSecurityError
from forge_mcp.testing import FakeTransport, sample_connection, sample_transport


def _connect(
    *, allow_write: bool = False, transport: FakeTransport | None = None, **conn_kw: object
) -> tuple[MCPGatewayClient, FakeTransport]:
    conn = sample_connection(allow_write=allow_write, **conn_kw)
    tr = transport or sample_transport()
    client = MCPGatewayClient(transport=tr)
    client.connect(conn)
    return client, tr


# --------------------------------------------------------------------------- #
# Structural conformance                                                       #
# --------------------------------------------------------------------------- #


def test_client_satisfies_mcp_client_protocol() -> None:
    assert isinstance(MCPGatewayClient(), MCPClient)


# --------------------------------------------------------------------------- #
# Read-only default (spec rule 1)                                             #
# --------------------------------------------------------------------------- #


def test_read_only_connection_rejects_write_tool_call() -> None:
    client, _ = _connect(allow_write=False)
    with pytest.raises(MCPWriteForbiddenError):
        client.call_tool("create_page", {"title": "x"})


def test_read_only_connection_records_forbidden_audit_entry() -> None:
    client, _ = _connect(allow_write=False)
    with pytest.raises(MCPWriteForbiddenError):
        client.call_tool("create_page", {"title": "x"})
    entries = client.audit_entries
    assert len(entries) == 1
    assert entries[0].tool == "create_page"
    assert entries[0].status == "forbidden"
    assert entries[0].payload_hash


def test_write_tool_allowed_when_connection_permits_write() -> None:
    client, tr = _connect(allow_write=True)
    result = client.call_tool("create_page", {"title": "x"})
    assert isinstance(result, MCPToolResult)
    assert result.status == "ok"
    assert ("create_page", {"title": "x"}) in tr.calls


# --------------------------------------------------------------------------- #
# Read tool calls + audit with redaction (spec rules 4 & 6)                    #
# --------------------------------------------------------------------------- #


def test_read_tool_call_returns_result_with_hash_and_latency() -> None:
    client, _ = _connect()
    result = client.call_tool("search_pages", {"q": "vault"})
    assert result.status == "ok"
    assert result.tool == "search_pages"
    assert result.payload_hash and len(result.payload_hash) == 64
    assert result.latency_ms is not None and result.latency_ms >= 0


def test_audit_entry_recorded_with_redacted_secrets() -> None:
    client, _ = _connect()
    client.call_tool("search_pages", {"q": "vault", "token": "super-secret-value"})
    entries = client.audit_entries
    assert len(entries) == 1
    entry = entries[0]
    assert entry.status == "ok"
    assert entry.redacted is True
    assert entry.payload_hash
    # The raw secret never appears anywhere in the serialized audit record.
    assert "super-secret-value" not in entry.model_dump_json()


def test_tool_result_content_is_redacted() -> None:
    tr = sample_transport(
        tool_results={"search_pages": {"answer": "ok", "api_key": "leak-me-123"}}
    )
    client, _ = _connect(transport=tr)
    result = client.call_tool("search_pages", {"q": "x"})
    assert "leak-me-123" not in result.model_dump_json()


# --------------------------------------------------------------------------- #
# Input validation (spec rule 3)                                              #
# --------------------------------------------------------------------------- #


def test_empty_tool_name_is_rejected() -> None:
    client, _ = _connect()
    with pytest.raises(MCPInputError):
        client.call_tool("", {"q": "x"})


def test_non_mapping_arguments_are_rejected() -> None:
    client, _ = _connect()
    with pytest.raises(MCPInputError):
        client.call_tool("search_pages", ["not", "a", "dict"])  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Namespace scoping (spec rule 5)                                             #
# --------------------------------------------------------------------------- #


def test_list_resources_filters_by_allowed_namespaces() -> None:
    client, _ = _connect()
    resources = client.list_resources()
    namespaces = {r.namespace for r in resources}
    assert "finance" not in namespaces
    assert namespaces <= {"engineering", "architecture"}


def test_list_resources_narrows_to_requested_namespace() -> None:
    client, _ = _connect()
    resources = client.list_resources(namespace="engineering")
    assert resources
    assert all(r.namespace == "engineering" for r in resources)


def test_read_resource_inside_scope_returns_redacted_content() -> None:
    client, _ = _connect()
    content = client.read_resource("confluence://engineering/page-1")
    assert isinstance(content, MCPResourceContent)
    assert "leak" not in content.model_dump_json().lower() or "[redacted]" in content.content


def test_read_resource_outside_scope_is_blocked() -> None:
    client, _ = _connect()
    with pytest.raises(MCPNamespaceError):
        client.read_resource("confluence://finance/secret")


# --------------------------------------------------------------------------- #
# RFC 8707 token binding (spec rule 2)                                        #
# --------------------------------------------------------------------------- #


def test_connect_binds_token_to_resource() -> None:
    conn = sample_connection(
        auth=MCPAuth(type=MCPAuthType.OAUTH, resource="https://mcp/confluence"),
    )
    client = MCPGatewayClient(transport=sample_transport())
    client.connect(conn)
    assert client.token_binding == "https://mcp/confluence"


def test_authenticated_connection_without_binding_is_rejected() -> None:
    conn = MCPConnection(
        id="c", name="c", endpoint=None, auth=MCPAuth(type=MCPAuthType.OAUTH)
    )
    client = MCPGatewayClient(transport=sample_transport())
    with pytest.raises(MCPSecurityError):
        client.connect(conn)


# --------------------------------------------------------------------------- #
# Policy evaluation hook (spec rule 7)                                        #
# --------------------------------------------------------------------------- #


class _DenyAllEvaluator:
    def load(self, repo_root: object) -> Policy:  # pragma: no cover - unused
        return Policy(repo_id="r")

    def evaluate(self, action: ToolCall, policy: Policy) -> Decision:
        return Decision(effect=DecisionEffect.DENY, reason="blocked by test policy")


def test_policy_evaluator_can_block_a_tool_call() -> None:
    conn = sample_connection(allow_write=False)
    client = MCPGatewayClient(
        transport=sample_transport(),
        policy=Policy(repo_id="r"),
        evaluator=_DenyAllEvaluator(),
    )
    client.connect(conn)
    with pytest.raises(PolicyViolationError):
        client.call_tool("search_pages", {"q": "x"})
    assert client.audit_entries[-1].status == "denied"
