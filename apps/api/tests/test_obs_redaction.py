"""Tests for secret redaction (Task 1.14 — observability + audit).

Spec Security: "Secrets stripped from logs, traces, and retrieval results."
Redaction is applied by the audit writer, run-trace assembler, and OTel hooks,
so it is exercised directly here as the shared primitive.
"""

from __future__ import annotations

from forge_api.observability.redaction import (
    REDACTED,
    redact_mapping,
    redact_text,
    redact_value,
)


def test_redacts_bearer_token_in_text() -> None:
    text = "calling api with Authorization: Bearer abcDEF123456ghiJKL"
    out = redact_text(text)
    assert "abcDEF123456ghiJKL" not in out
    assert REDACTED in out


def test_redacts_provider_api_key_pattern() -> None:
    out = redact_text("key=sk-ABCDEFGHIJKLMNOP1234567890")
    assert "sk-ABCDEFGHIJKLMNOP1234567890" not in out
    assert REDACTED in out


def test_redacts_jwt() -> None:
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9.aGVsbG8td29ybGQtc2ln"
    out = redact_text(f"token {jwt}")
    assert jwt not in out
    assert REDACTED in out


def test_non_secret_text_is_unchanged() -> None:
    text = "the quick brown fox jumps over the lazy dog"
    assert redact_text(text) == text


def test_redact_mapping_redacts_secret_keys_by_name() -> None:
    out = redact_mapping(
        {
            "password": "hunter2",
            "api_key": "literally-anything",
            "authorization": "Basic Zm9vOmJhcg==",
            "username": "alice",
            "count": 3,
        }
    )
    assert out["password"] == REDACTED
    assert out["api_key"] == REDACTED
    assert out["authorization"] == REDACTED
    # Non-secret keys are preserved verbatim.
    assert out["username"] == "alice"
    assert out["count"] == 3


def test_redact_mapping_is_recursive() -> None:
    out = redact_mapping(
        {
            "outer": {"client_secret": "s3cr3t", "ok": "fine"},
            "items": [{"token": "tok-123"}, {"label": "x"}],
        }
    )
    assert out["outer"]["client_secret"] == REDACTED
    assert out["outer"]["ok"] == "fine"
    assert out["items"][0]["token"] == REDACTED
    assert out["items"][1]["label"] == "x"


def test_redact_mapping_does_not_mutate_input() -> None:
    original = {"password": "hunter2", "nested": {"token": "abc"}}
    redact_mapping(original)
    assert original["password"] == "hunter2"
    assert original["nested"]["token"] == "abc"


def test_redact_value_scrubs_secret_substrings_in_values() -> None:
    out = redact_value("logged in with Bearer abcDEF123456ghiJKL ok")
    assert "abcDEF123456ghiJKL" not in out
