"""GenAI span helpers: the pin, span shape, cost, redaction and failure isolation (#213)."""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("opentelemetry.sdk.trace")

from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

from scf_contracts import TokenUsage
from scf_obs import genai
from scf_obs.logging import JsonLogFormatter
from scf_obs.tracing import get_span_store

RUN_ID = uuid.uuid4()
WORKSPACE_ID = uuid.uuid4()
SECRET = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"


@pytest.fixture
def exported(monkeypatch: pytest.MonkeyPatch) -> Iterator[InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(genai, "_tracer", lambda: provider.get_tracer("test"))
    get_span_store().clear()
    yield exporter
    get_span_store().clear()


def _attrs(span: ReadableSpan) -> dict[str, Any]:
    return dict(span.attributes or {})


def _by_name(exporter: InMemorySpanExporter) -> dict[str, ReadableSpan]:
    return {span.name: span for span in exporter.get_finished_spans()}


def test_the_pin_names_a_commit_of_the_genai_repository_and_adr_0016_names_it() -> None:
    assert genai.SEMCONV_REPOSITORY == "open-telemetry/semantic-conventions-genai"
    assert re.fullmatch(r"[0-9a-f]{40}", genai.SEMCONV_COMMIT)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", genai.SEMCONV_READ_ON)
    adr = Path(__file__).resolve().parents[3] / "docs/architecture/adr"
    text = (adr / "0016-interoperability-standards-are-pinned.md").read_text()
    assert genai.SEMCONV_COMMIT in text
    assert genai.SEMCONV_REPOSITORY in text
    declared = {v for k, v in vars(genai).items() if k.startswith("GEN_AI_") and isinstance(v, str)}
    assert declared == genai.GEN_AI_ATTRIBUTES


def test_agent_span_parents_inference_and_tool_spans_in_one_trace(
    exported: InMemorySpanExporter,
) -> None:
    with genai.agent_span(run_id=RUN_ID, workspace_id=WORKSPACE_ID):
        with genai.inference_span(provider="anthropic", model="claude-sonnet-4-6") as call:
            genai.record_inference(
                call,
                response_model="claude-sonnet-4-6",
                usage=TokenUsage(input_tokens=10, output_tokens=4),
                cost_usd=0.5,
            )
        with genai.tool_span("read_file"):
            pass

    spans = _by_name(exported)
    agent, chat = spans["invoke_agent"], spans["chat claude-sonnet-4-6"]
    tool = spans["execute_tool read_file"]
    assert agent.kind is tool.kind is SpanKind.INTERNAL
    assert chat.kind is SpanKind.CLIENT
    assert _attrs(agent)[genai.GEN_AI_OPERATION_NAME] == "invoke_agent"
    assert _attrs(agent)[genai.SCF_RUN_ID] == str(RUN_ID)
    assert _attrs(agent)[genai.SCF_WORKSPACE_ID] == str(WORKSPACE_ID)
    assert _attrs(chat)[genai.GEN_AI_OPERATION_NAME] == "chat"
    assert _attrs(chat)[genai.GEN_AI_PROVIDER_NAME] == "anthropic"
    assert _attrs(chat)[genai.GEN_AI_REQUEST_MODEL] == "claude-sonnet-4-6"
    assert _attrs(chat)[genai.GEN_AI_USAGE_INPUT_TOKENS] == 10
    assert _attrs(chat)[genai.GEN_AI_USAGE_OUTPUT_TOKENS] == 4
    assert _attrs(tool)[genai.GEN_AI_OPERATION_NAME] == "execute_tool"
    assert _attrs(tool)[genai.GEN_AI_TOOL_NAME] == "read_file"
    for child in (chat, tool):
        assert child.parent is not None
        assert child.parent.span_id == agent.context.span_id
        assert child.context.trace_id == agent.context.trace_id
    # The in-memory store holds the same tree.
    stored = {span.name: span for span in get_span_store().spans()}
    assert stored["chat claude-sonnet-4-6"].parent_span_id == stored["invoke_agent"].span_id
    assert stored["execute_tool read_file"].trace_id == stored["invoke_agent"].trace_id
    # The agent span sums the run's usage and priced cost.
    assert _attrs(agent)[genai.GEN_AI_USAGE_INPUT_TOKENS] == 10
    assert _attrs(agent)[genai.SCF_COST_USD] == 0.5
    keys = {key for span in exported.get_finished_spans() for key in _attrs(span)}
    assert {key for key in keys if key.startswith("gen_ai.")} <= genai.GEN_AI_ATTRIBUTES


def test_a_log_line_inside_an_exported_span_carries_that_spans_trace_id(
    exported: InMemorySpanExporter,
) -> None:
    record = logging.LogRecord("scf.test", logging.INFO, __file__, 1, "inside", None, None)
    with genai.agent_span(run_id=RUN_ID, workspace_id=WORKSPACE_ID):
        line = json.loads(JsonLogFormatter("test").format(record))

    (agent,) = exported.get_finished_spans()
    assert line["trace_id"] == format(agent.context.trace_id, "032x")
    assert line["span_id"] == format(agent.context.span_id, "016x")


@pytest.mark.parametrize(("reviewer_cost", "total"), [(0.25, 0.5), (None, None)])
def test_a_workflow_span_carries_the_sum_of_its_role_spans(
    exported: InMemorySpanExporter, reviewer_cost: float | None, total: float | None
) -> None:
    with genai.agent_span(run_id=RUN_ID, operation=genai.INVOKE_WORKFLOW):
        for role, cost in (("implementer", 0.25), ("reviewer", reviewer_cost)):
            with genai.agent_span(role), genai.inference_span(provider="p", model="m") as call:
                usage = TokenUsage(input_tokens=10, output_tokens=2)
                genai.record_inference(call, response_model=None, usage=usage, cost_usd=cost)

    workflow = _attrs(_by_name(exported)["invoke_workflow"])
    assert workflow[genai.GEN_AI_USAGE_INPUT_TOKENS] == 20
    assert workflow[genai.GEN_AI_USAGE_OUTPUT_TOKENS] == 4
    assert workflow.get(genai.SCF_COST_USD) == total


def test_an_unpriced_call_carries_no_cost_and_leaves_the_run_total_absent(
    exported: InMemorySpanExporter,
) -> None:
    with genai.agent_span(run_id=RUN_ID, workspace_id=WORKSPACE_ID):
        with genai.inference_span(provider="openai", model="priced") as priced:
            genai.record_inference(
                priced, response_model=None, usage=TokenUsage(input_tokens=1), cost_usd=0.25
            )
        with genai.inference_span(provider="openai", model="unpriced") as unpriced:
            genai.record_inference(
                unpriced, response_model=None, usage=TokenUsage(input_tokens=1), cost_usd=None
            )

    spans = _by_name(exported)
    assert _attrs(spans["chat priced"])[genai.SCF_COST_USD] == 0.25
    assert genai.SCF_COST_USD not in _attrs(spans["chat unpriced"])
    assert genai.SCF_COST_USD not in _attrs(spans["invoke_agent"])
    for span in exported.get_finished_spans():
        assert not [key for key in _attrs(span) if key.startswith("gen_ai.") and "cost" in key]


def test_secret_shaped_values_are_redacted_from_names_and_attributes(
    exported: InMemorySpanExporter,
) -> None:
    with (
        genai.agent_span(run_id=RUN_ID, workspace_id=WORKSPACE_ID),
        genai.tool_span(SECRET) as span,
    ):
        genai.set_attributes(span, {"scf.note": f"token {SECRET}"})

    recorded: list[tuple[str, dict[str, Any]]] = [
        (span.name, _attrs(span)) for span in exported.get_finished_spans()
    ]
    recorded += [(span.name, span.attributes) for span in get_span_store().spans()]
    assert len(recorded) == 4
    assert SECRET not in repr(recorded)
    assert "execute_tool [REDACTED]" in {name for name, _ in recorded}


def test_a_failure_in_the_block_is_recorded_and_reraised(exported: InMemorySpanExporter) -> None:
    with (
        pytest.raises(TimeoutError),
        genai.agent_span(run_id=RUN_ID, workspace_id=WORKSPACE_ID),
        genai.tool_span("run_command"),
    ):
        raise TimeoutError("command output that must not be captured")

    tool = _by_name(exported)["execute_tool run_command"]
    assert _attrs(tool)[genai.ERROR_TYPE] == "TimeoutError"
    assert tool.status.status_code is StatusCode.ERROR
    assert not tool.status.description
    assert not tool.events


def test_a_recording_failure_never_reaches_the_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_: object, **__: object) -> Any:
        raise RuntimeError("telemetry is broken")

    monkeypatch.setattr(get_span_store(), "add", boom)
    monkeypatch.setattr(genai, "_tracer", boom)

    with genai.agent_span(run_id=RUN_ID, workspace_id=WORKSPACE_ID):
        with genai.inference_span(provider="anthropic", model="m") as call:
            genai.record_inference(call, response_model="m", usage=TokenUsage(), cost_usd=0.0)
        result = "the run went on"
    assert result == "the run went on"

    with pytest.raises(KeyError), genai.tool_span("t"):
        raise KeyError("the block's own error still propagates")


def test_spans_reach_the_in_memory_store_without_the_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(genai, "_otel_trace", None)
    get_span_store().clear()

    with genai.agent_span(run_id=RUN_ID, workspace_id=WORKSPACE_ID), genai.tool_span("list_dir"):
        pass

    names = [span.name for span in get_span_store().spans()]
    assert names == ["execute_tool list_dir", "invoke_agent"]
    get_span_store().clear()
