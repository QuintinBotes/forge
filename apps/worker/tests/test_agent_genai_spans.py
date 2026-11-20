"""GenAI spans on the worker's agent run paths (#213).

Hermetic: a scripted model drives the Celery task body, spans go to an in-memory
OpenTelemetry exporter and the in-memory span store; no network, no database.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

pytest.importorskip("opentelemetry.sdk.trace")

from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

from scf_agent import AgentRunner
from scf_agent.providers.pricing import cost_usd
from scf_agent.replay import ReplayModelClient, ReplayToolRegistry, RunCassette
from scf_agent.testing import ScriptedModelClient, finish_response, tool_response
from scf_agent.tools import ToolRegistry, ToolResult
from scf_contracts import AgentObjective, ModelResponse, TokenUsage
from scf_coordinator import CoordinatorSettings, InMemorySubAgentRunSink
from scf_obs import genai
from scf_obs.tracing import get_span_store
from scf_worker import agent_runner as agent_runner_module
from scf_worker.agent_runner import build_agent_runner, run_agent_task
from scf_worker.multi_agent import _agent_factory, run_supervised_objective
from scf_worker.observability.genai import TracedModelClient, TracedToolRegistry

PRICED = "claude-sonnet-4-6"
WORKSPACE_ID = uuid.uuid4()
SECRET_TOOL = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
CONTENT = ("PROMPT-MARKER", "COMPLETION-MARKER", "ARGS-MARKER", "OUTPUT-MARKER")


@pytest.fixture
def exported(monkeypatch: pytest.MonkeyPatch) -> Iterator[InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(genai, "_tracer", lambda: provider.get_tracer("test"))
    get_span_store().clear()
    yield exporter
    get_span_store().clear()


def _turn(call: ModelResponse, *, model: str | None = PRICED) -> ModelResponse:
    """``call`` as a provider would report it: a model id and token usage."""
    return call.model_copy(
        update={"model": model, "usage": TokenUsage(input_tokens=100, output_tokens=20)}
    )


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.add("echo", lambda args: ToolResult(ok=True, output=CONTENT[3]))
    registry.add("boom", lambda args: ToolResult(ok=False, error="boom failed"))
    return registry


def _run_celery(
    monkeypatch: pytest.MonkeyPatch, responses: list[ModelResponse], **objective: Any
) -> dict[str, Any]:
    """Drive the Celery agent task's body with a scripted model and a two-tool registry."""
    model = ScriptedModelClient(responses)
    monkeypatch.setattr(
        agent_runner_module, "_resolve_model_client", lambda client: client or model
    )
    build = agent_runner_module.build_agent_runner
    monkeypatch.setattr(
        agent_runner_module,
        "build_agent_runner",
        lambda **kwargs: build(**{**kwargs, "tools": _registry()}),
    )
    payload = {
        "objective": f"Do the work {CONTENT[0]}",
        "model": PRICED,
        "restricted_actions": ["delete_branch"],
        "context": {"workspace_id": str(WORKSPACE_ID)},
        **objective,
    }
    return cast(dict[str, Any], run_agent_task(payload))


def _script() -> list[ModelResponse]:
    """Four model calls; two executed tool calls (one failing) and one denied."""
    return [
        _turn(tool_response("echo", {"text": CONTENT[2]})),
        _turn(tool_response("delete_branch", {"branch": "main"})),
        _turn(tool_response("boom")),
        _turn(finish_response(CONTENT[1]).model_copy(update={"content": CONTENT[1]})),
    ]


def _attrs(span: ReadableSpan) -> dict[str, Any]:
    return dict(span.attributes or {})


def _operation(spans: list[ReadableSpan], operation: str) -> list[ReadableSpan]:
    return [s for s in spans if _attrs(s).get(genai.GEN_AI_OPERATION_NAME) == operation]


def test_a_celery_run_is_one_agent_span_over_each_model_and_executed_tool_call(
    monkeypatch: pytest.MonkeyPatch, exported: InMemorySpanExporter
) -> None:
    result = _run_celery(monkeypatch, _script())

    spans = exported.get_finished_spans()
    (agent,) = _operation(spans, "invoke_agent")
    chats, tools = _operation(spans, "chat"), _operation(spans, "execute_tool")
    assert (len(chats), len(tools), len(spans)) == (4, 2, 1 + 4 + 2)
    assert agent.name == "invoke_agent"
    assert agent.kind is SpanKind.INTERNAL
    assert _attrs(agent)[genai.SCF_WORKSPACE_ID] == str(WORKSPACE_ID)
    assert uuid.UUID(_attrs(agent)[genai.SCF_RUN_ID])
    for span in (*chats, *tools):
        assert span.parent is not None and span.parent.span_id == agent.context.span_id
        assert span.context.trace_id == agent.context.trace_id
    for chat in chats:
        assert chat.name == f"chat {PRICED}"
        assert chat.kind is SpanKind.CLIENT
        assert _attrs(chat)[genai.GEN_AI_REQUEST_MODEL] == PRICED
        assert _attrs(chat)[genai.GEN_AI_PROVIDER_NAME] == "scf.scripted"
        assert _attrs(chat)[genai.GEN_AI_USAGE_INPUT_TOKENS] == 100
        assert _attrs(chat)[genai.GEN_AI_USAGE_OUTPUT_TOKENS] == 20
    echo, boom = sorted(tools, key=lambda s: s.name, reverse=True)
    assert (echo.name, boom.name) == ("execute_tool echo", "execute_tool boom")
    assert _attrs(echo)[genai.GEN_AI_TOOL_NAME] == "echo"
    assert genai.ERROR_TYPE not in _attrs(echo)
    assert _attrs(boom)[genai.ERROR_TYPE] == "tool_error"
    assert boom.status.status_code is StatusCode.ERROR
    # The denied call reached no registry, so it has no span.
    assert not [s for s in spans if "delete_branch" in s.name]
    assert result["artifacts"]["model_usage"]["calls"] == 4


def test_priced_calls_carry_their_cost_and_the_agent_span_the_total(
    monkeypatch: pytest.MonkeyPatch, exported: InMemorySpanExporter
) -> None:
    result = _run_celery(monkeypatch, _script())

    spans = exported.get_finished_spans()
    one_call = cost_usd(PRICED, TokenUsage(input_tokens=100, output_tokens=20))
    assert one_call > 0
    for chat in _operation(spans, "chat"):
        assert _attrs(chat)[genai.SCF_COST_USD] == pytest.approx(one_call)
    (agent,) = _operation(spans, "invoke_agent")
    assert _attrs(agent)[genai.SCF_COST_USD] == pytest.approx(4 * one_call)
    assert _attrs(agent)[genai.SCF_COST_USD] == pytest.approx(
        result["artifacts"]["model_usage"]["cost_usd"]
    )
    for span in spans:
        assert not [k for k in _attrs(span) if k.startswith("gen_ai.") and "cost" in k]


def test_an_unpriced_model_carries_no_cost_anywhere(
    monkeypatch: pytest.MonkeyPatch, exported: InMemorySpanExporter
) -> None:
    script = [_turn(r, model="a-local-model") for r in _script()]
    _run_celery(monkeypatch, script, model="a-local-model")

    spans = exported.get_finished_spans()
    assert len(_operation(spans, "chat")) == 4
    assert all(genai.SCF_COST_USD not in _attrs(span) for span in spans)
    (agent,) = _operation(spans, "invoke_agent")
    assert _attrs(agent)[genai.GEN_AI_USAGE_INPUT_TOKENS] == 400


def test_no_span_carries_content_and_secret_shaped_values_are_redacted(
    monkeypatch: pytest.MonkeyPatch, exported: InMemorySpanExporter
) -> None:
    script = _script()
    script.insert(0, _turn(tool_response(SECRET_TOOL, {"text": CONTENT[2]})))
    _run_celery(monkeypatch, script)

    recorded: list[tuple[str, dict[str, Any]]] = [
        (s.name, _attrs(s)) for s in exported.get_finished_spans()
    ]
    recorded += [(s.name, s.attributes) for s in get_span_store().spans()]
    assert len(recorded) == 2 * (1 + 5 + 3)
    for marker in (*CONTENT, SECRET_TOOL):
        assert marker not in repr(recorded)
    # A tool the model invented is labelled, never named from model output.
    unknown = [attrs for name, attrs in recorded if name == "execute_tool unknown_tool"]
    assert len(unknown) == 2
    for attrs in unknown:
        assert attrs[genai.GEN_AI_TOOL_NAME] == "unknown_tool"
        assert attrs[genai.ERROR_TYPE] == "tool_error"


def test_every_gen_ai_key_a_run_emits_is_declared_beside_the_pin(
    monkeypatch: pytest.MonkeyPatch, exported: InMemorySpanExporter
) -> None:
    _run_celery(monkeypatch, _script())

    keys = {key for span in exported.get_finished_spans() for key in _attrs(span)}
    keys |= {key for span in get_span_store().spans() for key in span.attributes}
    emitted = {key for key in keys if key.startswith("gen_ai.")}
    assert emitted
    assert emitted <= genai.GEN_AI_ATTRIBUTES, emitted - genai.GEN_AI_ATTRIBUTES


def test_spans_land_in_memory_without_the_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(genai, "_otel_trace", None)
    get_span_store().clear()

    _run_celery(monkeypatch, _script())

    stored = get_span_store().spans()
    get_span_store().clear()
    assert len(stored) == 1 + 4 + 2
    (agent,) = [s for s in stored if s.name == "invoke_agent"]
    assert {s.trace_id for s in stored} == {agent.trace_id}


def test_a_telemetry_failure_does_not_fail_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_: object, **__: object) -> Any:
        raise RuntimeError("telemetry is broken")

    monkeypatch.setattr(get_span_store(), "add", boom)
    monkeypatch.setattr(genai, "_tracer", boom)

    result = _run_celery(monkeypatch, [_turn(finish_response("done", confidence=0.9))])

    assert result["status"] == "succeeded"


@pytest.mark.parametrize(
    ("context", "roles"),
    [
        ({"review_required": True}, ["implementer", "reviewer"]),
        ({"fan_out_units": ["left", "right"]}, ["implementer", "implementer"]),
    ],
    ids=["maker_checker", "fan_out_on_threads"],
)
def test_a_supervised_run_has_one_agent_span_per_role_under_one_trace(
    exported: InMemorySpanExporter, context: dict[str, Any], roles: list[str]
) -> None:
    parent_id = uuid.uuid4()
    payload = {
        "objective": "Implement the feature under supervision",
        "execution_mode": "supervised_multi_agent",
        "subagent_policy": {"allowed": True, "max_parallel": 2},
        "context": {
            "workspace_id": str(WORKSPACE_ID),
            "parent_agent_run_id": str(parent_id),
            "subagent_rules": {
                "allow_subagents": True,
                "allowed_roles": sorted(set(roles)),
                "max_parallel": 2,
            },
            **context,
        },
    }
    model = ScriptedModelClient([], default=_turn(finish_response("done", confidence=0.9)))

    result = run_supervised_objective(
        payload,
        model_client=model,
        sub_agent_sink=InMemorySubAgentRunSink(),
        settings=CoordinatorSettings(enabled=True),
        resolve_repository=lambda objective: objective,
    )

    assert result.status.value == "succeeded"
    spans = exported.get_finished_spans()
    (workflow,) = _operation(spans, "invoke_workflow")
    assert workflow.name == "invoke_workflow"
    assert _attrs(workflow)[genai.SCF_RUN_ID] == str(parent_id)
    agents = _operation(spans, "invoke_agent")
    assert sorted(_attrs(a)[genai.GEN_AI_AGENT_NAME] for a in agents) == roles
    assert sorted(a.name for a in agents) == [f"invoke_agent {role}" for role in roles]
    assert {s.context.trace_id for s in spans} == {workflow.context.trace_id}
    for agent in agents:
        assert agent.parent is not None and agent.parent.span_id == workflow.context.span_id
    assert len(_operation(spans, "chat")) == len(roles)
    # The workflow span sums its roles, which a fan-out ends on pool threads.
    assert _attrs(workflow)[genai.GEN_AI_USAGE_INPUT_TOKENS] == 100 * len(roles)
    one_call = cost_usd(PRICED, TokenUsage(input_tokens=100, output_tokens=20))
    assert _attrs(workflow)[genai.SCF_COST_USD] == pytest.approx(len(roles) * one_call)


@pytest.mark.parametrize("in_worktree", [False, True], ids=["no_worktree", "worktree"])
def test_each_role_span_carries_its_role_runs_id(
    exported: InMemorySpanExporter, tmp_path: Path, in_worktree: bool
) -> None:
    context = {"role": "implementer", **({"worktree_path": str(tmp_path)} if in_worktree else {})}
    model = ScriptedModelClient([], default=finish_response("done", confidence=0.9))

    result = _agent_factory(model, workspace_id=WORKSPACE_ID)().run(
        AgentObjective(objective="Do the assigned work", context=context)
    )

    (role,) = _operation(exported.get_finished_spans(), "invoke_agent")
    assert _attrs(role)[genai.SCF_RUN_ID] == str(result.run_id)
    assert _attrs(role)[genai.SCF_WORKSPACE_ID] == str(WORKSPACE_ID)


def _tree(exporter: InMemorySpanExporter) -> list[tuple[str, str | None]]:
    spans = exporter.get_finished_spans()
    names = {s.context.span_id: s.name for s in spans}
    return sorted((s.name, names.get(s.parent.span_id) if s.parent else None) for s in spans)


def test_a_replayed_run_produces_the_live_run_span_tree(
    monkeypatch: pytest.MonkeyPatch, exported: InMemorySpanExporter
) -> None:
    monkeypatch.setenv("FORGE_RECORD_RUNS", "1")
    objective = AgentObjective(objective="Do the work", model=PRICED)
    live = build_agent_runner(
        model_client=ScriptedModelClient(_script()),
        tools=_registry(),
        genai_spans=True,
        token_budget=10_000,
    )
    with genai.agent_span():
        live.run(objective)
    live_tree = _tree(exported)
    exported.clear()

    cassette = cast(RunCassette, getattr(live, "cassette", None))
    replay = AgentRunner(
        TracedModelClient(ReplayModelClient(cassette), provider="scf.scripted", model=None),
        tools=cast(
            ToolRegistry, TracedToolRegistry(ReplayToolRegistry(cassette, tools=_registry()))
        ),
    )
    with genai.agent_span():
        replay.run(objective)

    assert live_tree
    assert _tree(exported) == live_tree


def test_a_budget_refusal_is_a_failed_model_call(
    monkeypatch: pytest.MonkeyPatch, exported: InMemorySpanExporter
) -> None:
    monkeypatch.setenv("FORGE_AGENT_RUN_TOKEN_BUDGET", "50")
    _run_celery(monkeypatch, _script())

    chats = _operation(exported.get_finished_spans(), "chat")
    assert [_attrs(c).get(genai.ERROR_TYPE) for c in chats] == [None, "budget_exhausted"]
    assert chats[1].status.status_code is StatusCode.ERROR
