"""Health checker behaviour — http retries, command exit code, null."""

from __future__ import annotations

import uuid

from forge_deploy.health import (
    CommandHealthChecker,
    HttpHealthChecker,
    NullHealthChecker,
)
from forge_deploy.schemas import HealthCheckSpec
from forge_deploy.states import HealthStatus

DID = uuid.uuid4()


def test_null_always_passes() -> None:
    result = NullHealthChecker().check(HealthCheckSpec(), deployment_id=DID)
    assert result.status == HealthStatus.PASSING


def test_http_passes_on_expected_status() -> None:
    checker = HttpHealthChecker(lambda url: 200)
    result = checker.check(
        HealthCheckSpec(kind="http", url="https://x/healthz", expect_status=200),
        deployment_id=DID,
    )
    assert result.status == HealthStatus.PASSING


def test_http_retries_then_fails() -> None:
    calls = {"n": 0}

    def getter(url: str) -> int:
        calls["n"] += 1
        return 503

    checker = HttpHealthChecker(getter)
    result = checker.check(
        HealthCheckSpec(kind="http", url="https://x/healthz", retries=3),
        deployment_id=DID,
    )
    assert result.status == HealthStatus.FAILING
    assert result.attempts == 3
    assert calls["n"] == 3


def test_http_recovers_within_retries() -> None:
    seq = iter([500, 200])
    checker = HttpHealthChecker(lambda url: next(seq))
    result = checker.check(
        HealthCheckSpec(kind="http", url="https://x", retries=3), deployment_id=DID
    )
    assert result.status == HealthStatus.PASSING
    assert result.attempts == 2


def test_command_maps_exit_code() -> None:
    ok = CommandHealthChecker(lambda cmd: 0).check(
        HealthCheckSpec(kind="command", command="true"), deployment_id=DID
    )
    bad = CommandHealthChecker(lambda cmd: 1).check(
        HealthCheckSpec(kind="command", command="false", retries=1), deployment_id=DID
    )
    assert ok.status == HealthStatus.PASSING
    assert bad.status == HealthStatus.FAILING
