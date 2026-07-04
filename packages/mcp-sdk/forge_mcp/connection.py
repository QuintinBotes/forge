"""Load the spec's "MCP Connection Schema" into an :class:`MCPConnection` DTO.

Accepts a mapping (already-parsed YAML/JSON) or a path to a YAML file. A
top-level ``mcp_connection:`` wrapper key (as written in the spec example) is
unwrapped automatically. ``allow_write`` always defaults to ``False`` via the
frozen DTO, so an omitted field can never silently enable writes (rule 1).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from forge_contracts import MCPConnection


def load_connection(data: Mapping[str, Any]) -> MCPConnection:
    """Validate a mapping into an :class:`MCPConnection`."""
    if not isinstance(data, Mapping):
        raise ValueError(f"MCP connection must be a mapping, got {type(data).__name__}")
    payload = data.get("mcp_connection", data) if "mcp_connection" in data else data
    if not isinstance(payload, Mapping):
        raise ValueError("'mcp_connection' must be a mapping")
    return MCPConnection.model_validate(dict(payload))


def load_connection_file(path: str | Path) -> MCPConnection:
    """Load and validate an MCP connection from a YAML file."""
    file_path = Path(path)
    if not file_path.is_file():
        raise FileNotFoundError(f"No MCP connection file at {file_path}")
    raw = yaml.safe_load(file_path.read_text(encoding="utf-8"))
    if raw is None:
        raise ValueError(f"MCP connection file is empty: {file_path}")
    if not isinstance(raw, Mapping):
        raise ValueError(f"MCP connection file must contain a mapping: {file_path}")
    return load_connection(raw)


__all__ = ["load_connection", "load_connection_file"]
