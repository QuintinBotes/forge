"""Registry catalog (palette) for the workflow visual editor (F28).

The foundation has **no** ``GuardRegistry``/``EffectRegistry`` (guards are context
flags, effects are ``action`` names in the DSL). To stay foundation-conforming the
catalog derives the *registered vocabulary* from the bundled definitions
(``default_feature`` + ``incident``) plus the engine built-ins, overlaid with
curated human metadata. This vocabulary is what the validator treats as
"registered" — you can only compose names the trusted bundled definitions already
use, satisfying the "no behavior injection from the UI" guarantee.

Deviation from the slice doc: the idealized ``approval_granted:<kind>`` arg syntax
and a per-registry ``is_precondition`` flag do not exist in the foundation DSL;
``GuardMeta`` keeps ``takes_arg``/``arg_hint``/``is_precondition`` for forward
compatibility but the bundled guards are plain boolean flags.
"""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

from pydantic import BaseModel, Field

from forge_contracts import WorkflowDefinition
from forge_contracts.enums import ExecutionMode, IncidentState, WorkflowState
from forge_workflow.fsm import RETRY_BUDGET_EXHAUSTED, RETRY_BUDGET_REMAINING


class GuardMeta(BaseModel):
    """Catalog entry for a guard or precondition predicate."""

    name: str
    description: str = ""
    takes_arg: bool = False
    arg_hint: str | None = None
    is_precondition: bool = False


class EffectMeta(BaseModel):
    """Catalog entry for an effect (a DSL ``action`` step)."""

    name: str
    description: str = ""
    provided_by: str | None = None


class CatalogResponse(BaseModel):
    """The palette returned to the editor UI."""

    states: list[str] = Field(default_factory=list)
    events: list[str] = Field(default_factory=list)
    guards: list[GuardMeta] = Field(default_factory=list)
    preconditions: list[GuardMeta] = Field(default_factory=list)
    effects: list[EffectMeta] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    modes: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Curated metadata                                                              #
# --------------------------------------------------------------------------- #

#: The four feature-class preconditions (AC 3).
PRECONDITION_META: dict[str, str] = {
    "repo_target_set": "A repository target is configured for the task.",
    "policy_loaded": "The workspace policy profile is loaded.",
    "skill_profile_set": "A skill profile is assigned to the run.",
    "knowledge_synced": "Knowledge sources are synced and indexed.",
}

#: Guard predicates (conditions / AND-signals) with human descriptions.
GUARD_META: dict[str, str] = {
    RETRY_BUDGET_REMAINING: "The retry budget still has room (retry_count < max_retries).",
    RETRY_BUDGET_EXHAUSTED: "The retry budget is spent (retry_count >= max_retries).",
    "remediation_within_blast_radius": "Proposed remediation is within the blast radius.",
    "remediation_exceeds_blast_radius": "Proposed remediation exceeds the blast radius.",
    "approval_granted": "A human granted the required approval.",
    "postmortem_persisted": "The postmortem record was persisted.",
    # AND-signals used in list-valued `when` clauses of default_feature.
    "spec_approved_by_human": "A human approved the spec (human gate).",
    "spec_changes_requested": "A human requested spec changes.",
    "plan_approved_by_human": "A human approved the plan (human gate).",
    "review_approved_by_human": "A human approved the PR review (merge gate).",
    "ci_status_green": "CI is green.",
    "spec_validated": "Spec traceability validated against the PR.",
    "all_checks_passed": "All verification checks passed.",
    "checks_failed": "One or more verification checks failed.",
    "low_confidence": "Agent confidence fell below the escalation threshold.",
}

#: Effects (DSL ``action`` steps) -> the slice that owns the effect body.
EFFECT_PROVIDED_BY: dict[str, str] = {
    "generate_spec_draft": "spec-engine",
    "gather_clarifications": "spec-engine",
    "submit_spec_for_review": "spec-engine",
    "generate_plan": "spec-engine",
    "submit_plan_for_review": "spec-engine",
    "generate_tasks": "board-core",
    "start_agent_run": "agent-runtime",
    "run_checks": "agent-runtime",
    "open_pr_with_spec_traceability": "integration-sdk",
    "request_reviews": "integration-sdk",
    "close_task": "board-core",
    # incident effects
    "alert_ingested": "board-core",
    "incident_acknowledged": "board-core",
    "context_gathered": "knowledge-core",
    "impact_assessed": "board-core",
    "remediation_proposed": "board-core",
    "remediation_blast_radius_exceeded": "policy-sdk",
    "remediation_approved": "board-core",
    "remediation_rejected": "board-core",
    "runbook_completed": "agent-runtime",
    "postmortem_requested": "board-core",
    "close": "board-core",
}


# --------------------------------------------------------------------------- #
# Vocabulary scan                                                              #
# --------------------------------------------------------------------------- #


class Vocabulary(BaseModel):
    """The registered token sets the validator consults."""

    events: frozenset[str]
    guards: frozenset[str]
    preconditions: frozenset[str]
    effects: frozenset[str]
    records: frozenset[str]
    checks: frozenset[str]


def scan_vocabulary(definitions: list[WorkflowDefinition]) -> Vocabulary:
    """Extract the registered vocabulary from the bundled definitions + built-ins."""
    events: set[str] = set()
    guards: set[str] = {RETRY_BUDGET_REMAINING, RETRY_BUDGET_EXHAUSTED}
    preconditions: set[str] = set()
    effects: set[str] = set()
    records: set[str] = set()
    checks: set[str] = set()

    for defn in definitions:
        for t in defn.transitions:
            if t.action:
                effects.add(t.action)
                events.add(t.action)
            if isinstance(t.when, str):
                events.add(t.when)
            elif isinstance(t.when, list):
                events.update(t.when)
            if t.condition:
                guards.add(t.condition)
            preconditions.update(t.preconditions)
            checks.update(t.checks)
            if t.record:
                records.add(t.record)

    # Curated guard names are registered even if a bundled def doesn't use them.
    guards.update(GUARD_META)
    preconditions.update(PRECONDITION_META)
    return Vocabulary(
        events=frozenset(events),
        guards=frozenset(guards),
        preconditions=frozenset(preconditions),
        effects=frozenset(effects),
        records=frozenset(records),
        checks=frozenset(checks),
    )


def _bundled_definitions() -> list[WorkflowDefinition]:
    from forge_workflow.default_workflow import default_feature_definition

    defs = [default_feature_definition()]
    try:
        from forge_workflow.incident.definition import default_incident_definition

        defs.append(default_incident_definition())
    except Exception:  # pragma: no cover - incident is a soft dependency
        pass
    return defs


# --------------------------------------------------------------------------- #
# Catalog                                                                       #
# --------------------------------------------------------------------------- #


class RegistryCatalog:
    """Builds the editor palette from the bundled vocabulary + a skill provider."""

    def __init__(
        self,
        *,
        definitions: list[WorkflowDefinition] | None = None,
        skill_names_provider: Callable[[UUID | None], list[str]] | None = None,
    ) -> None:
        self._definitions = definitions or _bundled_definitions()
        self._vocabulary = scan_vocabulary(self._definitions)
        self._skill_names_provider = skill_names_provider or (lambda _ws: [])

    @property
    def vocabulary(self) -> Vocabulary:
        return self._vocabulary

    def build(
        self,
        *,
        workspace_id: UUID | None = None,
        extra_states: list[str] | None = None,
    ) -> CatalogResponse:
        states: list[str] = [s.value for s in WorkflowState]
        states += [s.value for s in IncidentState if s.value not in states]
        for state in extra_states or []:
            if state not in states:
                states.append(state)

        guards = [
            GuardMeta(name=name, description=GUARD_META.get(name, ""))
            for name in sorted(self._vocabulary.guards)
        ]
        preconditions = [
            GuardMeta(
                name=name,
                description=PRECONDITION_META.get(name, ""),
                is_precondition=True,
            )
            for name in sorted(self._vocabulary.preconditions)
        ]
        effects = [
            EffectMeta(name=name, provided_by=EFFECT_PROVIDED_BY.get(name))
            for name in sorted(self._vocabulary.effects)
        ]
        skills = list(self._skill_names_provider(workspace_id))
        skills += [
            t.skill
            for defn in self._definitions
            for t in defn.transitions
            if t.skill and t.skill not in skills
        ]
        return CatalogResponse(
            states=states,
            events=sorted(self._vocabulary.events),
            guards=guards,
            preconditions=preconditions,
            effects=effects,
            skills=skills,
            modes=[m.value for m in ExecutionMode],
        )


__all__ = [
    "EFFECT_PROVIDED_BY",
    "GUARD_META",
    "PRECONDITION_META",
    "CatalogResponse",
    "EffectMeta",
    "GuardMeta",
    "RegistryCatalog",
    "Vocabulary",
    "scan_vocabulary",
]
