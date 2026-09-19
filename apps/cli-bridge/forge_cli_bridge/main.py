"""ASGI entrypoint. Builds settings from the environment and fails loudly if unsafe."""

from __future__ import annotations

import os

from forge_cli_bridge.app import create_app
from forge_cli_bridge.settings import BridgeSettings

__all__ = ["app"]


def _settings_from_env() -> BridgeSettings:
    return BridgeSettings(
        token=os.environ.get("FORGE_CLI_BRIDGE_TOKEN", ""),
        host=os.environ.get("FORGE_CLI_BRIDGE_HOST", "127.0.0.1"),
        port=int(os.environ.get("FORGE_CLI_BRIDGE_PORT", "8099")),
        allow_non_loopback=os.environ.get("FORGE_CLI_BRIDGE_ALLOW_NON_LOOPBACK", "")
        in {"1", "true", "yes", "on"},
        timeout_s=float(os.environ.get("FORGE_LOCAL_CLI_TIMEOUT_S", "600")),
    )


app = create_app(_settings_from_env())
