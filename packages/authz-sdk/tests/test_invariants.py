"""Escalation / lockout / team cycle+depth invariants (F30 AC11, AC12, AC14).

Pure logic, operating on in-memory grant lists (the single ``authz_service``
writer delegates to these same functions)."""

from __future__ import annotations

import uuid

import pytest
from forge_authz import (
    MAX_TEAM_DEPTH,
    EscalationError,
    LastAdminError,
    TeamCycleError,
    TeamDepthError,
    check_grant_allowed,
    ensure_not_last_admin,
    validate_team_parent,
)

from forge_contracts.authz import (
    PrincipalRef,
    PrincipalType,
    Role,
    RoleGrant,
    ScopeRef,
    ScopeType,
)

WS = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
CORE = uuid.UUID("00000000-0000-0000-0000-0000000000c0")
ACTOR = uuid.UUID("00000000-0000-0000-0000-0000000000d0")


def _grant(scope_type, scope_id, role, gid=None) -> RoleGrant:
    return RoleGrant(
        id=gid or uuid.uuid4(),
        workspace_id=WS,
        principal=PrincipalRef(type=PrincipalType.USER, id=ACTOR),
        scope=ScopeRef(type=scope_type, id=scope_id),
        role=role,
    )


# --- AC11 escalation --------------------------------------------------------- #


def test_admin_can_grant_member() -> None:
    actor = [_grant(ScopeType.WORKSPACE, WS, Role.ADMIN)]
    # no raise
    check_grant_allowed(
        actor_grants=actor,
        actor_has_role_grant=True,
        scope_type=ScopeType.WORKSPACE,
        scope_id=WS,
        granted_role=Role.MEMBER,
    )


def test_member_cannot_grant_admin() -> None:
    actor = [_grant(ScopeType.WORKSPACE, WS, Role.MEMBER)]
    with pytest.raises(EscalationError):
        check_grant_allowed(
            actor_grants=actor,
            actor_has_role_grant=False,  # member lacks role.grant
            scope_type=ScopeType.WORKSPACE,
            scope_id=WS,
            granted_role=Role.ADMIN,
        )


def test_project_admin_cannot_grant_above_own_rank() -> None:
    actor = [_grant(ScopeType.PROJECT, CORE, Role.ADMIN)]
    # Can grant member at the project scope they admin.
    check_grant_allowed(
        actor_grants=actor,
        actor_has_role_grant=True,
        scope_type=ScopeType.PROJECT,
        scope_id=CORE,
        granted_role=Role.MEMBER,
    )
    # But not at a scope where they hold nothing.
    with pytest.raises(EscalationError):
        check_grant_allowed(
            actor_grants=actor,
            actor_has_role_grant=True,
            scope_type=ScopeType.WORKSPACE,
            scope_id=WS,
            granted_role=Role.MEMBER,
        )


# --- AC12 lockout ------------------------------------------------------------ #


def test_last_admin_cannot_be_removed() -> None:
    last = _grant(ScopeType.WORKSPACE, WS, Role.ADMIN)
    with pytest.raises(LastAdminError):
        ensure_not_last_admin(
            all_workspace_grants=[last], workspace_id=WS, removing_grant_id=last.id
        )


def test_removing_one_of_two_admins_is_fine() -> None:
    a1 = _grant(ScopeType.WORKSPACE, WS, Role.ADMIN)
    a2 = _grant(ScopeType.WORKSPACE, WS, Role.ADMIN)
    ensure_not_last_admin(all_workspace_grants=[a1, a2], workspace_id=WS, removing_grant_id=a1.id)


def test_removing_non_admin_grant_is_fine_even_if_one_admin() -> None:
    admin = _grant(ScopeType.WORKSPACE, WS, Role.ADMIN)
    member = _grant(ScopeType.WORKSPACE, WS, Role.MEMBER)
    ensure_not_last_admin(
        all_workspace_grants=[admin, member], workspace_id=WS, removing_grant_id=member.id
    )


# --- AC14 team cycle / depth ------------------------------------------------- #


def test_team_cycle_detected() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    # Making b's parent = a would create a -> b -> a cycle (a's parent is b).
    with pytest.raises(TeamCycleError):
        validate_team_parent(team_id=b, parent_team_id=a, parents={a: b})


def test_self_parent_is_cycle() -> None:
    a = uuid.uuid4()
    with pytest.raises(TeamCycleError):
        validate_team_parent(team_id=a, parent_team_id=a, parents={})


def test_team_depth_exceeded() -> None:
    chain = [uuid.uuid4() for _ in range(MAX_TEAM_DEPTH + 2)]
    parents = {chain[i]: chain[i + 1] for i in range(len(chain) - 1)}
    parents[chain[-1]] = None
    new_team = uuid.uuid4()
    with pytest.raises(TeamDepthError):
        validate_team_parent(team_id=new_team, parent_team_id=chain[0], parents=parents)


def test_shallow_parent_ok() -> None:
    parent = uuid.uuid4()
    validate_team_parent(team_id=uuid.uuid4(), parent_team_id=parent, parents={parent: None})


def test_none_parent_ok() -> None:
    validate_team_parent(team_id=uuid.uuid4(), parent_team_id=None, parents={})
