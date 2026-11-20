"""Two workspaces may each register a connection with the same id (PT-13, #189)."""

from __future__ import annotations

import uuid

import pytest

from scf_mcp import MCPConnectionNotFoundError
from scf_mcp.manager import MCPConnectionManager
from scf_mcp.testing import sample_connection, sample_transport

A, B = uuid.uuid4(), uuid.uuid4()


def test_a_same_named_connection_is_another_workspaces_own() -> None:
    manager = MCPConnectionManager(transport_factory=lambda _: sample_transport())
    conn = sample_connection()
    manager.register(conn, workspace_id=A)
    manager.call_tool(conn.id, "search_pages", {"q": "vault"}, workspace_id=A)

    manager.register(conn.model_copy(update={"name": "B's"}), workspace_id=B)

    assert [c.name for c in manager.list_connections(workspace_id=A)] == [conn.name]
    assert [e.tool for e in manager.audit_entries(conn.id, workspace_id=A)] == ["search_pages"]
    assert manager.audit_entries(conn.id, workspace_id=B) == []
    with pytest.raises(MCPConnectionNotFoundError):
        manager.get(conn.id, workspace_id=uuid.uuid4())
