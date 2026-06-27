"""Spec-driven development lifecycle, manifest, and gating engine.

Public surface (plan Task 1.7):

- :class:`FileSpecEngine` — filesystem-backed implementation of the frozen
  ``forge_contracts.SpecEngine`` Protocol (constitution -> spec_create ->
  clarify -> plan -> approve -> tasks -> validate), plus manifest read/write,
  the implementation gate, and verification recording.
- manifest (de)serialization: :func:`dump_manifest` / :func:`load_manifest`.
- deterministic id helpers: :func:`spec_id_for_key`, :func:`task_id_for`.
- gating: :data:`IMPLEMENTABLE_STATUSES`, :func:`check_implementation_gate`.
- traceability: :func:`build_traceability`, :func:`build_validation_report`.
- :class:`SpecNotFoundError` for unresolved spec/task uuids.
"""

from __future__ import annotations

from forge_spec.engine import (
    DEFAULT_GUARDRAILS,
    DEFAULT_PRINCIPLES,
    FileSpecEngine,
)
from forge_spec.errors import SpecNotFoundError
from forge_spec.gates import IMPLEMENTABLE_STATUSES, check_implementation_gate
from forge_spec.ids import (
    constitution_id_for,
    slugify,
    spec_dirname,
    spec_id_for_key,
    spec_key,
    task_id_for,
    task_key,
)
from forge_spec.manifest import dump_manifest, load_manifest, manifest_to_dict
from forge_spec.tasks import generate_tasks, test_ref_for
from forge_spec.traceability import build_traceability, build_validation_report

__version__ = "0.1.0"

#: Convenience alias: the canonical engine implementation for this package.
SpecEngineService = FileSpecEngine

__all__ = [
    "DEFAULT_GUARDRAILS",
    "DEFAULT_PRINCIPLES",
    "IMPLEMENTABLE_STATUSES",
    "FileSpecEngine",
    "SpecEngineService",
    "SpecNotFoundError",
    "__version__",
    "build_traceability",
    "build_validation_report",
    "check_implementation_gate",
    "constitution_id_for",
    "dump_manifest",
    "generate_tasks",
    "load_manifest",
    "manifest_to_dict",
    "slugify",
    "spec_dirname",
    "spec_id_for_key",
    "spec_key",
    "task_id_for",
    "task_key",
    "test_ref_for",
]
