"""Postgres-backed policy-audit sink (policy-audit-sink persistence).

:class:`DbPolicyAuditSink` is a drop-in, durable alternative to
:class:`~forge_api.services.policy_service.InMemoryPolicyAuditSink` that satisfies
the **same** :class:`~forge_api.services.policy_service.PolicyAuditSink` seam
(``emit(PolicyDecisionEvent)``) — so the F29 composition root swaps it in behind
``FORGE_POLICY_AUDIT_BACKEND=db`` with no behavioural change. The default stays
``memory`` and the in-memory sink remains the unit-test default.

Where the in-memory sink appends each emitted ``policy.decision`` event to a
process-local list, this sink persists it durably: every ``emit`` opens its own
short unit-of-work and writes one append-only ``policy_rule_evaluation`` row (the
canonical, queryable F29 audit table created by migration 0011, hardened DB-side
by the F39 ``attach_immutability_trigger`` BEFORE UPDATE/DELETE block). The
compact, redacted :class:`~forge_api.services.policy_service.PolicyDecisionEvent`
maps field-for-field onto the row — it carries only the redacted projection
(never raw ``ToolCall.args`` / ``command``), so the durable trail inherits the
same F04/F10 redaction the event already guarantees. ``policy_snapshot_id`` and
the server-defaulted ``evaluated_at`` are the only row columns the event does not
carry (the event has no snapshot dimension; ``evaluated_at`` is stamped by the
database), matching how the in-memory sink also records neither.

Because the sink owns its own ``sessionmaker`` (the ``emit`` seam receives no
session), the referential guarantees of ``policy_rule_evaluation`` are enforced by
the database exactly as for a directly-written row: a non-null ``agent_run_id``
must resolve against the F07/F10 ``agent_run`` table or the insert is rejected.

Note on composition (F29): the ``emit`` seam is an *independent* audit stream —
in this foundation no HTTP route invokes ``PolicyService.evaluate_and_record``
(the only writer of the transactional row via a caller-supplied session), so
wiring this sink simply makes the emit stream itself durable. The default stays
``memory`` so every existing unit test is untouched.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from forge_api.services.policy_service import (
    InMemoryPolicyAuditSink,
    PolicyAuditSink,
    PolicyDecisionEvent,
)
from forge_db.models import PolicyRuleEvaluation

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

__all__ = ["DbPolicyAuditSink", "build_policy_audit_sink"]


class DbPolicyAuditSink:
    """A Postgres-backed policy-audit sink (implements ``PolicyAuditSink``)."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sf = session_factory

    def emit(self, event: PolicyDecisionEvent) -> None:
        """Persist ``event`` as one append-only ``policy_rule_evaluation`` row."""
        with self._sf() as session:
            session.add(
                PolicyRuleEvaluation(
                    workspace_id=event.workspace_id,
                    agent_run_id=event.agent_run_id,
                    step_id=event.step_id,
                    action=event.action,
                    base_effect=event.base_effect,
                    final_effect=event.final_effect,
                    requires_approval=event.requires_approval,
                    severity=event.severity,
                    matched_rule_ids=list(event.matched_rule_ids),
                    context_redacted=dict(event.context_redacted),
                )
            )
            session.commit()


# --------------------------------------------------------------------------- #
# Composition root                                                             #
# --------------------------------------------------------------------------- #


def build_policy_audit_sink() -> PolicyAuditSink:
    """Return the process-wide policy-audit sink selected by ``FORGE_POLICY_AUDIT_BACKEND``.

    ``memory`` (default) → the hermetic
    :class:`~forge_api.services.policy_service.InMemoryPolicyAuditSink` (unit-test
    default, no Postgres); ``db`` → the durable :class:`DbPolicyAuditSink` bound to
    the shared session factory. Both satisfy the same ``PolicyAuditSink`` seam, so
    the ``PolicyService`` is agnostic to which is wired.
    """
    from forge_api.settings import get_settings

    if get_settings().policy_audit_backend == "db":
        from forge_api.db import get_session_factory

        return DbPolicyAuditSink(get_session_factory())
    return InMemoryPolicyAuditSink()
