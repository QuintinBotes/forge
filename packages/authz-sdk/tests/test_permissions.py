"""Permission-table integrity (F30 AC20 + the scope-narrowing rule AC5)."""

from __future__ import annotations

from forge_authz.permissions import (
    ACCESS_LEVEL_ROLE,
    ROLE_PERMISSIONS,
    ROLE_RANK,
    WORKSPACE_ONLY_PERMISSIONS,
    scope_narrow,
)

from forge_contracts.authz import AccessLevel, Permission, Role, ScopeType

P = Permission


def test_subset_chain_viewer_member_admin() -> None:
    """AC20: VIEWER ⊆ MEMBER ⊆ ADMIN."""
    assert ROLE_PERMISSIONS[Role.VIEWER] <= ROLE_PERMISSIONS[Role.MEMBER]
    assert ROLE_PERMISSIONS[Role.MEMBER] <= ROLE_PERMISSIONS[Role.ADMIN]


def test_admin_holds_everything() -> None:
    assert ROLE_PERMISSIONS[Role.ADMIN] == frozenset(Permission)


def test_agent_runner_exclusions() -> None:
    """AC20: agent-runner must never hold approve/grant/manage-admin powers."""
    agent = ROLE_PERMISSIONS[Role.AGENT_RUNNER]
    for forbidden in (
        P.PR_APPROVE,
        P.DEPLOY_APPROVE,
        P.ROLE_GRANT,
        P.MEMBER_MANAGE,
        P.WORKSPACE_ADMIN,
        P.SECRETS_MANAGE,
    ):
        assert forbidden not in agent


def test_member_excludes_grant_and_member_manage() -> None:
    """AC2 (negative): a plain member cannot grant roles or manage members."""
    member = ROLE_PERMISSIONS[Role.MEMBER]
    assert P.ROLE_GRANT not in member
    assert P.MEMBER_MANAGE not in member
    assert P.PR_APPROVE in member  # but it can approve PRs


def test_access_level_role_mapping() -> None:
    assert ACCESS_LEVEL_ROLE[AccessLevel.READ] is Role.VIEWER
    assert ACCESS_LEVEL_ROLE[AccessLevel.WRITE] is Role.MEMBER
    assert ACCESS_LEVEL_ROLE[AccessLevel.ADMIN] is Role.ADMIN


def test_role_rank_ordering() -> None:
    assert ROLE_RANK[Role.VIEWER] == ROLE_RANK[Role.AGENT_RUNNER]
    assert ROLE_RANK[Role.VIEWER] < ROLE_RANK[Role.MEMBER] < ROLE_RANK[Role.ADMIN]


def test_scope_narrow_workspace_is_identity() -> None:
    full = ROLE_PERMISSIONS[Role.ADMIN]
    assert scope_narrow(full, ScopeType.WORKSPACE) == full


def test_scope_narrow_project_strips_workspace_only() -> None:
    """AC5: a project admin loses workspace-only powers but keeps project ones."""
    narrowed = scope_narrow(ROLE_PERMISSIONS[Role.ADMIN], ScopeType.PROJECT)
    # workspace-only powers removed
    for gone in WORKSPACE_ONLY_PERMISSIONS:
        assert gone not in narrowed
    # project-scoped powers retained
    for kept in (P.ROLE_GRANT, P.DEPLOY_APPROVE, P.PROJECT_DELETE, P.PROJECT_ADMIN):
        assert kept in narrowed


def test_workspace_only_set_contents() -> None:
    assert (
        frozenset(
            {
                P.WORKSPACE_ADMIN,
                P.MEMBER_MANAGE,
                P.TEAM_MANAGE,
                P.SECRETS_MANAGE,
                P.MCP_MANAGE,
                P.INTEGRATION_MANAGE,
                P.POLICY_MANAGE,
            }
        )
        == WORKSPACE_ONLY_PERMISSIONS
    )
