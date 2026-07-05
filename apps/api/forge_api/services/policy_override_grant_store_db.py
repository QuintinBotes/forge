"""Postgres-backed policy-override grant store (F36 J5 mint/consume seam).

:class:`DbGrantStore` is a drop-in, durable alternative to
:class:`~forge_approval.providers.policy_override.InMemoryGrantStore` that
satisfies the **same** ``GrantStore`` seam (``mint`` / async ``consume`` /
``all``) the ``policy_override`` resolution hook + F06/F29 resume path depend on
— so the F36 composition root swaps it in behind
``FORGE_OVERRIDE_GRANT_BACKEND=db`` with no behavioural change. The default stays
``memory`` and the in-memory store remains the unit-test default.

It lives in ``apps/api`` (not the frozen, DB-free ``forge_approval`` SDK), exactly
like the sibling :class:`~forge_api.services.approval_repository_db.SqlAlchemyApprovalRepository`
and :class:`~forge_api.observability.audit_db.DbAuditStore`: the SDK stays pure
domain and this adapter maps the domain
:class:`~forge_approval.models.PolicyOverrideGrant` onto the canonical
``policy_override_grant`` ORM row in ``forge_db`` (created by migration 0019, with
the partial-unique ``uq_active_override`` on active — unconsumed — grants).

The single-active + single-use DB invariants are enforced *by the database*:

* **single-active** — ``mint`` selects the active (unconsumed, unexpired) grant
  ``FOR UPDATE`` and returns it verbatim when present (idempotent, mirroring the
  in-memory store); otherwise it reaps any stale unconsumed-but-expired row (so
  the partial unique index is free) and inserts a fresh grant. A concurrent
  racing insert trips ``uq_active_override`` and is resolved by returning the
  winner — never a duplicate active grant.
* **single-use** — ``consume`` is one atomic ``UPDATE ... SET consumed = true
  WHERE active`` (the exact statement the worker resume task runs); its rowcount
  decides the boolean, so double-consumption is impossible even across workers.
* **TTL expiry** — every active check carries ``expires_at > now``; an expired
  grant denies on ``consume`` and does not block a fresh ``mint``.

One storage-boundary detail (shared with every DB-backed repo here): the row is
workspace-scoped (``policy_override_grant`` is a ``WorkspaceScopedModel``) but the
domain grant carries no workspace — the tenant is derived from the ``agent_run``
the grant is bound to, which is authoritative and always present in a coherent DB
deployment (the FK would otherwise reject the insert anyway).
"""

from __future__ import annotations

import builtins
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from forge_approval.models import PolicyOverrideGrant
from forge_db.models import AgentRun
from forge_db.models import PolicyOverrideGrant as PolicyOverrideGrantRow

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

__all__ = ["DbGrantStore"]


class UnknownAgentRunError(LookupError):
    """A grant references an ``agent_run`` that does not exist in the database."""

    def __init__(self, agent_run_id: uuid.UUID) -> None:
        super().__init__(f"unknown agent_run {agent_run_id}; cannot derive workspace")
        self.agent_run_id = agent_run_id


def _aware(value: datetime | None) -> datetime | None:
    """Normalise a stored timestamp to timezone-aware UTC (SQLite reads naive)."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class DbGrantStore:
    """A Postgres-backed policy-override grant store (implements ``GrantStore``)."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sf = session_factory

    # ------------------------------------------------------------------ #
    # Mapping                                                             #
    # ------------------------------------------------------------------ #

    def _to_domain(self, row: PolicyOverrideGrantRow) -> PolicyOverrideGrant:
        """Rebuild the domain grant from a persisted row."""
        return PolicyOverrideGrant(
            id=row.id,
            approval_request_id=row.approval_request_id,
            agent_run_id=row.agent_run_id,
            action_fingerprint=row.action_fingerprint,
            granted_by=row.granted_by,
            consumed=row.consumed,
            expires_at=_aware(row.expires_at),  # type: ignore[arg-type]
            created_at=_aware(row.created_at),
        )

    # ------------------------------------------------------------------ #
    # GrantStore seam                                                     #
    # ------------------------------------------------------------------ #

    def mint(self, grant: PolicyOverrideGrant) -> PolicyOverrideGrant:
        """Store a grant; at most one active per (agent_run_id, fingerprint).

        Returns the existing active grant unchanged when one is present
        (idempotent, exactly like the in-memory store); otherwise inserts a
        fresh grant, reaping any stale unconsumed-but-expired row first so the
        partial-unique ``uq_active_override`` never blocks a legitimate re-mint.
        """
        now = datetime.now(UTC)
        with self._sf() as session:
            # Lock every unconsumed row for this (run, fingerprint) so the
            # check-reap-insert below is atomic against a concurrent mint.
            unconsumed = session.scalars(
                select(PolicyOverrideGrantRow)
                .where(
                    PolicyOverrideGrantRow.agent_run_id == grant.agent_run_id,
                    PolicyOverrideGrantRow.action_fingerprint == grant.action_fingerprint,
                    PolicyOverrideGrantRow.consumed.is_(False),
                )
                .with_for_update()
            ).all()
            active = next(
                (r for r in unconsumed if _aware(r.expires_at) > now),  # type: ignore[operator]
                None,
            )
            if active is not None:
                return self._to_domain(active)

            # Any remaining unconsumed row is expired garbage — reap it (marking
            # consumed) to free the WHERE consumed = false partial unique index.
            for stale in unconsumed:
                stale.consumed = True

            workspace_id = session.scalars(
                select(AgentRun.workspace_id).where(AgentRun.id == grant.agent_run_id)
            ).first()
            if workspace_id is None:
                raise UnknownAgentRunError(grant.agent_run_id)

            row = PolicyOverrideGrantRow(
                id=grant.id,
                workspace_id=workspace_id,
                approval_request_id=grant.approval_request_id,
                agent_run_id=grant.agent_run_id,
                action_fingerprint=grant.action_fingerprint,
                granted_by=grant.granted_by,
                consumed=grant.consumed,
                expires_at=grant.expires_at,
            )
            if grant.created_at is not None:
                row.created_at = grant.created_at
            session.add(row)
            try:
                session.commit()
            except IntegrityError:
                # A concurrent mint won the race and inserted the active grant
                # (uq_active_override); return the winner rather than duplicate.
                session.rollback()
                winner = session.scalars(
                    select(PolicyOverrideGrantRow)
                    .where(
                        PolicyOverrideGrantRow.agent_run_id == grant.agent_run_id,
                        PolicyOverrideGrantRow.action_fingerprint == grant.action_fingerprint,
                        PolicyOverrideGrantRow.consumed.is_(False),
                    )
                    .with_for_update()
                ).first()
                if winner is not None:
                    return self._to_domain(winner)
                raise
            return self._to_domain(row)

    async def consume(self, *, agent_run_id: uuid.UUID, action_fingerprint: str) -> bool:
        """Atomically consume the active grant for this exact action, if any.

        Returns ``True`` only when a matching unconsumed, unexpired grant was
        flipped by *this* single ``UPDATE`` — never granting future scope.
        """
        now = datetime.now(UTC)
        with self._sf() as session:
            result = session.execute(
                update(PolicyOverrideGrantRow)
                .where(
                    PolicyOverrideGrantRow.agent_run_id == agent_run_id,
                    PolicyOverrideGrantRow.action_fingerprint == action_fingerprint,
                    PolicyOverrideGrantRow.consumed.is_(False),
                    PolicyOverrideGrantRow.expires_at > now,
                )
                .values(consumed=True)
            )
            session.commit()
            return (result.rowcount or 0) > 0

    def all(self) -> builtins.list[PolicyOverrideGrant]:
        """Every stored grant, in stable insertion order."""
        with self._sf() as session:
            rows = session.scalars(
                select(PolicyOverrideGrantRow).order_by(
                    PolicyOverrideGrantRow.created_at.asc(),
                    PolicyOverrideGrantRow.id.asc(),
                )
            ).all()
            return [self._to_domain(r) for r in rows]
