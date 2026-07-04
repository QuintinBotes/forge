"""Tests for RBAC role/permission evaluation (Task 1.15 — auth & secrets).

Spec Security: RBAC roles admin, member, viewer, agent-runner per workspace.
"""

from __future__ import annotations

import pytest

from forge_api.auth.rbac import (
    ROLE_PERMISSIONS,
    Permission,
    PermissionDeniedError,
    can,
    ensure,
    permissions_for,
)
from forge_contracts.enums import UserRole


def test_every_role_has_a_permission_set() -> None:
    assert set(ROLE_PERMISSIONS) == set(UserRole)


def test_admin_has_all_permissions() -> None:
    assert permissions_for(UserRole.ADMIN) == frozenset(Permission)


def test_viewer_is_read_only() -> None:
    assert can(UserRole.VIEWER, Permission.READ)
    assert not can(UserRole.VIEWER, Permission.WRITE)


def test_viewer_denied_write_raises() -> None:
    with pytest.raises(PermissionDeniedError):
        ensure(UserRole.VIEWER, Permission.WRITE)


def test_member_can_write_but_not_manage_secrets() -> None:
    assert can(UserRole.MEMBER, Permission.WRITE)
    assert not can(UserRole.MEMBER, Permission.MANAGE_SECRETS)
    assert not can(UserRole.MEMBER, Permission.MANAGE_KEYS)


def test_agent_runner_can_run_agents_but_not_manage_members() -> None:
    assert can(UserRole.AGENT_RUNNER, Permission.RUN_AGENT)
    assert not can(UserRole.AGENT_RUNNER, Permission.MANAGE_MEMBERS)


def test_only_admin_manages_keys_and_secrets() -> None:
    for role in UserRole:
        managing = can(role, Permission.MANAGE_KEYS) or can(role, Permission.MANAGE_SECRETS)
        assert managing == (role is UserRole.ADMIN)


def test_ensure_allows_permitted_action() -> None:
    ensure(UserRole.ADMIN, Permission.MANAGE_SECRETS)  # must not raise
