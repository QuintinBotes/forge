"""``forge-verify`` CLI tests — attestation + audit-chain verify exit codes.

Offline-first: the DSSE-envelope and audit-chain-export modes need no live
services (a fixed Ed25519 seed makes the signer deterministic + silent, and the
chain export is re-walked from NDJSON), and the ``--run`` DB mode runs against a
throwaway file-backed SQLite so the CLI's own engine sees the committed row.
Each mode is exercised both green (exit 0) and tampered (exit non-zero).
"""

from __future__ import annotations

import base64
import hashlib
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from forge_api.cli_verify import main
from forge_contracts.attestation import (
    DSSE_PAYLOAD_TYPE_INTOTO,
    DigestSet,
    DsseEnvelope,
    Statement,
    Subject,
)
from forge_contracts.audit import AuditEvent, canonical_json
from forge_db.attest.repository import AttestationRepository
from forge_db.audit.writer import SqlAuditWriter
from forge_db.models.attestation import Attestation
from forge_db.models.audit import AuditChainHead, AuditLog
from forge_obs.attest.signing import DsseSigner, EnvSigningKeyProvider, pae

#: A fixed 32-byte Ed25519 seed → deterministic, silent signer (an unset key
#: would fail open to a warned, process-ephemeral key).
_SEED_B64 = base64.b64encode(bytes(range(1, 33))).decode("ascii")


@pytest.fixture
def signer() -> DsseSigner:
    return DsseSigner(EnvSigningKeyProvider(environ={"FORGE_ATTEST_SIGNING_KEY": _SEED_B64}))


def _signed_statement(signer: DsseSigner) -> tuple[DsseEnvelope, bytes, str]:
    """Sign a small in-toto Statement; return (envelope, payload_bytes, payload_hash)."""
    statement = Statement(
        subject=[Subject(name="changeset", digest=DigestSet(sha256="ab" * 32))],
        predicateType="https://example.test/pred/v1",
        predicate={"agent_role": "implementer", "model": "claude"},
    )
    payload = canonical_json(statement.model_dump(mode="json", by_alias=True)).encode("utf-8")
    envelope = signer.sign(payload_type=DSSE_PAYLOAD_TYPE_INTOTO, payload=payload)
    payload_hash = hashlib.sha256(pae(DSSE_PAYLOAD_TYPE_INTOTO, payload)).hexdigest()
    return envelope, payload, payload_hash


# --------------------------------------------------------------------------- #
# --attestation (offline DSSE verify)                                         #
# --------------------------------------------------------------------------- #


def test_attestation_good_verifies(tmp_path: Path, signer: DsseSigner) -> None:
    envelope, _, _ = _signed_statement(signer)
    path = tmp_path / "att.json"
    path.write_text(envelope.model_dump_json(), encoding="utf-8")

    rc = main(["--attestation", str(path), "--public-key", signer.public_key_b64])
    assert rc == 0


def test_attestation_tampered_rejected(tmp_path: Path, signer: DsseSigner) -> None:
    envelope, _, _ = _signed_statement(signer)
    # Swap the payload for different bytes — the signature covers the PAE of the
    # original payload, so it no longer verifies.
    tampered = envelope.model_copy(
        update={"payload": base64.b64encode(b'{"tampered":true}').decode("ascii")}
    )
    path = tmp_path / "att.json"
    path.write_text(tampered.model_dump_json(), encoding="utf-8")

    rc = main(["--attestation", str(path), "--public-key", signer.public_key_b64])
    assert rc == 1


def test_attestation_wrong_key_rejected(tmp_path: Path, signer: DsseSigner) -> None:
    envelope, _, _ = _signed_statement(signer)
    path = tmp_path / "att.json"
    path.write_text(envelope.model_dump_json(), encoding="utf-8")

    other_seed = base64.b64encode(bytes(range(33, 65))).decode("ascii")
    other = DsseSigner(EnvSigningKeyProvider(environ={"FORGE_ATTEST_SIGNING_KEY": other_seed}))
    rc = main(["--attestation", str(path), "--public-key", other.public_key_b64])
    assert rc == 1


# --------------------------------------------------------------------------- #
# --audit-export (offline hash-chain re-walk)                                 #
# --------------------------------------------------------------------------- #


def _export_lines(workspace_id: uuid.UUID) -> list[str]:
    """Emit a two-event chain on in-memory SQLite and return the NDJSON export."""
    from forge_api.services.audit import AuditService

    engine = create_engine("sqlite://")
    AuditChainHead.__table__.create(bind=engine)
    AuditLog.__table__.create(bind=engine)
    with Session(engine) as session:
        writer = SqlAuditWriter(session)
        writer.emit(AuditEvent(workspace_id=workspace_id, action="a.one", details={"k": 1}))
        writer.emit(AuditEvent(workspace_id=workspace_id, action="a.two", details={"k": 2}))
        session.commit()
        lines = list(AuditService(session).export_ndjson(workspace_id))
    return [line.rstrip("\n") for line in lines]


def test_audit_export_intact(tmp_path: Path) -> None:
    lines = _export_lines(uuid.uuid4())
    path = tmp_path / "audit.ndjson"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    rc = main(["--audit-export", str(path)])
    assert rc == 0


def test_audit_export_tampered_rejected(tmp_path: Path) -> None:
    import json

    lines = _export_lines(uuid.uuid4())
    # Tamper the first row's action (part of entry_hash) → re-walk detects it.
    first = json.loads(lines[0])
    first["action"] = "a.forged"
    lines[0] = canonical_json(first)
    path = tmp_path / "audit.ndjson"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    rc = main(["--audit-export", str(path)])
    assert rc == 1


def test_audit_export_payload_tamper_rejected(tmp_path: Path) -> None:
    import json

    lines = _export_lines(uuid.uuid4())
    # Tamper the recorded details without recomputing payload_hash.
    row = json.loads(lines[-1])
    row["details"] = {"k": 999}
    lines[-1] = canonical_json(row)
    path = tmp_path / "audit.ndjson"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    rc = main(["--audit-export", str(path)])
    assert rc == 1


# --------------------------------------------------------------------------- #
# --run (DB-backed attestation verify)                                        #
# --------------------------------------------------------------------------- #


def _seed_attestation(
    url: str, signer: DsseSigner, *, payload_hash_override: str | None = None
) -> uuid.UUID:
    """Insert one Attestation into a file-backed SQLite DB; return its run id."""
    engine = create_engine(url)
    Attestation.__table__.create(bind=engine)
    workspace_id = uuid.uuid4()
    run_id = uuid.uuid4()
    envelope, _, payload_hash = _signed_statement(signer)
    with Session(engine) as session:
        AttestationRepository(session).insert(
            workspace_id,
            subject_digest="sha256:" + "ab" * 32,
            predicate_type="https://example.test/pred/v1",
            envelope=envelope.model_dump(mode="json"),
            payload_hash=payload_hash_override or payload_hash,
            keyid=signer.keyid,
            workflow_run_id=run_id,
        )
        session.commit()
    engine.dispose()
    return run_id


def test_run_good_verifies(tmp_path: Path, signer: DsseSigner) -> None:
    url = f"sqlite:///{tmp_path / 'verify.db'}"
    run_id = _seed_attestation(url, signer)

    rc = main(["--run", str(run_id), "--database-url", url, "--public-key", signer.public_key_b64])
    assert rc == 0


def test_run_hash_mismatch_rejected(tmp_path: Path, signer: DsseSigner) -> None:
    url = f"sqlite:///{tmp_path / 'verify.db'}"
    # Store a payload_hash that does not match the envelope's PAE digest.
    run_id = _seed_attestation(url, signer, payload_hash_override="00" * 32)

    rc = main(["--run", str(run_id), "--database-url", url, "--public-key", signer.public_key_b64])
    assert rc == 1


def test_run_missing_attestation_errors(tmp_path: Path, signer: DsseSigner) -> None:
    url = f"sqlite:///{tmp_path / 'verify.db'}"
    _seed_attestation(url, signer)

    rc = main(
        ["--run", str(uuid.uuid4()), "--database-url", url, "--public-key", signer.public_key_b64]
    )
    assert rc == 1


def test_run_without_database_url_parks(monkeypatch: pytest.MonkeyPatch) -> None:
    # No --database-url and no FORGE_DATABASE_URL → "can't check" exit 3.
    monkeypatch.delenv("FORGE_DATABASE_URL", raising=False)
    rc = main(["--run", str(uuid.uuid4())])
    assert rc == 3
