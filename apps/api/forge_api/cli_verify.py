"""``forge verify`` CLI — offline/DB-backed attestation & audit-chain verifier.

Runnable as ``python -m forge_api.cli_verify`` (wired as the ``forge-verify``
console script; conforms to the repo's ``cli_<name>.py`` sibling convention —
cli_bench / cli_cost / cli_marketplace). Three mutually-exclusive modes, all of
which exit **non-zero on any verification failure** so they can gate a release:

- ``--attestation <file|->`` — offline DSSE verification of a
  :class:`~forge_contracts.attestation.DsseEnvelope` JSON (from a file or
  stdin). Re-derives the PAE and checks the Ed25519 signature against the
  supplied ``--public-key`` (or the public half of ``FORGE_ATTEST_SIGNING_KEY``,
  mirroring ``forge_obs.attest.signing``). No database or network.
- ``--run <id>`` — DB-backed: load the stored :class:`Attestation` for a
  workflow/agent run, re-derive ``payload_hash`` from the envelope's PAE and
  confirm it matches the recorded column, then verify the signature. Needs a
  database (``--database-url`` / ``FORGE_DATABASE_URL``).
- ``--audit-export <ndjson>`` — offline re-walk of an F39 audit-chain export
  (``AuditService.export_ndjson`` output). Recomputes each row's
  ``payload_hash``/``entry_hash`` via the shared pure helpers
  (:func:`forge_contracts.audit.compute_entry_hash` /
  :func:`~forge_contracts.audit.compute_payload_hash`) and re-checks the
  per-workspace ``prev_hash`` linkage — mirroring ``forge_db.audit.chain``'s
  verifier from the exported dicts rather than live ORM rows.

The DB mode returns exit ``3`` (not ``1``) when no database is configured, so a
"can't check" is distinguishable from a "checked and it's tampered" failure —
same convention as the parked-path exit in ``cli_bench`` / ``cli_cost``.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from forge_contracts.attestation import DsseEnvelope, decode_statement
from forge_contracts.audit import (
    GENESIS_HASH,
    compute_entry_hash,
    compute_payload_hash,
)
from forge_obs.attest.signing import DsseVerifier, pae

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

__all__ = ["build_parser", "main"]


# --------------------------------------------------------------------------- #
# Shared helpers                                                              #
# --------------------------------------------------------------------------- #


def _read_source(path: str) -> str:
    """Read ``path`` as UTF-8, or stdin when ``path`` is ``-``."""
    if path == "-":
        return sys.stdin.read()
    return Path(path).read_text(encoding="utf-8")


def _resolve_public_key(explicit: str | None) -> str:
    """The base64 Ed25519 public key to verify against.

    An explicit ``--public-key`` wins; otherwise fall back to the public half of
    the env signing key (``FORGE_ATTEST_SIGNING_KEY``) so an attestation signed
    on this host can be verified without re-passing the key. When no signing key
    is set, ``EnvSigningKeyProvider`` warns and generates an ephemeral key whose
    public half will simply not match a real signature — an honest REJECT.
    """
    if explicit:
        return explicit
    from forge_obs.attest.signing import EnvSigningKeyProvider

    return EnvSigningKeyProvider().public_key_b64


def _print_envelope_summary(envelope: DsseEnvelope) -> None:
    """Print the envelope's predicate type + first subject digest, best-effort."""
    print(f"payloadType: {envelope.payloadType}")
    print(
        f"signatures:  {len(envelope.signatures)} (keyid(s): "
        f"{', '.join(s.keyid for s in envelope.signatures) or '<none>'})"
    )
    try:
        statement = decode_statement(envelope.payload)
    except (ValueError, TypeError):
        return
    print(f"predicateType: {statement.predicateType}")
    for subject in statement.subject:
        print(f"subject: {subject.name} sha256:{subject.digest.sha256}")


def _uuid_or_none(value: Any) -> uuid.UUID | None:
    if value is None:
        return None
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _session_factory(database_url: str | None) -> sessionmaker[Session] | None:
    import os

    from forge_db.session import create_db_engine, create_session_factory

    url = database_url or os.environ.get("FORGE_DATABASE_URL")
    if not url:
        print(
            "no database configured: pass --database-url or set FORGE_DATABASE_URL",
            file=sys.stderr,
        )
        return None
    return create_session_factory(create_db_engine(url))


# --------------------------------------------------------------------------- #
# Mode: --attestation (offline DSSE verify)                                   #
# --------------------------------------------------------------------------- #


def _cmd_attestation(args: argparse.Namespace) -> int:
    try:
        raw = _read_source(args.attestation)
        envelope = DsseEnvelope.model_validate_json(raw)
    except (OSError, ValueError) as exc:
        print(f"error: invalid DSSE envelope: {exc}", file=sys.stderr)
        return 1
    public_key = _resolve_public_key(args.public_key)
    ok = DsseVerifier().verify(envelope, public_key_b64=public_key)
    _print_envelope_summary(envelope)
    print("VERIFIED" if ok else "REJECTED")
    return 0 if ok else 1


# --------------------------------------------------------------------------- #
# Mode: --run (DB-backed attestation verify)                                  #
# --------------------------------------------------------------------------- #


def _load_attestation(session: Session, run_id: uuid.UUID, workspace: str | None) -> Any:
    from sqlalchemy import or_, select

    from forge_db.models import Attestation

    query = select(Attestation).where(
        or_(Attestation.workflow_run_id == run_id, Attestation.agent_run_id == run_id)
    )
    if workspace:
        query = query.where(Attestation.workspace_id == uuid.UUID(workspace))
    query = query.order_by(Attestation.created_at.desc(), Attestation.id.desc())
    return session.scalars(query).first()


def _cmd_run(args: argparse.Namespace) -> int:
    try:
        run_id = uuid.UUID(args.run)
    except ValueError:
        print(f"error: --run must be a UUID: {args.run}", file=sys.stderr)
        return 1
    factory = _session_factory(args.database_url)
    if factory is None:
        return 3
    with factory() as session:
        row = _load_attestation(session, run_id, args.workspace)
        if row is None:
            print(f"error: no attestation found for run {run_id}", file=sys.stderr)
            return 1
        envelope = DsseEnvelope.model_validate(row.envelope)
        recorded_hash = row.payload_hash
        keyid = row.keyid
        attestation_id = row.id

    try:
        payload_bytes = base64.b64decode(envelope.payload, validate=True)
    except (ValueError, TypeError) as exc:
        print(f"error: envelope payload is not valid base64: {exc}", file=sys.stderr)
        return 1
    recomputed_hash = hashlib.sha256(pae(envelope.payloadType, payload_bytes)).hexdigest()
    hash_ok = recomputed_hash == recorded_hash

    public_key = _resolve_public_key(args.public_key)
    sig_ok = DsseVerifier().verify(envelope, public_key_b64=public_key)

    print(f"attestation: {attestation_id} (keyid {keyid})")
    _print_envelope_summary(envelope)
    print(f"payload_hash match: {hash_ok} (recorded={recorded_hash}, recomputed={recomputed_hash})")
    print(f"signature verified: {sig_ok}")
    ok = hash_ok and sig_ok
    print("VERIFIED" if ok else "REJECTED")
    return 0 if ok else 1


# --------------------------------------------------------------------------- #
# Mode: --audit-export (offline hash-chain re-walk)                           #
# --------------------------------------------------------------------------- #


def _rewalk_chain(lines: list[str]) -> tuple[bool, int, str | None]:
    """Re-verify an NDJSON audit-chain export offline.

    Recomputes each row's ``payload_hash``/``entry_hash`` from the exported
    field values and re-checks the per-workspace ``prev_hash`` linkage, mirroring
    :func:`forge_db.audit.chain.verify_chain` from parsed dicts. The first row of
    each workspace anchors on ``GENESIS_HASH`` (``seq == 1``) or trusts its own
    ``prev_hash`` (a filtered/range export that does not start at genesis); every
    row's own field integrity is still checked via its recomputed ``entry_hash``.
    Returns ``(ok, entries_checked, detail)``.
    """
    import json

    prev_by_ws: dict[uuid.UUID, str] = {}
    checked = 0
    for raw in lines:
        try:
            row = json.loads(raw)
            workspace_id = uuid.UUID(row["workspace_id"])
            seq = int(row["seq"])
            created_at = datetime.fromisoformat(row["created_at"])
        except (ValueError, KeyError, TypeError) as exc:
            return False, checked, f"malformed export row: {exc}"

        recorded_payload_hash = row.get("payload_hash") or ""
        prev = prev_by_ws.get(workspace_id)
        if prev is None:
            prev = GENESIS_HASH if seq == 1 else (row.get("prev_hash") or GENESIS_HASH)

        if row.get("prev_hash") != prev:
            return False, checked, f"prev_hash mismatch at ws={workspace_id} seq={seq}"

        recomputed_payload = compute_payload_hash(
            {"before": row.get("before"), "after": row.get("after"), "details": row.get("details")}
        )
        if recorded_payload_hash != recomputed_payload:
            return False, checked, f"payload_hash mismatch at ws={workspace_id} seq={seq}"

        recomputed_entry = compute_entry_hash(
            prev_hash=prev,
            workspace_id=workspace_id,
            seq=seq,
            occurred_at=created_at,
            actor_type=row.get("actor_type"),
            actor_id=_uuid_or_none(row.get("actor_id")),
            actor_label=row.get("actor_label"),
            action=row.get("action"),
            target_type=row.get("target_type"),
            target_id=_uuid_or_none(row.get("target_id")),
            scope_type=row.get("scope_type"),
            scope_id=_uuid_or_none(row.get("scope_id")),
            result=row.get("result"),
            payload_hash=recorded_payload_hash,
        )
        if row.get("entry_hash") != recomputed_entry:
            return False, checked, f"entry_hash mismatch at ws={workspace_id} seq={seq}"

        prev_by_ws[workspace_id] = recomputed_entry
        checked += 1

    return True, checked, None


def _cmd_audit_export(args: argparse.Namespace) -> int:
    try:
        text = _read_source(args.audit_export)
    except OSError as exc:
        print(f"error: cannot read export: {exc}", file=sys.stderr)
        return 1
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        print("error: empty audit export", file=sys.stderr)
        return 1
    ok, checked, detail = _rewalk_chain(lines)
    print(f"entries checked: {checked}")
    if ok:
        print("CHAIN INTACT")
        return 0
    print(f"reason: {detail}", file=sys.stderr)
    print("CHAIN BROKEN")
    return 1


# --------------------------------------------------------------------------- #
# Parser / entrypoint                                                         #
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="forge-verify",
        description="Verify attestations and audit-chain exports (exit non-zero on failure).",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--attestation",
        metavar="FILE",
        help="offline DSSE-verify an envelope JSON ('-' for stdin)",
    )
    mode.add_argument(
        "--run",
        metavar="ID",
        help="verify the stored Attestation for a workflow/agent run (DB-backed)",
    )
    mode.add_argument(
        "--audit-export",
        metavar="NDJSON",
        dest="audit_export",
        help="offline re-walk an NDJSON audit-chain export",
    )
    parser.add_argument(
        "--public-key",
        dest="public_key",
        help="base64 Ed25519 public key (default: public half of FORGE_ATTEST_SIGNING_KEY)",
    )
    parser.add_argument(
        "--database-url",
        dest="database_url",
        help="database URL for --run (else FORGE_DATABASE_URL)",
    )
    parser.add_argument(
        "--workspace",
        dest="workspace",
        help="workspace UUID to scope --run",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.attestation is not None:
        return _cmd_attestation(args)
    if args.run is not None:
        return _cmd_run(args)
    return _cmd_audit_export(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
