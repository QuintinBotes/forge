"""Local CLI provider for agent runtime."""

from __future__ import annotations

from forge_agent.providers.local_cli.detect import (
    DEFAULT_PREFERENCE,
    CliName,
    DetectedCli,
    detect_clis,
)

__all__ = ["DEFAULT_PREFERENCE", "CliName", "DetectedCli", "detect_clis"]
