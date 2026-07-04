"""F38 observability worker tasks: ``cost.reprice`` + ``obs.refresh_freshness_gauges``.

``cost.reprice`` re-computes ``cost_usd`` for historical ``cost_event`` rows
whose ``(provider, model, kind)`` price changed (Journey G), idempotently, and
writes one immutable ``cost.repriced`` audit row (the worker writes
:class:`AuditLog` rows directly — it must not import ``forge_api``; same
precedent as the F30 authz purge task).

``obs.refresh_freshness_gauges`` samples ``forge_mcp_freshness_lag_seconds``
per MCP connection from each MCP-backed knowledge source's last sync timestamp
(freshness lag is a sampled gauge, not an event). Runs on Celery beat.

Deterministic cores (``run_reprice`` / ``run_refresh_freshness``) take an
injected session factory + metrics facade so they unit-test hermetically; the
Celery wrappers only wire the environment.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from forge_db.models import AuditLog, KnowledgeSource, MCPConnection
from forge_db.session import create_session_factory
from forge_obs.cost.pricing import DbPriceBook, PriceBook
from forge_obs.cost.repository import SqlCostLedger
from forge_obs.metrics import ForgeMetrics, get_metrics
from forge_worker.celery_app import celery_app

__all__ = [
    "FRESHNESS_TASK",
    "REPRICE_TASK",
    "refresh_freshness_gauges",
    "reprice_cost",
    "run_refresh_freshness",
    "run_reprice",
]

REPRICE_TASK = "cost.reprice"
FRESHNESS_TASK = "obs.refresh_freshness_gauges"


def run_reprice(
    session_factory: sessionmaker[Session],
    *,
    workspace_id: uuid.UUID,
    since: datetime,
    provider: str | None = None,
    model: str | None = None,
    actor_id: uuid.UUID | None = None,
    price_book: PriceBook | None = None,
) -> int:
    """Reprice + audit (idempotent: a re-run updates 0 rows, audits the 0)."""
    ledger = SqlCostLedger(session_factory)
    book = price_book or DbPriceBook(session_factory)
    updated = ledger.reprice(
        workspace_id=workspace_id, since=since, provider=provider, model=model, price_book=book
    )
    with session_factory() as session:
        session.add(
            AuditLog(
                workspace_id=workspace_id,
                action="cost.repriced",
                actor_id=actor_id,
                actor_type="system" if actor_id is None else "user",
                target_type="cost_event",
                details={
                    "since": since.isoformat(),
                    "provider": provider,
                    "model": model,
                    "updated": updated,
                },
            )
        )
        session.commit()
    return updated


@celery_app.task(name=REPRICE_TASK)
def reprice_cost(
    workspace_id: str,
    since_iso: str,
    provider: str | None = None,
    model: str | None = None,
    actor_id: str | None = None,
) -> dict:
    """Celery seam over :func:`run_reprice` (queue ``observability``)."""
    factory = create_session_factory()
    updated = run_reprice(
        factory,
        workspace_id=uuid.UUID(workspace_id),
        since=datetime.fromisoformat(since_iso),
        provider=provider,
        model=model,
        actor_id=uuid.UUID(actor_id) if actor_id else None,
    )
    return {"workspace_id": workspace_id, "updated": updated}


def run_refresh_freshness(
    session_factory: sessionmaker[Session],
    *,
    metrics: ForgeMetrics | None = None,
    now: datetime | None = None,
) -> dict[str, float]:
    """Set ``forge_mcp_freshness_lag_seconds{connection}`` per MCP connection.

    A connection's lag is measured from the most-recent ``last_synced_at``
    across its MCP-backed knowledge sources; never-synced sources are skipped
    (no gauge is better than a fake infinite one).
    """
    facade = metrics if metrics is not None else get_metrics()
    now = now or datetime.now(UTC)
    lags: dict[str, float] = {}
    with session_factory() as session:
        rows = session.execute(
            select(MCPConnection.slug, KnowledgeSource.last_synced_at)
            .join(KnowledgeSource, KnowledgeSource.mcp_connection_id == MCPConnection.id)
            .where(KnowledgeSource.last_synced_at.is_not(None))
        ).all()
    for slug, synced_at in rows:
        if synced_at.tzinfo is None:
            synced_at = synced_at.replace(tzinfo=UTC)
        lag = max((now - synced_at).total_seconds(), 0.0)
        if slug not in lags or lag < lags[slug]:
            lags[slug] = lag
    for slug, lag in lags.items():
        facade.set_mcp_freshness_lag(connection=slug, lag_seconds=lag)
    return lags


@celery_app.task(name=FRESHNESS_TASK)
def refresh_freshness_gauges() -> dict:
    """Celery beat seam over :func:`run_refresh_freshness` (every 60s)."""
    factory = create_session_factory()
    lags = run_refresh_freshness(factory)
    return {"connections": len(lags)}
