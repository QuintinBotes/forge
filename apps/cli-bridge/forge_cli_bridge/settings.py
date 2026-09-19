"""Bridge configuration. Refuses unsafe combinations at construction time."""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["BridgeSettings"]

_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})


@dataclass(frozen=True)
class BridgeSettings:
    token: str = field(repr=False)
    host: str = "127.0.0.1"
    port: int = 8099
    allow_non_loopback: bool = False
    timeout_s: float = 600.0

    def __post_init__(self) -> None:
        if not self.token.strip():
            raise ValueError(
                "FORGE_CLI_BRIDGE_TOKEN is required; the bridge executes host "
                "binaries and will not start unauthenticated"
            )
        if self.host not in _LOOPBACK and not self.allow_non_loopback:
            raise ValueError(
                f"refusing to bind {self.host!r}: set "
                "FORGE_CLI_BRIDGE_ALLOW_NON_LOOPBACK=1 to override"
            )
