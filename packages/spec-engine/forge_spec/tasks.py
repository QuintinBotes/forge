"""Deterministic task generation from an approved spec manifest.

``spec_tasks`` turns an approved spec into phased implementation units. Generation
is *deterministic* given the manifest: one task per requirement, carrying the
acceptance criteria that reference that requirement (with a ``spec_ref`` back to
the spec). Determinism means a re-instantiated engine reproduces identical task
ids from disk, which is what lets :func:`forge_spec.engine.FileSpecEngine.validate`
locate a task by uuid without a sidecar index.
"""

from __future__ import annotations

from forge_contracts import (
    AcceptanceCriterion,
    SpecManifest,
    TaskDTO,
    TaskKind,
    TaskStatus,
)
from forge_spec.ids import slugify, spec_id_for_key, task_id_for, task_key


def _acceptance_for(manifest: SpecManifest, requirement_id: str) -> list[AcceptanceCriterion]:
    """Return the spec's acceptance criteria that reference ``requirement_id``."""
    return [a for a in manifest.acceptance_criteria if requirement_id in a.req_refs]


def test_ref_for(manifest: SpecManifest, acceptance_id: str) -> str:
    """Synthesise the canonical test reference for an acceptance criterion.

    Mirrors the requirement-to-test mapping the validate phase records; the agent
    runtime writes the real test at this path during implementation.
    """
    return f"tests/test_{slugify(manifest.id)}.py::test_{acceptance_id.lower()}"


def generate_tasks(manifest: SpecManifest) -> list[TaskDTO]:
    """Return the deterministic task list for an (approved) ``manifest``."""
    spec_uuid = spec_id_for_key(manifest.id)
    tasks: list[TaskDTO] = []
    for ordinal, requirement in enumerate(manifest.requirements, start=1):
        key = task_key(manifest.id, ordinal)
        criteria = [
            AcceptanceCriterion(
                id=a.id,
                text=a.text,
                req_refs=list(a.req_refs),
                spec_ref=f"{manifest.id}/{a.id}",
            )
            for a in _acceptance_for(manifest, requirement.id)
        ]
        tasks.append(
            TaskDTO(
                id=task_id_for(manifest.id, key),
                key=key,
                spec_id=spec_uuid,
                kind=TaskKind.FEATURE,
                title=requirement.text,
                description=f"Implement {requirement.id}: {requirement.text}",
                status=TaskStatus.READY_FOR_AGENT,
                execution_mode=manifest.execution_mode,
                skill_profile=manifest.skill_profile,
                acceptance_criteria=criteria,
            )
        )
    return tasks


__all__ = ["generate_tasks", "test_ref_for"]
