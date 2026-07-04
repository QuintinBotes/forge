"""Flat V1 role ranking (F37). F30's scoped resolver supersedes this at runtime.

``admin > member > {agent-runner, viewer}`` — ``agent-runner`` and ``viewer``
share the lowest rank: an agent key can never satisfy a ``member`` gate, so an
agent cannot mint keys, write secrets, or escalate (Build-Prompt constraint #2).
"""

from __future__ import annotations

from forge_contracts.enums import UserRole

__all__ = ["ROLE_RANK", "has_at_least", "max_grantable_role"]

#: The authoritative flat role ranking.
ROLE_RANK: dict[UserRole, int] = {
    UserRole.VIEWER: 0,
    UserRole.AGENT_RUNNER: 0,
    UserRole.MEMBER: 1,
    UserRole.ADMIN: 2,
}


def has_at_least(role: UserRole, minimum: UserRole) -> bool:
    """True iff ``role`` ranks at or above ``minimum``."""
    return ROLE_RANK[role] >= ROLE_RANK[minimum]


def max_grantable_role(actor: UserRole) -> UserRole:
    """The highest role an actor may assign to a key/user: their own (no
    self-escalation)."""
    return actor
