"""Authorization errors raised by the resolver invariants (F30).

These are pure domain errors (no HTTP coupling). The API layer maps them to
status codes: :class:`AccessDenied` -> 403, :class:`EscalationError` -> 403,
:class:`LastAdminError` -> 409, :class:`TeamCycleError` -> 409,
:class:`TeamDepthError` -> 409.
"""

from __future__ import annotations

from uuid import UUID

from forge_contracts.authz import Permission, Role, ScopeType

__all__ = [
    "AccessDenied",
    "AuthzError",
    "EscalationError",
    "LastAdminError",
    "TeamCycleError",
    "TeamDepthError",
]


class AuthzError(Exception):
    """Base class for all authorization errors."""


class AccessDenied(AuthzError):
    """A required permission is absent at the requested scope."""

    def __init__(
        self,
        permission: Permission,
        scope_type: ScopeType,
        scope_id: UUID | None = None,
    ) -> None:
        self.permission = permission
        self.scope_type = scope_type
        self.scope_id = scope_id
        super().__init__(
            f"permission '{permission.value}' denied at scope "
            f"'{scope_type.value}'" + (f" ({scope_id})" if scope_id else "")
        )


class EscalationError(AuthzError):
    """An actor attempted to grant a role above their own at a scope."""

    def __init__(self, granted_role: Role, actor_max_role: Role | None) -> None:
        self.granted_role = granted_role
        self.actor_max_role = actor_max_role
        actor = actor_max_role.value if actor_max_role is not None else "none"
        super().__init__(
            f"cannot grant role '{granted_role.value}' — actor's max role at "
            f"this scope is '{actor}'"
        )


class LastAdminError(AuthzError):
    """Removing/replacing this grant would leave a workspace with no admin."""

    def __init__(self) -> None:
        super().__init__("cannot remove the last workspace admin")


class TeamCycleError(AuthzError):
    """A team's ``parent_team_id`` would form a cycle."""

    def __init__(self, path: list[UUID] | None = None) -> None:
        self.path = path or []
        super().__init__("team parent assignment would create a cycle")


class TeamDepthError(AuthzError):
    """A team's nesting depth would exceed ``MAX_TEAM_DEPTH``."""

    def __init__(self, max_depth: int) -> None:
        self.max_depth = max_depth
        super().__init__(f"team nesting exceeds the maximum depth of {max_depth}")
