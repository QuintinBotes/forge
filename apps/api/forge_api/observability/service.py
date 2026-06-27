"""Observability service: glue for the /observability/* API routes (Task 1.14).

Combines the immutable :class:`AuditLog`, the :class:`RunTraceAssembler`, and the
OTel :class:`SpanRecorder` behind one object the router depends on. Runs are
registered here (in memory for Phase 1; Phase-2 wiring reads ``AgentRun.steps``
from Postgres) so the trace viewer can reconstruct a step-level trace by run id.
"""

from __future__ import annotations

import uuid
from typing import Any

from forge_api.observability.audit import AuditCategory, AuditEntry, AuditLog
from forge_api.observability.otel import SpanRecorder, get_span_recorder
from forge_api.observability.trace import RunTrace, RunTraceAssembler
from forge_contracts import AgentRunResult, Step
from forge_contracts.enums import RunStatus


class RunNotFoundError(KeyError):
    """Raised when a run trace is requested for an unknown run id."""


class ObservabilityService:
    """Facade combining audit log, run-trace assembly, and span recording."""

    def __init__(
        self,
        *,
        audit_log: AuditLog | None = None,
        assembler: RunTraceAssembler | None = None,
        recorder: SpanRecorder | None = None,
    ) -> None:
        self.audit = audit_log or AuditLog()
        self.assembler = assembler or RunTraceAssembler()
        self.recorder = recorder or get_span_recorder()
        self._runs: dict[uuid.UUID, RunTrace] = {}

    def record_run(
        self,
        run_id: uuid.UUID,
        *,
        steps: list[Step] | None = None,
        result: AgentRunResult | None = None,
        status: RunStatus | None = None,
        confidence: float | None = None,
        subagent_steps: dict[str, list[Step]] | None = None,
    ) -> RunTrace:
        """Assemble and register a run's trace, keyed by run id."""
        if result is not None:
            trace = self.assembler.from_agent_result(result)
            key = result.run_id or run_id
        else:
            trace = self.assembler.assemble(
                run_id,
                steps or [],
                status=status,
                confidence=confidence,
                subagent_steps=subagent_steps,
            )
            key = run_id
        self._runs[key] = trace
        return trace

    def get_run_trace(self, run_id: uuid.UUID) -> RunTrace:
        try:
            return self._runs[run_id]
        except KeyError as exc:
            raise RunNotFoundError(run_id) from exc

    def query_audit(
        self,
        *,
        category: AuditCategory | None = None,
        actor: str | None = None,
        run_id: uuid.UUID | None = None,
        connection_id: str | None = None,
        limit: int | None = None,
    ) -> list[AuditEntry]:
        return self.audit.query(
            category=category,
            actor=actor,
            run_id=run_id,
            connection_id=connection_id,
            limit=limit,
        )

    def verify_audit_integrity(self) -> bool:
        return self.audit.verify_integrity()

    def span(self, name: str, attributes: dict[str, Any] | None = None) -> Any:
        """Open an OTel span bound to this service's recorder."""
        from forge_api.observability.otel import span as _span

        return _span(name, attributes, recorder=self.recorder)


_default_service: ObservabilityService | None = None


def get_observability_service() -> ObservabilityService:
    """FastAPI dependency returning the process-wide observability service.

    Overridable via ``app.dependency_overrides`` for isolated tests.
    """
    global _default_service
    if _default_service is None:
        _default_service = ObservabilityService()
    return _default_service
