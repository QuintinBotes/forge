"""Forge API application entrypoint.

``create_app`` is the application factory; ``app`` is the module-level instance
uvicorn serves (``uvicorn forge_api.main:app``). Every feature router is mounted
here once, in Phase 0, so Phase-1 tasks only ever edit their own router module.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from forge_api.routers import FEATURE_ROUTERS, HEALTH_ROUTER
from forge_api.settings import Settings, get_settings


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

    app = FastAPI(
        title=cfg.app_name,
        version=cfg.version,
        description="Forge — OSS engineering orchestration platform API.",
        docs_url="/docs" if cfg.docs_enabled else None,
        redoc_url="/redoc" if cfg.docs_enabled else None,
    )

    if cfg.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cfg.cors_origins,
            allow_credentials=cfg.cors_allow_credentials,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # Health/liveness at the root; feature routers under the configurable prefix.
    app.include_router(HEALTH_ROUTER)
    prefix = cfg.api_prefix.rstrip("/")
    for router in FEATURE_ROUTERS:
        app.include_router(router, prefix=prefix)

    @app.get("/", response_model=ServiceInfo, tags=["health"], summary="Service info")
    def root() -> ServiceInfo:
        return ServiceInfo(
            name=cfg.app_name,
            version=cfg.version,
            environment=cfg.environment,
            docs_url="/docs" if cfg.docs_enabled else None,
        )

    return app


app = create_app()
