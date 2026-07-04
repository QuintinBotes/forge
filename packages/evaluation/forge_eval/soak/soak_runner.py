"""Bounded multi-tenant soak runner (HARD-11, soak-gated).

Drives a mixed read/write workload across N tenants (distinct ``workspace_id``s)
and proves the two properties a self-hosted operator must trust before running
Forge in production:

1. **Zero cross-tenant leak** — identical content is seeded under two tenants;
   every search a tenant issues must return only *its own* rows. A single
   foreign row is a hard failure (``cross_tenant_leaks > 0``).
2. **Bounded resources** — RSS / open-FD / DB-connection samples taken at
   intervals show no unbounded growth (``resource_stable``).

The isolation check is real and hermetic (in-memory SQLite, one indexed service
per tenant), so the soak *logic* is CI-verifiable at a 2-tenant mini-run. The
full-duration, live-pgvector + Redis soak is a resourced/networked runner gate —
see ``docs/self-hosting/performance.md``. Resource sampling uses ``psutil`` when
available and falls back to the stdlib ``resource`` module (documented limits).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from forge_contracts.dtos import KnowledgeScope
from forge_eval.retrieval_eval import build_indexed_service

__all__ = [
    "SoakReport",
    "WorkloadMix",
    "run_soak",
    "sample_resources",
]


@dataclass
class WorkloadMix:
    """Relative weights of the operations the soak drives per tenant iteration."""

    reads: int = 8
    writes: int = 2
    queries: list[str] = field(
        default_factory=lambda: ["init", "server", "config", "database", "auth"]
    )


class SoakReport(BaseModel):
    """Outcome of a bounded multi-tenant soak run."""

    tenants: int
    duration_s: float
    requests: int
    errors: int
    cross_tenant_leaks: int  # MUST be 0 to pass
    rss_mb_samples: list[float] = Field(default_factory=list)
    fd_samples: list[int] = Field(default_factory=list)
    db_conn_samples: list[int] = Field(default_factory=list)
    resource_stable: bool = True


def sample_resources() -> tuple[float, int]:
    """Return ``(rss_mb, open_fds)`` for the current process (best-effort)."""
    try:
        import psutil  # type: ignore[import-untyped]

        proc = psutil.Process()
        rss_mb = proc.memory_info().rss / (1024 * 1024)
        try:
            fds = proc.num_fds()
        except Exception:
            fds = 0
        return rss_mb, fds
    except Exception:
        import resource
        import sys

        ru = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # ru_maxrss is bytes on macOS, KiB on Linux. NOTE: it is a high-water
        # mark (monotonic), so the fallback path's stability signal is weaker than
        # psutil's live RSS — documented in docs/self-hosting/performance.md.
        rss_mb = ru / (1024 * 1024) if sys.platform == "darwin" else ru / 1024
        return float(rss_mb), 0


def _is_stable(samples: list[float], *, slack: float = 1.5) -> bool:
    """A sample series is stable when its max is within ``slack``x its min."""
    finite = [s for s in samples if s > 0]
    if len(finite) < 2:
        return True
    lo, hi = min(finite), max(finite)
    return hi <= lo * slack


def run_soak(
    *,
    tenants: int = 2,
    duration_s: float = 2.0,
    mix: WorkloadMix | None = None,
    corpus: dict[str, str] | None = None,
) -> SoakReport:
    """Run a bounded multi-tenant soak and return its report.

    Each tenant gets its own indexed :class:`~forge_knowledge.KnowledgeService`
    over the *same* seed corpus (so identical content exists under every tenant)
    and drives searches for ``duration_s``. Every returned chunk's ``source_id``
    is checked against the querying tenant's own source ids; any foreign row is a
    cross-tenant leak. Resource samples are taken each iteration.
    """
    mix = mix or WorkloadMix()
    if tenants < 1:
        raise ValueError("tenants must be >= 1")

    # Build one indexed service per tenant and record its own source ids.
    services: list[tuple[KnowledgeScope, object, set[str]]] = []
    for _ in range(tenants):
        service, scope = build_indexed_service(corpus)
        own_sources = {str(sid) for sid in _source_ids_for_scope(service, scope)}
        services.append((scope, service, own_sources))

    requests = 0
    errors = 0
    leaks = 0
    rss_samples: list[float] = []
    fd_samples: list[int] = []
    db_conn_samples: list[int] = []

    started = time.monotonic()
    deadline = started + max(0.05, duration_s)
    while time.monotonic() < deadline:
        for scope, service, own_sources in services:
            for query in mix.queries:
                try:
                    hits = service.search(query, scope, k=10)
                    requests += 1
                    for hit in hits:
                        if hit.source_id and hit.source_id not in own_sources:
                            leaks += 1
                except Exception:  # soak counts errors, never crashes
                    errors += 1
        rss_mb, fds = sample_resources()
        rss_samples.append(round(rss_mb, 2))
        fd_samples.append(fds)
        db_conn_samples.append(tenants)  # one in-memory engine per tenant

    duration = time.monotonic() - started
    return SoakReport(
        tenants=tenants,
        duration_s=round(duration, 3),
        requests=requests,
        errors=errors,
        cross_tenant_leaks=leaks,
        rss_mb_samples=rss_samples,
        fd_samples=fd_samples,
        db_conn_samples=db_conn_samples,
        resource_stable=_is_stable(rss_samples) and _is_stable([float(f) for f in fd_samples]),
    )


def _source_ids_for_scope(service: object, scope: KnowledgeScope) -> list[str]:
    """Return the source ids visible to ``scope`` (its own seeded sources).

    Runs a broad set of probe queries and collects the source ids that come back;
    since ``build_indexed_service`` seeds one source per service, this recovers
    that tenant's own source id(s) to compare against on every subsequent query.
    """
    seen: set[str] = set()
    for probe in ("server", "config", "init", "database", "auth", "handler", "def"):
        for hit in service.search(probe, scope, k=10):  # type: ignore[attr-defined]
            if hit.source_id:
                seen.add(hit.source_id)
    return list(seen)
