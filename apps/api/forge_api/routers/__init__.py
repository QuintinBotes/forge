"""Feature routers for the Forge API.

Every router is pre-registered here (plan Task 0.4) so Phase-1 tasks fill their
handlers inside the individual ``routers/<name>.py`` modules they own — never
touching ``main.py`` or this aggregator. ``health`` is mounted at the root;
``FEATURE_ROUTERS`` are mounted under the configurable API prefix.
"""

from __future__ import annotations

from fastapi import APIRouter

from forge_api.routers import (
    access,
    agent,
    alerts,
    approval,
    approvals,
    auth,
    automations,
    benchmarks,
    board,
    deployments,
    health,
    incidents,
    integration,
    knowledge,
    marketplace,
    mcp,
    observability,
    pm,
    policy,
    project_access,
    saml,
    scim,
    spec,
    sprints,
    sso_admin,
    teams,
    workflow,
    workflow_editor,
)

#: Mounted at the application root (no API prefix, no auth) for probes.
HEALTH_ROUTER: APIRouter = health.router

#: Mounted under ``Settings.api_prefix``; ordered for a stable OpenAPI document.
FEATURE_ROUTERS: tuple[APIRouter, ...] = (
    auth.router,
    board.router,
    spec.router,
    knowledge.router,
    workflow.router,
    workflow_editor.router,
    agent.router,
    policy.router,
    mcp.router,
    marketplace.router,
    benchmarks.router,
    integration.router,
    approval.router,
    approvals.router,
    incidents.router,
    alerts.router,
    observability.router,
    pm.router,
    automations.router,
    deployments.router,
    sprints.router,
    teams.router,
    access.router,
    project_access.router,
    sso_admin.router,
    saml.router,
    scim.router,
)

__all__ = [
    "FEATURE_ROUTERS",
    "HEALTH_ROUTER",
    "access",
    "agent",
    "alerts",
    "approval",
    "approvals",
    "auth",
    "automations",
    "benchmarks",
    "board",
    "deployments",
    "health",
    "incidents",
    "integration",
    "knowledge",
    "marketplace",
    "mcp",
    "observability",
    "pm",
    "policy",
    "project_access",
    "saml",
    "scim",
    "spec",
    "sprints",
    "sso_admin",
    "teams",
    "workflow",
    "workflow_editor",
]
