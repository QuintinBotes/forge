"""HARD-11: multi-tenant soak (isolation hermetic; full-duration run gated)."""

from __future__ import annotations

import os

import pytest

from forge_eval.soak.soak_runner import (
    SoakReport,
    WorkloadMix,
    run_soak,
    sample_resources,
)

# --------------------------------------------------------------------------- #
# Isolation + resource logic (hermetic 2-tenant mini-run, always verifiable)   #
# --------------------------------------------------------------------------- #


def test_soak_mini_run_has_zero_cross_tenant_leak() -> None:
    report = run_soak(tenants=2, duration_s=0.3)
    assert isinstance(report, SoakReport)
    assert report.tenants == 2
    assert report.requests > 0
    # The security-critical invariant: identical content under two tenants, and
    # no tenant ever sees another's rows.
    assert report.cross_tenant_leaks == 0
    assert report.errors == 0


def test_soak_reports_resource_samples() -> None:
    report = run_soak(tenants=2, duration_s=0.3)
    assert len(report.rss_mb_samples) >= 1
    assert len(report.db_conn_samples) >= 1
    # A tiny run keeps memory flat -> resource_stable holds.
    assert report.resource_stable is True


def test_sample_resources_returns_tuple() -> None:
    rss_mb, fds = sample_resources()
    assert rss_mb >= 0.0
    assert fds >= 0


def test_workload_mix_defaults() -> None:
    mix = WorkloadMix()
    assert mix.reads > 0 and mix.writes > 0
    assert mix.queries


def test_run_soak_rejects_zero_tenants() -> None:
    with pytest.raises(ValueError):
        run_soak(tenants=0)


# --------------------------------------------------------------------------- #
# Full-duration soak — gated (runner + FORGE_RUN_SOAK=1)                       #
# --------------------------------------------------------------------------- #


@pytest.mark.soak
def test_full_soak_isolation_and_stability() -> None:
    if not os.environ.get("FORGE_RUN_SOAK"):
        pytest.skip(
            "PARKED: set FORGE_RUN_SOAK=1 on a resourced runner to run the "
            "full-duration multi-tenant soak (FORGE_SOAK_TENANTS / "
            "FORGE_SOAK_DURATION_SECONDS); see docs/self-hosting/performance.md."
        )
    tenants = int(os.environ.get("FORGE_SOAK_TENANTS", "5"))
    duration = float(os.environ.get("FORGE_SOAK_DURATION_SECONDS", "30"))
    report = run_soak(tenants=tenants, duration_s=duration)
    assert report.cross_tenant_leaks == 0
    assert report.resource_stable is True
    assert report.errors == 0
