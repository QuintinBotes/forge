"""Tool registry with policy-checked dispatch.

A :class:`Tool` wraps a side-effecting handler and declares the *policy action*
it performs (e.g. ``write_code``) so the runtime's policy gate can decide whether
the call is allowed before dispatch. The registry is pluggable: callers register
their own tools (filesystem, tests, PRs, MCP) and a deterministic set of fakes in
tests. Built-in read/write helpers scoped to a sandbox root are provided via
:func:`default_tool_registry`.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from forge_contracts import CommandOutput

__all__ = [
    "FINISH_TOOL",
    "CommandRunner",
    "GitHubCIClient",
    "Tool",
    "ToolHandler",
    "ToolRegistry",
    "ToolResult",
    "agent_tool_belt",
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


#: Runs one exact ``policy.commands`` string inside the run's sandbox. The model
#: picks *which* declared command runs, never the command text.
CommandRunner = Callable[[str], CommandOutput]


class GitHubCIClient(Protocol):
    """The injected integration SDK transport; credentials stay outside the sandbox."""

    def _request(
        self,
        method: str,
        path: str,
        *,
        action: str | None = None,
        repo: str | None = None,
        **kwargs: Any,
    ) -> Any: ...

    @staticmethod
    def _raise_for_status(resp: Any) -> None: ...


_SEARCH_MAX_MATCHES = 200
_SEARCH_MAX_FILE_BYTES = 1_000_000
_SEARCH_SKIP_DIRS = frozenset({".git", "node_modules", ".venv", "__pycache__", ".mypy_cache"})
_OUTPUT_TAIL_LINES = 400
#: Keys looked up in ``policy.commands`` for each command tool, first match wins.
_COMMAND_TOOLS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("run_tests", ("test", "tests"), "Run the repository's declared test command"),
    ("run_lint", ("lint",), "Run the repository's declared lint command"),
    ("run_typecheck", ("typecheck", "type_check"), "Run the repository's declared type check"),
)


def _tail(text: str, lines: int = _OUTPUT_TAIL_LINES) -> str:
    return "\n".join(text.splitlines()[-lines:])


def agent_tool_belt(
    root: str | Path,
    *,
    run_id: str,
    commands: Mapping[str, str] | None = None,
    run_command: CommandRunner | None = None,
    ci_client: GitHubCIClient | None = None,
    ci_repo: str | None = None,
) -> ToolRegistry:
    """The tools a code-changing agent gets on the main paths (spec G §3.2).

    Everything acts inside ``root`` (the run's worktree). Command tools exist
    only for commands the repository declares in ``.forge/policy.yaml`` and only
    when a sandbox ``run_command`` is supplied. ``git_commit`` commits locally
    with hooks disabled; pushing and opening PRs happen in the run finalizer,
    never from a tool (ADR-0014). Optional CI reads use a host-side integration
    client bound to one repository; no client or credentials enter the sandbox.
    """
    base = Path(root)
    registry = default_tool_registry(base)

    def search_code(args: dict[str, Any]) -> ToolResult:
        needle = str(args.get("query", ""))
        if not needle:
            return ToolResult(ok=False, error="query is required")
        start = _resolve(base, args.get("path", "."))
        matches: list[str] = []
        for path in sorted(start.rglob("*")):
            if len(matches) >= _SEARCH_MAX_MATCHES:
                break
            rel = path.relative_to(base)
            if not path.is_file() or set(rel.parts) & _SEARCH_SKIP_DIRS:
                continue
            if path.stat().st_size > _SEARCH_MAX_FILE_BYTES:
                continue
            try:
                text = path.read_text()
            except (UnicodeDecodeError, OSError):
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                if needle in line:
                    matches.append(f"{rel}:{number}: {line.strip()[:200]}")
                    if len(matches) >= _SEARCH_MAX_MATCHES:
                        break
        return ToolResult(
            ok=True,
            output="\n".join(matches) or "no matches",
            data={"matches": len(matches), "capped": len(matches) >= _SEARCH_MAX_MATCHES},
        )

    registry.add(
        "search_code",
        search_code,
        action="read_repo",
        description="Find lines containing a literal string in the repository",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Literal text to find"},
                "path": {"type": "string", "description": "Repo-relative directory"},
            },
            "required": ["query"],
        },
    )

    declared = dict(commands or {})
    if run_command is not None:
        for tool_name, keys, description in _COMMAND_TOOLS:
            command = next((declared[k] for k in keys if declared.get(k)), None)
            if command is None:
                continue

            def run_declared(_: dict[str, Any], command: str = command) -> ToolResult:
                out = run_command(command)
                status = "timed out" if out.timed_out else f"exit {out.exit_code}"
                return ToolResult(
                    ok=True,
                    output=f"{status}\n{_tail(out.stdout + out.stderr)}",
                    data={"exit_code": out.exit_code, "timed_out": out.timed_out},
                )

            registry.add(
                tool_name,
                run_declared,
                action="run_tests",
                description=description,
                input_schema={"type": "object", "properties": {}},
            )

    def git_commit(args: dict[str, Any]) -> ToolResult:
        message = str(args.get("message", "")).strip()
        if not message:
            return ToolResult(ok=False, error="message is required")
        git = [
            "git",
            "-c", "core.hooksPath=/dev/null",
            "-c", "user.name=forge-agent",
            "-c", "user.email=agent@forge.local",
        ]  # fmt: skip
        subprocess.run([*git, "add", "-A"], cwd=base, check=True, capture_output=True)
        staged = subprocess.run(
            [*git, "diff", "--cached", "--quiet"], cwd=base, capture_output=True
        )
        if staged.returncode == 0:
            return ToolResult(ok=False, error="nothing to commit")
        subprocess.run(
            [*git, "commit", "--no-verify", "-m", f"{message}\n\nForge-Run: {run_id}"],
            cwd=base,
            check=True,
            capture_output=True,
        )
        sha = subprocess.run(
            [*git, "rev-parse", "HEAD"], cwd=base, check=True, capture_output=True, text=True
        ).stdout.strip()
        return ToolResult(ok=True, output=f"committed {sha}", data={"sha": sha})

    registry.add(
        "git_commit",
        git_commit,
        action="write_code",
        description="Commit all changes in the worktree (local only; never pushes)",
        input_schema={
            "type": "object",
            "properties": {"message": {"type": "string", "description": "Commit message"}},
            "required": ["message"],
        },
    )
    if (ci_client is None) != (ci_repo is None):
        raise ValueError("ci_client and ci_repo must be configured together")
    if ci_client is not None and ci_repo is not None:
        _register_ci_tools(registry, ci_client, ci_repo)
    return registry


def _register_ci_tools(registry: ToolRegistry, client: GitHubCIClient, repo: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", repo) or repo.split("/")[
        -1
    ] in {".", ".."}:
        raise ValueError("ci_repo must be an owner/repository name")

    def identifier(args: dict[str, Any], key: str) -> int:
        value = args.get(key)
        if set(args) != {key} or type(value) is not int or value <= 0:
            raise ValueError(f"only a positive integer {key} is accepted")
        return value

    def get(path: str, action: str, **kwargs: Any) -> Any:
        response = client._request(
            "GET", f"/repos/{repo}/{path}", action=action, repo=repo, **kwargs
        )
        client._raise_for_status(response)
        return response

    def ci_failures(args: dict[str, Any]) -> ToolResult:
        check_run_id = identifier(args, "check_run_id")
        annotations: list[dict[str, Any]] = []
        # shortcut: inspect at most 200 annotations, add pagination budgets for larger checks.
        for page in (1, 2):
            items = get(
                f"check-runs/{check_run_id}/annotations",
                "ci_failures",
                params={"per_page": 100, "page": page},
            ).json()
            annotations.extend(items[:100])
            if len(items) < 100:
                break
        failures = [a for a in annotations if a.get("annotation_level") in {"failure", "warning"}]
        output = (
            "\n".join(
                f"{a.get('path', '')}:{a.get('start_line', '')}: {a.get('message', '')}"
                for a in failures
            )
            or "no failure annotations"
        )
        if len(annotations) == 200:
            output += "\nAnnotation scan capped at 200; later failures may be omitted."
        return ToolResult(
            ok=True,
            output=f"GitHub {repo} check run {check_run_id}:\n{output}",
            data={
                "repo": repo,
                "check_run_id": check_run_id,
                "failures": failures,
                "capped": len(annotations) == 200,
            },
        )

    def ci_job_log(args: dict[str, Any]) -> ToolResult:
        job_id = identifier(args, "job_id")
        response = get(f"actions/jobs/{job_id}/logs", "ci_job_log", follow_redirects=True)
        return ToolResult(
            ok=True,
            output=f"GitHub {repo} job {job_id}:\n{_tail(response.text)}",
            data={"repo": repo, "job_id": job_id},
        )

    def pr_status(args: dict[str, Any]) -> ToolResult:
        pr_number = identifier(args, "pr_number")
        pr = get(f"pulls/{pr_number}", "pr_status").json()
        sha = pr["head"]["sha"]
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40,64}", sha):
            raise ValueError("invalid PR head SHA")
        checks = get(f"commits/{sha}/check-runs", "pr_status", params={"per_page": 100}).json()
        # shortcut: expose the first 100 check runs, add pagination budgets for larger PRs.
        check_runs = [
            {key: check.get(key) for key in ("id", "name", "status", "conclusion")}
            for check in checks["check_runs"][:100]
        ]
        data = {
            "repo": repo,
            "pr_number": pr_number,
            **{key: pr.get(key) for key in ("state", "draft", "merged", "mergeable")},
            "head_sha": sha,
            "check_runs": check_runs,
            "capped": checks.get("total_count", len(check_runs)) > len(check_runs),
        }
        return ToolResult(ok=True, output=json.dumps(data), data=data)

    for name, handler, key, description in (
        ("ci_failures", ci_failures, "check_run_id", "Read GitHub check failure annotations"),
        ("ci_job_log", ci_job_log, "job_id", "Read the last 400 lines of a GitHub Actions job log"),
        ("pr_status", pr_status, "pr_number", "Read GitHub PR state and its head check runs"),
    ):
        registry.add(
            name,
            handler,
            action="read_repo",
            description=description,
            input_schema={
                "type": "object",
                "properties": {key: {"type": "integer", "minimum": 1}},
                "required": [key],
                "additionalProperties": False,
            },
        )
