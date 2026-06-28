"""Feature routers for the Forge API.

Every router is pre-registered here (plan Task 0.4) so Phase-1 tasks fill their
handlers inside the individual ``routers/<name>.py`` modules they own — never
touching ``main.py`` or this aggregator. ``health`` is mounted at the root;
``FEATURE_ROUTERS`` are mounted under the configurable API prefix.
"""

from __future__ import annotations

from fastapi import APIRouter

from forge_api.routers import (
    agent,
    alerts,
    approval,
    auth,
    automations,
    board,
    health,
    incidents,
    integration,
    knowledge,
    mcp,
    observability,
    pm,
    policy,
    spec,
    sprints,
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
    integration.router,
    approval.router,
    incidents.router,
    alerts.router,
    observability.router,
    pm.router,
    automations.router,
    sprints.router,
)

__all__ = [
    "FEATURE_ROUTERS",
    "HEALTH_ROUTER",
    "agent",
    "alerts",
    "approval",
    "auth",
    "automations",
    "board",
    "health",
    "incidents",
    "integration",
    "knowledge",
    "mcp",
    "observability",
    "pm",
    "policy",
    "spec",
    "sprints",
    "workflow",
    "workflow_editor",
]
