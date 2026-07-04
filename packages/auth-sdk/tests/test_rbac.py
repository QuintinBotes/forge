"""F37 flat-RBAC tests (AC8, AC13)."""

from __future__ import annotations

import pytest

from forge_auth.rbac import ROLE_RANK, has_at_least, max_grantable_role
from forge_contracts.enums import UserRole


def test_rank_ordering() -> None:
    """viewer = agent-runner < member < admin (AC13)."""
    assert ROLE_RANK[UserRole.VIEWER] == ROLE_RANK[UserRole.AGENT_RUNNER]
    assert ROLE_RANK[UserRole.VIEWER] < ROLE_RANK[UserRole.MEMBER] < ROLE_RANK[UserRole.ADMIN]
    assert set(ROLE_RANK) == set(UserRole)


@pytest.mark.parametrize(
    ("role", "minimum", "expected"),
    [
        (UserRole.ADMIN, UserRole.ADMIN, True),
        (UserRole.ADMIN, UserRole.MEMBER, True),
        (UserRole.MEMBER, UserRole.ADMIN, False),
        (UserRole.MEMBER, UserRole.MEMBER, True),
        (UserRole.MEMBER, UserRole.VIEWER, True),
        (UserRole.VIEWER, UserRole.MEMBER, False),
        (UserRole.AGENT_RUNNER, UserRole.MEMBER, False),
        (UserRole.AGENT_RUNNER, UserRole.VIEWER, True),
        (UserRole.VIEWER, UserRole.AGENT_RUNNER, True),
    ],
)
def test_has_at_least_matrix(role: UserRole, minimum: UserRole, expected: bool) -> None:
    assert has_at_least(role, minimum) is expected


@pytest.mark.parametrize("actor", list(UserRole))
def test_max_grantable_role_caps_at_actor(actor: UserRole) -> None:
    granted = max_grantable_role(actor)
    assert granted == actor
    assert ROLE_RANK[granted] <= ROLE_RANK[actor]
