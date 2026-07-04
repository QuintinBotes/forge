"""Tests for MCP security primitives (plan Task 1.12; spec: MCP Security Rules).

Covers write-tool classification (rule 1), RFC 8707 token binding (rule 2),
namespace scoping (rule 5), and secret redaction + payload hashing (rules 4/6).
"""

from __future__ import annotations

from forge_contracts import MCPAuth, MCPAuthType, MCPConnection, MCPResource
from forge_mcp.security import (
    SENSITIVE_KEYS,
    filter_resources,
    is_write_tool,
    namespace_of,
    payload_hash,
    redact,
    resource_in_scope,
    token_binding,
)
from forge_mcp.transport import ToolSpec

# --------------------------------------------------------------------------- #
# Write-tool classification                                                    #
# --------------------------------------------------------------------------- #


def test_read_tool_is_not_a_write_by_name() -> None:
    assert is_write_tool("search_pages") is False
    assert is_write_tool("get_document") is False
    assert is_write_tool("list_spaces") is False


def test_write_tool_detected_by_name() -> None:
    assert is_write_tool("create_page") is True
    assert is_write_tool("delete_document") is True
    assert is_write_tool("updateRecord") is True
    assert is_write_tool("set_label") is True


def test_annotation_read_only_hint_overrides_name_heuristic() -> None:
    # A tool whose name looks writeish but is annotated read-only is a read.
    spec = ToolSpec(name="post_search", read_only=True)
    assert is_write_tool("post_search", spec) is False


def test_annotation_marks_write_even_for_neutral_name() -> None:
    spec = ToolSpec(name="run", read_only=False)
    assert is_write_tool("run", spec) is True


def test_unannotated_mutating_tool_with_unknown_verb_defaults_to_write() -> None:
    # Fail-closed (Phase-2 bug fix r4): the MCP 2025 convention defaults
    # ``readOnlyHint`` to false, so an un-annotated tool whose verb is NOT a
    # recognised read verb must be assumed destructive. Previously these slipped
    # through as reads because the name matched no WRITE_KEYWORD.
    assert is_write_tool("merge") is True
    assert is_write_tool("approve") is True
    assert is_write_tool("merge_pull_request") is True
    assert is_write_tool("dispatchWorkflow") is True
    assert is_write_tool("transfer_funds") is True


def test_unannotated_read_verb_tool_is_still_read() -> None:
    # A clearly read-only leading verb keeps the tool a read without annotation.
    assert is_write_tool("search_pages") is False
    assert is_write_tool("get_document") is False
    assert is_write_tool("list_spaces") is False
    assert is_write_tool("fetchRecord") is False


def test_read_verb_first_but_mutating_verb_present_is_write() -> None:
    # A read-looking leading verb does not whitewash an embedded mutating verb.
    assert is_write_tool("list_and_merge") is True


# --------------------------------------------------------------------------- #
# RFC 8707 token binding                                                        #
# --------------------------------------------------------------------------- #


def test_token_binding_prefers_explicit_resource() -> None:
    conn = MCPConnection(
        id="c",
        name="c",
        endpoint="https://mcp.internal/confluence",
        auth=MCPAuth(type=MCPAuthType.OAUTH, resource="https://mcp.internal/confluence"),
    )
    assert token_binding(conn) == "https://mcp.internal/confluence"


def test_token_binding_falls_back_to_endpoint() -> None:
    conn = MCPConnection(
        id="c",
        name="c",
        endpoint="https://mcp.internal/jira",
        auth=MCPAuth(type=MCPAuthType.API_KEY),
    )
    assert token_binding(conn) == "https://mcp.internal/jira"


def test_token_binding_none_for_unauthenticated() -> None:
    conn = MCPConnection(
        id="c", name="c", endpoint="https://x", auth=MCPAuth(type=MCPAuthType.NONE)
    )
    assert token_binding(conn) is None


# --------------------------------------------------------------------------- #
# Namespace scoping                                                            #
# --------------------------------------------------------------------------- #


def test_namespace_of_parses_scheme_uri() -> None:
    assert namespace_of("confluence://engineering/page-1") == "engineering"
    assert namespace_of("confluence://architecture/adr/7") == "architecture"


def test_resource_in_scope_allows_when_unscoped() -> None:
    assert resource_in_scope("anything", []) is True


def test_resource_in_scope_enforces_allowlist() -> None:
    assert resource_in_scope("engineering", ["engineering", "architecture"]) is True
    assert resource_in_scope("finance", ["engineering", "architecture"]) is False


def test_filter_resources_by_allowlist_and_request() -> None:
    resources = [
        MCPResource(uri="confluence://engineering/a", namespace="engineering"),
        MCPResource(uri="confluence://architecture/b", namespace="architecture"),
        MCPResource(uri="confluence://finance/c", namespace="finance"),
    ]
    allowed = ["engineering", "architecture"]
    # Allowlist removes finance.
    kept = filter_resources(resources, allowed)
    assert {r.namespace for r in kept} == {"engineering", "architecture"}
    # Requested namespace narrows further.
    only_eng = filter_resources(resources, allowed, requested="engineering")
    assert [r.namespace for r in only_eng] == ["engineering"]


# --------------------------------------------------------------------------- #
# Redaction + payload hashing                                                  #
# --------------------------------------------------------------------------- #


def test_redact_masks_sensitive_dict_keys() -> None:
    raw = {"query": "hello", "token": "secret-abc", "nested": {"api_key": "xyz"}}
    out = redact(raw)
    assert out["query"] == "hello"
    assert out["token"] != "secret-abc"
    assert "secret-abc" not in repr(out)
    assert "xyz" not in repr(out)
    # Original is not mutated.
    assert raw["token"] == "secret-abc"


def test_sensitive_keys_cover_common_secret_names() -> None:
    for key in ("token", "secret", "password", "api_key", "authorization"):
        assert key in SENSITIVE_KEYS


def test_redact_masks_bearer_tokens_in_strings() -> None:
    out = redact("Authorization: Bearer sk-abcdef123456")
    assert "sk-abcdef123456" not in out


def test_payload_hash_is_deterministic_and_secret_free() -> None:
    args = {"q": "x", "token": "super-secret"}
    h1 = payload_hash(args)
    h2 = payload_hash(args)
    assert h1 == h2
    assert len(h1) == 64  # sha256 hexdigest
    # The hash is computed over redacted input, so the secret never reaches it.
    assert payload_hash({"q": "x", "token": "different"}) == h1
