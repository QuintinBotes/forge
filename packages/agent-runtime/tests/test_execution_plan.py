"""Tests for the Adaptive Orchestration ExecutionPlan policy (ao-policy).

Pure unit tests: a fake in-memory ``RoleConfigStore`` + the default tier->model
router, no database and no live model. Exercises the three required cases
(simple->single, complex->swarm, override wins) plus per-role model resolution,
default-tier escalation, and loop-depth scaling.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from forge_agent import ExecutionPlan, ModelRouter, ProviderName, plan_execution
from forge_agent.execution_plan import plan_role_execution
from forge_contracts import Priority, TaskKind
from forge_contracts.orchestration_config import AgentRole, Effort, RoleConfigOverride
from forge_orchestration_policy import SizingSignals, score_complexity

WORKSPACE = uuid.uuid4()
PROJECT = uuid.uuid4()


@dataclass
class FakeRoleConfigStore:
    """In-memory double conforming structurally to ``RoleConfigStore``."""

    _rows: dict[tuple[uuid.UUID, uuid.UUID | None, AgentRole], RoleConfigOverride] = field(
        default_factory=dict
    )

    def get_override(
        self, workspace_id: uuid.UUID, role: AgentRole, *, project_id: uuid.UUID | None = None
    ) -> RoleConfigOverride | None:
        return self._rows.get((workspace_id, project_id, role))

    def upsert_override(
        self,
        workspace_id: uuid.UUID,
        role: AgentRole,
        model_or_tier: str,
        effort: Effort,
        *,
        project_id: uuid.UUID | None = None,
    ) -> RoleConfigOverride:
        row = RoleConfigOverride(
            workspace_id=workspace_id,
            project_id=project_id,
            role=role,
            model_or_tier=model_or_tier,
            effort=effort,
        )
        self._rows[(workspace_id, project_id, role)] = row
        return row

    def delete_override(
        self, workspace_id: uuid.UUID, role: AgentRole, *, project_id: uuid.UUID | None = None
    ) -> bool:
        return self._rows.pop((workspace_id, project_id, role), None) is not None

    def list_overrides(
        self, workspace_id: uuid.UUID, *, project_id: uuid.UUID | None = None
    ) -> list[RoleConfigOverride]:
        return [
            row
            for (ws, proj, _role), row in self._rows.items()
            if ws == workspace_id and (project_id is None or proj == project_id)
        ]


def _simple_signals() -> SizingSignals:
    # A trivial chore: junior tier, single strategy.
    return SizingSignals(kind=TaskKind.CHORE, priority=Priority.LOW)


def _complex_signals() -> SizingSignals:
    # Cross-cutting, contract- and security-touching work: senior + swarm.
    return SizingSignals(
        kind=TaskKind.FEATURE,
        priority=Priority.HIGH,
        blast_radius="high",
        file_count=40,
        touches_contracts=True,
        touches_security=True,
        requirement_count=12,
    )


def _router() -> ModelRouter:
    return ModelRouter(provider=ProviderName.anthropic)


def test_simple_task_plans_single_strategy() -> None:
    sizing = score_complexity(_simple_signals())
    assert sizing.tier == "junior"
    assert sizing.strategy == "single"

    plan = plan_execution(
        signals=_simple_signals(),
        store=FakeRoleConfigStore(),
        workspace_id=WORKSPACE,
        provider=ProviderName.anthropic,
    )
    assert isinstance(plan, ExecutionPlan)
    assert plan.strategy == "single"
    assert plan.strategy_source == "complexity"


def test_complex_task_plans_swarm_strategy() -> None:
    sizing = score_complexity(_complex_signals())
    assert sizing.tier == "senior"
    assert sizing.strategy == "swarm"

    plan = plan_execution(
        signals=_complex_signals(),
        store=FakeRoleConfigStore(),
        workspace_id=WORKSPACE,
        provider=ProviderName.anthropic,
    )
    assert plan.strategy == "swarm"
    assert plan.strategy_source == "complexity"


def test_explicit_strategy_override_wins_over_sizing() -> None:
    # A complex task would size to swarm; an explicit override forces single.
    plan = plan_execution(
        signals=_complex_signals(),
        store=FakeRoleConfigStore(),
        workspace_id=WORKSPACE,
        provider=ProviderName.anthropic,
        strategy_override="single",
    )
    assert plan.sizing.strategy == "swarm"
    assert plan.strategy == "single"
    assert plan.strategy_source == "override"

    # And the reverse: a simple task forced up to a swarm.
    plan2 = plan_execution(
        signals=_simple_signals(),
        store=FakeRoleConfigStore(),
        workspace_id=WORKSPACE,
        provider=ProviderName.anthropic,
        strategy_override="swarm",
    )
    assert plan2.sizing.strategy == "single"
    assert plan2.strategy == "swarm"
    assert plan2.strategy_source == "override"


def test_all_five_roles_resolve_to_a_concrete_model() -> None:
    plan = plan_execution(
        signals=_simple_signals(),
        store=FakeRoleConfigStore(),
        workspace_id=WORKSPACE,
        provider=ProviderName.anthropic,
    )
    assert set(plan.roles) == set(AgentRole)
    for role in AgentRole:
        role_exec = plan.for_role(role)
        assert role_exec.model  # non-empty concrete model string
        assert isinstance(role_exec.effort, Effort)


def test_default_coder_tier_escalates_to_senior_on_complex_work() -> None:
    router = _router()
    # Default coder is medior; a senior-sized task escalates it to senior=Opus.
    plan = plan_execution(
        signals=_complex_signals(),
        store=FakeRoleConfigStore(),
        workspace_id=WORKSPACE,
        router=router,
    )
    coder = plan.for_role(AgentRole.CODER)
    assert coder.source == "default"
    assert coder.escalated is True
    assert coder.tier == "senior"
    assert coder.model == router.resolve("senior")


def test_simple_work_keeps_default_medior_coder() -> None:
    router = _router()
    plan = plan_execution(
        signals=_simple_signals(),
        store=FakeRoleConfigStore(),
        workspace_id=WORKSPACE,
        router=router,
    )
    coder = plan.for_role(AgentRole.CODER)
    assert coder.escalated is False
    assert coder.tier == "medior"
    assert coder.model == router.resolve("medior")


def test_pinned_concrete_model_override_respected_verbatim_never_escalated() -> None:
    store = FakeRoleConfigStore()
    store.upsert_override(WORKSPACE, AgentRole.CODER, "claude-opus-4-6", Effort.MAX)
    plan = plan_execution(
        signals=_complex_signals(),  # would otherwise escalate the coder
        store=store,
        workspace_id=WORKSPACE,
        provider=ProviderName.anthropic,
    )
    coder = plan.for_role(AgentRole.CODER)
    assert coder.source == "workspace"
    assert coder.model == "claude-opus-4-6"  # verbatim, not a router tier model
    assert coder.tier is None
    assert coder.escalated is False
    assert coder.effort is Effort.MAX


def test_explicit_tier_override_respected_and_not_escalated() -> None:
    store = FakeRoleConfigStore()
    # A human pins the coder to junior; a complex task must NOT escalate it.
    store.upsert_override(WORKSPACE, AgentRole.CODER, "junior", Effort.LOW)
    router = _router()
    plan = plan_execution(
        signals=_complex_signals(),
        store=store,
        workspace_id=WORKSPACE,
        router=router,
    )
    coder = plan.for_role(AgentRole.CODER)
    assert coder.source == "workspace"
    assert coder.escalated is False
    assert coder.tier == "junior"
    assert coder.model == router.resolve("junior")


def test_project_override_beats_workspace_in_plan() -> None:
    store = FakeRoleConfigStore()
    store.upsert_override(WORKSPACE, AgentRole.REVIEWER, "medior", Effort.MEDIUM)
    store.upsert_override(
        WORKSPACE, AgentRole.REVIEWER, "claude-opus-4-6", Effort.MAX, project_id=PROJECT
    )
    plan = plan_execution(
        signals=_simple_signals(),
        store=store,
        workspace_id=WORKSPACE,
        provider=ProviderName.anthropic,
        project_id=PROJECT,
    )
    reviewer = plan.for_role(AgentRole.REVIEWER)
    assert reviewer.source == "project"
    assert reviewer.model == "claude-opus-4-6"


def test_loop_budget_scales_with_complexity() -> None:
    simple = plan_execution(
        signals=_simple_signals(),
        store=FakeRoleConfigStore(),
        workspace_id=WORKSPACE,
        provider=ProviderName.anthropic,
    )
    complex_ = plan_execution(
        signals=_complex_signals(),
        store=FakeRoleConfigStore(),
        workspace_id=WORKSPACE,
        provider=ProviderName.anthropic,
    )
    assert simple.review_loop_budget < complex_.review_loop_budget
    assert simple.review_loop_budget == 1
    assert complex_.review_loop_budget >= 2


def test_accepts_precomputed_sizing() -> None:
    sizing = score_complexity(_complex_signals())
    plan = plan_execution(
        sizing=sizing,
        store=FakeRoleConfigStore(),
        workspace_id=WORKSPACE,
        provider=ProviderName.anthropic,
    )
    assert plan.sizing is sizing
    assert plan.strategy == "swarm"


def test_requires_signals_or_sizing() -> None:
    try:
        plan_execution(
            store=FakeRoleConfigStore(),
            workspace_id=WORKSPACE,
            provider=ProviderName.anthropic,
        )
    except ValueError as exc:
        assert "signals or sizing" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")


def test_requires_router_or_provider() -> None:
    try:
        plan_execution(
            signals=_simple_signals(),
            store=FakeRoleConfigStore(),
            workspace_id=WORKSPACE,
        )
    except ValueError as exc:
        assert "router or provider" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")


def test_plan_role_execution_direct() -> None:
    sizing = score_complexity(_complex_signals())
    role_exec = plan_role_execution(
        AgentRole.PLANNER,
        sizing=sizing,
        store=FakeRoleConfigStore(),
        workspace_id=WORKSPACE,
        router=_router(),
    )
    # Planner defaults to senior already; senior task keeps it senior (no escalation).
    assert role_exec.tier == "senior"
    assert role_exec.escalated is False
