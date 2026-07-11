"""Tests for the Attested Changesets contract surface (forge_contracts.attestation)."""

from __future__ import annotations

import base64
import json
import uuid

import pytest
from pydantic import ValidationError

import forge_contracts.attestation as att
from forge_contracts.sandbox import SandboxKind

WORKFLOW_RUN_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
AGENT_RUN_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a2")
APPROVER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a3")


def _provenance(**overrides: object) -> att.ChangesetProvenance:
    kwargs: dict[str, object] = {
        "agent_role": "coder",
        "model": "claude-sonnet-5",
        "model_version": "20260115",
        "prompt_spec_revision": 3,
        "sandbox_tier": SandboxKind.WORKTREE,
        "policy_version_hash": "a" * 64,
        "tool_calls": ["read_file", "run_tests"],
        "human_approver": APPROVER_ID,
        "workflow_run_id": WORKFLOW_RUN_ID,
        "agent_run_id": AGENT_RUN_ID,
        "pr_numbers": [42],
        "spec_key": "SPEC-17",
        "spec_version": 4,
    }
    kwargs.update(overrides)
    return att.ChangesetProvenance(**kwargs)


def _statement(predicate: dict[str, object]) -> att.Statement:
    return att.Statement(
        subject=[
            att.Subject(
                name="apps/api/forge_api/routers/changesets.py",
                digest=att.DigestSet(sha256="b" * 64),
            )
        ],
        predicateType=att.CHANGESET_PROVENANCE_PREDICATE_TYPE,
        predicate=predicate,
    )


# --------------------------------------------------------------------------- #
# Module surface                                                               #
# --------------------------------------------------------------------------- #


def test_module_exports_resolve() -> None:
    for name in att.__all__:
        assert hasattr(att, name), f"__all__ lists missing symbol: {name}"


# --------------------------------------------------------------------------- #
# DsseEnvelope / DsseSignature                                                 #
# --------------------------------------------------------------------------- #


def test_dsse_envelope_round_trip() -> None:
    envelope = att.DsseEnvelope(
        payloadType=att.DSSE_PAYLOAD_TYPE_INTOTO,
        payload=base64.b64encode(b'{"hello":"world"}').decode("ascii"),
        signatures=[att.DsseSignature(keyid="key-1", sig=base64.b64encode(b"sig-bytes").decode())],
    )
    assert att.DsseEnvelope.model_validate(envelope.model_dump()) == envelope


def test_dsse_envelope_signatures_default_empty() -> None:
    envelope = att.DsseEnvelope(payloadType=att.DSSE_PAYLOAD_TYPE_INTOTO, payload="")
    assert envelope.signatures == []


@pytest.mark.parametrize("missing", ["payloadType", "payload"])
def test_dsse_envelope_requires_core_fields(missing: str) -> None:
    kwargs = {"payloadType": att.DSSE_PAYLOAD_TYPE_INTOTO, "payload": "cGF5bG9hZA=="}
    del kwargs[missing]
    with pytest.raises(ValidationError):
        att.DsseEnvelope(**kwargs)


# --------------------------------------------------------------------------- #
# Statement / Subject / DigestSet                                              #
# --------------------------------------------------------------------------- #


def test_digest_set_requires_sha256() -> None:
    with pytest.raises(ValidationError):
        att.DigestSet()


def test_digest_set_allows_extra_algorithms() -> None:
    digest = att.DigestSet.model_validate({"sha256": "c" * 64, "gitCommit": "d" * 40})
    assert digest.sha256 == "c" * 64
    assert digest.model_dump()["gitCommit"] == "d" * 40


def test_subject_requires_name_and_digest() -> None:
    with pytest.raises(ValidationError):
        att.Subject(name="only-a-name")  # type: ignore[call-arg]


def test_statement_type_defaults_and_is_aliased() -> None:
    statement = _statement(_provenance().model_dump(mode="json"))
    assert statement.type_ == att.INTOTO_STATEMENT_TYPE
    dumped = statement.model_dump(by_alias=True)
    assert dumped["_type"] == att.INTOTO_STATEMENT_TYPE
    assert "type_" not in dumped


def test_statement_rejects_wrong_type_value() -> None:
    with pytest.raises(ValidationError):
        att.Statement.model_validate(
            {
                "_type": "https://example.com/not-in-toto",
                "subject": [],
                "predicateType": "x",
                "predicate": {},
            }
        )


@pytest.mark.parametrize("missing", ["subject", "predicateType"])
def test_statement_requires_core_fields(missing: str) -> None:
    kwargs: dict[str, object] = {
        "subject": [att.Subject(name="n", digest=att.DigestSet(sha256="e" * 64))],
        "predicateType": att.CHANGESET_PROVENANCE_PREDICATE_TYPE,
    }
    del kwargs[missing]
    with pytest.raises(ValidationError):
        att.Statement(**kwargs)


# --------------------------------------------------------------------------- #
# ChangesetProvenance                                                          #
# --------------------------------------------------------------------------- #


def test_changeset_provenance_round_trip() -> None:
    provenance = _provenance()
    assert att.ChangesetProvenance.model_validate(provenance.model_dump()) == provenance


def test_changeset_provenance_optional_fields_default() -> None:
    provenance = att.ChangesetProvenance(
        agent_role="spec_author",
        model="claude-opus-4-6",
        prompt_spec_revision=1,
        sandbox_tier=SandboxKind.CONTAINER,
        policy_version_hash="f" * 64,
        workflow_run_id=WORKFLOW_RUN_ID,
        agent_run_id=AGENT_RUN_ID,
        spec_key="SPEC-1",
        spec_version=1,
    )
    assert provenance.model_version is None
    assert provenance.tool_calls == []
    assert provenance.human_approver is None
    assert provenance.pr_numbers == []


@pytest.mark.parametrize(
    "missing",
    [
        "agent_role",
        "model",
        "prompt_spec_revision",
        "sandbox_tier",
        "policy_version_hash",
        "workflow_run_id",
        "agent_run_id",
        "spec_key",
        "spec_version",
    ],
)
def test_changeset_provenance_requires_core_fields(missing: str) -> None:
    kwargs: dict[str, object] = {
        "agent_role": "coder",
        "model": "claude-sonnet-5",
        "prompt_spec_revision": 3,
        "sandbox_tier": SandboxKind.WORKTREE,
        "policy_version_hash": "a" * 64,
        "workflow_run_id": WORKFLOW_RUN_ID,
        "agent_run_id": AGENT_RUN_ID,
        "spec_key": "SPEC-17",
        "spec_version": 4,
    }
    del kwargs[missing]
    with pytest.raises(ValidationError):
        att.ChangesetProvenance(**kwargs)


def test_changeset_provenance_sandbox_tier_is_the_shared_enum() -> None:
    provenance = _provenance(sandbox_tier="gvisor")
    assert provenance.sandbox_tier is SandboxKind.GVISOR
    with pytest.raises(ValidationError):
        _provenance(sandbox_tier="not-a-real-tier")


def test_changeset_provenance_is_frozen() -> None:
    provenance = _provenance()
    with pytest.raises(ValidationError):
        provenance.model = "different-model"  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# encode_statement / decode_statement — canonical serialization + round-trip   #
# --------------------------------------------------------------------------- #


def test_encode_statement_round_trips_through_decode() -> None:
    provenance = _provenance()
    statement = _statement(provenance.model_dump(mode="json"))

    payload_b64 = att.encode_statement(statement)
    decoded = att.decode_statement(payload_b64)

    assert decoded == statement
    recovered = att.ChangesetProvenance.model_validate(decoded.predicate)
    assert recovered == provenance


def test_encode_statement_payload_is_dsse_envelope_ready() -> None:
    statement = _statement(_provenance().model_dump(mode="json"))
    payload_b64 = att.encode_statement(statement)

    envelope = att.DsseEnvelope(
        payloadType=att.DSSE_PAYLOAD_TYPE_INTOTO,
        payload=payload_b64,
        signatures=[att.DsseSignature(keyid="key-1", sig=base64.b64encode(b"sig").decode())],
    )
    assert att.decode_statement(envelope.payload) == statement


def test_encode_statement_is_canonical_and_deterministic() -> None:
    # Two predicates built with keys inserted in different orders must encode
    # to byte-identical payloads — required for stable signature verification.
    predicate_a = {"b": 1, "a": {"y": 2, "x": [1, 2]}}
    predicate_b = {"a": {"x": [1, 2], "y": 2}, "b": 1}

    statement_a = _statement(predicate_a)
    statement_b = _statement(predicate_b)

    encoded_a = att.encode_statement(statement_a)
    encoded_b = att.encode_statement(statement_b)
    assert encoded_a == encoded_b

    # Re-encoding is deterministic (idempotent) too.
    assert att.encode_statement(statement_a) == encoded_a

    raw = base64.b64decode(encoded_a).decode("utf-8")
    assert " " not in raw  # no whitespace: byte-stable canonical JSON
    parsed = json.loads(raw)
    assert parsed["_type"] == att.INTOTO_STATEMENT_TYPE
    assert parsed["predicateType"] == att.CHANGESET_PROVENANCE_PREDICATE_TYPE


def test_decode_statement_rejects_malformed_payload() -> None:
    not_json_b64 = base64.b64encode(b"not-json").decode("ascii")
    with pytest.raises(json.JSONDecodeError):
        att.decode_statement(not_json_b64)
