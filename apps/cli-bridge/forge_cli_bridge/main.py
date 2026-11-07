"""ASGI entrypoint. Builds settings from the environment and fails loudly if unsafe."""

from __future__ import annotations

import os

import uvicorn

from forge_agent.providers.local_cli.detect import parse_cli_model, parse_preference
from forge_cli_bridge.app import create_app
from forge_cli_bridge.settings import BridgeSettings

__all__ = ["app"]


def _guard_non_loopback_bind(*, is_loopback: bool, allow_non_loopback: bool) -> None:
    """Refuse to bind a non-loopback host unless explicitly allowed.

    Fix round 2, IMPORTANT: this used to be a bare ``assert``, which
    ``python -O`` strips entirely -- a security guard that can disappear
    under an optimisation flag is not a guard. An explicit ``if: raise``
    cannot be compiled away. Takes plain booleans rather than a
    ``BridgeSettings`` instance so it can be exercised directly for every
    combination in tests, without needing to bypass
    ``BridgeSettings.__post_init__`` (which already makes the "false" case
    unreachable via the normal constructor -- this is the last-resort check
    at the one remaining place that actually binds a socket).
    """
    if not (allow_non_loopback or is_loopback):
        raise RuntimeError(
            "refusing to bind a non-loopback host without FORGE_CLI_BRIDGE_ALLOW_NON_LOOPBACK=1"
        )


def _settings_from_env() -> BridgeSettings:
    return BridgeSettings(
        token=os.environ.get("FORGE_CLI_BRIDGE_TOKEN", ""),
        host=os.environ.get("FORGE_CLI_BRIDGE_HOST", "127.0.0.1"),
        port=int(os.environ.get("FORGE_CLI_BRIDGE_PORT", "8099")),
        allow_non_loopback=os.environ.get("FORGE_CLI_BRIDGE_ALLOW_NON_LOOPBACK", "")
        in {"1", "true", "yes", "on"},
        timeout_s=float(os.environ.get("FORGE_LOCAL_CLI_TIMEOUT_S", "600")),
        max_concurrent_completions=int(os.environ.get("FORGE_CLI_BRIDGE_MAX_CONCURRENCY", "4")),
        # Read on the host, where the CLIs actually live: the worker's copy of
        # this variable only labels the run (see BridgeSettings.preference).
        preference=parse_preference(os.environ.get("FORGE_LOCAL_CLI_PREFERENCE")),
        cli_model=parse_cli_model(os.environ.get("FORGE_LOCAL_CLI_MODEL")),
    )


#: Built at import time so `uvicorn forge_cli_bridge.main:app` (an ASGI server
#: that imports the target directly, never running `__main__`) also gets the
#: module-level app. `BridgeSettings.__post_init__` already refuses an empty
#: token / unapproved non-loopback host at construction time, so a bare
#: import fails loudly instead of silently starting unsafely.
_settings = _settings_from_env()
app = create_app(_settings)


if __name__ == "__main__":
    # Fix round 1, IMPORTANT: `settings.host`/`settings.port` used to be dead
    # code -- `uvicorn ... --host/--port` on the command line decided the
    # actual bind address, completely independent of `BridgeSettings`'s
    # loopback guard. An operator following the README's own
    # `uvicorn ... --host 127.0.0.1` example could change that one flag to
    # `0.0.0.0`: `BridgeSettings` would validate happily (it never saw the
    # non-loopback host) and `/health` would still report the bridge as
    # loopback-only. Binding here, from the SAME validated `_settings` object
    # `app` was built from, makes the validated value the bound value --
    # there is no second, independently-typed place left to specify the
    # host. `python -m forge_cli_bridge.main` is the supported way to run
    # this (see README.md); `uvicorn forge_cli_bridge.main:app` still works
    # for ASGI-managed deployments and is bound by whatever `--host`/`--port`
    # that server is given, same as any other ASGI app -- the guard below is
    # this module's own last-resort belt-and-braces check that a direct
    # `python -m` run of *this* file never binds non-loopback silently (an
    # `if: raise`, not an `assert`, so it survives `python -O`; Fix round 2).
    _guard_non_loopback_bind(
        is_loopback=_settings.is_loopback, allow_non_loopback=_settings.allow_non_loopback
    )
    uvicorn.run(app, host=_settings.host, port=_settings.port)
