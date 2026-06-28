"""The frozen role -> permission tables + the scope-narrowing rule (F30 §4).

Pure data + one pure function. This is the canonical RBAC mapping; both the
resolver and the privilege-drift guard test (AC20) read it. Permissions are
*additive*: a principal's effective set is the union of the permission sets of
all applicable roles. There is no explicit-deny rule (that is the separate
Advanced Policy Engine slice).
"""

from __future__ import annotations

from collections.abc import Mapping

from forge_contracts.authz import AccessLevel, Permission, Role, ScopeType

__all__ = [
    "ACCESS_LEVEL_ROLE",
    "ROLE_PERMISSIONS",
    "ROLE_RANK",
    "WORKSPACE_ONLY_PERMISSIONS",
    "scope_narrow",
]

P = Permission

#: The canonical role -> permission mapping (frozen).
ROLE_PERMISSIONS: Mapping[Role, frozenset[Permission]] = {
    # Admin holds every permission *within the granted scope* (scope narrowing
    # strips workspace-only powers at project/team scope — see ``scope_narrow``).
    Role.ADMIN: frozenset(Permission),
    Role.MEMBER: frozenset(
        {
            P.PROJECT_CREATE,
            P.PROJECT_READ,
            P.PROJECT_WRITE,
            P.TASK_READ,
            P.TASK_WRITE,
            P.SPEC_APPROVE,
            P.PR_APPROVE,
            P.KNOWLEDGE_MANAGE,
            P.AGENT_RUN,
            P.AUDIT_READ,
        }
    ),
    Role.VIEWER: frozenset({P.PROJECT_READ, P.TASK_READ, P.AUDIT_READ}),
    # The agent-runner can read/write tasks and run, but never approves a PR or
    # deploy, never grants roles, and holds no *_MANAGE-admin / member.manage.
    Role.AGENT_RUNNER: frozenset(
        {
            P.PROJECT_READ,
            P.TASK_READ,
            P.TASK_WRITE,
            P.AGENT_RUN,
            P.KNOWLEDGE_MANAGE,
        }
    ),
}

#: A team's access level maps onto a role.
ACCESS_LEVEL_ROLE: Mapping[AccessLevel, Role] = {
    AccessLevel.READ: Role.VIEWER,
    AccessLevel.WRITE: Role.MEMBER,
    AccessLevel.ADMIN: Role.ADMIN,
}

#: Comparable rank for escalation checks (viewer == agent_runner are the floor).
ROLE_RANK: Mapping[Role, int] = {
    Role.VIEWER: 0,
    Role.AGENT_RUNNER: 0,
    Role.MEMBER: 1,
    Role.ADMIN: 2,
}

#: Permissions that only make sense at workspace scope. At project/team scope an
#: ``admin`` grant is narrowed so a project admin cannot manage workspace
#: members / teams / secrets / integrations / MCP / policy. ``role.grant``,
#: ``deploy.approve`` and ``project.delete`` are intentionally *retained* at
#: project scope (project admins manage project-scoped grants + deploys).
WORKSPACE_ONLY_PERMISSIONS: frozenset[Permission] = frozenset(
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


def scope_narrow(perms: frozenset[Permission], scope_type: ScopeType) -> frozenset[Permission]:
    """Return ``perms`` narrowed for ``scope_type``.

    At workspace scope the set is unchanged; at project/team scope the
    workspace-only permissions are removed (least privilege by scope).
    """
    if scope_type is ScopeType.WORKSPACE:
        return perms
    return perms - WORKSPACE_ONLY_PERMISSIONS
