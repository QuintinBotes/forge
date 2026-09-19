"""Loopback HTTP bridge exposing the host's agent CLIs to a containerised worker.

The worker runs Linux/aarch64; the host CLIs are Mach-O/arm64 binaries and cannot
execute inside the container. This bridge runs on the host, so CLI credentials
never enter a container and no Linux CLI build is required.

Development and self-host convenience only. Not for shared or production use.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import shutil
import subprocess
from collections.abc import Callable

from fastapi import Depends, FastAPI, Header, HTTPException

from forge_agent.providers.local_cli.client import LocalCliModelClient
from forge_agent.providers.local_cli.detect import CliName, detect_clis
from forge_cli_bridge.settings import BridgeSettings
from forge_contracts import ModelRequest, ModelResponse

__all__ = ["create_app"]

_logger = logging.getLogger("forge_cli_bridge")

#: Unconditional startup banner (Fix round 1, spec gap): the README states this
#: is a development/self-host convenience, never a shared or production
#: deployment; the constraint is "README *and startup banner*" — this makes
#: that statement visible every time the process starts, not only when
#: ``allow_non_loopback`` happens to be set.
_STARTUP_BANNER = (
    "forge-cli-bridge: DEVELOPMENT / SELF-HOST CONVENIENCE ONLY. This process "
    "executes host CLI binaries (claude/codex) on behalf of any caller that "
    "reaches it. It is NOT for a shared or production deployment."
)


def _default_redactor() -> Callable[[str], str]:
    """The shared secret redactor (``forge_api``), or identity if unavailable.

    Mirrors ``forge_worker.agent_runner._default_redactor``. The bridge
    deliberately does not depend on ``forge-api`` (a heavy DB-backed app; this
    is meant to stay a small self-host binary) and instead reuses it
    opportunistically when the shared monorepo environment has it installed;
    otherwise raw (unscrubbed) CLI stderr would reach the host log, since
    ``LocalCliModelClient`` defaults its own ``redactor`` to identity.
    """
    try:
        from forge_api.observability.redaction import redact_text
    except Exception:
        # Fix round 2, minor: a standalone self-host install (precisely the
        # deployment this bridge targets) will not have forge_api installed,
        # so this is the COMMON case for an external user, not an edge case.
        # Degrading to identity silently would mean "no redaction, no
        # warning, raw CLI stderr in the host log" by default -- make the
        # downgrade visible instead of assumed.
        _logger.warning(
            "forge-api's shared secret redactor is not installed; CLI stderr "
            "will reach the host log UNREDACTED on failure. Install the full "
            "Forge monorepo environment, or inject your own redactor, to "
            "enable scrubbing."
        )
        return lambda value: value
    return redact_text


def create_app(settings: BridgeSettings) -> FastAPI:
    app = FastAPI(title="Forge CLI bridge", version="0.1.0")
    app.state.settings = settings
    app.state.runner = subprocess.run
    #: Injection point for detect_clis()'s own `which=`, so tests never depend
    #: on which CLIs happen to be installed on the host running them (Fix
    #: round 1, IMPORTANT: the suite must be hermetic).
    app.state.which = shutil.which
    app.state.redactor = _default_redactor()
    #: Fix round 1, minor: bounds concurrent CLI subprocess spawns. Each
    #: /v1/complete call can run for up to settings.timeout_s (default 600s)
    #: and spends real subscription quota; an unbounded burst is a plausible
    #: accident, not just an attack.
    app.state.completion_semaphore = asyncio.Semaphore(settings.max_concurrent_completions)

    _logger.warning(_STARTUP_BANNER)

    def _authorize(authorization: str = Header(default="")) -> None:
        expected = f"Bearer {settings.token}"
        # Fix round 1, minor: constant-time comparison. Not exploitable over
        # loopback (no network-observable timing channel), but it is free.
        #
        # Fix round 2, regression: hmac.compare_digest raises TypeError on a
        # non-ASCII str, and Starlette decodes header values as latin-1, so a
        # header like "Bearer t\xf6ken" used to reach here as a non-ASCII str
        # and turn an unhandled TypeError into an HTTP 500 (with a traceback
        # in the log) where the old plain `!=` returned a clean 401. Encoding
        # both sides to bytes first never raises for a latin-1-sourced str
        # (every latin-1 codepoint is representable in UTF-8) and keeps the
        # constant-time comparison property `compare_digest` provides.
        provided = authorization.encode("utf-8", "surrogateescape")
        wanted = expected.encode("utf-8", "surrogateescape")
        if not hmac.compare_digest(provided, wanted):
            raise HTTPException(status_code=401, detail="invalid or missing bridge token")

    @app.get("/health")
    def health() -> dict[str, object]:
        return {
            "status": "ok",
            "clis": [d.name.value for d in detect_clis(which=app.state.which)],
            # Fix round 2, IMPORTANT: this used to be "loopback_only": a claim
            # about the actual bound socket. This app object cannot verify
            # that claim -- under `uvicorn forge_cli_bridge.main:app --host
            # 0.0.0.0`, main.py's own bind (and its loopback guard) is
            # bypassed entirely, and the ASGI app has no visibility into what
            # --host/--port that external uvicorn invocation was given. A
            # health field asserting a property it has not checked is worse
            # than omitting it -- an operator relying on it to confirm
            # exposure would be actively misled. This reports only what IS
            # genuinely known from inside the process: whether THIS
            # BridgeSettings object's non-loopback override was set. It is
            # not a substitute for checking the actual listening socket
            # (e.g. `lsof -i :8099`) when running via the ASGI-import path.
            "non_loopback_override_set": settings.allow_non_loopback,
        }

    @app.post("/v1/complete", dependencies=[Depends(_authorize)])
    async def complete(request: ModelRequest) -> ModelResponse:
        detected = detect_clis(which=app.state.which)
        if not detected:
            return ModelResponse(content="", stop_reason="provider_unavailable")
        client = LocalCliModelClient(
            cli_name=CliName(detected[0].name),
            timeout_s=settings.timeout_s,
            redactor=app.state.redactor,
            runner=app.state.runner,
        )
        # The CLI subprocess call is blocking and can run for up to
        # settings.timeout_s; running it in a worker thread (rather than
        # directly in this async route) keeps the event loop free to serve
        # /health and other queued /v1/complete callers while this one waits.
        # The semaphore bounds how many such threads run at once.
        async with app.state.completion_semaphore:
            return await asyncio.to_thread(client.complete, request)

    if settings.allow_non_loopback:
        _logger.warning(
            "CLI bridge is bound to %s — it executes host binaries; do not expose it",
            settings.host,
        )
    return app
