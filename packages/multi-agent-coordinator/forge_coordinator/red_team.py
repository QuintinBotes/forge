"""Red-Team Gate adversary harness (Red-Team Gate feature).

``run_red_team`` runs a HETEROGENEOUS adversary against a candidate diff *before*
it reaches the human implementation gate. The adversary is a distinct
:class:`~forge_agent.AgentRunner` driven by a model on a **different provider**
than the coder used (a homogeneous adversary is not a real adversary and is
rejected up front). It authors a candidate failing test targeting the diff's
NEW behavior, and that test is executed in a sandbox through the frozen
:class:`~forge_contracts.SandboxProvider` ``create``/``run`` seam — the same
seam the container/worktree providers implement, wired here for the first time
(it was previously reached only by the orphan reaper).

A change is BLOCKED only when:

* the adversary's authored test **actually fails** when executed in the sandbox
  (exit code != 0) — an *executed* failing test, never a self-reported pass; or
* the adversary reports a structured spec-violation that references a real
  :class:`~forge_contracts.AcceptanceCriterion` of the spec.

Otherwise the change earns a ``survived`` verdict (feeds the Phase-1
attestation as a "survived adversarial review" input, wired by the workflow).

The adversary is scoped to ``ROLE_TOOLS[SubAgentRole.ADVERSARY]`` — it may read
the repo, author a test, and run SAST, but never edit product code. Its own
``run_tests`` claim is *not* trusted: the trusted signal is the exit code of the
sandbox execution driven here.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from forge_agent import AgentRunner, ProviderName, ToolRegistry
from forge_agent.providers.router import DEFAULT_TIER_MODELS
from forge_agent.tools import ToolResult
from forge_contracts import (
    AcceptanceCriterion,
    AgentObjective,
    ModelClient,
    SandboxKind,
    SandboxProvider,
    SandboxSpec,
)
from forge_contracts.coordinator import ROLE_TOOLS
from forge_contracts.enums import SubAgentRole

__all__ = [
    "FailingTestRef",
    "HomogeneousAdversaryError",
    "RedTeamError",
    "RedTeamResult",
    "SpecViolation",
    "run_red_team",
]

#: Default wall-clock ceiling for the sandboxed adversary test (seconds).
DEFAULT_TEST_TIMEOUT_S = 300
#: Bytes of combined stdout/stderr retained as blocking evidence.
_EVIDENCE_CAP = 4096


# --------------------------------------------------------------------------- #
# Errors                                                                         #
# --------------------------------------------------------------------------- #


class RedTeamError(RuntimeError):
    """A red-team scan could not be run (misconfiguration, unknown provider)."""


class HomogeneousAdversaryError(RedTeamError):
    """The adversary shares the coder's provider — not a heterogeneous adversary."""


# --------------------------------------------------------------------------- #
# Typed result                                                                  #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FailingTestRef:
    """A reference to the adversary's authored test that failed in the sandbox."""

    path: str
    test_cmd: str
    exit_code: int


@dataclass(frozen=True)
class SpecViolation:
    """A structured spec-violation referencing a real ``AcceptanceCriterion``."""

    criterion_id: str
    detail: str = ""


@dataclass(frozen=True)
class RedTeamResult:
    """The verdict of an adversarial review of a candidate diff."""

    blocked: bool
    kind: Literal["failing_test", "spec_violation", "survived"]
    failing_test_ref: FailingTestRef | None = None
    violation: SpecViolation | None = None
    evidence: str = ""
    adversary_model: str = ""
    coder_model: str = ""


# --------------------------------------------------------------------------- #
# Provider inference (heterogeneity enforcement)                                #
# --------------------------------------------------------------------------- #

#: Exact ``model -> provider`` map derived from the router's default tier tables.
_MODEL_PROVIDER: dict[str, ProviderName] = {
    model: provider for provider, tiers in DEFAULT_TIER_MODELS.items() for model in tiers.values()
}


def _provider_of(model: str) -> ProviderName:
    """Infer the BYOK provider for a concrete model id (deterministic, offline)."""
    key = (model or "").strip().lower()
    if not key:
        raise RedTeamError("cannot determine provider: empty model id")
    if key in _MODEL_PROVIDER:
        return _MODEL_PROVIDER[key]
    if any(tok in key for tok in ("claude", "opus", "sonnet", "haiku", "fable", "anthropic")):
        return ProviderName.anthropic
    if "gpt" in key or "openai" in key or key.startswith(("o1", "o3", "o4")):
        return ProviderName.openai
    raise RedTeamError(f"cannot determine provider for model {model!r}")


# --------------------------------------------------------------------------- #
# Adversary tool capture                                                        #
# --------------------------------------------------------------------------- #


class _AttackCapture:
    """Records what the adversary authored (test file / spec-violation claim)."""

    def __init__(self) -> None:
        self.test_path: str | None = None
        self.test_cmd: str | None = None
        self.violation_criterion_id: str | None = None
        self.violation_detail: str = ""


def _resolve_under(root: Path, raw: str) -> Path:
    """Resolve ``raw`` under ``root``, rejecting traversal outside it."""
    candidate = (root / raw).resolve()
    root_resolved = root.resolve()
    if root_resolved != candidate and root_resolved not in candidate.parents:
        raise ValueError(f"path escapes worktree: {raw!r}")
    return candidate


def _build_adversary_tools(worktree: Path, capture: _AttackCapture) -> ToolRegistry:
    """Role-scoped tools for the adversary: read the repo + author a test.

    Every tool's policy action lies within ``ROLE_TOOLS[ADVERSARY]`` so the
    runtime's policy gate admits it; nothing here can edit product code.
    """
    registry = ToolRegistry()

    def read_file(args: dict[str, Any]) -> ToolResult:
        path = _resolve_under(worktree, str(args.get("path", "")))
        if not path.is_file():
            return ToolResult(ok=False, error=f"not a file: {args.get('path')}")
        return ToolResult(ok=True, output=path.read_text())

    def write_test(args: dict[str, Any]) -> ToolResult:
        rel = str(args.get("path", "")).strip()
        if not rel:
            return ToolResult(ok=False, error="write_test requires a 'path'")
        target = _resolve_under(worktree, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(args.get("content", "")))
        capture.test_path = rel
        capture.test_cmd = str(args.get("test_cmd") or f"python -m pytest {rel} -q").strip()
        return ToolResult(ok=True, output=f"authored candidate test {rel}")

    def report_spec_violation(args: dict[str, Any]) -> ToolResult:
        cid = str(args.get("criterion_id", "")).strip()
        if not cid:
            return ToolResult(ok=False, error="report_spec_violation requires a 'criterion_id'")
        capture.violation_criterion_id = cid
        capture.violation_detail = str(args.get("detail", ""))
        return ToolResult(ok=True, output=f"recorded spec-violation against {cid}")

    registry.add("read_file", read_file, action="read_repo", description="Read a repo file")
    registry.add(
        "write_test",
        write_test,
        action="write_test",
        description="Author a candidate failing test targeting the diff's new behavior",
    )
    # A structured spec-violation is a read/analysis output (action=read_repo), not
    # a code edit — it stays within the adversary's read-only allow-list.
    registry.add(
        "report_spec_violation",
        report_spec_violation,
        action="read_repo",
        description="Report a spec-violation referencing an acceptance criterion id",
    )
    return registry


# --------------------------------------------------------------------------- #
# Spec helpers                                                                   #
# --------------------------------------------------------------------------- #


def _spec_criteria(spec: Any) -> list[AcceptanceCriterion]:
    """Extract acceptance criteria from a spec object / list / mapping."""
    raw: Any = spec
    if raw is None:
        return []
    if isinstance(raw, Mapping):
        raw = raw.get("acceptance_criteria", [])
    elif hasattr(raw, "acceptance_criteria"):
        raw = raw.acceptance_criteria
    out: list[AcceptanceCriterion] = []
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        for item in raw:
            if isinstance(item, AcceptanceCriterion):
                out.append(item)
            elif isinstance(item, Mapping):
                out.append(AcceptanceCriterion(**item))
    return out


def _spec_text(spec: Any, criteria: list[AcceptanceCriterion]) -> str:
    if isinstance(spec, str):
        return spec
    lines = [f"- {c.id}: {c.text}" for c in criteria]
    return "Acceptance criteria:\n" + "\n".join(lines) if lines else "(no acceptance criteria)"


# --------------------------------------------------------------------------- #
# The harness                                                                   #
# --------------------------------------------------------------------------- #


async def run_red_team(
    *,
    diff: str,
    spec: Any,
    adversary_client: ModelClient,
    sandbox_provider: SandboxProvider,
    coder_model: str,
    worktree_path: str,
    adversary_model: str | None = None,
    workspace_id: uuid.UUID | None = None,
    agent_run_id: uuid.UUID | None = None,
    timeout_s: int = DEFAULT_TEST_TIMEOUT_S,
    max_iterations: int = 6,
) -> RedTeamResult:
    """Attack ``diff`` with a heterogeneous adversary and return a typed verdict.

    Raises :class:`HomogeneousAdversaryError` when the adversary's provider
    matches the coder's (``coder_model``). The adversary authors a candidate
    test which is executed through the sandbox ``create``/``run`` seam; a
    non-zero exit BLOCKS. Alternatively a structured spec-violation referencing
    a real acceptance criterion BLOCKS. Otherwise the diff SURVIVES.
    """
    # (1) Enforce heterogeneity BEFORE spending any model/sandbox work.
    adv_model = (adversary_model or getattr(adversary_client, "model", "") or "").strip()
    if not adv_model:
        raise RedTeamError(
            "adversary model id is required to verify heterogeneity "
            "(pass adversary_model= or expose .model on the client)"
        )
    coder_provider = _provider_of(coder_model)
    adv_provider = _provider_of(adv_model)
    if adv_provider == coder_provider:
        raise HomogeneousAdversaryError(
            f"adversary provider {adv_provider.value!r} matches the coder's "
            f"({coder_model!r}); a homogeneous adversary is not allowed"
        )

    worktree = Path(worktree_path)
    criteria = _spec_criteria(spec)
    valid_ids = {c.id for c in criteria}

    # (2) Drive the adversary AgentRunner (ADVERSARY role tools) to author a test.
    capture = _AttackCapture()
    registry = _build_adversary_tools(worktree, capture)
    objective = AgentObjective(
        task_id=uuid.uuid4(),
        key="red-team",
        objective=(
            "You are a red-team ADVERSARY. A candidate diff is under review before "
            "a human gate. Author a CANDIDATE FAILING TEST that targets the NEW "
            "behavior in the diff and would fail if the change is wrong, using the "
            "write_test tool. If instead the diff violates a specific acceptance "
            "criterion, call report_spec_violation with its criterion id. You may "
            "NOT edit product code.\n\n"
            f"--- CANDIDATE DIFF ---\n{diff}\n\n"
            f"--- SPEC ---\n{_spec_text(spec, criteria)}"
        ),
        allowed_actions=sorted(ROLE_TOOLS[SubAgentRole.ADVERSARY]),
        acceptance_criteria=criteria,
        model=adv_model,
        context={"role": SubAgentRole.ADVERSARY.value},
    )
    runner = AgentRunner(
        adversary_client,
        tools=registry,
        repo_root=worktree,
        max_iterations=max_iterations,
    )
    runner.run(objective)

    # (3) Execute the authored test IN-SANDBOX through the create/run seam.
    if capture.test_path and capture.test_cmd:
        spec_ = SandboxSpec(
            agent_run_id=agent_run_id or uuid.uuid4(),
            workspace_id=workspace_id or uuid.uuid4(),
            kind=SandboxKind.WORKTREE,
            host_worktree_path=str(worktree),
            exec_timeout_seconds=timeout_s,
        )
        # Preflight the substrate before touching it (no-op for worktree; loud
        # failure for kernel-boundary providers — never a silent downgrade).
        await sandbox_provider.preflight()
        session = await sandbox_provider.create(spec_)
        try:
            out = await session.run(
                capture.test_cmd,
                cwd=session.workspace_dir,
                timeout_s=timeout_s,
            )
        finally:
            await session.teardown(reason="red_team_complete")

        # (4) BLOCK only on a REAL non-zero exit — never a self-reported pass.
        if out.exit_code != 0:
            evidence = (out.stdout + out.stderr)[-_EVIDENCE_CAP:]
            return RedTeamResult(
                blocked=True,
                kind="failing_test",
                failing_test_ref=FailingTestRef(
                    path=capture.test_path,
                    test_cmd=capture.test_cmd,
                    exit_code=out.exit_code,
                ),
                evidence=evidence,
                adversary_model=adv_model,
                coder_model=coder_model,
            )

    # A structured spec-violation blocks only if it references a real criterion.
    if capture.violation_criterion_id and capture.violation_criterion_id in valid_ids:
        return RedTeamResult(
            blocked=True,
            kind="spec_violation",
            violation=SpecViolation(
                criterion_id=capture.violation_criterion_id,
                detail=capture.violation_detail,
            ),
            evidence=capture.violation_detail,
            adversary_model=adv_model,
            coder_model=coder_model,
        )

    # Nothing landed: the change survived adversarial review.
    return RedTeamResult(
        blocked=False,
        kind="survived",
        evidence="adversary produced no executed failing test or valid spec-violation",
        adversary_model=adv_model,
        coder_model=coder_model,
    )
