"""Unit tests for defensive secret redaction before persistence (AC11)."""

from __future__ import annotations

from forge_knowledge.mcp_chunking import McpResourceChunker, McpResourceSnapshot
from forge_knowledge.redaction import REDACTED, redact_secrets

AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
PEM = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "MIIBOgIBAAJBAKj34GkxFh\nfakekeymaterialfakekeymaterial\n"
    "-----END RSA PRIVATE KEY-----"
)


def test_redacts_aws_key_and_pem_block() -> None:
    body = f"Runbook.\nUse key {AWS_KEY} and this cert:\n{PEM}\nend."
    out = redact_secrets(body)
    assert AWS_KEY not in out
    assert "BEGIN RSA PRIVATE KEY" not in out
    assert "fakekeymaterial" not in out
    assert REDACTED in out


def test_redacts_bearer_kv_and_jwt() -> None:
    body = (
        "Authorization: Bearer sk-fixture-secret-123\n"
        "api_key = supersecretvalue\n"
        "jwt eyJabc.def.ghi rest"
    )
    out = redact_secrets(body)
    assert "sk-fixture-secret-123" not in out
    assert "supersecretvalue" not in out
    assert "eyJabc.def.ghi" not in out


def test_redaction_is_idempotent() -> None:
    body = f"key {AWS_KEY}"
    once = redact_secrets(body)
    assert redact_secrets(once) == once


def test_no_secret_substring_survives_into_chunks() -> None:
    body = f"# Runbook\n\nUse {AWS_KEY} then:\n{PEM}"
    redacted = redact_secrets(body)
    snap = McpResourceSnapshot(
        uri="confluence://engineering/p",
        content=redacted,
        connection_slug="confluence-engineering",
        mime_type="text/markdown",
    )
    chunks = McpResourceChunker().chunk(snap)
    joined = "\n".join(c.content for c in chunks)
    assert AWS_KEY not in joined
    assert "fakekeymaterial" not in joined
