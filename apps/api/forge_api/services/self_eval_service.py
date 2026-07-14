"""Persistence for the Self-Eval Gate baseline (F41).

The Self-Eval Gate refuses a model/prompt/router config change if, re-evaluated
on a workspace's private per-repo suite, its resolution rate drops below a frozen
baseline. This service owns that baseline: reading it (the gate's
``baseline_for`` lookup) and writing it when a run establishes or an admin
promotes one. It is deliberately storage-only — the *policy* of when to promote
a baseline lives in the run/enforcement layer, and callers must pass an already
redacted ``config`` snapshot (no secrets ever land in this table).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from forge_db.models.benchmark import SelfEvalBaseline


@dataclass(frozen=True)
class BaselineRecord:
    """A workspace's frozen Self-Eval baseline for one private suite."""

    workspace_id: uuid.UUID
    benchmark_suite_id: uuid.UUID
    baseline_rate: float
    resolved: int
    total: int


class SelfEvalService:
    """Read/write the per-(workspace, suite) Self-Eval baseline resolution rate."""

    def __init__(self, *, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def workspace_baseline(self, workspace_id: uuid.UUID) -> float | None:
        """The rate a config change in this workspace must not fall below.

        Cold start (no baseline recorded yet) returns ``None`` so the gate is a
        no-op. If several private suites in the workspace carry a baseline, the
        most recently updated one wins — this is the value bound as the gate's
        ``baseline_for`` lookup.
        """
        with self._session_factory() as session:
            stmt = (
                select(SelfEvalBaseline.baseline_rate)
                .where(SelfEvalBaseline.workspace_id == workspace_id)
                .order_by(SelfEvalBaseline.updated_at.desc())
            )
            return session.scalars(stmt).first()

    def baseline_for_suite(
        self, workspace_id: uuid.UUID, benchmark_suite_id: uuid.UUID
    ) -> BaselineRecord | None:
        """The full baseline record for one (workspace, suite), or ``None``."""
        with self._session_factory() as session:
            row = self._get(session, workspace_id, benchmark_suite_id)
            return _to_record(row) if row is not None else None

    def record_baseline(
        self,
        *,
        workspace_id: uuid.UUID,
        benchmark_suite_id: uuid.UUID,
        resolved: int,
        total: int,
        resolution_rate: float,
        config: Mapping[str, Any],
        recorded_by: uuid.UUID | None = None,
        overwrite: bool = True,
    ) -> BaselineRecord:
        """Upsert the baseline for (workspace, suite); ``config`` must be redacted.

        With ``overwrite=False`` an existing baseline is left untouched (cold-start
        establish semantics) and its current value is returned — so a regressing
        run can never silently *lower* the bar it is meant to defend.
        """
        with self._session_factory() as session:
            row = self._get(session, workspace_id, benchmark_suite_id)
            if row is None:
                row = SelfEvalBaseline(
                    workspace_id=workspace_id,
                    benchmark_suite_id=benchmark_suite_id,
                    baseline_rate=resolution_rate,
                    resolved=resolved,
                    total=total,
                    config=dict(config),
                    recorded_by=recorded_by,
                )
                session.add(row)
            elif overwrite:
                row.baseline_rate = resolution_rate
                row.resolved = resolved
                row.total = total
                row.config = dict(config)
                row.recorded_by = recorded_by
            session.commit()
            session.refresh(row)
            return _to_record(row)

    @staticmethod
    def _get(
        session: Session, workspace_id: uuid.UUID, benchmark_suite_id: uuid.UUID
    ) -> SelfEvalBaseline | None:
        return session.scalars(
            select(SelfEvalBaseline).where(
                SelfEvalBaseline.workspace_id == workspace_id,
                SelfEvalBaseline.benchmark_suite_id == benchmark_suite_id,
            )
        ).one_or_none()


def _to_record(row: SelfEvalBaseline) -> BaselineRecord:
    return BaselineRecord(
        workspace_id=row.workspace_id,
        benchmark_suite_id=row.benchmark_suite_id,
        baseline_rate=row.baseline_rate,
        resolved=row.resolved,
        total=row.total,
    )
