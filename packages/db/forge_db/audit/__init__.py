"""F39 audit-log machinery: chained writer, verifier, and query repository.

The canonical contract (DTOs, enums, hash helpers) lives in
``forge_contracts.audit``; the ORM models in ``forge_db.models.audit``. This
subpackage supplies the pieces both ``forge_api`` and ``forge_worker`` share:

- :class:`~forge_db.audit.writer.SqlAuditWriter` — the durable ``AuditSink``
  that assigns the per-workspace ``seq``/hash chain under the
  ``audit_chain_head`` row lock,
- :func:`~forge_db.audit.chain.verify_chain` — the tamper detector,
- :class:`~forge_db.audit.repository.AuditQueryRepository` — workspace-scoped,
  keyset-paginated reads (no update/delete surface), and
- :func:`~forge_db.audit.redaction.redact_metadata` — the deep-walk hook for
  F37's canonical ``SecretRedactor`` (duck-typed so ``forge_db`` takes no
  dependency on ``forge_auth``).

The reusable Postgres append-only trigger the slice doc assigns here already
ships as :func:`forge_db.base.attach_immutability_trigger` (F30 landed it);
it is re-exported for the documented F39 path.
"""

from __future__ import annotations

from forge_db.audit.chain import verify_chain
from forge_db.audit.redaction import MetadataRedactor, redact_metadata
from forge_db.audit.repository import AuditQueryRepository
from forge_db.audit.writer import SqlAuditWriter
from forge_db.base import attach_immutability_trigger

__all__ = [
    "AuditQueryRepository",
    "MetadataRedactor",
    "SqlAuditWriter",
    "attach_immutability_trigger",
    "redact_metadata",
    "verify_chain",
]
