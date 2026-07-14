"""Production Self-Eval runner (A3): drive the real agent runtime + sandbox.

This is the live implementation of the injected ``EvalRunner`` seam the Self-Eval
Gate (``forge_eval.sweval.gate``) blocks on. It lives in the worker because this
is one of the only layers that may depend on BOTH ``forge_agent`` (to run a
coding agent) and ``forge_eval`` (the sandboxed scoring engine) — keeping
``forge_eval`` itself free of any agent-runtime dependency.

For a workspace's private per-repo suite it:

1. resolves the suite (on-disk case dir + a local git clone to check out from);
2. for each minted case, checks out ``base_commit`` into a scratch worktree,
   runs a coding :class:`~forge_agent.AgentRunner` (the config under test) with
   repo read/write tools, and diffs the worktree into a file-write patch — the
   ``SolveFn`` the sandboxed runner re-applies before running the HIDDEN tests;
3. scores the resolution rate via :func:`forge_eval.sweval.run_self_eval`.

Cold start is honest: no private suite, no minted cases, or no resolvable model
client (offline / no BYOK) all return ``None``, so the gate no-ops rather than
fabricating a score.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from forge_agent import AgentRunner, ToolRegistry
from forge_agent.tools import ToolResult
from forge_contracts import AgentObjective, ModelClient
from forge_contracts.sandbox import SandboxProvider
from forge_eval.benchmark.manifest import load_manifest
from forge_eval.benchmark.swe_case import parse_swe_case_fields
from forge_eval.golden import GoldenCase
from forge_eval.sweval import SelfEvalScorecard, run_self_eval

__all__ = [
    "ProductionEvalRunner",
    "SelfEvalSuiteHandle",
    "agent_solve",
    "build_coder_tools",
    "execute_self_eval_run",
]

#: Tool policy actions the eval agent is scoped to (allow-list on the objective).
_READ = "read_repo"
_WRITE = "write_code"


def _resolve_under(root: Path, raw: str) -> Path:
    """Resolve ``raw`` under ``root``, rejecting traversal outside it."""
    candidate = (root / raw).resolve()
    root_resolved = root.resolve()
    if root_resolved != candidate and root_resolved not in candidate.parents:
        raise ValueError(f"path escapes worktree: {raw!r}")
    return candidate


def build_coder_tools(worktree: Path) -> ToolRegistry:
    """A minimal repo-editing tool set (read/write/list) scoped under ``worktree``."""
    registry = ToolRegistry()

    def read_file(args: dict[str, Any]) -> ToolResult:
        path = _resolve_under(worktree, str(args.get("path", "")))
        if not path.is_file():
            return ToolResult(ok=False, error=f"not a file: {args.get('path')}")
        return ToolResult(ok=True, output=path.read_text())

    def write_file(args: dict[str, Any]) -> ToolResult:
        rel = str(args.get("path", "")).strip()
        if not rel:
            return ToolResult(ok=False, error="write_file requires a 'path'")
        target = _resolve_under(worktree, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(args.get("content", "")))
        return ToolResult(ok=True, output=f"wrote {rel}")

    def list_files(args: dict[str, Any]) -> ToolResult:
        base = _resolve_under(worktree, str(args.get("path", ".")))
        if not base.is_dir():
            return ToolResult(ok=False, error=f"not a directory: {args.get('path')}")
        names = sorted(
            p.relative_to(worktree).as_posix()
            for p in base.rglob("*")
            if p.is_file() and ".git" not in p.parts
        )
        return ToolResult(ok=True, output="\n".join(names))

    registry.add("read_file", read_file, action=_READ, description="Read a repo file")
    registry.add("write_file", write_file, action=_WRITE, description="Create or overwrite a file")
    registry.add("list_files", list_files, action=_READ, description="List repo files under a path")
    return registry


def _git(repo_path: str, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", repo_path, *args],
        check=check,
        capture_output=True,
        text=True,
    )


def _add_worktree(repo_path: str, base_commit: str) -> Path:
    """Check out ``base_commit`` into a fresh detached worktree; return its path.

    ``git worktree add`` must create the leaf itself, so the checkout goes in a
    ``wt`` subdir of a throwaway parent (removed wholesale by ``_remove_worktree``).
    """
    parent = Path(tempfile.mkdtemp(prefix="forge-selfeval-"))
    worktree = parent / "wt"
    _git(repo_path, "worktree", "add", "--detach", str(worktree), base_commit)
    return worktree


def _remove_worktree(repo_path: str, worktree: Path) -> None:
    _git(repo_path, "worktree", "remove", "--force", str(worktree), check=False)
    shutil.rmtree(worktree.parent, ignore_errors=True)


def _changed_files(worktree: Path) -> dict[str, str]:
    """Every added/modified/renamed file in ``worktree`` vs its base, as a patch map."""
    _git(str(worktree), "add", "-A", check=False)
    diff = _git(
        str(worktree),
        "diff",
        "--cached",
        "--name-only",
        "--diff-filter=ACMR",
        check=False,
    )
    patch: dict[str, str] = {}
    for rel in diff.stdout.splitlines():
        rel = rel.strip()
        if not rel:
            continue
        path = worktree / rel
        if path.is_file():
            patch[rel] = path.read_text()
    return patch


def agent_solve(
    case: GoldenCase,
    *,
    model_client: ModelClient,
    repo_path: str,
    model: str | None = None,
    max_iterations: int = 12,
) -> dict[str, str]:
    """Run the coding agent over ``case`` in a scratch worktree; return its edits.

    The agent only ever sees the case's public ``query`` (the hidden tests are
    stripped by :func:`forge_eval.sweval.run_swe_case` before this is called),
    edits a throwaway checkout at the case's ``base_commit`` through the
    read/write tools, and the diff of that checkout is the patch the sandboxed
    runner then applies before scoring against the hidden tests.
    """
    fields = parse_swe_case_fields(case)
    base = fields.base_commit or "HEAD"
    scratch = _add_worktree(repo_path, base)
    try:
        tools = build_coder_tools(scratch)
        objective = AgentObjective(
            objective=case.query,
            instructions=(
                "Modify the repository so the described change is fully implemented. "
                "Read files with read_file/list_files and save every edit with write_file. "
                "Do not add or edit test files — only the implementation."
            ),
            allowed_actions=[_READ, _WRITE],
            model=model,
            context={"self_eval_case_id": case.id},
        )
        runner = AgentRunner(
            model_client,
            tools=tools,
            repo_root=str(scratch),
            max_iterations=max_iterations,
        )
        runner.run(objective)
        return _changed_files(scratch)
    finally:
        _remove_worktree(repo_path, scratch)


@dataclass(frozen=True)
class SelfEvalSuiteHandle:
    """Where a workspace's private suite lives: its case dir + a git clone."""

    benchmark_suite_id: uuid.UUID
    #: On-disk suite version directory (holds ``manifest.yaml`` + minted cases).
    version_dir: str
    #: Local git clone of the suite's source repo, to check out ``base_commit`` from.
    repo_path: str


#: Resolve the private suite for a workspace; ``None`` = no suite (cold start).
SuiteResolver = Callable[[uuid.UUID], SelfEvalSuiteHandle | None]
#: Build the model client for a proposed config; ``None`` = offline / no BYOK.
ModelClientFor = Callable[[Any], ModelClient | None]


@dataclass(frozen=True)
class ProductionEvalRunner:
    """The live ``EvalRunner`` the Self-Eval Gate blocks on.

    Satisfies ``Callable[[UUID, Any], Awaitable[SelfEvalScorecard | None]]``:
    given a workspace and a proposed config, it runs the config's agent over the
    workspace's private suite and returns the scorecard, or ``None`` on any
    cold-start condition (no suite / no cases / no resolvable model client).
    """

    resolve_suite: SuiteResolver
    model_client_for: ModelClientFor
    sandbox_provider: SandboxProvider
    max_iterations: int = 12

    async def __call__(
        self, workspace_id: uuid.UUID, proposed_config: Any
    ) -> SelfEvalScorecard | None:
        handle = self.resolve_suite(workspace_id)
        if handle is None:
            return None  # no private suite for this workspace
        model_client = self.model_client_for(proposed_config)
        if model_client is None:
            return None  # offline / no BYOK — never fabricate a score
        _scoring, cases = load_manifest(Path(handle.version_dir))
        if not cases:
            return None  # suite has no minted cases yet
        model = _config_model(proposed_config)

        worktrees: list[Path] = []

        def worktree_for(case: GoldenCase) -> str:
            fields = parse_swe_case_fields(case)
            worktree = _add_worktree(handle.repo_path, fields.base_commit or "HEAD")
            worktrees.append(worktree)
            return str(worktree)

        def solve_fn(case: GoldenCase) -> dict[str, str]:
            return agent_solve(
                case,
                model_client=model_client,
                repo_path=handle.repo_path,
                model=model,
                max_iterations=self.max_iterations,
            )

        try:
            return await run_self_eval(
                cases=cases,
                solve_fn=solve_fn,
                sandbox_provider=self.sandbox_provider,
                worktree_for=worktree_for,
            )
        finally:
            for worktree in worktrees:
                _remove_worktree(handle.repo_path, worktree)


def _config_model(proposed_config: Any) -> str | None:
    """Best-effort extract of a model name from a proposed config (dict or attr)."""
    if isinstance(proposed_config, dict):
        model = proposed_config.get("model")
        return str(model) if model else None
    model = getattr(proposed_config, "model", None)
    return str(model) if model else None


#: Persist a baseline for a (workspace, suite) — matches
#: ``SelfEvalService.record_baseline`` (keyword-only, caller passes redacted config).
BaselineRecorder = Callable[..., Any]
#: Run a private suite for a config; ``None`` when there is nothing to score.
EvalRun = Callable[[uuid.UUID, Any], Any]


async def execute_self_eval_run(
    *,
    workspace_id: uuid.UUID,
    proposed_config: Any,
    benchmark_suite_id: uuid.UUID,
    runner: EvalRun,
    record_baseline: BaselineRecorder,
    recorded_by: uuid.UUID | None = None,
    overwrite: bool = True,
) -> SelfEvalScorecard | None:
    """Run the private-suite eval for a config and record it as the baseline (A4).

    This is the worker-owned, un-parked "self-eval run": it drives the (injected)
    live runner, and on a real score persists it as the workspace's baseline via
    ``record_baseline``. Returns the scorecard, or ``None`` when the runner had
    nothing to score (no suite / no cases / offline) — in which case no baseline
    is written. ``proposed_config`` must be free of secrets (AO role/tier config).
    """
    scorecard = await runner(workspace_id, proposed_config)
    if scorecard is None:
        return None
    record_baseline(
        workspace_id=workspace_id,
        benchmark_suite_id=benchmark_suite_id,
        resolved=scorecard.resolved,
        total=scorecard.total,
        resolution_rate=scorecard.resolution_rate,
        config=dict(proposed_config) if isinstance(proposed_config, dict) else {},
        recorded_by=recorded_by,
        overwrite=overwrite,
    )
    return scorecard
