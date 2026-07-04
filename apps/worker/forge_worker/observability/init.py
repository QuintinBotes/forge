"""Worker telemetry init + the process ``UsageMeter`` (HARD-10 §3.3).

``setup_worker_telemetry`` is the single call the Celery ``worker_process_init``
signal makes: it installs the env-driven telemetry providers (real OTLP export +
W3C trace-context propagation when ``OBS_ENABLED=true`` and an endpoint resolves,
a true no-op otherwise) and re-applies the canonical secret redaction at the log
sink so worker logs never leak a BYOK key.

``build_usage_meter`` constructs the ONE cost-emission point per process: a
:class:`~forge_obs.cost.repository.SqlCostLedger` over the durable ``cost_event``
table + a :class:`~forge_obs.cost.pricing.DbPriceBook` + the process
:class:`~forge_obs.metrics.ForgeMetrics` facade. The agent runner wraps every
model / embedding / rerank call so ``client.call(...) -> UsageMeter.record(...)``
is the single place token usage becomes money — idempotent on
``(workspace_id, request_id)`` and guarded so a ledger/metric failure never
aborts a run.
"""

from __future__ import annotations

from forge_obs.cost.meter import UsageMeter
from forge_obs.cost.pricing import DbPriceBook, PriceBook
from forge_obs.cost.repository import CostLedger, SqlCostLedger
from forge_obs.metrics import ForgeMetrics, get_metrics
from forge_obs.settings import ObsSettings
from forge_obs.telemetry import Telemetry, setup_telemetry

__all__ = ["build_usage_meter", "setup_worker_telemetry"]

_SERVICE = "forge-worker"


def setup_worker_telemetry(settings: ObsSettings | None = None) -> Telemetry:
    """Install worker telemetry (idempotent) and the log-sink redaction filter."""
    handle = setup_telemetry(_SERVICE, settings)
    # Structural redaction at the sink (identical to the API), not call-site
    # discipline — lazily imported so the module stays import-light for the
    # hermetic task-inspection path.
    try:  # pragma: no cover - depends on forge_api being importable in-process
        from forge_api.observability.redaction import install_log_redaction

        install_log_redaction()
    except Exception:
        pass
    return handle


def build_usage_meter(
    session_factory=None,
    *,
    ledger: CostLedger | None = None,
    price_book: PriceBook | None = None,
    metrics: ForgeMetrics | None = None,
    strict: bool = False,
) -> UsageMeter:
    """Construct the process ``UsageMeter`` over the durable cost ledger.

    ``session_factory`` (a SQLAlchemy ``sessionmaker``) backs the default SQL
    ledger + DB price book; callers may inject a ledger/price_book/metrics
    directly (tests). ``strict`` re-raises on a ledger-write failure instead of
    swallowing it (default guarded — observability must never crash a run).
    """
    if ledger is None or price_book is None:
        if session_factory is None:
            from forge_db.session import create_session_factory

            session_factory = create_session_factory()
        ledger = ledger or SqlCostLedger(session_factory)
        price_book = price_book or DbPriceBook(session_factory)
    return UsageMeter(
        ledger=ledger,
        price_book=price_book,
        metrics=metrics or get_metrics(),
        strict=strict,
    )
