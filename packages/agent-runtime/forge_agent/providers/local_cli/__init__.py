"""Local CLI provider for agent runtime."""

from __future__ import annotations

from forge_agent.providers.local_cli.detect import (
    CliName,
    DEFAULT_PREFERENCE,
    DetectedCli,
    detect_clis,
)

__all__ = ["CliName", "DetectedCli", "DEFAULT_PREFERENCE", "detect_clis"]
