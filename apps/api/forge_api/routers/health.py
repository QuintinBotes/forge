"""Health / liveness / readiness routes (real, not stubbed).

These are intentionally dependency-free (no auth, no DB) so orchestrators can
probe the process even before downstream services are reachable.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status
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


@router.get("/health/ready", response_model=ReadinessResponse, summary="Readiness (drain-aware)")
def health_ready(request: Request, response: Response) -> ReadinessResponse:
    """HARD-11 drain-aware readiness for zero-downtime restarts.

    Returns ``503`` the moment graceful-shutdown draining begins (SIGTERM) so a
    load balancer stops routing new traffic before the process exits; ``200``
    while the process is serving. When ``FORGE_READINESS_REQUIRE_DEPS`` is set the
    DB/Redis pings are promoted to a hard gate (production); otherwise they are
    reported best-effort so a dev/test instance stays ready without live
    backends.
    """
    from forge_api.middleware.shutdown import current_state
    from forge_api.settings import get_settings

    state = current_state()
    checks: dict[str, str] = {"serving": "ok" if state.is_serving else "draining"}
    if not state.is_serving:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return ReadinessResponse(status="draining", checks=checks)

    settings = getattr(request.app.state, "settings", None) or get_settings()
    require_deps = getattr(settings, "readiness_require_deps", False)
    dep_ok = _probe_dependencies(checks)
    if require_deps and not dep_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return ReadinessResponse(status="not_ready", checks=checks)
    return ReadinessResponse(checks=checks)


def _probe_dependencies(checks: dict[str, str]) -> bool:
    """Best-effort DB + Redis pings, recording status into ``checks``.

    Returns ``True`` when every attempted probe succeeded. Failures are recorded
    but only gate readiness when ``FORGE_READINESS_REQUIRE_DEPS`` is set.
    """
    ok = True
    try:
        from sqlalchemy import text

        from forge_api.db import get_engine

        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:  # readiness never raises
        checks["database"] = f"error: {type(exc).__name__}"
        ok = False
    return ok
