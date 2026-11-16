"""F39 audit-log machinery: chained writer, verifier, and query repository.

The canonical contract (DTOs, enums, hash helpers) lives in
``scf_contracts.audit``; the ORM models in ``scf_db.models.audit``. This
subpackage supplies the pieces both ``scf_api`` and ``scf_worker`` share:

- :class:`~scf_db.audit.writer.SqlAuditWriter` — the durable ``AuditSink``
  that assigns the per-workspace ``seq``/hash chain under the
  ``audit_chain_head`` row lock,
- :func:`~scf_db.audit.chain.verify_chain` — the tamper detector,
- :class:`~scf_db.audit.repository.AuditQueryRepository` — workspace-scoped,
  keyset-paginated reads (no update/delete surface), and
- :func:`~scf_db.audit.redaction.redact_metadata` — the deep-walk hook for
  F37's canonical ``SecretRedactor`` (duck-typed so ``scf_db`` takes no
  dependency on ``scf_auth``).

The reusable Postgres append-only trigger the slice doc assigns here already
ships as :func:`scf_db.base.attach_immutability_trigger` (F30 landed it);
it is re-exported for the documented F39 path.
"""

from __future__ import annotations

from scf_db.audit.chain import verify_chain
from scf_db.audit.redaction import MetadataRedactor, redact_metadata
from scf_db.audit.repository import AuditQueryRepository
from scf_db.audit.writer import SqlAuditWriter
from scf_db.base import attach_immutability_trigger

__all__ = [
    "AuditQueryRepository",
    "MetadataRedactor",
    "SqlAuditWriter",
    "attach_immutability_trigger",
    "redact_metadata",
    "verify_chain",
]
