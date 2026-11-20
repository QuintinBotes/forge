"""OpenTelemetry GenAI spans for agent runs, model calls and tool calls (#213).

Span names and every ``gen_ai.*`` attribute name come from one pinned snapshot of
the GenAI semantic conventions (FR-014), declared here beside the pin. SCF's own
names sit under ``scf.*``: the conventions define no cost attribute, so cost is
never a ``gen_ai.*`` one (FR-015).

Each span lands in the always-on in-memory store (:func:`scf_obs.tracing.traced`)
and, when the OpenTelemetry API is importable, on the installed tracer as well (a
no-op when no exporter is configured). Names and attributes are secret-redacted;
no prompt, completion, tool argument or tool output is captured, and an
exception's message is never recorded (FR-016). Recording is best effort: a
failure is logged, never raised into the run (FR-017).
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from types import ModuleType
from typing import Any

from scf_contracts import TokenUsage
from scf_obs.redaction import redact_text, redact_value
from scf_obs.tracing import Span, traced

_otel_trace: ModuleType | None
try:  # pragma: no cover - trivial import guard
    from opentelemetry import trace as _otel_trace
except ImportError:  # pragma: no cover - API genuinely absent
    _otel_trace = None

_log = logging.getLogger(__name__)

#: The conventions snapshot SCF follows. The repository had no tag or release when
#: read, so the pin is a commit (ADR-0016).
SEMCONV_REPOSITORY = "open-telemetry/semantic-conventions-genai"
SEMCONV_COMMIT = "622b2b6073b15e5ed886c3c12a65a892b9cd5c70"
SEMCONV_READ_ON = "2026-11-20"

GEN_AI_OPERATION_NAME = "gen_ai.operation.name"
GEN_AI_AGENT_NAME = "gen_ai.agent.name"
GEN_AI_PROVIDER_NAME = "gen_ai.provider.name"
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
GEN_AI_RESPONSE_MODEL = "gen_ai.response.model"
GEN_AI_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
GEN_AI_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
GEN_AI_TOOL_NAME = "gen_ai.tool.name"
#: Every ``gen_ai.*`` attribute SCF emits; a test fails on any other.
GEN_AI_ATTRIBUTES = frozenset(
    {
        GEN_AI_OPERATION_NAME,
        GEN_AI_AGENT_NAME,
        GEN_AI_PROVIDER_NAME,
        GEN_AI_REQUEST_MODEL,
        GEN_AI_RESPONSE_MODEL,
        GEN_AI_USAGE_INPUT_TOKENS,
        GEN_AI_USAGE_OUTPUT_TOKENS,
        GEN_AI_TOOL_NAME,
    }
)

#: The stable core attribute for an operation that ended in an error.
ERROR_TYPE = "error.type"
SCF_RUN_ID = "scf.run.id"
SCF_WORKSPACE_ID = "scf.workspace.id"
#: US dollars; absent when the model has no known price.
SCF_COST_USD = "scf.cost.usd"

#: ``gen_ai.operation.name`` values.
INVOKE_AGENT = "invoke_agent"
INVOKE_WORKFLOW = "invoke_workflow"
CHAT = "chat"
EXECUTE_TOOL = "execute_tool"


@dataclass
class _RunTotals:
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0  # model calls that reported usage
    cost_usd: float | None = 0.0  # None once such a call had no known price


_TOTALS: ContextVar[_RunTotals | None] = ContextVar("scf_obs_genai_totals", default=None)
#: Fan-out roles end concurrently on pool threads and add into one workflow's totals.
_TOTALS_LOCK = threading.Lock()


def _add(totals: _RunTotals, other: _RunTotals) -> None:
    with _TOTALS_LOCK:
        totals.calls += other.calls
        totals.input_tokens += other.input_tokens
        totals.output_tokens += other.output_tokens
        if totals.cost_usd is not None:
            totals.cost_usd = None if other.cost_usd is None else totals.cost_usd + other.cost_usd


def _tracer() -> Any:
    return _otel_trace.get_tracer(__name__) if _otel_trace is not None else None


def set_attributes(span: Span, attributes: Mapping[str, Any]) -> None:
    """Set redacted ``attributes`` on ``span``; a ``None`` value is left out."""
    try:
        present = {key: value for key, value in attributes.items() if value is not None}
        span.attributes.update(redact_value(present))
    except Exception:
        _log.warning("could not set GenAI span attributes", exc_info=True)


def record_error(span: Span, error_type: str) -> None:
    """Mark ``span`` failed with a low-cardinality ``error.type``."""
    span.status = "error"
    set_attributes(span, {ERROR_TYPE: error_type})


@contextmanager
def _span(
    operation: str, target: str | None, attributes: Mapping[str, Any], *, client: bool = False
) -> Iterator[Span]:
    """``{operation} {target}`` in the in-memory store and on the OTel tracer."""
    name = f"{operation} {redact_text(target)}" if target else operation
    stack = ExitStack()
    try:
        span = stack.enter_context(traced(name))
    except Exception:
        _log.warning("could not open GenAI span %s", name, exc_info=True)
        span = Span(name=name, trace_id="", span_id="")
    set_attributes(span, {GEN_AI_OPERATION_NAME: operation, **attributes})
    otel = None
    try:
        tracer = _tracer()
        if tracer is not None and _otel_trace is not None:
            kind = _otel_trace.SpanKind.CLIENT if client else _otel_trace.SpanKind.INTERNAL
            otel = stack.enter_context(
                # Off: both would record the exception's message, which may hold content.
                tracer.start_as_current_span(
                    name, kind=kind, record_exception=False, set_status_on_exception=False
                )
            )
            # Logs stamp the in-memory ids: make them the exported span's.
            context = otel.get_span_context()
            if context.is_valid:
                span.trace_id = format(context.trace_id, "032x")
                span.span_id = format(context.span_id, "016x")
    except Exception:
        _log.warning("could not start OTel span %s", name, exc_info=True)
    try:
        yield span
    except BaseException as exc:
        record_error(span, type(exc).__name__)
        _end(stack, span, otel, exc)
        raise
    _end(stack, span, otel, None)


def _end(stack: ExitStack, span: Span, otel: Any, exc: BaseException | None) -> None:
    try:
        if otel is not None and _otel_trace is not None:
            otel.set_attributes(span.attributes)
            if span.status == "error":
                otel.set_status(_otel_trace.StatusCode.ERROR)
        if exc is None:
            stack.close()
        else:
            stack.__exit__(type(exc), exc, exc.__traceback__)
    except Exception:
        _log.warning("could not record GenAI span %s", span.name, exc_info=True)


@contextmanager
def agent_span(
    agent_name: str | None = None,
    *,
    run_id: uuid.UUID | None = None,
    workspace_id: uuid.UUID | None = None,
    operation: str = INVOKE_AGENT,
) -> Iterator[Span]:
    """The agent (or ``invoke_workflow``) span; it ends carrying the run's totals.

    A nested agent span adds its totals into the enclosing one's when it ends.
    """
    parent = _TOTALS.get()
    totals = _RunTotals()
    token = _TOTALS.set(totals)
    attributes = {
        GEN_AI_AGENT_NAME: agent_name,
        SCF_RUN_ID: str(run_id) if run_id else None,
        SCF_WORKSPACE_ID: str(workspace_id) if workspace_id else None,
    }
    try:
        with _span(operation, agent_name, attributes) as span:
            try:
                yield span
            finally:
                if totals.calls:
                    set_attributes(
                        span,
                        {
                            GEN_AI_USAGE_INPUT_TOKENS: totals.input_tokens,
                            GEN_AI_USAGE_OUTPUT_TOKENS: totals.output_tokens,
                            SCF_COST_USD: totals.cost_usd,
                        },
                    )
    finally:
        _TOTALS.reset(token)
        if parent is not None and totals.calls:
            _add(parent, totals)


@contextmanager
def inference_span(*, provider: str, model: str | None) -> Iterator[Span]:
    """One model call: ``chat {gen_ai.request.model}``, kind CLIENT."""
    attributes = {GEN_AI_PROVIDER_NAME: provider, GEN_AI_REQUEST_MODEL: model}
    with _span(CHAT, model, attributes, client=True) as span:
        yield span


def record_inference(
    span: Span, *, response_model: str | None, usage: TokenUsage | None, cost_usd: float | None
) -> None:
    """Stamp a model call's usage and cost onto ``span`` and its agent span's totals."""
    reported = (
        {}
        if usage is None
        else {
            GEN_AI_USAGE_INPUT_TOKENS: usage.input_tokens,
            GEN_AI_USAGE_OUTPUT_TOKENS: usage.output_tokens,
            SCF_COST_USD: cost_usd,
        }
    )
    set_attributes(span, {GEN_AI_RESPONSE_MODEL: response_model, **reported})
    totals = _TOTALS.get()
    if usage is not None and totals is not None:
        _add(totals, _RunTotals(usage.input_tokens, usage.output_tokens, 1, cost_usd))


@contextmanager
def tool_span(tool_name: str) -> Iterator[Span]:
    """One executed tool call: ``execute_tool {gen_ai.tool.name}``."""
    with _span(EXECUTE_TOOL, tool_name, {GEN_AI_TOOL_NAME: tool_name}) as span:
        yield span


__all__ = [
    "CHAT",
    "ERROR_TYPE",
    "EXECUTE_TOOL",
    "GEN_AI_AGENT_NAME",
    "GEN_AI_ATTRIBUTES",
    "GEN_AI_OPERATION_NAME",
    "GEN_AI_PROVIDER_NAME",
    "GEN_AI_REQUEST_MODEL",
    "GEN_AI_RESPONSE_MODEL",
    "GEN_AI_TOOL_NAME",
    "GEN_AI_USAGE_INPUT_TOKENS",
    "GEN_AI_USAGE_OUTPUT_TOKENS",
    "INVOKE_AGENT",
    "INVOKE_WORKFLOW",
    "SCF_COST_USD",
    "SCF_RUN_ID",
    "SCF_WORKSPACE_ID",
    "SEMCONV_COMMIT",
    "SEMCONV_READ_ON",
    "SEMCONV_REPOSITORY",
    "agent_span",
    "inference_span",
    "record_error",
    "record_inference",
    "set_attributes",
    "tool_span",
]
