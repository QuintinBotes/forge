"""Health / liveness / readiness routes (real, not stubbed).

These are intentionally dependency-free (no auth, no DB) so orchestrators can
probe the process even before downstream services are reachable.
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status
from pydantic import BaseModel

from forge_api import __version__

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    """Liveness payload returned by ``/health``."""

    status: str = "ok"
    service: str = "forge-api"
    version: str = __version__


class ReadinessResponse(BaseModel):
    """Readiness payload. Phase 1 wires real dependency checks (db/redis)."""

    status: str = "ok"
    service: str = "forge-api"
    checks: dict[str, str] = {}


@router.get("/health", response_model=HealthResponse, summary="Liveness probe")
def health() -> HealthResponse:
    return HealthResponse()


@router.get("/healthz", response_model=HealthResponse, summary="Liveness probe (alias)")
def healthz() -> HealthResponse:
    return HealthResponse()


@router.get("/readyz", response_model=ReadinessResponse, summary="Readiness probe")
def readyz(response: Response) -> ReadinessResponse:
    """Process readiness, plus a Temporal frontend check when that backend is
    selected (F25 AC19). Returns 503 (listing the unready dependency) when not
    ready; 200 otherwise."""
    from forge_api.services.temporal_health import readiness

    ready, checks = readiness()
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return ReadinessResponse(status="not_ready", checks=checks)
    return ReadinessResponse(checks=checks)
