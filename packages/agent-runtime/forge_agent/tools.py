"""Tool registry with policy-checked dispatch.

A :class:`Tool` wraps a side-effecting handler and declares the *policy action*
it performs (e.g. ``write_code``) so the runtime's policy gate can decide whether
the call is allowed before dispatch. The registry is pluggable: callers register
their own tools (filesystem, tests, PRs, MCP) and a deterministic set of fakes in
tests. Built-in read/write helpers scoped to a sandbox root are provided via
:func:`default_tool_registry`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "FINISH_TOOL",
    "Tool",
    "ToolHandler",
    "ToolRegistry",
    "ToolResult",
    "default_tool_registry",
]

#: The control tool a model calls to finish the run and report its result.
FINISH_TOOL = "finish"


@dataclass
class ToolResult:
    """The outcome of dispatching a tool."""

    ok: bool
    output: str = ""
    error: str | None = None
    data: dict[str, Any] = field(default_factory=dict)


ToolHandler = Callable[[dict[str, Any]], ToolResult]


@dataclass
class Tool:
    """A registered tool: a named handler plus its policy action."""

    name: str
    handler: ToolHandler
    action: str | None = None
    description: str = ""
    #: Optional JSON Schema for the tool's parameters, advertised to real model
    #: providers so they emit well-formed tool arguments (HARD-02). ``None`` keeps
    #: the legacy name+description-only schema shape.
    input_schema: dict[str, Any] | None = None

    @property
    def policy_action(self) -> str:
        """The action string the policy gate evaluates (defaults to the name)."""
        return self.action or self.name

    def run(self, arguments: dict[str, Any]) -> ToolResult:
        return self.handler(arguments)


class ToolRegistry:
    """A name -> :class:`Tool` registry with safe dispatch."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> Tool:
        if tool.name in self._tools:
            raise ValueError(f"tool already registered: {tool.name}")
        self._tools[tool.name] = tool
        return tool

    def add(
        self,
        name: str,
        handler: ToolHandler,
        *,
        action: str | None = None,
        description: str = "",
        input_schema: dict[str, Any] | None = None,
    ) -> Tool:
        return self.register(
            Tool(
                name=name,
                handler=handler,
                action=action,
                description=description,
                input_schema=input_schema,
            )
        )

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        return name in self._tools

    def names(self) -> list[str]:
        return list(self._tools)

    def action_for(self, name: str) -> str:
        tool = self._tools.get(name)
        return tool.policy_action if tool is not None else name

    def schemas(self) -> list[dict[str, Any]]:
        """Tool schemas advertised to the model in a request.

        ``input_schema`` is included only for tools that declare one, so the
        legacy ``{name, description}`` shape is preserved for tools that don't
        (backward-compatible with existing callers/tests).
        """
        out: list[dict[str, Any]] = []
        for t in self._tools.values():
            schema: dict[str, Any] = {"name": t.name, "description": t.description}
            if t.input_schema is not None:
                schema["input_schema"] = t.input_schema
            out.append(schema)
        return out

    def dispatch(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(ok=False, error=f"unknown_tool: {name}")
        try:
            return tool.run(arguments)
        except Exception as exc:
            # Surface any handler failure as a structured result, not a crash.
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")


def _resolve(root: Path, raw: object) -> Path:
    """Resolve ``raw`` under ``root``, rejecting path traversal outside it."""
    candidate = (root / str(raw)).resolve()
    root_resolved = root.resolve()
    if root_resolved != candidate and root_resolved not in candidate.parents:
        raise ValueError(f"path escapes sandbox root: {raw!r}")
    return candidate


def default_tool_registry(root: str | Path | None = None) -> ToolRegistry:
    """A registry of safe, side-effect-scoped built-in tools.

    Filesystem tools are confined to ``root`` (the sandbox/worktree). No network
    or subprocess tools are registered by default.
    """
    registry = ToolRegistry()

    if root is not None:
        base = Path(root)

        def read_file(args: dict[str, Any]) -> ToolResult:
            path = _resolve(base, args.get("path", ""))
            if not path.is_file():
                return ToolResult(ok=False, error=f"not a file: {args.get('path')}")
            return ToolResult(ok=True, output=path.read_text())

        def write_file(args: dict[str, Any]) -> ToolResult:
            path = _resolve(base, args.get("path", ""))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(args.get("content", "")))
            return ToolResult(ok=True, output=f"wrote {args.get('path')}")

        def list_dir(args: dict[str, Any]) -> ToolResult:
            path = _resolve(base, args.get("path", "."))
            if not path.is_dir():
                return ToolResult(ok=False, error=f"not a directory: {args.get('path')}")
            names = sorted(p.name for p in path.iterdir())
            return ToolResult(ok=True, output="\n".join(names), data={"entries": names})

        _path_schema = {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Repo-relative path"}},
            "required": ["path"],
        }
        _write_schema = {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Repo-relative path"},
                "content": {"type": "string", "description": "File contents to write"},
            },
            "required": ["path", "content"],
        }
        registry.add(
            "read_file",
            read_file,
            action="read_repo",
            description="Read a repo file",
            input_schema=_path_schema,
        )
        registry.add(
            "write_file",
            write_file,
            action="write_code",
            description="Write a repo file",
            input_schema=_write_schema,
        )
        registry.add(
            "list_dir",
            list_dir,
            action="read_repo",
            description="List a directory",
            input_schema=_path_schema,
        )

    return registry
