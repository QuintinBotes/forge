"""Editor-domain errors (F28 workflow visual editor).

All inherit from :class:`~forge_workflow.exceptions.WorkflowError` so callers can
catch the shared workflow base; the API layer maps them to HTTP status codes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from forge_workflow.exceptions import WorkflowError

if TYPE_CHECKING:
    from forge_workflow.editor.validation import ValidationIssue


class DefinitionNotFoundError(WorkflowError):
    """Raised when a named definition does not exist in the workspace (-> 404)."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"workflow definition not found: {name!r}")


class DefinitionNameConflictError(WorkflowError):
    """Raised when creating/forking a name that already has a DB row (-> 409)."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"workflow definition already exists: {name!r}")


class BundledReadOnlyError(WorkflowError):
    """Raised when editing a bundled (unforked) definition (-> 409)."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"bundled definition {name!r} is read-only; fork it to customize")


class RevisionNotFoundError(WorkflowError):
    """Raised when a revision number/id does not exist for a definition (-> 404)."""

    def __init__(self, name: str, revision: object) -> None:
        self.name = name
        self.revision = revision
        super().__init__(f"revision {revision!r} not found for definition {name!r}")


class PublishBlockedError(WorkflowError):
    """Raised when a draft cannot be published because of ERROR issues (-> 409)."""

    def __init__(self, errors: list[ValidationIssue]) -> None:
        self.errors = errors
        super().__init__(f"publish blocked: {len(errors)} validation error(s) remain")


class UnknownDefinitionError(WorkflowError):
    """Raised by the resolver when neither a DB nor a bundled definition exists."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"no workflow definition named {name!r}")


__all__ = [
    "BundledReadOnlyError",
    "DefinitionNameConflictError",
    "DefinitionNotFoundError",
    "PublishBlockedError",
    "RevisionNotFoundError",
    "UnknownDefinitionError",
]
