"""Post-deploy health/verification checkers.

Every checker implements the :class:`HealthChecker` Protocol. All I/O (HTTP GET,
shell command) goes through an injected callable so tests use scripted doubles
and never touch the network or a shell. Synchronous to match the foundation.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Protocol, runtime_checkable

from forge_deploy.schemas import HealthCheckResult, HealthCheckSpec
from forge_deploy.states import HealthStatus


@runtime_checkable
class HealthChecker(Protocol):
    def check(self, spec: HealthCheckSpec, *, deployment_id: uuid.UUID) -> HealthCheckResult: ...


class NullHealthChecker:
    """Always-passing checker (used when ``health_check.kind == 'none'``)."""

    def check(self, spec: HealthCheckSpec, *, deployment_id: uuid.UUID) -> HealthCheckResult:
        return HealthCheckResult(
            status=HealthStatus.PASSING, attempts=0, detail="no health check configured"
        )


class ScriptedHealthChecker:
    """Test double returning a scripted sequence of pass/fail outcomes."""

    def __init__(self, results: list[bool]) -> None:
        self._results = list(results)
        self._idx = 0

    def check(self, spec: HealthCheckSpec, *, deployment_id: uuid.UUID) -> HealthCheckResult:
        ok = self._results[min(self._idx, len(self._results) - 1)]
        self._idx += 1
        return HealthCheckResult(
            status=HealthStatus.PASSING if ok else HealthStatus.FAILING,
            attempts=1,
            detail="scripted pass" if ok else "scripted fail",
        )


class HttpHealthChecker:
    """Polls an HTTP endpoint, retrying up to ``spec.retries`` times.

    ``getter`` is an injected ``(url) -> status_code`` callable.
    """

    def __init__(self, getter: Callable[[str], int]) -> None:
        self._getter = getter

    def check(self, spec: HealthCheckSpec, *, deployment_id: uuid.UUID) -> HealthCheckResult:
        if not spec.url:
            return HealthCheckResult(
                status=HealthStatus.UNKNOWN, attempts=0, detail="no url configured"
            )
        attempts = 0
        last = "no attempts"
        for _ in range(max(1, spec.retries)):
            attempts += 1
            try:
                code = self._getter(spec.url)
            except Exception as exc:
                last = f"error: {exc}"
                continue
            if code == spec.expect_status:
                return HealthCheckResult(
                    status=HealthStatus.PASSING,
                    attempts=attempts,
                    detail=f"GET {spec.url} -> {code}",
                )
            last = f"GET {spec.url} -> {code} (want {spec.expect_status})"
        return HealthCheckResult(status=HealthStatus.FAILING, attempts=attempts, detail=last)


class CommandHealthChecker:
    """Runs a command, mapping exit code 0 -> passing.

    ``runner`` is an injected ``(command) -> exit_code`` callable.
    """

    def __init__(self, runner: Callable[[str], int]) -> None:
        self._runner = runner

    def check(self, spec: HealthCheckSpec, *, deployment_id: uuid.UUID) -> HealthCheckResult:
        if not spec.command:
            return HealthCheckResult(
                status=HealthStatus.UNKNOWN, attempts=0, detail="no command configured"
            )
        attempts = 0
        last = "no attempts"
        for _ in range(max(1, spec.retries)):
            attempts += 1
            try:
                code = self._runner(spec.command)
            except Exception as exc:
                last = f"error: {exc}"
                continue
            if code == 0:
                return HealthCheckResult(
                    status=HealthStatus.PASSING,
                    attempts=attempts,
                    detail=f"`{spec.command}` exited 0",
                )
            last = f"`{spec.command}` exited {code}"
        return HealthCheckResult(status=HealthStatus.FAILING, attempts=attempts, detail=last)


__all__ = [
    "CommandHealthChecker",
    "HealthChecker",
    "HttpHealthChecker",
    "NullHealthChecker",
    "ScriptedHealthChecker",
]
