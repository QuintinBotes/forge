"""Adaptive Orchestration (ao-policy) wiring into the Supervisor.

Proves the ExecutionPlan gates the coordinator: a ``single`` strategy forces a
single-agent run (no fan-out) even when the objective would otherwise select a
multi-role pattern; a ``swarm`` strategy leaves fan-out reachable; the per-role
model is pinned onto each subagent's objective; and the review loop budget scales
with complexity. All hermetic — the ExecutionPlan is built from a defaults-only
config store + the default tier->model router (no DB, no live model).
"""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

from _helpers import AgentScript, ScriptingHub, make_objective, obj_parent

from forge_agent import ModelRouter, ProviderName, plan_execution
from forge_contracts import AcceptanceCriterion
from forge_contracts.orchestration_config import AgentRole, RoleConfigOverride
from forge_orchestration_policy import SizingSignals


class _DefaultsOnlyStore:
    """A ``RoleConfigStore`` with no overrides — every role resolves to its default."""

    def get_override(
        self, workspace_id: uuid.UUID, role: AgentRole, *, project_id: uuid.UUID | None = None
    ) -> RoleConfigOverride | None:
        return None

    def upsert_override(self, *a: object, **k: object) -> RoleConfigOverride:  # pragma: no cover
        raise NotImplementedError

    def delete_override(self, *a: object, **k: object) -> bool:  # pragma: no cover
        raise NotImplementedError

    def list_overrides(self, *a: object, **k: object) -> list[RoleConfigOverride]:
        return []


def _plan(strategy: str):
    # Score a complex task, then force the strategy under test via the override
    # seam so both branches use identical per-role config.
    signals = SizingSignals(touches_contracts=True, touches_security=True, file_count=40)
    return plan_execution(
        signals=signals,
        store=_DefaultsOnlyStore(),
        workspace_id=uuid.uuid4(),
        router=ModelRouter(provider=ProviderName.anthropic),
        strategy_override=strategy,  # type: ignore[arg-type]
    )


def test_single_strategy_forces_single_agent_no_fanout(
    tmp_git_repo: Path, hub: ScriptingHub, make_supervisor, allow_all_rules
) -> None:
    # review_required + reviewer allowed would normally select MAKER_CHECKER
    # (implementer + reviewer). A single-strategy plan collapses it to one agent.
    hub.set("implementer", AgentScript(confidence=0.9, files=[("app/f.py", "X=1\n")]))
    hub.set("reviewer", AgentScript(confidence=0.95, review_verdict="approved"))
    obj = make_objective(
        tmp_git_repo,
        rules=allow_all_rules,
        review_required=True,
        acceptance=[AcceptanceCriterion(id="ac1", text="feature", spec_ref="app/f.py")],
    )
    result = make_supervisor(execution_plan=_plan("single")).run(obj)

    assert result.artifacts["pattern"] == "orchestrator_worker"
    assert hub.calls_for("implementer")  # implementer ran
    assert hub.calls_for("reviewer") == []  # no fan-out to a reviewer


def test_swarm_strategy_allows_fanout(
    tmp_git_repo: Path, hub: ScriptingHub, make_supervisor, allow_all_rules
) -> None:
    hub.set("implementer", AgentScript(confidence=0.9, files=[("app/f.py", "X=1\n")]))
    hub.set("reviewer", AgentScript(confidence=0.95, review_verdict="approved"))
    obj = make_objective(
        tmp_git_repo,
        rules=allow_all_rules,
        review_required=True,
        acceptance=[AcceptanceCriterion(id="ac1", text="feature", spec_ref="app/f.py")],
    )
    result = make_supervisor(execution_plan=_plan("swarm")).run(obj)

    assert result.artifacts["pattern"] == "maker_checker"
    assert hub.calls_for("implementer")
    assert hub.calls_for("reviewer")  # fan-out happened


def test_explicit_coordination_pattern_hint_still_wins_over_single_strategy(
    tmp_git_repo: Path, hub: ScriptingHub, make_supervisor, allow_all_rules
) -> None:
    hub.set("implementer", AgentScript(confidence=0.9, files=[("app/f.py", "X=1\n")]))
    hub.set("reviewer", AgentScript(confidence=0.95, review_verdict="approved"))
    obj = make_objective(
        tmp_git_repo,
        rules=allow_all_rules,
        pattern="maker_checker",  # explicit human override
    )
    result = make_supervisor(execution_plan=_plan("single")).run(obj)

    # The explicit pattern hint beats the sized single strategy.
    assert result.artifacts["pattern"] == "maker_checker"
    assert hub.calls_for("reviewer")


def test_per_role_model_pinned_onto_subagent_objective(
    tmp_git_repo: Path, hub: ScriptingHub, make_supervisor, sink, allow_all_rules
) -> None:
    hub.set("implementer", AgentScript(confidence=0.9, files=[("app/f.py", "X=1\n")]))
    hub.set("reviewer", AgentScript(confidence=0.95, review_verdict="approved"))
    obj = make_objective(tmp_git_repo, rules=allow_all_rules, review_required=True)

    plan = _plan("swarm")
    make_supervisor(execution_plan=plan).run(obj)

    rows = {r["role"]: r for r in sink.rows_for_parent(obj_parent(obj))}
    coder_model = plan.for_role(AgentRole.CODER).model
    reviewer_model = plan.for_role(AgentRole.REVIEWER).model
    assert rows["implementer"]["objective"]["model"] == coder_model
    assert rows["reviewer"]["objective"]["model"] == reviewer_model


def test_per_role_model_routes_a_distinct_model_client(
    tmp_git_repo: Path, hub: ScriptingHub, make_supervisor, allow_all_rules
) -> None:
    # het-model seam: each subagent must be built from its own per-role model via
    # the model_client_factory (RoleExecution.model -> ModelClient), not one
    # constructor-bound constant. A low-complexity swarm plan keeps CODER (medior)
    # and REVIEWER (senior) on genuinely different models to route.
    hub.set("implementer", AgentScript(confidence=0.9, files=[("app/f.py", "X=1\n")]))
    hub.set("reviewer", AgentScript(confidence=0.95, review_verdict="approved"))
    obj = make_objective(
        tmp_git_repo,
        rules=allow_all_rules,
        review_required=True,
        acceptance=[AcceptanceCriterion(id="ac1", text="feature", spec_ref="app/f.py")],
    )
    plan = plan_execution(
        signals=SizingSignals(),  # simple task: no complexity escalation of CODER
        store=_DefaultsOnlyStore(),
        workspace_id=uuid.uuid4(),
        router=ModelRouter(provider=ProviderName.anthropic),
        strategy_override="swarm",  # type: ignore[arg-type]
    )
    coder_model = plan.for_role(AgentRole.CODER).model
    reviewer_model = plan.for_role(AgentRole.REVIEWER).model
    assert coder_model != reviewer_model  # precondition: genuinely heterogeneous

    built: list[str] = []

    def model_client_factory(model: str) -> object:
        built.append(model)
        return SimpleNamespace(model=model)

    make_supervisor(execution_plan=plan, model_client_factory=model_client_factory).run(obj)

    # Every dispatched subagent got a dedicated client built from its role's model
    # (nothing fell back to the factory default), covering both distinct models.
    assert coder_model in built and reviewer_model in built
    assert hub.model_clients and all(c is not None for c in hub.model_clients)
    routed = {c.model for c in hub.model_clients}  # type: ignore[attr-defined]
    assert routed >= {coder_model, reviewer_model}


def test_no_factory_keeps_single_default_client(
    tmp_git_repo: Path, hub: ScriptingHub, make_supervisor, allow_all_rules
) -> None:
    # Back-compat: without a model_client_factory the coordinator passes ``None``
    # so agent_factory falls back to its own default client (single-model runtime).
    hub.set("implementer", AgentScript(confidence=0.9, files=[("app/f.py", "X=1\n")]))
    hub.set("reviewer", AgentScript(confidence=0.95, review_verdict="approved"))
    obj = make_objective(tmp_git_repo, rules=allow_all_rules, review_required=True)

    make_supervisor(execution_plan=_plan("swarm")).run(obj)

    assert hub.model_clients  # subagents ran
    assert all(c is None for c in hub.model_clients)  # no per-role client built


def test_review_loop_budget_scales_with_complexity(
    tmp_git_repo: Path, hub: ScriptingHub, make_supervisor, allow_all_rules
) -> None:
    # A senior/swarm plan raises the loop budget; a reviewer that keeps requesting
    # changes should therefore drive more than the default single retry loop.
    hub.set(
        "implementer",
        AgentScript(confidence=0.9, files=[("app/f.py", "X=1\n")]),
    )
    hub.set("reviewer", AgentScript(review_verdict="changes_requested", findings=["fix"]))
    obj = make_objective(tmp_git_repo, rules=allow_all_rules, review_required=True)

    plan = _plan("swarm")
    assert plan.review_loop_budget >= 2
    result = make_supervisor(execution_plan=plan).run(obj)

    # The reviewer kept rejecting; with a budget >= 2 the supervisor re-dispatched
    # the implementer at least twice before escalating to a human.
    impl_calls = len(hub.calls_for("implementer"))
    assert impl_calls >= 2
    assert result.needs_human is True
