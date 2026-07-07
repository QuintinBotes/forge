"""The deployment state machine.

A deterministic, Postgres-backed FSM driving a single promotion. The foundation's
``WorkflowDefinition`` DTO (``forge_contracts.dtos``) has no ``guards``/``effects``/
``priority`` fields (its transitions are ``from``/``to``/``action`` and
``_Model`` ignores extras), so the deployment FSM uses a native deterministic
transition table parsed from YAML rather than round-tripping through F07's
``load_definition`` (deviation noted in the slice notes). The *algorithm* mirrors
F07: row-lock -> guard eval -> append append-only transition -> commit -> dispatch
effects post-commit.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from forge_db.models.deployment import Environment
from forge_deploy.effects import EffectDispatcher, RecordingEffectDispatcher
from forge_deploy.errors import InvalidTransitionError
from forge_deploy.guards import GuardContext, GuardFn, default_guard_registry
from forge_deploy.repository import DeploymentRepository
from forge_deploy.states import (
    TERMINAL_STATES,
    DeploymentEvent,
    DeploymentEventType,
    DeploymentState,
)

_DEFINITIONS_DIR = Path(__file__).resolve().parent / "definitions"
_REDACT_TOKENS = ("secret", "token", "password", "authorization", "signature", "apikey")


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if any(tok in str(k).lower() for tok in _REDACT_TOKENS):
                out[k] = "***"
            else:
                out[k] = _redact(v)
        return out
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


class DeploymentTransitionRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_state: str = Field(alias="from")
    event: str = Field(alias="on")
    to_state: str = Field(alias="to")
    guards: list[str] = Field(default_factory=list)
    effects: list[str] = Field(default_factory=list)
    priority: int = 0
    record: str | None = None


class DeploymentDefinition(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str
    version: str = "1"
    transitions: list[DeploymentTransitionRule] = Field(default_factory=list)

    def candidates(self, state: str, event: str) -> list[DeploymentTransitionRule]:
        return [r for r in self.transitions if r.from_state == state and r.event == event]


def parse_deployment_definition(text: str) -> DeploymentDefinition:
    raw = yaml.safe_load(text)
    if not isinstance(raw, dict):
        raise ValueError("deployment definition must be a mapping")
    definition = DeploymentDefinition.model_validate(raw)
    _inject_cancel_edges(definition)
    return definition


def load_deployment_definition(source: str | Path | None = None) -> DeploymentDefinition:
    if source is None:
        source = _DEFINITIONS_DIR / "deployment_promotion.yaml"
    if isinstance(source, Path):
        return parse_deployment_definition(source.read_text())
    candidate = Path(source)
    if candidate.is_file():
        return parse_deployment_definition(candidate.read_text())
    return parse_deployment_definition(source)


def _inject_cancel_edges(definition: DeploymentDefinition) -> None:
    """Add a universal ``cancel -> cancelled`` edge for every non-terminal state."""
    states = {r.from_state for r in definition.transitions} | {
        r.to_state for r in definition.transitions
    }
    terminal = {s.value for s in TERMINAL_STATES}
    have = {
        (r.from_state, r.event)
        for r in definition.transitions
        if r.event == DeploymentEventType.CANCEL.value
    }
    for state in sorted(states):
        if state in terminal:
            continue
        if (state, DeploymentEventType.CANCEL.value) in have:
            continue
        definition.transitions.append(
            DeploymentTransitionRule.model_validate(
                {
                    "from": state,
                    "on": DeploymentEventType.CANCEL.value,
                    "to": DeploymentState.CANCELLED.value,
                }
            )
        )


class DeploymentStateMachine:
    def __init__(
        self,
        session: Session,
        *,
        workspace_id: uuid.UUID,
        definition: DeploymentDefinition | None = None,
        guards: dict[str, GuardFn] | None = None,
        dispatcher: EffectDispatcher | None = None,
    ) -> None:
        self.session = session
        self.workspace_id = workspace_id
        self.definition = definition or load_deployment_definition()
        self.guards = guards or default_guard_registry()
        self.dispatcher = dispatcher or RecordingEffectDispatcher()
        self.repo = DeploymentRepository(session, workspace_id=workspace_id)
        # Fail fast on an unregistered guard name (DSL drift guard, AC23).
        for rule in self.definition.transitions:
            for guard in rule.guards:
                if guard not in self.guards:
                    raise ValueError(f"unregistered deploy guard: {guard!r}")

    def transition(self, deployment_id: uuid.UUID, event: DeploymentEvent) -> DeploymentState:
        dep = self.repo.lock(deployment_id)

        # Idempotent replay: same event idempotency key already recorded.
        if event.idempotency_key is not None:
            for existing in self.repo.transitions(deployment_id):
                if existing.idempotency_key == event.idempotency_key:
                    return dep.state

        env = self.session.get(Environment, dep.environment_id)
        ctx = GuardContext(repo=self.repo, deployment=dep, environment=env, event=event)
        rule, guard_results = self._select(dep.state.value, event.type.value, ctx)
        if rule is None:
            raise InvalidTransitionError(
                f"no transition from {dep.state.value!r} on {event.type.value!r}"
            )

        from_state = dep.state
        to_state = DeploymentState(rule.to_state)
        now = datetime.now(UTC)
        dep.state = to_state
        dep.version += 1
        if to_state == DeploymentState.DEPLOYING and dep.started_at is None:
            dep.started_at = now
        if to_state in TERMINAL_STATES:
            dep.finished_at = now

        self.repo.append_transition(
            dep,
            from_state=from_state.value,
            to_state=to_state.value,
            event=event.type.value,
            guard_results=guard_results,
            effects=rule.effects,
            actor=event.actor,
            payload=_redact(event.payload),
            idempotency_key=event.idempotency_key,
        )
        self.session.flush()

        for effect in rule.effects:
            self.dispatcher.dispatch(
                effect,
                deployment_id=dep.id,
                payload=event.payload,
                actor=event.actor,
            )
        return to_state

    def _select(
        self, state: str, event: str, ctx: GuardContext
    ) -> tuple[DeploymentTransitionRule | None, dict[str, bool]]:
        candidates = sorted(
            self.definition.candidates(state, event),
            key=lambda r: -r.priority,
        )
        results: dict[str, bool] = {}
        passing: list[DeploymentTransitionRule] = []
        for rule in candidates:
            ok = True
            for guard in rule.guards:
                value = self.guards[guard](ctx)
                results[guard] = value
                if not value:
                    ok = False
            if ok:
                passing.append(rule)
        if not passing:
            return None, results
        top_priority = passing[0].priority
        top = [r for r in passing if r.priority == top_priority]
        if len(top) > 1:
            raise InvalidTransitionError(f"ambiguous transition from {state!r} on {event!r}")
        return top[0], results


__all__ = [
    "DeploymentDefinition",
    "DeploymentStateMachine",
    "DeploymentTransitionRule",
    "load_deployment_definition",
    "parse_deployment_definition",
]
