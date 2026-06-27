"""Deterministic identifier helpers for the spec engine.

Specs and tasks expose two identifiers: a human-facing ``key`` (e.g. ``SPEC-17``,
``SPEC-17-T1``) stored in artifacts, and a ``uuid.UUID`` used by the frozen
``SpecEngine`` Protocol (``forge_contracts``). The uuid is derived
*deterministically* from the key with :func:`uuid.uuid5`, so a re-instantiated,
filesystem-backed engine resolves the same spec/task from disk without any
in-process or sidecar index.
"""

from __future__ import annotations

import re
import uuid

#: Stable namespaces for uuid5 derivation. Changing these reshapes every id, so
#: they are frozen alongside the rest of the contract surface.
SPEC_NAMESPACE: uuid.UUID = uuid.uuid5(uuid.NAMESPACE_URL, "https://forge.dev/spec")
TASK_NAMESPACE: uuid.UUID = uuid.uuid5(uuid.NAMESPACE_URL, "https://forge.dev/task")
CONSTITUTION_NAMESPACE: uuid.UUID = uuid.uuid5(uuid.NAMESPACE_URL, "https://forge.dev/constitution")

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")
_SPEC_KEY = re.compile(r"^SPEC-(\d+)")


def slugify(text: str, *, max_len: int = 60) -> str:
    """Return a filesystem-safe, lower-kebab slug for ``text``."""
    slug = _SLUG_STRIP.sub("-", text.strip().lower()).strip("-")
    slug = slug[:max_len].strip("-")
    return slug or "spec"


def spec_id_for_key(key: str) -> uuid.UUID:
    """Return the deterministic uuid for a spec ``key`` (e.g. ``SPEC-17``)."""
    return uuid.uuid5(SPEC_NAMESPACE, key)


def task_id_for(spec_key: str, task_key: str) -> uuid.UUID:
    """Return the deterministic uuid for a task within a spec."""
    return uuid.uuid5(TASK_NAMESPACE, f"{spec_key}:{task_key}")


def constitution_id_for(project_id: uuid.UUID) -> uuid.UUID:
    """Return the deterministic uuid for a project's constitution."""
    return uuid.uuid5(CONSTITUTION_NAMESPACE, str(project_id))


def spec_key(number: int) -> str:
    """Return the canonical spec key for an ordinal (1 -> ``SPEC-1``)."""
    return f"SPEC-{number}"


def task_key(spec_key_value: str, ordinal: int) -> str:
    """Return the canonical task key within a spec (``SPEC-1`` + 1 -> ``SPEC-1-T1``)."""
    return f"{spec_key_value}-T{ordinal}"


def spec_number(key: str) -> int | None:
    """Extract the ordinal from a spec key, or ``None`` if it does not match."""
    match = _SPEC_KEY.match(key)
    return int(match.group(1)) if match else None


def spec_dirname(key: str, name: str) -> str:
    """Return the on-disk directory name for a spec: ``<KEY>-<slug>``."""
    return f"{key}-{slugify(name)}"


__all__ = [
    "CONSTITUTION_NAMESPACE",
    "SPEC_NAMESPACE",
    "TASK_NAMESPACE",
    "constitution_id_for",
    "slugify",
    "spec_dirname",
    "spec_id_for_key",
    "spec_key",
    "spec_number",
    "task_id_for",
    "task_key",
]
