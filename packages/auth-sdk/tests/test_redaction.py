"""F37 canonical SecretRedactor tests (AC15)."""

from __future__ import annotations

import pytest

from forge_auth.redaction import REDACTED, SecretRedactor


@pytest.fixture
def redactor() -> SecretRedactor:
    return SecretRedactor()


def test_aws_access_key_id(redactor: SecretRedactor) -> None:
    out = redactor.redact("creds: AKIAIOSFODNN7EXAMPLE used")
    assert "AKIAIOSFODNN7EXAMPLE" not in out
    assert REDACTED in out


def test_pem_block(redactor: SecretRedactor) -> None:
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEpAIBAAKCAQEA7c3kW1\nmoreb64==\n"
        "-----END RSA PRIVATE KEY-----"
    )
    out = redactor.redact(f"before {pem} after")
    assert "BEGIN RSA PRIVATE KEY" not in out
    assert out.startswith("before ") and out.endswith(" after")


def test_key_assignments_keep_name(redactor: SecretRedactor) -> None:
    out = redactor.redact("ANTHROPIC_API_KEY=sk-live-123 DB_PASSWORD: hunter2!")
    assert "sk-live-123" not in out and "hunter2" not in out
    assert "ANTHROPIC_API_KEY=" in out and "DB_PASSWORD:" in out


def test_bearer_and_jwt(redactor: SecretRedactor) -> None:
    out = redactor.redact("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl")
    assert "eyJ" not in out


def test_forge_platform_token(redactor: SecretRedactor) -> None:
    out = redactor.redact("token forge_svc_a1b2c3d4_wUdOaBk3xLmQ9RtY7ZpF leaked")
    assert "wUdOaBk3xLmQ9RtY7ZpF" not in out


def test_high_entropy_string(redactor: SecretRedactor) -> None:
    out = redactor.redact("value kH8s2Lq0Xw3vZr7YtB1cN5mD9fGjP4aQ appears")
    assert "kH8s2Lq0Xw3vZr7YtB1cN5mD9fGjP4aQ" not in out


def test_uuid_and_plain_text_preserved(redactor: SecretRedactor) -> None:
    text = (
        "workspace 0fc82acd-52b9-429f-8850-ec1e1a963f82 ran the "
        "internationalization pipeline for 42 documents"
    )
    assert redactor.redact(text) == text


def test_registered_known_secret_scrubbed_everywhere(redactor: SecretRedactor) -> None:
    redactor.register_known_secret("plainpassword")
    out = redactor.redact("the value plainpassword appeared as 'plainpassword'!")
    assert "plainpassword" not in out
    assert out.count(REDACTED) == 2


def test_short_known_secret_ignored(redactor: SecretRedactor) -> None:
    redactor.register_known_secret("ab")
    assert redactor.redact("absolutely fabulous") == "absolutely fabulous"


def test_redact_value_recursive(redactor: SecretRedactor) -> None:
    redactor.register_known_secret("super-secret-value")
    out = redactor.redact_value(
        {
            "name": "ci key",
            "nested": {"token": "Bearer abcdef123456789012345"},
            "list": ["super-secret-value", 42, None],
        }
    )
    assert out["name"] == "ci key"
    assert "abcdef" not in out["nested"]["token"]
    assert out["list"] == [REDACTED, 42, None]
