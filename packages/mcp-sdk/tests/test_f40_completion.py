"""F40 MCP-gateway completion deltas (SDK layer).

Covers the delta wired on top of the existing Task-1.12 seam:

* write tool calls are routed through the approval gate (fail-closed),
* ``prompts/list`` + ``prompts/get`` are consumed (and message content redacted),
* a server-initiated elicitation request is surfaced as a typed error,
* a per-connection rate limit raises a typed, retryable error (NOT a run failure).

Live transport is mocked via :class:`forge_mcp.testing.FakeTransport`. The Redis
token-bucket test skips cleanly when no Redis server answers (repo convention).
"""

from __future__ import annotations

import pytest

from forge_contracts import ApprovalRequiredError, MCPToolResult
from forge_mcp import (
    InMemoryRateLimiter,
    MCPElicitationRequiredError,
    MCPGatewayClient,
    MCPRateLimitedError,
    MCPWriteApprovalEvaluator,
    PromptMessage,
    PromptSpec,
    RedisTokenBucket,
    default_mcp_policy,
    redis_rate_limiter,
)
from forge_mcp.testing import FakeTransport, sample_connection, sample_transport

_REDIS_URL = "redis://localhost:6379/0"


def _approving_client(
    *, allow_write: bool, transport: FakeTransport | None = None
) -> MCPGatewayClient:
    client = MCPGatewayClient(
        transport=transport or sample_transport(),
        policy=default_mcp_policy(),
        evaluator=MCPWriteApprovalEvaluator(),
    )
    client.connect(sample_connection(allow_write=allow_write))
    return client


# --------------------------------------------------------------------------- #
# Delta 1: write tool calls routed through approval (fail-closed)              #
# --------------------------------------------------------------------------- #


def test_write_tool_on_writable_connection_requires_approval() -> None:
    # allow_write clears rule 1, so the (now-armed) approval gate fires instead
    # of the tool executing directly.
    tr = FakeTransport()
    client = MCPGatewayClient(
        transport=tr, policy=default_mcp_policy(), evaluator=MCPWriteApprovalEvaluator()
    )
    client.connect(sample_connection(allow_write=True))

    with pytest.raises(ApprovalRequiredError):
        client.call_tool("create_page", {"title": "x"})
    # Fail-closed: the live transport was never reached.
    assert tr.calls == []
    assert client.audit_entries[-1].tool == "create_page"
    assert client.audit_entries[-1].status == "needs_approval"


def test_read_tool_passes_the_approval_gate() -> None:
    client = _approving_client(allow_write=True)
    result = client.call_tool("search_pages", {"q": "vault"})
    assert result.status == "ok"


def test_read_only_connection_still_rejects_write_before_approval() -> None:
    # The rule-1 write-forbidden check precedes the approval gate.
    from forge_contracts import MCPWriteForbiddenError

    client = _approving_client(allow_write=False)
    with pytest.raises(MCPWriteForbiddenError):
        client.call_tool("create_page", {"title": "x"})


# --------------------------------------------------------------------------- #
# Delta 2: prompts/list + prompts/get consumed (with redaction)               #
# --------------------------------------------------------------------------- #


def test_list_prompts_returns_specs_and_audits() -> None:
    client = _approving_client(allow_write=False)
    prompts = client.list_prompts()
    assert prompts and all(isinstance(p, PromptSpec) for p in prompts)
    assert "summarize_page" in {p.name for p in prompts}
    assert client.audit_entries[-1].tool == "prompts/list"
    assert client.audit_entries[-1].status == "ok"


def test_get_prompt_returns_redacted_messages() -> None:
    client = _approving_client(allow_write=False)
    messages = client.get_prompt("summarize_page", {"uri": "confluence://engineering/page-1"})
    assert messages and all(isinstance(m, PromptMessage) for m in messages)
    # Rule 6: a secret planted in a rendered message never leaves the client.
    joined = " ".join(m.content for m in messages)
    assert "sk-fixture-secret-123" not in joined
    assert "[redacted]" in joined
    assert client.audit_entries[-1].tool == "prompts/get:summarize_page"


def test_get_prompt_empty_name_rejected() -> None:
    from forge_mcp.exceptions import MCPInputError

    client = _approving_client(allow_write=False)
    with pytest.raises(MCPInputError):
        client.get_prompt("  ")


# --------------------------------------------------------------------------- #
# Delta 3: server-initiated elicitation surfaced to the approver              #
# --------------------------------------------------------------------------- #


def test_elicitation_request_is_surfaced_as_typed_error() -> None:
    tr = FakeTransport(
        elicitations={
            "search_pages": {
                "message": "Which space should I search?",
                "requestedSchema": {"type": "object", "properties": {"space": {"type": "string"}}},
            }
        }
    )
    client = MCPGatewayClient(transport=tr)
    client.connect(sample_connection())

    with pytest.raises(MCPElicitationRequiredError) as exc_info:
        client.call_tool("search_pages", {"q": "x"})
    assert exc_info.value.elicit_message == "Which space should I search?"
    assert exc_info.value.schema["type"] == "object"
    assert client.audit_entries[-1].status == "needs_input"


def test_tool_without_elicitation_returns_result() -> None:
    client = MCPGatewayClient(transport=FakeTransport())
    client.connect(sample_connection())
    result = client.call_tool("search_pages", {"q": "x"})
    assert isinstance(result, MCPToolResult)
    assert result.status == "ok"


# --------------------------------------------------------------------------- #
# Delta 4: per-connection rate limit -> typed retryable error                 #
# --------------------------------------------------------------------------- #


def test_rate_limit_raises_typed_error_not_run_failure() -> None:
    # capacity 1, negligible refill: the second call in the window is rejected.
    limiter = InMemoryRateLimiter(capacity=1, refill_per_sec=0.001)
    client = MCPGatewayClient(transport=sample_transport(), rate_limiter=limiter)
    client.connect(sample_connection())

    assert client.call_tool("search_pages", {"q": "1"}).status == "ok"
    with pytest.raises(MCPRateLimitedError) as exc_info:
        client.call_tool("search_pages", {"q": "2"})
    # A typed, retryable signal — not an MCPToolResult(status="error").
    assert exc_info.value.retry_after_s is not None
    assert client.audit_entries[-1].status == "rate_limited"


def test_rate_limit_is_per_connection() -> None:
    limiter = InMemoryRateLimiter(capacity=1, refill_per_sec=0.001)
    a = MCPGatewayClient(transport=sample_transport(), rate_limiter=limiter)
    a.connect(sample_connection(id="conn-a"))
    b = MCPGatewayClient(transport=sample_transport(), rate_limiter=limiter)
    b.connect(sample_connection(id="conn-b"))
    # Draining conn-a's budget must not affect conn-b (independent buckets).
    assert a.call_tool("search_pages", {"q": "x"}).status == "ok"
    assert b.call_tool("search_pages", {"q": "x"}).status == "ok"
    with pytest.raises(MCPRateLimitedError):
        a.call_tool("search_pages", {"q": "x"})


def test_redis_rate_limiter_absent_server_returns_none() -> None:
    # redis_rate_limiter degrades to None when no server answers, so the gateway
    # falls back to no limiter rather than hard-failing.
    limiter = redis_rate_limiter("redis://127.0.0.1:6390/0", capacity=5, refill_per_sec=1.0)
    assert limiter is None


def test_redis_token_bucket_enforces_budget() -> None:
    limiter = redis_rate_limiter(_REDIS_URL, capacity=2, refill_per_sec=0.001)
    if limiter is None:
        pytest.skip("no Redis server available (PARKED per repo convention)")
    assert isinstance(limiter, RedisTokenBucket)
    key = "test-conn-f40"
    assert limiter.allow(key) is True
    assert limiter.allow(key) is True
    assert limiter.allow(key) is False
