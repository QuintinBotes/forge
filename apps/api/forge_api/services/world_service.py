"""Composition root for the world-model service (the ``/world`` router).

``get_world_service`` builds a per-request :class:`WorldModelService` over
the request session with no model client; ingest resolves the workspace
client separately via :func:`resolve_world_model_client` — the same
``AuthService.resolve_model_client`` path the spec router uses — and refuses
with 503 when no provider is configured. Nothing here ever substitutes a
scripted or fake model.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, status
from sqlalchemy.orm import Session

from forge_api.deps import DbSession, Principal, get_current_principal
from forge_contracts import ModelClient
from forge_world import WorldModelService, WorldRepository
from forge_world.service import Clock


def make_world_service(
    session: Session,
    model_client: ModelClient | None = None,
    clock: Clock | None = None,
    *,
    model: str = "",
) -> WorldModelService:
    """Build a world-model service over ``session`` (tests inject fakes here)."""
    return WorldModelService(
        WorldRepository(session), model_client=model_client, clock=clock, model=model
    )


def get_world_service(session: DbSession) -> WorldModelService:
    """Per-request world service without a model client (all but ingest)."""
    return make_world_service(session)


def resolve_world_model_client(
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> ModelClient:
    """Resolve the workspace BYOK model client for extraction, else 503.

    Mirrors ``routers/spec.py``: the client comes from
    ``AuthService.resolve_model_client`` (workspace BYOK config); when no
    provider is configured the request is refused with the reason rather
    than silently falling back to anything fake.
    """
    from forge_agent.providers import ModelClientError
    from forge_api.auth.service import get_auth_service

    try:
        return get_auth_service().resolve_model_client(principal.workspace_id)
    except ModelClientError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc


__all__ = ["get_world_service", "make_world_service", "resolve_world_model_client"]
