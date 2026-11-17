"""forge-factory-bench harness: run one solver over a whole suite and report (#197).

A case ships an issue (``query``), a repository tree and held-out tests (its
``metadata``, see :class:`~scf_eval.benchmark.swe_case.SweCaseFields`). For each case
the harness writes the tree to a fresh directory and scores it with
:func:`~scf_eval.sweval.runner.run_swe_case`, which hands the solver a case with the
held-out tests stripped and runs them in the given sandbox. A case ends resolved, not
resolved, or an infrastructure error (it could not be scored).

With ``results_path`` each finished case is appended to a JSON-lines file and a rerun
skips the cases stored there, so an interrupted run resumes. A stored infrastructure
error is run again; remove a case's line to rerun any other. The file is tied to one
run's provenance (suite hash, SCF commit, sandbox kind, solver), and a file from
another run is refused rather than reported under this run's name.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

import yaml
from pydantic import BaseModel, Field, TypeAdapter

from scf_contracts.sandbox import SandboxProvider
from scf_eval.benchmark.manifest import compute_content_hash, load_manifest
from scf_eval.benchmark.swe_case import CaseFile, parse_swe_case_fields
from scf_eval.golden import GoldenCase
from scf_eval.sweval.runner import SolveFn, _apply_patch, run_swe_case

#: Known-good patches, beside the manifest; only the reference solver reads it.
REFERENCE_FILENAME = "reference.yaml"


class CaseOutcome(StrEnum):
    """How one case ended."""

    RESOLVED = "resolved"
    NOT_RESOLVED = "not_resolved"
    INFRASTRUCTURE_ERROR = "infrastructure_error"


class BenchCaseResult(BaseModel):
    """One case of a bench run."""

    case_id: str
    outcome: CaseOutcome
    #: Pass-to-pass tests the change broke.
    regressed: list[str] = Field(default_factory=list)
    #: Why the case could not be scored, or why its change was refused unscored.
    reason: str | None = None
    cost_usd: float | None = None
    wall_time_s: float = 0.0


class BenchRunReport(BaseModel):
    """What a bench run states (FR-016); serialises to JSON and to Markdown."""

    suite_slug: str
    suite_version: str
    suite_content_hash: str
    scf_commit: str | None
    solver: str
    models: list[str]
    sandbox_tier: str
    cases: list[BenchCaseResult]
    resolve_rate: float
    total_cost_usd: float | None
    #: Summed over the cases, so a resumed run counts each case once.
    wall_time_s: float

    def to_markdown(self) -> str:
        resolved = sum(c.outcome == CaseOutcome.RESOLVED for c in self.cases)
        cost = "n/a" if self.total_cost_usd is None else f"{self.total_cost_usd:.4f}"
        lines = [
            f"# {self.suite_slug} {self.suite_version}",
            "",
            f"- Suite content hash: `{self.suite_content_hash}`",
            f"- SCF commit: `{self.scf_commit or 'unknown'}`",
            f"- Solver: {self.solver}",
            f"- Models: {', '.join(self.models) or 'n/a'}",
            f"- Sandbox tier: {self.sandbox_tier}",
            f"- Resolved: {resolved}/{len(self.cases)} ({self.resolve_rate:.1%})",
            f"- Total cost (USD): {cost}",
            f"- Wall time: {self.wall_time_s:.1f} s",
            "",
            "| Case | Outcome | Regressed tests | Reason |",
            "| --- | --- | --- | --- |",
        ]
        for c in self.cases:
            reason = c.reason.splitlines()[0][:160].replace("|", "\\|") if c.reason else ""
            lines.append(
                f"| {c.case_id} | {c.outcome.value} | {', '.join(c.regressed)} | {reason} |"
            )
        return "\n".join(lines) + "\n"


class Solver(Protocol):
    """Any solver the harness runs (FR-015): a public case in, file writes out."""

    name: str
    #: What else tells two solvers of one name apart (an instance, a patch set);
    #: results stored under another key are never reused.
    key: str
    #: Model identifiers the solver ran on, for the report; empty when not given.
    models: tuple[str, ...]
    #: What solving the last case cost, when the solver knows; reset before each case.
    last_cost_usd: float | None

    def __call__(self, case: GoldenCase) -> Mapping[str, str]: ...


@dataclass
class FnSolver:
    """A plain solve function as a :class:`Solver`."""

    name: str
    fn: SolveFn
    key: str = ""
    models: tuple[str, ...] = ()
    last_cost_usd: float | None = None

    def __call__(self, case: GoldenCase) -> Mapping[str, str]:
        return self.fn(case)


def reference_solver(suite_dir: str | Path) -> FnSolver:
    """Apply each case's known-good patch: every case should resolve."""
    raw = (Path(suite_dir) / REFERENCE_FILENAME).read_bytes()
    patches = TypeAdapter(dict[str, list[CaseFile]]).validate_python(yaml.safe_load(raw))
    return FnSolver(
        name="reference",
        key="sha256:" + hashlib.sha256(raw).hexdigest(),
        fn=lambda case: {f.path: f.content for f in patches[case.id]},
    )


def empty_solver() -> FnSolver:
    """Change nothing: no case should resolve."""
    return FnSolver(name="empty", fn=lambda _case: {})


async def run_suite(
    suite_dir: str | Path,
    solver: Solver,
    *,
    sandbox_provider: SandboxProvider,
    results_path: Path | None = None,
    scf_commit: str | None = None,
) -> BenchRunReport:
    """Run ``solver`` over every case of the suite at ``suite_dir``."""
    manifest, cases = load_manifest(suite_dir)
    content_hash = compute_content_hash(cases, manifest.scoring)
    run_key = {
        "suite_content_hash": content_hash,
        "scf_commit": scf_commit,
        "sandbox": str(sandbox_provider.kind),
        "solver": solver.name,
        "solver_key": solver.key,
        "models": list(solver.models),
    }
    stored = _stored_results(results_path, run_key)
    results: list[BenchCaseResult] = []
    for case in cases:
        result = stored.get(case.id)
        if result is None or result.outcome == CaseOutcome.INFRASTRUCTURE_ERROR:
            result = await _run_case(case, solver, sandbox_provider)
            if results_path is not None:
                row = {"run": run_key, "result": result.model_dump(mode="json")}
                with results_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row) + "\n")
        results.append(result)
    resolved = sum(r.outcome == CaseOutcome.RESOLVED for r in results)
    costs = [r.cost_usd for r in results if r.cost_usd is not None]
    return BenchRunReport(
        suite_slug=manifest.slug,
        suite_version=manifest.version,
        suite_content_hash=content_hash,
        scf_commit=scf_commit,
        solver=solver.name,
        models=list(solver.models),
        sandbox_tier=str(sandbox_provider.kind),
        cases=results,
        resolve_rate=resolved / len(results) if results else 0.0,
        total_cost_usd=sum(costs) if costs else None,
        wall_time_s=sum(r.wall_time_s for r in results),
    )


async def _run_case(
    case: GoldenCase, solver: Solver, sandbox_provider: SandboxProvider
) -> BenchCaseResult:
    solver.last_cost_usd = None
    started = time.monotonic()
    regressed: list[str] = []
    reason: str | None = None
    with tempfile.TemporaryDirectory(prefix="scf-bench-") as worktree:
        try:
            repo = parse_swe_case_fields(case).repo
            _apply_patch(Path(worktree), {f.path: f.content for f in repo})
            scored = await run_swe_case(
                case=case,
                solve_fn=solver,
                sandbox_provider=sandbox_provider,
                worktree_path=worktree,
            )
        except Exception as exc:  # a crashed solver, sandbox or checkout: nothing to score
            outcome, reason = CaseOutcome.INFRASTRUCTURE_ERROR, f"{type(exc).__name__}: {exc}"
        else:
            regressed = scored.regressed
            if scored.infra_error:
                outcome, reason = CaseOutcome.INFRASTRUCTURE_ERROR, scored.evidence
            elif scored.refused:
                outcome, reason = CaseOutcome.NOT_RESOLVED, scored.evidence
            elif scored.resolved:
                outcome = CaseOutcome.RESOLVED
            else:
                outcome = CaseOutcome.NOT_RESOLVED
    return BenchCaseResult(
        case_id=case.id,
        outcome=outcome,
        regressed=regressed,
        reason=reason,
        cost_usd=solver.last_cost_usd,
        wall_time_s=time.monotonic() - started,
    )


def _stored_results(path: Path | None, run_key: dict[str, Any]) -> dict[str, BenchCaseResult]:
    """Results an earlier, interrupted run with the same provenance stored."""
    if path is None or not path.is_file():
        return {}
    stored: dict[str, BenchCaseResult] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue  # cut short when the run was killed: that case runs again
        if row.get("run") != run_key:
            raise ValueError(
                f"{path} holds results of another run (suite, SCF commit, sandbox or solver): "
                f"{row.get('run')}; use another file"
            )
        result = BenchCaseResult.model_validate(row["result"])
        stored[result.case_id] = result
    return stored


__all__ = [
    "REFERENCE_FILENAME",
    "BenchCaseResult",
    "BenchRunReport",
    "CaseOutcome",
    "FnSolver",
    "Solver",
    "empty_solver",
    "reference_solver",
    "run_suite",
]
