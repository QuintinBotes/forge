"""``manifest.yaml`` (de)serialization for the spec engine.

The manifest is the machine-readable spec artifact (FORGE_SPEC: Spec Manifest
Schema). Dump/load go through Pydantic so the on-disk YAML and the
``SpecManifest`` DTO stay in lockstep: ``model_dump(mode="json")`` renders
``StrEnum`` fields to their wire value, and ``safe_dump`` keeps declaration order
and emits plain YAML scalars (never ``!!python`` tags).
"""

from __future__ import annotations

from typing import Any

import yaml

from forge_contracts import SpecManifest

#: Canonical artifact filenames (FORGE_SPEC: Spec Folder Layout).
MANIFEST_FILENAME = "manifest.yaml"
SPEC_FILENAME = "spec.md"
CLARIFY_FILENAME = "clarify.md"
PLAN_FILENAME = "plan.md"
TASKS_FILENAME = "tasks.md"
TASKS_DATA_FILENAME = "tasks.yaml"
VALIDATION_FILENAME = "validation.md"
DECISIONS_FILENAME = "decisions.md"
CONSTITUTION_FILENAME = "constitution.md"
VERIFICATION_FILENAME = "verification.yaml"


def manifest_to_dict(manifest: SpecManifest) -> dict[str, Any]:
    """Return the JSON-mode dict for a manifest (enum -> wire value)."""
    return manifest.model_dump(mode="json")


def dump_manifest(manifest: SpecManifest) -> str:
    """Serialise a :class:`SpecManifest` to deterministic YAML text."""
    return yaml.safe_dump(
        manifest_to_dict(manifest),
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    )


def load_manifest(text: str) -> SpecManifest:
    """Parse YAML manifest ``text`` back into a :class:`SpecManifest`."""
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise ValueError("manifest YAML must deserialise to a mapping")
    return SpecManifest.model_validate(data)


__all__ = [
    "CLARIFY_FILENAME",
    "CONSTITUTION_FILENAME",
    "DECISIONS_FILENAME",
    "MANIFEST_FILENAME",
    "PLAN_FILENAME",
    "SPEC_FILENAME",
    "TASKS_DATA_FILENAME",
    "TASKS_FILENAME",
    "VALIDATION_FILENAME",
    "VERIFICATION_FILENAME",
    "dump_manifest",
    "load_manifest",
    "manifest_to_dict",
]
