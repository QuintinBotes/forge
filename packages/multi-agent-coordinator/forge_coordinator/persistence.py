"""Persistence of ``sub_agent_run`` rows (F27 §3.1).

One immutable audit row per spawned specialist, linked to its child
``agent_run_id``. All persisted JSON (objective / artifact / error) is
secret-redacted before it lands. An in-memory sink keeps unit tests hermetic; the
SQLAlchemy sink writes the real ``sub_agent_run`` table on Postgres.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from forge_contracts import (
    RunStatus,
    SubAgentArtifact,
    SubAgentResult,
    SubAgentRole,
    TokenUsage,
)
from forge_coordinator.redaction import redact_obj

__all__ = [
    "InMemorySubAgentRunSink",
    "SqlAlchemySubAgentRunSink",
    "SubAgentRunCreate",
    "SubAgentRunSink",
]

_RESULT_STATUS_TO_RUN = {
    "succeeded": RunStatus.SUCCEEDED,
    "failed": RunStatus.FAILED,
    "blocked": RunStatus.CANCELLED,
    "skipped": RunStatus.CANCELLED,
    "awaiting_input": RunStatus.ESCALATED,
    "running": RunStatus.RUNNING,
    "pending": RunStatus.PENDING,
}
_RUN_TO_RESULT_STATUS = {
    RunStatus.SUCCEEDED: "succeeded",
    RunStatus.FAILED: "failed",
    RunStatus.ESCALATED: "awaiting_input",
    RunStatus.RUNNING: "running",
    RunStatus.PENDING: "pending",
    RunStatus.CANCELLED: "blocked",
}


@dataclass
class SubAgentRunCreate:
    """The fields needed to insert a ``sub_agent_run`` row at dispatch time."""

    parent_agent_run_id: uuid.UUID
    workspace_id: uuid.UUID
    assignment_id: str
    role: SubAgentRole
    pattern: str
    ordinal: int
    objective: dict[str, Any] = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)
    optional: bool = False
    status: RunStatus = RunStatus.RUNNING


@runtime_checkable
class SubAgentRunSink(Protocol):
    """Persists and reads ``sub_agent_run`` rows."""

    def create(self, row: SubAgentRunCreate) -> uuid.UUID: ...

    def update(self, sub_agent_run_id: uuid.UUID, **fields: Any) -> None: ...

    def list_for_parent(self, parent_agent_run_id: uuid.UUID) -> list[SubAgentResult]: ...


def _to_result(record: dict[str, Any]) -> SubAgentResult:
    artifact_raw = record.get("artifact") or {}
    if isinstance(artifact_raw, dict) and artifact_raw:
        artifact = SubAgentArtifact.model_validate(artifact_raw)
    else:
        artifact = SubAgentArtifact(kind="code_change", summary="")
    token_raw = record.get("token_usage") or {}
    token = TokenUsage(
        input_tokens=int(token_raw.get("input_tokens", 0) or 0),
        output_tokens=int(token_raw.get("output_tokens", 0) or 0),
    )
    status = record.get("status")
    result_status = _RUN_TO_RESULT_STATUS.get(status, "succeeded") if status else "succeeded"
    return SubAgentResult(
        assignment_id=record["assignment_id"],
        role=SubAgentRole(record["role"]),
        agent_run_id=record.get("agent_run_id"),
        status=result_status,  # type: ignore[arg-type]
        confidence=float(record.get("confidence") or 0.0),
        artifact=artifact,
        token_usage=token,
    )


class InMemorySubAgentRunSink:
    """A hermetic sink for unit/integration tests."""

    def __init__(self) -> None:
        self._rows: dict[uuid.UUID, dict[str, Any]] = {}

    def create(self, row: SubAgentRunCreate) -> uuid.UUID:
        run_id = uuid.uuid4()
        self._rows[run_id] = {
            "id": run_id,
            "parent_agent_run_id": row.parent_agent_run_id,
            "workspace_id": row.workspace_id,
            "assignment_id": row.assignment_id,
            "role": row.role.value,
            "pattern": row.pattern,
            "ordinal": row.ordinal,
            "depends_on": list(row.depends_on),
            "optional": row.optional,
            "status": row.status,
            "objective": redact_obj(row.objective),
            "artifact": {},
            "token_usage": {},
            "agent_run_id": None,
            "confidence": None,
            "branch_name": None,
            "merged": False,
            "error": None,
            "started_at": datetime.now(UTC),
            "completed_at": None,
        }
        return run_id

    def update(self, sub_agent_run_id: uuid.UUID, **fields: Any) -> None:
        row = self._rows[sub_agent_run_id]
        for key in ("objective", "artifact", "error"):
            if key in fields and fields[key] is not None:
                fields[key] = redact_obj(fields[key])
        row.update(fields)

    def list_for_parent(self, parent_agent_run_id: uuid.UUID) -> list[SubAgentResult]:
        rows = [
            r for r in self._rows.values() if r["parent_agent_run_id"] == parent_agent_run_id
        ]
        rows.sort(key=lambda r: (r["ordinal"], r["assignment_id"]))
        return [_to_result(r) for r in rows]

    # Test/diagnostic helpers ------------------------------------------------ #
    def rows_for_parent(self, parent_agent_run_id: uuid.UUID) -> list[dict[str, Any]]:
        rows = [
            r for r in self._rows.values() if r["parent_agent_run_id"] == parent_agent_run_id
        ]
        rows.sort(key=lambda r: (r["ordinal"], r["assignment_id"]))
        return rows


class SqlAlchemySubAgentRunSink:
    """Writes the real ``sub_agent_run`` table via a sync ``Session`` factory."""

    def __init__(self, session_factory: Callable[[], Any]) -> None:
        self._session_factory = session_factory

    def create(self, row: SubAgentRunCreate) -> uuid.UUID:
        from forge_db.models import SubAgentRun

        run_id = uuid.uuid4()
        with self._session_factory() as session:
            session.add(
                SubAgentRun(
                    id=run_id,
                    parent_agent_run_id=row.parent_agent_run_id,
                    workspace_id=row.workspace_id,
                    assignment_id=row.assignment_id,
                    role=row.role.value,
                    pattern=row.pattern,
                    ordinal=row.ordinal,
                    depends_on=list(row.depends_on),
                    optional=row.optional,
                    status=row.status,
                    objective=redact_obj(row.objective),
                    started_at=datetime.now(UTC),
                )
            )
            session.commit()
        return run_id

    def update(self, sub_agent_run_id: uuid.UUID, **fields: Any) -> None:
        from forge_db.models import SubAgentRun

        for key in ("objective", "artifact", "error"):
            if key in fields and fields[key] is not None:
                fields[key] = redact_obj(fields[key])
        with self._session_factory() as session:
            obj = session.get(SubAgentRun, sub_agent_run_id)
            if obj is None:  # pragma: no cover - defensive
                return
            for key, value in fields.items():
                setattr(obj, key, value)
            session.commit()

    def list_for_parent(self, parent_agent_run_id: uuid.UUID) -> list[SubAgentResult]:
        from sqlalchemy import select

        from forge_db.models import SubAgentRun

        with self._session_factory() as session:
            rows = (
                session.execute(
                    select(SubAgentRun)
                    .where(SubAgentRun.parent_agent_run_id == parent_agent_run_id)
                    .order_by(SubAgentRun.ordinal, SubAgentRun.assignment_id)
                )
                .scalars()
                .all()
            )
            return [
                _to_result(
                    {
                        "assignment_id": r.assignment_id,
                        "role": r.role,
                        "agent_run_id": r.agent_run_id,
                        "status": r.status,
                        "confidence": r.confidence,
                        "artifact": r.artifact,
                        "token_usage": r.token_usage,
                    }
                )
                for r in rows
            ]


def result_status_to_run(status: str) -> RunStatus:
    """Map a :class:`SubAgentResult` status literal to :class:`RunStatus`."""
    return _RESULT_STATUS_TO_RUN.get(status, RunStatus.PENDING)
