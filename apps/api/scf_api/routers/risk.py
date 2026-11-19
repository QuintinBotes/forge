"""Risk classifications, per-gate agreement and a dry-run classification (spec A §7, #193).

* ``GET  /risk/classifications`` — the workspace's recorded classifications, newest
  first, filterable by commit, agent run, workflow run and spec key
* ``GET  /risk/agreement``       — per gate, how many observations and the share that
  agreed over the last ``days``: the evidence for switching shadow to enforcing
* ``POST /risk/classify``        — classify changed paths against the given rules and
  record nothing (for authoring rules)

Every route needs the read permission and reads only the caller's workspace.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator
from sqlalchemy.orm import Session

from scf_api.auth.rbac import Permission
from scf_api.db import get_db
from scf_api.deps import Principal, get_current_principal
from scf_api.routers._rbac import require_permission
from scf_contracts import RiskClass, RiskRules
from scf_db.risk_repository import RiskRepository
from scf_risk import derive
from scf_risk.signals import signals_from_changes

router = APIRouter(prefix="/risk", tags=["risk"], dependencies=[Depends(get_current_principal)])

ReaderDep = Annotated[Principal, Depends(require_permission(Permission.READ))]
SessionDep = Annotated[Session, Depends(get_db)]

#: Bounds on a dry run's caller-supplied rules, checked before they validate, so that a
#: reader cannot hold the API with them (validation and matching are CPU-bound).
_MAX_RULES = 500
_MAX_GLOBS = 2_000
_MAX_GLOB_LENGTH = 1_024
_MAX_PATH_GLOB_PAIRS = 200_000


class RiskClassificationOut(BaseModel):
    """One recorded classification of an agent change."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    commit_sha: str
    pr_numbers: list[int]
    agent_run_id: uuid.UUID | None
    workflow_run_id: uuid.UUID | None
    spec_key: str | None
    spec_version: int | None
    derived_class: RiskClass
    declared_class: RiskClass | None
    final_class: RiskClass
    matched_rule_ids: list[str]
    signals: dict[str, Any]
    override_approver: str | None
    override_reason: str | None
    enforcement_mode: str
    fallback_applied: bool
    created_at: datetime


class GateAgreement(BaseModel):
    gate_name: str
    observations: int
    agreed: int
    agreement_rate: float


class AgreementOut(BaseModel):
    days: int
    since: datetime
    gates: list[GateAgreement]


class ChangedPath(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    lines_added: int = Field(default=0, ge=0)
    lines_removed: int = Field(default=0, ge=0)


class ClassifyRequest(BaseModel):
    changes: list[ChangedPath] = Field(max_length=10_000)
    #: Without rules nothing is classified, as for a policy with no ``risk_rules``.
    rules: RiskRules | None = None

    @field_validator("rules", mode="before")
    @classmethod
    def _bounded(cls, rules: Any, info: ValidationInfo) -> Any:
        derive = rules.get("derive") if isinstance(rules, dict) else None
        if not isinstance(derive, list):
            return rules  # no globs to match; validation reports any other shape
        if len(derive) > _MAX_RULES:
            raise ValueError(f"a dry run takes at most {_MAX_RULES} rules")
        globs = [
            glob
            for rule in derive
            if isinstance(rule, dict) and isinstance(rule.get("paths"), list)
            for glob in rule["paths"]
        ]
        if len(globs) > _MAX_GLOBS:
            raise ValueError(f"a dry run takes at most {_MAX_GLOBS} path globs")
        if any(isinstance(glob, str) and len(glob) > _MAX_GLOB_LENGTH for glob in globs):
            raise ValueError(f"a path glob is at most {_MAX_GLOB_LENGTH} characters")
        if len(globs) * len(info.data.get("changes") or []) > _MAX_PATH_GLOB_PAIRS:
            raise ValueError(f"changed paths times globs is at most {_MAX_PATH_GLOB_PAIRS}")
        return rules


class ClassifyResponse(BaseModel):
    risk_class: RiskClass | None
    matched_rule_ids: list[str]


@router.get("/classifications", response_model=list[RiskClassificationOut])
def list_classifications(
    principal: ReaderDep,
    session: SessionDep,
    commit_sha: Annotated[str | None, Query(max_length=64)] = None,
    agent_run_id: Annotated[uuid.UUID | None, Query()] = None,
    workflow_run_id: Annotated[uuid.UUID | None, Query()] = None,
    spec_key: Annotated[str | None, Query(max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[RiskClassificationOut]:
    """The caller's workspace's classifications, newest first."""
    rows = RiskRepository(session).list_classifications(
        principal.workspace_id,
        commit_sha=commit_sha,
        agent_run_id=agent_run_id,
        workflow_run_id=workflow_run_id,
        spec_key=spec_key,
        limit=limit,
        offset=offset,
    )
    return [RiskClassificationOut.model_validate(row) for row in rows]


@router.get("/agreement", response_model=AgreementOut)
def agreement(
    principal: ReaderDep,
    session: SessionDep,
    days: Annotated[int, Query(ge=1, le=366)] = 30,
) -> AgreementOut:
    """Per gate observed in the window: observations, how many agreed, and the share."""
    since = datetime.now(UTC) - timedelta(days=days)
    counts = RiskRepository(session).agreement(principal.workspace_id, since=since)
    return AgreementOut(
        days=days,
        since=since,
        gates=[
            GateAgreement(
                gate_name=gate, observations=total, agreed=agreed, agreement_rate=agreed / total
            )
            for gate, total, agreed in counts
        ],
    )


@router.post("/classify", response_model=ClassifyResponse)
def classify(_principal: ReaderDep, body: ClassifyRequest) -> ClassifyResponse:
    """The class the rules derive for the changed paths. Records nothing."""
    if body.rules is None:
        return ClassifyResponse(risk_class=None, matched_rule_ids=[])
    signals = signals_from_changes((c.path, c.lines_added, c.lines_removed) for c in body.changes)
    derived = derive(signals, body.rules)
    return ClassifyResponse(
        risk_class=derived.risk_class, matched_rule_ids=derived.matched_rule_ids
    )


__all__ = ["router"]
