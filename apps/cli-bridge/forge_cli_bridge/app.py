"""Loopback HTTP bridge exposing the host's agent CLIs to a containerised worker.

The worker runs Linux/aarch64; the host CLIs are Mach-O/arm64 binaries and cannot
execute inside the container. This bridge runs on the host, so CLI credentials
never enter a container and no Linux CLI build is required.

Development and self-host convenience only. Not for shared or production use.
"""

from __future__ import annotations

import logging
import subprocess

from fastapi import Depends, FastAPI, Header, HTTPException

from forge_agent.providers.local_cli.client import LocalCliModelClient
from forge_agent.providers.local_cli.detect import CliName, detect_clis
from forge_cli_bridge.settings import BridgeSettings
from forge_contracts import ModelRequest, ModelResponse

__all__ = ["create_app"]

_logger = logging.getLogger("forge_cli_bridge")


def create_app(settings: BridgeSettings) -> FastAPI:
    app = FastAPI(title="Forge CLI bridge", version="0.1.0")
    app.state.settings = settings
    app.state.runner = subprocess.run

    def _authorize(authorization: str = Header(default="")) -> None:
        expected = f"Bearer {settings.token}"
        if authorization != expected:
            raise HTTPException(status_code=401, detail="invalid or missing bridge token")

    @app.get("/health")
    def health() -> dict[str, object]:
        return {
            "status": "ok",
            "clis": [d.name.value for d in detect_clis()],
            "loopback_only": not settings.allow_non_loopback,
        }

    @app.post("/v1/complete", dependencies=[Depends(_authorize)])
    def complete(request: ModelRequest) -> ModelResponse:
        detected = detect_clis()
        if not detected:
            return ModelResponse(content="", stop_reason="provider_unavailable")
        client = LocalCliModelClient(
            cli_name=CliName(detected[0].name),
            timeout_s=settings.timeout_s,
            runner=app.state.runner,
        )
        return client.complete(request)

    if settings.allow_non_loopback:
        _logger.warning(
            "CLI bridge is bound to %s — it executes host binaries; do not expose it",
            settings.host,
        )
    return app
