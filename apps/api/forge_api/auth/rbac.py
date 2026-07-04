"""Role-based access control (Task 1.15 — auth & secrets).

Spec Security: "RBAC — admin, member, viewer, agent-runner roles per workspace".
Maps the frozen :class:`~forge_contracts.enums.UserRole` to a fixed permission
set and exposes pure helpers (:func:`can`, :func:`ensure`) plus the matrix
itself, so both the API dependency layer and other services evaluate access the
same way.
"""

from __future__ import annotations

import enum

from forge_contracts.enums import UserRole


class Permission(enum.StrEnum):
    """Coarse-grained capabilities checked at the API boundary."""

    READ = "read"
    WRITE = "write"
    RUN_AGENT = "run_agent"
    MANAGE_KEYS = "manage_keys"
    MANAGE_SECRETS = "manage_secrets"
    MANAGE_MEMBERS = "manage_members"
    ADMIN = "admin"


class PermissionDeniedError(PermissionError):
    """Raised when a role lacks a required permission."""

    def __init__(self, role: UserRole, permission: Permission) -> None:
        self.role = role
        self.permission = permission
        super().__init__(f"role '{role.value}' lacks permission '{permission.value}'")


_ALL: frozenset[Permission] = frozenset(Permission)

#: The authoritative role -> permission matrix (least privilege per role).
ROLE_PERMISSIONS: dict[UserRole, frozenset[Permission]] = {
    UserRole.ADMIN: _ALL,
    UserRole.MEMBER: frozenset(
        {Permission.READ, Permission.WRITE, Permission.RUN_AGENT}
    ),
    UserRole.VIEWER: frozenset({Permission.READ}),
    # agent-runner executes agent runs but cannot mutate the board, manage
    # members, or touch keys/secrets — it acts only through policy-gated runs.
    UserRole.AGENT_RUNNER: frozenset({Permission.READ, Permission.RUN_AGENT}),
}


def permissions_for(role: UserRole) -> frozenset[Permission]:
    """Return the permission set granted to ``role``."""
    return ROLE_PERMISSIONS.get(role, frozenset())


def can(role: UserRole, permission: Permission) -> bool:
    """True iff ``role`` is granted ``permission``."""
    return permission in permissions_for(role)


def ensure(role: UserRole, permission: Permission) -> None:
    """Raise :class:`PermissionDeniedError` unless ``role`` has ``permission``."""
    if not can(role, permission):
        raise PermissionDeniedError(role, permission)


__all__ = [
    "ROLE_PERMISSIONS",
    "Permission",
    "PermissionDeniedError",
    "can",
    "ensure",
    "permissions_for",
]
