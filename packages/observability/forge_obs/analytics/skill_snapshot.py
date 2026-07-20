"""Immutable per-run skill-profile snapshot (F40-OBS-ANALYTICS).

Captures the resolved :class:`~forge_contracts.SkillProfile` an ``agent_run``
executed under, at the moment it started — an audit trail answering "what
skill-profile directives were actually in force for this run", independent of
any later edit to the named profile. Analogous to the append-only
``policy_rule_evaluation`` audit table: one immutable row per run, written once,
never updated.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from pydantic import BaseModel

from forge_contracts import SkillProfile

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

    from forge_db.models.obs_analytics import SkillProfileSnapshot

__all__ = [
    "SkillProfileSnapshotDTO",
    "SqlSkillProfileSnapshotRepository",
    "build_directives_payload",
]


def build_directives_payload(profile: SkillProfile) -> dict[str, Any]:
    """Project a :class:`SkillProfile` into its JSON-safe snapshot payload."""
    return {
        "requires_plan": profile.requires_plan,
        "requires_tests_before_implementation": profile.requires_tests_before_implementation,
        "min_test_coverage": profile.min_test_coverage,
        "review_required": profile.review_required,
        "requires_human_approval_before_action": profile.requires_human_approval_before_action,
        "max_blast_radius": profile.max_blast_radius,
        "allowed_actions": sorted(profile.allowed_actions),
        "forbidden_actions": sorted(profile.forbidden_actions),
        "forbidden_shortcuts": sorted(profile.forbidden_shortcuts),
    }


class SkillProfileSnapshotDTO(BaseModel):
    """A recorded (or about-to-be-recorded) skill-profile snapshot."""

    id: UUID | None = None
    workspace_id: UUID
    agent_run_id: UUID
    profile_name: str
    min_test_coverage: int | None = None
    directives: dict[str, Any]
    captured_at: datetime | None = None


class SqlSkillProfileSnapshotRepository:
    """Insert-only writer/reader over ``skill_profile_snapshot``.

    Idempotent on ``agent_run_id`` (mirrors ``SqlCostLedger.upsert_event``): a
    replayed ``record`` for a run that already has a snapshot returns the
    existing row rather than raising or double-inserting — the row is
    immutable once written (DB-level trigger on Postgres), so a retried
    "record the snapshot for this run" call must be a safe no-op, not a crash.
    """

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    @staticmethod
    def _dto(row: SkillProfileSnapshot) -> SkillProfileSnapshotDTO:
        return SkillProfileSnapshotDTO(
            id=row.id,
            workspace_id=row.workspace_id,
            agent_run_id=row.agent_run_id,
            profile_name=row.profile_name,
            min_test_coverage=row.min_test_coverage,
            directives=dict(row.directives),
            captured_at=row.captured_at,
        )

    def record(
        self, *, workspace_id: UUID, agent_run_id: UUID, profile: SkillProfile
    ) -> SkillProfileSnapshotDTO:
        from sqlalchemy import select
        from sqlalchemy.exc import IntegrityError

        from forge_db.models.obs_analytics import SkillProfileSnapshot

        def _existing(session: Session) -> SkillProfileSnapshotDTO | None:
            row = session.scalars(
                select(SkillProfileSnapshot).where(
                    SkillProfileSnapshot.agent_run_id == agent_run_id
                )
            ).first()
            return None if row is None else self._dto(row)

        with self._session_factory() as session:
            found = _existing(session)
            if found is not None:
                return found
            row = SkillProfileSnapshot(
                workspace_id=workspace_id,
                agent_run_id=agent_run_id,
                profile_name=profile.name,
                min_test_coverage=profile.min_test_coverage,
                directives=build_directives_payload(profile),
            )
            session.add(row)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                raced = _existing(session)
                if raced is not None:
                    return raced
                raise
            session.refresh(row)
            return self._dto(row)

    def get(self, *, agent_run_id: UUID) -> SkillProfileSnapshotDTO | None:
        from sqlalchemy import select

        from forge_db.models.obs_analytics import SkillProfileSnapshot

        with self._session_factory() as session:
            row = session.scalars(
                select(SkillProfileSnapshot).where(
                    SkillProfileSnapshot.agent_run_id == agent_run_id
                )
            ).first()
            return None if row is None else self._dto(row)
