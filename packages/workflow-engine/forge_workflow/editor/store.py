"""Definition resolution: DB-published definitions override bundled files (F28).

The :class:`ResolvingDefinitionProvider` is injected into the engine so a new run
resolves the workspace's published definition (DB) over the bundled YAML, and an
in-flight run loads its *pinned* revision so a re-publish never mutates it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol
from uuid import UUID

from forge_contracts import WorkflowDefinition
from forge_workflow.dsl import parse_definition
from forge_workflow.editor.errors import UnknownDefinitionError

#: A bundled loader maps a definition name to its parsed definition (or None).
BundledLoader = Callable[[str], WorkflowDefinition | None]


def default_bundled_loader(name: str) -> WorkflowDefinition | None:
    """Resolve a bundled definition by name (``default_feature`` / ``incident``)."""
    from forge_workflow.default_workflow import (
        DEFAULT_WORKFLOW_NAME,
        default_feature_definition,
    )

    if name == DEFAULT_WORKFLOW_NAME:
        return default_feature_definition()
    try:
        from forge_workflow.incident.definition import (
            INCIDENT_DEFINITION_NAME,
            default_incident_definition,
        )

        if name == INCIDENT_DEFINITION_NAME:
            return default_incident_definition()
    except Exception:  # pragma: no cover - incident is a soft dependency
        pass
    return None


class WorkflowDefinitionStore(Protocol):
    """The DB side of resolution."""

    def resolve_published(
        self, name: str, *, workspace_id: UUID
    ) -> tuple[WorkflowDefinition, UUID, str] | None:
        """Return ``(definition, revision_id, dsl_version)`` for an ACTIVE
        workspace definition with a published revision, else ``None``."""
        ...

    def load_revision(self, revision_id: UUID) -> WorkflowDefinition | None:
        """Parse the persisted DSL of a specific revision (for run pinning)."""
        ...


class ResolvingDefinitionProvider:
    """Resolves a definition name to a concrete definition, DB over bundled."""

    def __init__(
        self,
        store: WorkflowDefinitionStore | None = None,
        bundled_loader: BundledLoader = default_bundled_loader,
    ) -> None:
        self._store = store
        self._bundled_loader = bundled_loader

    def resolve(
        self, name: str, *, workspace_id: UUID | None = None
    ) -> tuple[WorkflowDefinition, UUID | None, str]:
        """Return ``(definition, revision_id | None, dsl_version)``.

        Tries the DB store first (when a workspace is given); falls back to the
        bundled loader with ``revision_id=None``. Raises
        :class:`UnknownDefinitionError` if neither exists.
        """
        if self._store is not None and workspace_id is not None:
            resolved = self._store.resolve_published(name, workspace_id=workspace_id)
            if resolved is not None:
                return resolved

        bundled = self._bundled_loader(name)
        if bundled is not None:
            return bundled, None, bundled.version

        raise UnknownDefinitionError(name)

    def load_pinned(self, revision_id: UUID) -> WorkflowDefinition:
        """Load and parse the exact persisted revision (no drift to latest)."""
        if self._store is None:
            raise UnknownDefinitionError(str(revision_id))
        loaded = self._store.load_revision(revision_id)
        if loaded is None:
            raise UnknownDefinitionError(str(revision_id))
        return loaded


class DbWorkflowDefinitionStore:
    """A :class:`WorkflowDefinitionStore` backed by the editor repository."""

    def __init__(self, repository: object) -> None:
        # Typed as object to avoid a hard import cycle with repository.py.
        self._repo = repository

    def resolve_published(
        self, name: str, *, workspace_id: UUID
    ) -> tuple[WorkflowDefinition, UUID, str] | None:
        definition = self._repo.get(workspace_id, name)  # type: ignore[attr-defined]
        if definition is None or not definition.is_active:
            return None
        if definition.current_published_revision_id is None:
            return None
        revision = self._repo.get_revision_by_id(  # type: ignore[attr-defined]
            definition.current_published_revision_id
        )
        if revision is None:
            return None
        return (
            parse_definition(revision.dsl_yaml),
            revision.id,
            revision.dsl_version,
        )

    def load_revision(self, revision_id: UUID) -> WorkflowDefinition | None:
        revision = self._repo.get_revision_by_id(revision_id)  # type: ignore[attr-defined]
        if revision is None:
            return None
        return parse_definition(revision.dsl_yaml)


__all__ = [
    "BundledLoader",
    "DbWorkflowDefinitionStore",
    "ResolvingDefinitionProvider",
    "WorkflowDefinitionStore",
    "default_bundled_loader",
]
