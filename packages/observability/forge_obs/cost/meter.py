"""The single cost-emission point (F38 §4): usage -> money, exactly once.

``UsageMeter.record`` is the ONLY place token usage becomes cost: it resolves
the price, computes ``cost_usd``, idempotently upserts the ``cost_event`` row,
increments the Prometheus cost counters (only on first insert — a deduplicated
replay never double-counts), and returns the :class:`CostRecord` that F06's
step sink stamps onto the per-step trace. One emission, three sinks.

Guarded emission (spec AC7): a metric-export failure never raises; a
ledger-write failure increments ``forge_cost_emit_failures_total`` and
re-raises only in strict mode — cost/observability must never crash a run.
"""

from __future__ import annotations

import logging

from forge_obs.cost.models import COST_KINDS, CostRecord, ModelUsage
from forge_obs.cost.pricing import PriceBook, compute_cost
from forge_obs.cost.repository import CostLedger
from forge_obs.metrics import ForgeMetrics, get_metrics

__all__ = ["NoopUsageMeter", "UsageMeter"]

_log = logging.getLogger(__name__)


class UsageMeter:
    """Resolve price -> ledger upsert -> counters -> :class:`CostRecord`."""

    def __init__(
        self,
        *,
        ledger: CostLedger,
        price_book: PriceBook,
        metrics: ForgeMetrics | None = None,
        strict: bool = False,
    ) -> None:
        self._ledger = ledger
        self._price_book = price_book
        self._metrics = metrics
        self._strict = strict

    @property
    def _facade(self) -> ForgeMetrics:
        return self._metrics if self._metrics is not None else get_metrics()

    def record(self, usage: ModelUsage) -> CostRecord:
        """Turn one model call into cost. Idempotent on (workspace, request_id)."""
        if usage.kind not in COST_KINDS:
            raise ValueError(f"unknown cost kind {usage.kind!r} (allowed: {sorted(COST_KINDS)})")

        price = self._price_book.resolve(
            workspace_id=usage.workspace_id,
            provider=usage.provider,
            model=usage.model,
            kind=usage.kind,
            at=usage.occurred_at,
        )
        cost = compute_cost(usage, price)
        price_id = price.id if price is not None else None

        try:
            record = self._ledger.upsert_event(usage, cost=cost, price_id=price_id)
        except Exception:
            # The ledger is the authoritative billing record: a gap must be
            # alertable (forge_cost_emit_failures_total), never silent — but it
            # must not crash the calling run outside strict mode.
            self._safe(lambda m: m.record_cost_emit_failure(reason="ledger"))
            if self._strict:
                raise
            _log.exception("cost ledger write failed (non-strict; run continues)")
            return CostRecord(cost_usd=cost, priced=price is not None, price_id=price_id)

        if not record.deduplicated:
            if price is None:
                self._safe(
                    lambda m: m.record_unpriced_model(provider=usage.provider, model=usage.model)
                )
            self._safe(
                lambda m: m.record_model_cost(
                    provider=usage.provider,
                    model=usage.model,
                    kind=usage.kind,
                    phase=usage.phase,
                    prompt_tokens=usage.prompt_tokens,
                    completion_tokens=usage.completion_tokens,
                    cost_usd=float(record.cost_usd),
                )
            )
        return record

    def _safe(self, emit) -> None:
        """Metric export must never break cost emission (spec AC7)."""
        try:
            emit(self._facade)
        except Exception:
            _log.warning("metric emission failed (swallowed)", exc_info=True)


class NoopUsageMeter:
    """Degraded meter for contexts with no ledger at all (unit harnesses).

    NOTE: in the real degraded mode (``OBS_ENABLED=false``) cost is STILL
    persisted — the ledger is Postgres, not the metrics stack — so production
    wiring uses :class:`UsageMeter` with ``NoopMetrics``. This class is for
    callers that genuinely have no database (pure unit fixtures).
    """

    def record(self, usage: ModelUsage) -> CostRecord:
        del usage
        return CostRecord()
