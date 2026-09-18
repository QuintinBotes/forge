"""Request/response schemas for the Adaptive Orchestration settings API
(``ao-settings-api``): per-role model+effort config, the workspace-wide
``tier -> model`` map / complexity thresholds / auto-route toggle, a
routing-preview endpoint, and the Self-Eval Gate status/run surface.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from forge_agent.providers.config import ProviderName
from forge_contracts.orchestration_config import AgentRole, Effort, RoleConfigSource
from forge_orchestration_policy import Strategy, Tier

__all__ = [
    "AoSettingsOut",
    "AoSettingsUpdateRequest",
    "RoleConfigListResponse",
    "RoleConfigOut",
    "RoleConfigUpsertRequest",
    "RoutingPreviewRequest",
    "RoutingPreviewResponse",
    "SelfEvalBaselineOut",
    "SelfEvalRunAccepted",
    "SelfEvalStatusOut",
    "SelfEvalSuiteOut",
]


class RoleConfigOut(BaseModel):
    """The effective ``{model_or_tier, effort}`` for one role, plus its source."""

    role: AgentRole
    model_or_tier: str
    effort: Effort
    source: RoleConfigSource


class RoleConfigListResponse(BaseModel):
    """Body for ``GET /ao/role-config``."""

    items: list[RoleConfigOut]


class RoleConfigUpsertRequest(BaseModel):
    """Body for ``PUT /ao/role-config/{role}``: pin a human override."""

    model_or_tier: str
    effort: Effort


class AoSettingsOut(BaseModel):
    """The effective workspace-wide Adaptive Orchestration settings.

    ``junior_max``/``medior_max`` are always populated with the *effective*
    threshold (the workspace override, or the hardcoded default when unset) so
    a caller never has to know the fallback rule itself.
    """

    workspace_id: UUID
    auto_route: bool
    tier_model_overrides: dict[str, dict[str, str]]
    junior_max: int
    medior_max: int
    junior_max_is_default: bool
    medior_max_is_default: bool


class AoSettingsUpdateRequest(BaseModel):
    """``PUT /ao/settings`` — every field is optional (partial update).

    ``clear_junior_max``/``clear_medior_max`` reset the corresponding threshold
    back to the hardcoded default (setting the field to ``None`` cannot itself
    mean "clear" since it also means "leave unchanged").
    """

    auto_route: bool | None = None
    tier_model_overrides: dict[str, dict[str, str]] | None = None
    junior_max: int | None = None
    medior_max: int | None = None
    clear_junior_max: bool = False
    clear_medior_max: bool = False


class RoutingPreviewRequest(BaseModel):
    """A sample task's sizing signals (mirrors ``SizingSignals``, all optional)."""

    kind: str = "feature"
    priority: str = "medium"
    blast_radius: str | None = None
    file_count: int = 0
    repo_count: int = 1
    requirement_count: int = 0
    acceptance_criteria_count: int = 0
    touches_contracts: bool = False
    touches_security: bool = False
    dependency_count: int = 0
    open_questions_count: int = 0
    underspecified: bool = False
    provider: ProviderName = ProviderName.anthropic


class RoutingPreviewResponse(BaseModel):
    """What tier/model/strategy the sample task in ``RoutingPreviewRequest`` gets."""

    tier: Tier
    strategy: Strategy
    score: int
    reasons: list[str] = Field(default_factory=list)
    model: str
    provider: ProviderName
    junior_max: int
    medior_max: int
    auto_route_enabled: bool


class SelfEvalSuiteOut(BaseModel):
    """The workspace's private Self-Eval suite (case content is never exposed)."""

    id: UUID
    slug: str
    version: str
    title: str
    task_count: int
    repo_id: str | None
    published: bool


class SelfEvalBaselineOut(BaseModel):
    """The frozen baseline the Self-Eval Gate blocks regressions against."""

    benchmark_suite_id: UUID
    baseline_rate: float
    resolved: int
    total: int
    #: When the baseline row was last minted/refreshed by a scoring run.
    recorded_at: datetime


class SelfEvalStatusOut(BaseModel):
    """Body for ``GET /ao/self-eval/status`` — raw facts, no derived verdicts.

    ``suite``/``baseline`` are ``None`` on cold start; ``enforced`` mirrors the
    ``self_eval_enforce`` app setting. The UI derives gate status from these.
    """

    workspace_id: UUID
    enforced: bool
    suite: SelfEvalSuiteOut | None
    baseline: SelfEvalBaselineOut | None


class SelfEvalRunAccepted(BaseModel):
    """Body for ``POST /ao/self-eval/runs`` (202): the run is queued, not done."""

    status: Literal["queued"] = "queued"
    task: str
    workspace_id: UUID
    benchmark_suite_id: UUID
