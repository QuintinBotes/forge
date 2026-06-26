"""Health / liveness / readiness routes (real, not stubbed).

These are intentionally dependency-free (no auth, no DB) so orchestrators can
probe the process even before downstream services are reachable.
"""

from __future__ import annotations

from fastapi import APIRouter
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
def readyz() -> ReadinessResponse:
    # Phase 0: process is up; dependency checks are added with their services.
    return ReadinessResponse(checks={"process": "ok"})
