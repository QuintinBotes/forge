"""Tests for the MCP connection model loader (plan Task 1.12).

The on-disk schema is the spec's "MCP Connection Schema". Loading defaults
``allow_write`` to ``False`` (security rule 1) regardless of omission.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from forge_contracts import MCPConnection, MCPIndexStrategy, MCPTransport, SyncMode
from forge_mcp.connection import load_connection, load_connection_file

SPEC_EXAMPLE = {
    "id": "confluence-engineering",
    "name": "Engineering Confluence",
    "transport": "http",
    "endpoint": "https://mcp.company.internal/confluence",
    "auth": {"type": "oauth"},
    "capabilities": {"resources": True, "tools": True, "prompts": False},
    "sync_mode": "incremental",
    "index_strategy": "sync_and_index",
    "freshness_sla_minutes": 30,
    "allow_write": False,
    "allowed_namespaces": ["engineering", "architecture"],
}


def test_load_connection_from_spec_example() -> None:
    conn = load_connection(SPEC_EXAMPLE)
    assert isinstance(conn, MCPConnection)
    assert conn.id == "confluence-engineering"
    assert conn.transport is MCPTransport.HTTP
    assert conn.sync_mode is SyncMode.INCREMENTAL
    assert conn.index_strategy is MCPIndexStrategy.SYNC_AND_INDEX
    assert conn.capabilities.tools is True
    assert conn.allowed_namespaces == ["engineering", "architecture"]


def test_allow_write_defaults_false_when_omitted() -> None:
    data = {k: v for k, v in SPEC_EXAMPLE.items() if k != "allow_write"}
    conn = load_connection(data)
    assert conn.allow_write is False


def test_load_connection_accepts_mcp_connection_key_wrapper() -> None:
    conn = load_connection({"mcp_connection": SPEC_EXAMPLE})
    assert conn.id == "confluence-engineering"


def test_load_connection_file_reads_yaml(tmp_path: Path) -> None:
    import yaml

    path = tmp_path / "confluence.yaml"
    path.write_text(yaml.safe_dump({"mcp_connection": SPEC_EXAMPLE}), encoding="utf-8")
    conn = load_connection_file(path)
    assert conn.id == "confluence-engineering"


def test_load_connection_rejects_non_mapping() -> None:
    with pytest.raises(ValueError):
        load_connection(["not", "a", "mapping"])  # type: ignore[arg-type]
