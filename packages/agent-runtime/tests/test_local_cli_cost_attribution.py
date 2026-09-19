"""A local-CLI run is metered as ``local-cli:*``, never as the pinned model (spec F §5).

``AgentRunner`` prices a run by ``ModelResponse.model`` and falls back to
``AgentObjective.model`` when the provider reports none
(``runtime.py::_to_result``). A subscription run has no per-token price, so a
local-CLI response that arrives untagged is priced at the *API* rates of
whatever per-role model the objective happened to pin — the exact corruption
§5 exists to prevent.

These tests assert the namespaced id survives all the way into what the cost
meter is handed, not merely that the client sets a field: they run a real
``AgentRunner`` over each local-CLI transport with an objective that pins a
priced Anthropic model, and check the derived cost.
"""

from __future__ import annotations

import subprocess

import httpx

from forge_agent import AgentRunner
from forge_agent.providers.bridge_client import LocalCliBridgeClient
from forge_agent.providers.local_cli.client import LocalCliModelClient
from forge_agent.providers.local_cli.detect import CliName
from forge_agent.providers.pricing import MODEL_PRICING, cost_usd
from forge_contracts import AgentObjective, ModelMessage, ModelRequest, RunStatus, TokenUsage

#: A real, priced model id — the kind an operator pins per role. If the
#: local-CLI model id failed to reach the meter, the run below would be priced
#: against this row.
PINNED_MODEL = "claude-opus-4-8"

ENVELOPE = (
    '{"content": "done", "tool_calls": [], "stop_reason": "end_turn", '
    '"usage": {"input_tokens": 1000000, "output_tokens": 1000000}}'
)


def _objective() -> AgentObjective:
    return AgentObjective(objective="summarise the repo", model=PINNED_MODEL)


def _runner(stdout: str):
    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    return run


def test_the_pinned_model_would_otherwise_be_priced() -> None:
    """The premise: without the fix these tokens cost real money on this row."""
    assert PINNED_MODEL in MODEL_PRICING
    usage = TokenUsage(input_tokens=1_000_000, output_tokens=1_000_000)
    assert cost_usd(PINNED_MODEL, usage) > 0
    assert cost_usd("local-cli:claude", usage) == 0.0


def test_subprocess_transport_is_metered_as_local_cli() -> None:
    client = LocalCliModelClient(cli_name=CliName.claude, runner=_runner(ENVELOPE))
    result = AgentRunner(client).run(_objective())

    assert result.status is RunStatus.SUCCEEDED
    usage = result.artifacts["model_usage"]
    # Tokens were really accounted (volume accounting still happens) ...
    assert usage["input_tokens"] == 1_000_000
    # ... but not at the pinned model's API rates.
    assert usage["cost_usd"] == 0.0


def test_bridge_transport_is_metered_as_local_cli_when_the_bridge_reports_a_model() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "content": "done",
                "model": "local-cli:codex",
                "stop_reason": "end_turn",
                "tool_calls": [],
                "usage": {"input_tokens": 1000000, "output_tokens": 1000000},
            },
        )

    client = LocalCliBridgeClient(
        base_url="http://host.docker.internal:8099",
        token="t0ken",
        cli_name=CliName.claude,
        transport=httpx.MockTransport(handler),
    )
    result = AgentRunner(client).run(_objective())
    assert result.status is RunStatus.SUCCEEDED
    assert result.artifacts["model_usage"]["cost_usd"] == 0.0


def test_bridge_transport_is_metered_as_local_cli_when_the_bridge_reports_none() -> None:
    """An untagged reply (e.g. an older bridge) still must not be priced."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "content": "done",
                "stop_reason": "end_turn",
                "tool_calls": [],
                "usage": {"input_tokens": 1000000, "output_tokens": 1000000},
            },
        )

    client = LocalCliBridgeClient(
        base_url="http://host.docker.internal:8099",
        token="t0ken",
        cli_name=CliName.claude,
        transport=httpx.MockTransport(handler),
    )
    result = AgentRunner(client).run(_objective())
    assert result.artifacts["model_usage"]["cost_usd"] == 0.0


def test_a_failed_local_cli_turn_still_labels_the_run_local_cli() -> None:
    """The failure paths carry the id too, so the run is never *labelled* as the
    pinned model. A failed turn reports no tokens, so the derived cost cannot
    discriminate here — the per-path assertions live with each client
    (``tests/providers/test_local_cli_client.py``,
    ``tests/providers/test_local_cli_bridge_client.py``); this pins the shape
    the runtime consumes.
    """

    def run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 1.0)

    client = LocalCliModelClient(cli_name=CliName.claude, runner=run, timeout_s=1.0)
    response = client.complete(
        ModelRequest(model=PINNED_MODEL, messages=[ModelMessage(role="user", content="hi")])
    )
    assert response.model == "local-cli:claude"

    result = AgentRunner(client).run(_objective())
    assert result.status is RunStatus.FAILED
    assert result.artifacts["model_usage"]["cost_usd"] == 0.0
