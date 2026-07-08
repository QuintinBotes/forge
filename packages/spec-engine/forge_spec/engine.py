"""Filesystem-backed implementation of the frozen ``SpecEngine`` Protocol.

:class:`FileSpecEngine` materialises the SDD lifecycle on disk under a ``root``
directory, one folder per spec (FORGE_SPEC: Spec Folder Layout). It is
*stateless*: spec/task uuids are derived deterministically from their keys
(:mod:`forge_spec.ids`), so any engine instance over the same root resolves the
same specs and tasks — no in-process registry, no sidecar index.

Lifecycle: ``constitution_init`` -> ``spec_create`` (draft) -> ``spec_clarify``
-> ``spec_plan`` -> ``approve_spec`` (the human gate) -> ``spec_tasks`` ->
``validate``. Task generation and implementation are gated on an approved spec
(:mod:`forge_spec.gates`).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

from forge_contracts import (
    AcceptanceCriterion,
    CheckResult,
    Constitution,
    OpenQuestion,
    Requirement,
    SpecManifest,
    SpecStatus,
    TaskDTO,
    ValidationReport,
)
from forge_contracts.dtos import ADR
from forge_spec import manifest as manifest_io
from forge_spec.errors import SpecNotFoundError
from forge_spec.gates import check_implementation_gate
from forge_spec.ids import (
    constitution_id_for,
    spec_dirname,
    spec_id_for_key,
    spec_key,
    spec_number,
)
from forge_spec.tasks import generate_tasks
from forge_spec.templates import (
    render_clarify_md,
    render_constitution_md,
    render_decisions_md,
    render_plan_md,
    render_spec_md,
    render_tasks_md,
    render_validation_md,
)
from forge_spec.traceability import build_validation_report

#: Constitution principles seeded when none are supplied (FORGE_SPEC principles).
DEFAULT_PRINCIPLES: tuple[str, ...] = (
    "Spec-driven development is native for all feature-class work.",
    "Tests precede implementation; quality is enforced structurally, not by prompt.",
    "Every run is repo-aware and policy-aware before it starts.",
    "Human approval gates spec, plan, PR, and deploy.",
    "Retrieval is always hybrid and source-attributed.",
)

#: Architecture guardrails seeded into a new constitution.
DEFAULT_GUARDRAILS: tuple[str, ...] = (
    "Follow existing repository conventions and entrypoints.",
    "No breaking changes without an ADR and an approved migration path.",
    "Secrets are never written to code, logs, or specs.",
)


class FileSpecEngine:
    """SDD lifecycle + manifest read/write over a filesystem ``root``."""

    def __init__(self, root: str | Path = "specs") -> None:
        self.root = Path(root)

    # ----------------------------------------------------------------- #
    # Constitution                                                       #
    # ----------------------------------------------------------------- #

    def constitution_init(
        self, project_id: uuid.UUID, principles: list[str] | None = None
    ) -> Constitution:
        """Initialise a project constitution and persist ``constitution.md``."""
        resolved = list(principles) if principles else list(DEFAULT_PRINCIPLES)
        constitution = Constitution(
            id=constitution_id_for(project_id),
            project_id=project_id,
            principles=resolved,
            architecture_guardrails=list(DEFAULT_GUARDRAILS),
        )
        content = render_constitution_md(constitution)
        constitution.content = content
        self.root.mkdir(parents=True, exist_ok=True)
        self._write(self.root / manifest_io.CONSTITUTION_FILENAME, content)
        self._write(
            self.root / manifest_io.CONSTITUTION_DATA_FILENAME,
            yaml.safe_dump(
                constitution.model_dump(mode="json"), sort_keys=False, allow_unicode=True
            ),
        )
        return constitution

    def read_constitution(self, project_id: uuid.UUID) -> Constitution | None:
        """Read the project's constitution, or ``None`` if never initialised.

        Constitutions are stored as a single ``constitution.yaml`` sidecar at the
        engine root (one per workspace, mirroring ``constitution_init``'s
        markdown write), so a constitution initialised for a *different*
        project reads back as ``None`` rather than ambiguously standing in for
        the requested project's constitution.
        """
        path = self.root / manifest_io.CONSTITUTION_DATA_FILENAME
        if not path.exists():
            return None
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        constitution = Constitution.model_validate(data)
        if constitution.project_id != project_id:
            return None
        return constitution

    # ----------------------------------------------------------------- #
    # Spec lifecycle                                                     #
    # ----------------------------------------------------------------- #

    def spec_create(
        self, epic_id: uuid.UUID, name: str, requirements: list[Requirement] | None = None
    ) -> SpecManifest:
        """Create a draft spec for an epic and write its initial artifacts."""
        reqs = list(requirements) if requirements else []
        key = spec_key(self._next_spec_number())
        acceptance = [
            self._default_acceptance(index, requirement)
            for index, requirement in enumerate(reqs, start=1)
        ]
        manifest = SpecManifest(
            id=key,
            name=name,
            status=SpecStatus.DRAFT,
            requirements=reqs,
            acceptance_criteria=acceptance,
        )
        spec_dir = self.root / spec_dirname(key, name)
        spec_dir.mkdir(parents=True, exist_ok=True)
        self._persist(manifest, spec_dir)
        self._write(spec_dir / manifest_io.SPEC_FILENAME, render_spec_md(manifest))
        return manifest

    def spec_clarify(self, spec_id: uuid.UUID) -> SpecManifest:
        """Run the clarification pass: surface + resolve open questions."""
        spec_dir, manifest = self._resolve(spec_id)
        if not manifest.open_questions:
            manifest.open_questions = [
                OpenQuestion(
                    id="Q1",
                    text=(
                        "Are there non-functional constraints (latency, throughput, "
                        "compatibility) the requirements must respect?"
                    ),
                    resolution=("None blocking identified; constraints captured in the manifest."),
                )
            ]
        manifest.status = SpecStatus.CLARIFYING
        self._persist(manifest, spec_dir)
        self._write(spec_dir / manifest_io.CLARIFY_FILENAME, render_clarify_md(manifest))
        return manifest

    def spec_plan(self, spec_id: uuid.UUID) -> SpecManifest:
        """Generate the technical plan + ADRs and link ``plan.md``."""
        spec_dir, manifest = self._resolve(spec_id)
        if not manifest.decisions:
            manifest.decisions = [
                ADR(
                    id="ADR-1",
                    title=f"Architecture for {manifest.name}",
                    status="accepted",
                    context="Derived from the approved requirements and constitution.",
                    decision="Follow existing repository conventions and guardrails.",
                    consequences="Consistent, reviewable implementation aligned to the spec.",
                )
            ]
        manifest.plan_ref = manifest_io.PLAN_FILENAME
        self._persist(manifest, spec_dir)
        self._write(spec_dir / manifest_io.PLAN_FILENAME, render_plan_md(manifest))
        self._write(spec_dir / manifest_io.DECISIONS_FILENAME, render_decisions_md(manifest))
        return manifest

    def approve_spec(self, spec_id: uuid.UUID) -> SpecManifest:
        """Approve a spec (the human gate); moves it to ``approved``."""
        spec_dir, manifest = self._resolve(spec_id)
        manifest.status = SpecStatus.APPROVED
        self._persist(manifest, spec_dir)
        return manifest

    def spec_tasks(self, spec_id: uuid.UUID) -> list[TaskDTO]:
        """Generate implementation tasks from an *approved* spec (gated)."""
        spec_dir, manifest = self._resolve(spec_id)
        check_implementation_gate(manifest)
        tasks = generate_tasks(manifest)
        manifest.tasks_ref = manifest_io.TASKS_FILENAME
        self._persist(manifest, spec_dir)
        self._write(spec_dir / manifest_io.TASKS_FILENAME, render_tasks_md(manifest, tasks))
        self._write(
            spec_dir / manifest_io.TASKS_DATA_FILENAME,
            yaml.safe_dump(
                [t.model_dump(mode="json") for t in tasks],
                sort_keys=False,
                allow_unicode=True,
            ),
        )
        return tasks

    def validate(self, task_id: uuid.UUID) -> ValidationReport:
        """Validate a task against its spec: requirement-to-test traceability."""
        spec_dir, manifest, task, tasks = self._locate_task(task_id)
        checks, coverage = self._read_verification(spec_dir, task.key or str(task.id))
        report = build_validation_report(
            manifest=manifest,
            task=task,
            tasks=tasks,
            checks=checks,
            coverage=coverage,
        )
        manifest.validation_ref = manifest_io.VALIDATION_FILENAME
        if report.passed and manifest.status in {SpecStatus.APPROVED, SpecStatus.IMPLEMENTING}:
            manifest.status = SpecStatus.VALIDATED
        self._persist(manifest, spec_dir)
        self._write(spec_dir / manifest_io.VALIDATION_FILENAME, render_validation_md(report))
        return report

    # ----------------------------------------------------------------- #
    # Manifest read/write                                                #
    # ----------------------------------------------------------------- #

    def latest_validation(self, spec_id: uuid.UUID) -> ValidationReport | None:
        """Return the spec's latest validation report, or ``None`` if never validated.

        Non-mutating: recomputes traceability from the current manifest and its
        (deterministic) generated tasks -- the same inputs :meth:`validate`
        persists -- folding in the most recently *recorded* verification (if
        any), without writing state or advancing the spec's status. Backs
        read-only projections (the F23 spec-validation dashboard) that must not
        trigger lifecycle side effects merely by being viewed.
        """
        spec_dir, manifest = self._resolve(spec_id)
        if manifest.validation_ref is None:
            return None
        tasks = generate_tasks(manifest)
        if not tasks:
            return None
        verification = self._verification_data(spec_dir)
        task = tasks[-1]
        checks: list[CheckResult] | None = None
        coverage: float | None = None
        for candidate in reversed(tasks):
            entry = verification.get(candidate.key or str(candidate.id))
            if entry:
                task = candidate
                checks = [CheckResult.model_validate(c) for c in entry.get("checks", [])]
                coverage = entry.get("coverage")
                break
        return build_validation_report(
            manifest=manifest, task=task, tasks=tasks, checks=checks, coverage=coverage
        )

    def read_manifest(self, spec_id: uuid.UUID) -> SpecManifest:
        """Read a spec manifest by its (deterministic) uuid."""
        _, manifest = self._resolve(spec_id)
        return manifest

    def write_manifest(self, manifest: SpecManifest) -> SpecManifest:
        """Persist (create or update) a spec manifest, returning it."""
        spec_dir = self._resolve_dir_optional(spec_id_for_key(manifest.id))
        if spec_dir is None:
            spec_dir = self.root / spec_dirname(manifest.id, manifest.name)
            spec_dir.mkdir(parents=True, exist_ok=True)
        self._persist(manifest, spec_dir)
        return manifest

    # ----------------------------------------------------------------- #
    # Gates / verification (extra public surface)                        #
    # ----------------------------------------------------------------- #

    def ensure_implementable(self, spec_id: uuid.UUID) -> SpecManifest:
        """Return the manifest if implementable; else raise ``SpecGateError``."""
        _, manifest = self._resolve(spec_id)
        return check_implementation_gate(manifest)

    def record_verification(
        self,
        task_id: uuid.UUID,
        *,
        checks: list[dict[str, Any]] | list[CheckResult],
        coverage: float | None = None,
    ) -> None:
        """Record verification results for a task (consumed by :meth:`validate`)."""
        spec_dir, _manifest, task, _tasks = self._locate_task(task_id)
        normalised = [
            c if isinstance(c, CheckResult) else CheckResult.model_validate(c) for c in checks
        ]
        path = spec_dir / manifest_io.VERIFICATION_FILENAME
        data: dict[str, Any] = {}
        if path.exists():
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        data[task.key or str(task.id)] = {
            "checks": [c.model_dump(mode="json") for c in normalised],
            "coverage": coverage,
        }
        self._write(path, yaml.safe_dump(data, sort_keys=False, allow_unicode=True))

    def spec_path(self, spec_id: uuid.UUID) -> Path:
        """Return the on-disk directory for a spec (raises if unknown)."""
        spec_dir, _ = self._resolve(spec_id)
        return spec_dir

    # ----------------------------------------------------------------- #
    # Internals                                                          #
    # ----------------------------------------------------------------- #

    @staticmethod
    def _default_acceptance(index: int, requirement: Requirement) -> AcceptanceCriterion:
        return AcceptanceCriterion(
            id=f"A{index}",
            req_refs=[requirement.id],
            text=f"Implementation satisfies {requirement.id}: {requirement.text}",
        )

    def _iter_spec_dirs(self) -> Iterator[Path]:
        if not self.root.exists():
            return
        for entry in sorted(self.root.iterdir()):
            if entry.is_dir() and (entry / manifest_io.MANIFEST_FILENAME).exists():
                yield entry

    def _load_dir_manifest(self, spec_dir: Path) -> SpecManifest:
        text = (spec_dir / manifest_io.MANIFEST_FILENAME).read_text(encoding="utf-8")
        return manifest_io.load_manifest(text)

    def _resolve(self, spec_id: uuid.UUID) -> tuple[Path, SpecManifest]:
        result = self._resolve_optional(spec_id)
        if result is None:
            raise SpecNotFoundError(f"no spec resolves to id {spec_id}")
        return result

    def _resolve_optional(self, spec_id: uuid.UUID) -> tuple[Path, SpecManifest] | None:
        for spec_dir in self._iter_spec_dirs():
            manifest = self._load_dir_manifest(spec_dir)
            if spec_id_for_key(manifest.id) == spec_id:
                return spec_dir, manifest
        return None

    def _resolve_dir_optional(self, spec_id: uuid.UUID) -> Path | None:
        resolved = self._resolve_optional(spec_id)
        return resolved[0] if resolved else None

    def _locate_task(self, task_id: uuid.UUID) -> tuple[Path, SpecManifest, TaskDTO, list[TaskDTO]]:
        for spec_dir in self._iter_spec_dirs():
            manifest = self._load_dir_manifest(spec_dir)
            tasks = generate_tasks(manifest)
            for task in tasks:
                if task.id == task_id:
                    return spec_dir, manifest, task, tasks
        raise SpecNotFoundError(f"no task resolves to id {task_id}")

    def _verification_data(self, spec_dir: Path) -> dict[str, Any]:
        path = spec_dir / manifest_io.VERIFICATION_FILENAME
        if not path.exists():
            return {}
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    def _read_verification(
        self, spec_dir: Path, task_key: str
    ) -> tuple[list[CheckResult] | None, float | None]:
        entry = self._verification_data(spec_dir).get(task_key)
        if not entry:
            return None, None
        checks = [CheckResult.model_validate(c) for c in entry.get("checks", [])]
        return checks, entry.get("coverage")

    def _next_spec_number(self) -> int:
        highest = 0
        for spec_dir in self._iter_spec_dirs():
            number = spec_number(self._load_dir_manifest(spec_dir).id)
            if number is not None:
                highest = max(highest, number)
        return highest + 1

    def _persist(self, manifest: SpecManifest, spec_dir: Path) -> None:
        self._write(spec_dir / manifest_io.MANIFEST_FILENAME, manifest_io.dump_manifest(manifest))

    @staticmethod
    def _write(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


__all__ = ["DEFAULT_GUARDRAILS", "DEFAULT_PRINCIPLES", "FileSpecEngine"]
