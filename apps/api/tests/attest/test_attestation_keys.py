"""The deployment's attestation signing key (#175).

With no ``FORGE_ATTEST_SIGNING_KEY``, the first process stores a key in
``attestation_signing_key`` (encrypted under ``FORGE_SECRET_KEY``) and every later
process signs and verifies with it. Two engines over one SQLite file stand for a
restart, and for the API and the worker.
"""

from __future__ import annotations

import base64
import hashlib
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from scf_api.cli_verify import main
from scf_api.services import attestation_keys
from scf_api.services.attestation_keys import signing_key
from scf_auth.errors import KeyMaterialError
from scf_contracts.attestation import DSSE_PAYLOAD_TYPE_INTOTO
from scf_db.attest.repository import AttestationRepository
from scf_db.models.attestation import Attestation
from scf_db.models.attestation_key import AttestationSigningKey
from scf_obs.attest.signing import (
    SIGNING_KEY_ENV,
    DsseSigner,
    DsseVerifier,
    EnvSigningKeyProvider,
    pae,
)

_CONFIGURED = base64.b64encode(bytes(range(1, 33))).decode("ascii")
PAYLOAD = b'{"subject":"head"}'


@pytest.fixture
def url(tmp_path: Path) -> str:
    url = f"sqlite:///{tmp_path / 'keys.db'}"
    engine = create_engine(url)
    AttestationSigningKey.__table__.create(bind=engine)
    Attestation.__table__.create(bind=engine)
    engine.dispose()
    return url


def _key_in_a_new_process(url: str, environ: dict[str, str] | None = None):  # type: ignore[no-untyped-def]
    """The key a freshly started process resolves over ``url``."""
    engine = create_engine(url)
    try:
        with Session(engine) as session:
            return signing_key(session, environ=environ if environ is not None else {})
    finally:
        engine.dispose()


def _stored_rows(url: str) -> int:
    engine = create_engine(url)
    try:
        with Session(engine) as session:
            return session.scalar(select(func.count()).select_from(AttestationSigningKey)) or 0
    finally:
        engine.dispose()


def test_two_processes_without_a_configured_key_sign_and_verify_with_one_key(url: str) -> None:
    """Criterion 5: no process-ephemeral key; a restart, or the API and the worker,
    verify each other's envelopes."""
    first = _key_in_a_new_process(url)
    second = _key_in_a_new_process(url)

    assert first.keyid == second.keyid
    envelope = DsseSigner(first).sign(payload_type=DSSE_PAYLOAD_TYPE_INTOTO, payload=PAYLOAD)
    assert DsseVerifier().verify(envelope, public_key_b64=second.public_key_b64)
    assert _stored_rows(url) == 1


def test_the_stored_key_is_encrypted(url: str) -> None:
    key = _key_in_a_new_process(url)
    engine = create_engine(url)
    with Session(engine) as session:
        row = session.get(AttestationSigningKey, 1)
        assert row is not None
        stored = bytes(row.private_key_encrypted)
    engine.dispose()
    seed = key.private_key.private_bytes_raw()
    assert seed not in stored and base64.b64encode(seed) not in stored


def test_two_processes_racing_to_store_a_key_converge_on_one(url: str) -> None:
    """The process that loses the insert race reads the winner's key."""
    engine = create_engine(url)
    cipher = attestation_keys.default_cipher(
        attestation_keys._subkey(
            attestation_keys._resolve_master_key(None), b"scf-attest-signing-key"
        )
    )
    with Session(engine) as winner, Session(engine) as loser:
        won = attestation_keys._store_new_key(winner, cipher)
        lost = attestation_keys._store_new_key(loser, cipher)
        assert lost.keyid == won.keyid
    engine.dispose()
    assert _key_in_a_new_process(url).keyid == won.keyid


def test_a_configured_key_takes_precedence_over_the_stored_one(url: str) -> None:
    """Criterion 6."""
    stored = _key_in_a_new_process(url)
    configured = _key_in_a_new_process(url, {SIGNING_KEY_ENV: _CONFIGURED})

    expected = EnvSigningKeyProvider({SIGNING_KEY_ENV: _CONFIGURED}).keyid
    assert configured.keyid == expected != stored.keyid
    assert _stored_rows(url) == 1  # the stored key is kept, not replaced


def test_a_malformed_configured_key_fails_closed(url: str) -> None:
    """Criterion 6: a set but malformed key is refused, even with a stored key."""
    _key_in_a_new_process(url)

    with pytest.raises(KeyMaterialError):
        _key_in_a_new_process(url, {SIGNING_KEY_ENV: "not base64!"})


def test_a_stored_key_under_another_master_key_fails_closed(
    url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A key stored under one FORGE_SECRET_KEY is not silently replaced under another."""
    _key_in_a_new_process(url)
    monkeypatch.setenv("FORGE_SECRET_KEY", "another-master-secret-0123456789abcdef")

    with pytest.raises(KeyMaterialError, match="FORGE_SECRET_KEY"):
        _key_in_a_new_process(url)


def test_scf_verify_accepts_an_attestation_signed_before_a_restart(
    url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion 7: ``scf-verify --run`` (also installed under the old name) finds the
    stored key after the signer is gone."""
    monkeypatch.delenv(SIGNING_KEY_ENV, raising=False)
    run_id = uuid.uuid4()
    engine = create_engine(url)
    with Session(engine) as session:
        signer = DsseSigner(signing_key(session, environ={}))
        envelope = signer.sign(payload_type=DSSE_PAYLOAD_TYPE_INTOTO, payload=PAYLOAD)
        AttestationRepository(session).insert(
            uuid.uuid4(),
            subject_digest="sha256:" + "ab" * 32,
            predicate_type="https://example.test/pred/v1",
            envelope=envelope.model_dump(mode="json"),
            payload_hash=hashlib.sha256(pae(DSSE_PAYLOAD_TYPE_INTOTO, PAYLOAD)).hexdigest(),
            keyid=signer.keyid,
            workflow_run_id=run_id,
        )
        session.commit()
    engine.dispose()  # the signing process is gone

    assert main(["--run", str(run_id), "--database-url", url]) == 0
