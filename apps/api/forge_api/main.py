"""Forge API application entrypoint.

``create_app`` is the application factory; ``app`` is the module-level instance
uvicorn serves (``uvicorn forge_api.main:app``). Every feature router is mounted
here once, in Phase 0, so Phase-1 tasks only ever edit their own router module.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from forge_api.auth.vault import SecretExpiredError
from forge_api.middleware import install_middleware, lifespan
from forge_api.observability.redaction import install_log_redaction
from forge_api.routers import FEATURE_ROUTERS, HEALTH_ROUTER
from forge_api.security import (
    BodySizeLimitMiddleware,
    RateLimitMiddleware,
    SecurityHeadersMiddleware,
)
from forge_api.settings import Settings, get_settings
from forge_api.sso.errors import ScimApiError
from forge_contracts.sso import ScimError
from forge_obs.telemetry import setup_telemetry


class ServiceInfo(BaseModel):
    """Root payload describing the running service."""

    name: str
    version: str
    environment: str
    docs_url: str | None = None


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build a configured :class:`FastAPI` instance.

    Accepts an explicit ``settings`` (handy for tests) and otherwise resolves the
    process-wide cached settings.
    """
    cfg = settings or get_settings()

    # F38: one shared telemetry init per service. Env-driven (OBS_ENABLED
    # defaults false -> no-op providers + JSON logging only), idempotent, and
    # never raises — the app boots identically with or without the
    # observability stack (spec AC1/AC18).
    setup_telemetry("forge-api")

    # HARD-13: install the structural log-redaction filter on the root + ASGI
    # loggers so an accidental ``logger.info(secret)`` anywhere is scrubbed at
    # the sink, not left to call-site discipline. Idempotent across app builds.
    install_log_redaction()

    # HARD-09: OpenAPI docs are forced off in production unless the operator
    # explicitly sets FORGE_DOCS_ENABLED (info-disclosure reduction). The
    # openapi.json document itself is gated too, not just the UI pages.
    docs_on = cfg.docs_effectively_enabled
    app = FastAPI(
        title=cfg.app_name,
        version=cfg.version,
        description="Forge — OSS engineering orchestration platform API.",
        docs_url="/docs" if docs_on else None,
        redoc_url="/redoc" if docs_on else None,
        openapi_url="/openapi.json" if docs_on else None,
        # HARD-11: graceful shutdown — startup marks the process serving; SIGTERM
        # flips readiness to 503, drains in-flight requests, then disposes the DB
        # engine / Redis pool / telemetry exporters.
        lifespan=lifespan,
    )

    # HARD-09 edge controls. ``add_middleware`` wraps outside-in (the last one
    # added runs first), so adding headers -> rate limit -> body limit yields
    # the required order: body limit outermost, then rate limit, then headers.
    app.add_middleware(SecurityHeadersMiddleware)
    if cfg.ratelimit_enabled:
        app.add_middleware(
            RateLimitMiddleware,
            rate_per_min=cfg.ratelimit_rpm,
            burst=cfg.ratelimit_burst,
            overrides=cfg.ratelimit_overrides,
        )
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=cfg.max_body_bytes)

    if cfg.cors_origins:
        allow_credentials = cfg.cors_allow_credentials
        # A wildcard origin combined with credentials is forbidden by the CORS
        # spec and a credential-leak vector (the browser would receive a
        # reflected origin + ``Allow-Credentials: true``). If a deployment
        # configures both, fail safe: keep the wildcard but drop credentials.
        if "*" in cfg.cors_origins and allow_credentials:
            allow_credentials = False
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cfg.cors_origins,
            allow_credentials=allow_credentials,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # F33: SCIM protocol errors must serialize as RFC 7644 Error resources
    # (top-level ``schemas``/``status``/``scimType``), not FastAPI's
    # ``{"detail": ...}`` envelope — IdP provisioning engines parse this shape.
    @app.exception_handler(ScimApiError)
    async def _scim_error_handler(_request: Request, exc: ScimApiError) -> JSONResponse:
        body = ScimError(status=str(exc.status), scimType=exc.scim_type, detail=exc.detail)
        return JSONResponse(
            status_code=exc.status,
            content=body.model_dump(exclude_none=True),
            media_type="application/scim+json",
        )

    # HARD-13: a BYOK secret resolved past its ``expires_at`` surfaces as
    # HTTP 409 ("rotate this credential") wherever it is used, not a 500 —
    # the spec's automatic-expiry guarantee for stored credentials.
    @app.exception_handler(SecretExpiredError)
    async def _secret_expired_handler(_request: Request, _exc: SecretExpiredError) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={"detail": "secret has expired; rotate this credential"},
        )

    # Expose the resolved settings on app.state so dependency-free routes (e.g.
    # the drain-aware readiness probe) can read this app's config rather than the
    # process-wide cached instance.
    app.state.settings = cfg

    # Health/liveness at the root; feature routers under the configurable prefix.
    app.include_router(HEALTH_ROUTER)

    # RT-1: the board-push WebSocket mounts at the app ROOT (``/ws``), not under
    # the API prefix — it must match the URL the web board hook opens
    # (``NEXT_PUBLIC_WS_URL`` / ``ws://localhost:8000/ws``), exactly like the
    # root-mounted health probes.
    from forge_api.routers.realtime import router as realtime_router

    app.include_router(realtime_router)
    prefix = cfg.api_prefix.rstrip("/")
    for router in FEATURE_ROUTERS:
        app.include_router(router, prefix=prefix)

    # F35: the public, unauthenticated, read-only leaderboard router mounts at
    # the app root ONLY when explicitly enabled (self-hosted privacy default:
    # disabled -> every /public/* path 404s and internal eval data never leaks).
    if cfg.public_leaderboard_enabled:
        from forge_api.routers.public_leaderboard import router as public_leaderboard_router

        app.include_router(public_leaderboard_router)

    @app.get("/", response_model=ServiceInfo, tags=["health"], summary="Service info")
    def root() -> ServiceInfo:
        return ServiceInfo(
            name=cfg.app_name,
            version=cfg.version,
            environment=cfg.environment,
            docs_url="/docs" if docs_on else None,
        )

    # HARD-11: mount the reliability layer last so it wraps outermost — a replayed
    # idempotent request short-circuits before rate-limit consumption, and the
    # in-flight counter feeds the lifespan drain.
    install_middleware(app, cfg)

    return app


app = create_app()
