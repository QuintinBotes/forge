"""Automation lines: DSL v2 models, the service catalog and the pure validator.

Spec K §4.1 and ADR-0020. This lives in contracts so the marketplace SDK can
validate a line without importing ``forge_workflow`` (which pulls in
``forge_db`` and ``temporalio``). Nothing here does I/O.

:func:`parse_line` checks the document's shape (pydantic errors);
:func:`validate_line` checks the graph and returns coded errors.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictStr,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from forge_contracts.constants import DEFAULT_CONFIDENCE_THRESHOLD
from forge_contracts.coordinator import ROLE_TOOLS
from forge_contracts.dtos import WorkflowDefinition, WorkflowTransition
from forge_contracts.enums import ApprovalGate, SubAgentRole

_NAME = r"[a-z][a-z0-9_]{0,63}"
STATE_NAME_PATTERN = rf"^{_NAME}$"
#: State names, decision values and output keys share this grammar.
StateName = Annotated[str, StringConstraints(pattern=STATE_NAME_PATTERN)]

#: A reference path: no expressions, only these shapes.
REFERENCE_RE = re.compile(
    r"^(?:task\.(?:id|key|title|description|repo)|trigger\.external_ref|run\.id"
    rf"|outputs\.(?P<state>{_NAME})\.(?P<key>{_NAME}))$"
)
#: ``{{path}}`` in an objective or summary; the inside must match ``REFERENCE_RE`` exactly.
PLACEHOLDER_RE = re.compile(r"\{\{([^{}]*)\}\}")
_CONDITION_RE = re.compile(rf"^(?P<state>{_NAME})\.(?P<key>{_NAME})$")

OBJECTIVE_MAX_BYTES = 8 * 1024

SUCCEEDED = "succeeded"
APPROVED = "approved"
AGENT_OUTCOMES = frozenset(
    {
        SUCCEEDED,
        "failed",
        "awaiting_input",
        "low_confidence",
        "budget_exhausted",
        "timed_out",
        "visit_limit",
    }
)
SERVICE_OUTCOMES = frozenset({SUCCEEDED, "failed", "timed_out", "not_configured", "visit_limit"})
HUMAN_OUTCOMES = frozenset({APPROVED, "rejected", "changes_requested", "expired", "visit_limit"})
#: Failure-class outcomes, with each catalog entry's ``failure_outcomes``: after one, every
#: path to success must run the failing station again (lore #2374).
FAILURE_OUTCOMES = frozenset(
    {
        "failed",
        "timed_out",
        "rejected",
        "expired",
        "visit_limit",
        "no_checks",
        "changes_requested",
        "low_confidence",
        "awaiting_input",
    }
)
#: These always take the fail default; any edge on them is a fail-open route.
UNROUTABLE_OUTCOMES = frozenset({"not_configured", "budget_exhausted"})
_RESERVED_OUTCOMES = AGENT_OUTCOMES | SERVICE_OUTCOMES | HUMAN_OUTCOMES | FAILURE_OUTCOMES
#: What a ``trust: task`` input may read besides a ``trust: task`` service output.
#: ``task.title`` and ``task.description`` are tracker text, so they are ``trust: any``.
TASK_TRUSTED_REFERENCES = frozenset(
    {"task.id", "task.key", "task.repo", "trigger.external_ref", "run.id"}
)
#: The DSL v1 retry guards (``forge_workflow.fsm``); lines bound loops with ``max_visits``.
RETRY_GUARDS = frozenset({"retry_budget_remaining", "retry_budget_exhausted"})

AGENT_OUTPUTS = frozenset({"summary_ref", "branch", "head_sha"})
#: Removed from ``ROLE_TOOLS[role]`` for an agent station (ADR-0014).
AGENT_STRIPPED_TOOLS = frozenset({"open_pr", "write_review_comment"})
#: An agent station's tools must never include these (``agent_no_write_tools``).
AGENT_FORBIDDEN_TOOLS = frozenset({"open_pr", "push", "merge", "approve", "write_review_comment"})
HUMAN_GATES = frozenset({ApprovalGate.SPEC, ApprovalGate.PLAN, ApprovalGate.PR})
#: ``failed`` and ``cancelled`` are implicit terminals and reserved names.
IMPLICIT_TERMINALS: dict[str, str] = {"failed": "failed", "cancelled": "cancelled"}

_V1_LINE_KEYS = frozenset({"modes", "retry_policy", "escalation_policy"})
_V1_TRANSITION_KEYS = frozenset({"action", "preconditions", "checks", "record", "skill"})


Trust = Literal["task", "any"]


def _reject_keys(data: Any, keys: frozenset[str], where: str) -> Any:
    if isinstance(data, Mapping):
        found = sorted(data.keys() & keys)
        if found:
            raise ValueError(f"{where} cannot carry {', '.join(found)} (DSL v1 only)")
    return data


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- #
# Stations                                                                     #
# --------------------------------------------------------------------------- #


class LineBudget(_Strict):
    """``max_tokens`` is the enforced cap; ``max_usd`` is recorded and alerts (§4.7)."""

    max_tokens: int = Field(gt=0)
    max_usd: Decimal | None = Field(default=None, ge=0)


class LineConcurrency(_Strict):
    max_active_runs: int = Field(default=3, ge=1, le=50)


class _StationBase(_Strict):
    title: str | None = None
    max_visits: int = Field(default=3, ge=1, le=20)


class AgentStation(_StationBase):
    kind: Literal["agent"]
    role: SubAgentRole
    skill_profile: str = Field(min_length=1)
    context_template: Literal["default", "review", "implementation", "research"]
    objective: str
    #: Checked against the workspace allowlist at start, not here.
    model: StrictStr | None = None
    decisions: list[StateName] | None = Field(default=None, min_length=1)
    confidence_threshold: float = Field(default=DEFAULT_CONFIDENCE_THRESHOLD, ge=0, le=1)
    timeout_seconds: int = Field(default=7200, ge=1, le=14400)
    budget: LineBudget | None = None

    @field_validator("objective")
    @classmethod
    def _objective_size(cls, value: str) -> str:
        if len(value.encode()) > OBJECTIVE_MAX_BYTES:
            raise ValueError(f"objective is over {OBJECTIVE_MAX_BYTES} bytes")
        return value

    @field_validator("decisions")
    @classmethod
    def _closed_decisions(cls, value: list[str] | None) -> list[str] | None:
        if value is not None:
            if len(set(value)) != len(value):
                raise ValueError("decisions must be unique")
            reserved = sorted(set(value) & _RESERVED_OUTCOMES)
            if reserved:
                raise ValueError(f"decisions cannot reuse outcome names: {reserved}")
        return value


class ServiceRetry(_Strict):
    max_attempts: int = Field(ge=1)


class ServiceStation(_StationBase):
    kind: Literal["service"]
    service: str
    inputs: dict[str, str] = Field(default_factory=dict)
    params: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    timeout_seconds: int | None = Field(default=None, ge=1)
    retry: ServiceRetry | None = None


class HumanStation(_StationBase):
    kind: Literal["human"]
    gate: ApprovalGate
    required_approvals: int = Field(default=1, ge=1, le=5)
    sla_seconds: int = Field(ge=300, le=2_592_000)
    summary: str | None = None


Station = Annotated[AgentStation | ServiceStation | HumanStation, Field(discriminator="kind")]


# --------------------------------------------------------------------------- #
# Line                                                                         #
# --------------------------------------------------------------------------- #


class LineTransition(WorkflowTransition):
    """``from``/``to``/``when``/``condition`` only; ``when`` is one outcome of the source.

    It subclasses the v1 transition so ``TransitionEvaluator`` reads it unchanged;
    the v1-only fields keep their empty defaults and are refused on input, and so
    are the field names ``from_state``/``to_state``.
    """

    # validate_by_name, not populate_by_name: the v1 base's populate_by_name sets it True.
    model_config = ConfigDict(extra="forbid", validate_by_name=False)

    from_state: StateName = Field(alias="from")
    to_state: StateName = Field(alias="to")
    when: str
    condition: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _no_v1_keys(cls, data: Any) -> Any:
        return _reject_keys(data, _V1_TRANSITION_KEYS, "a line transition")


class LineDefinition(WorkflowDefinition):
    """A DSL v2 workflow definition (spec K §4.1). Only ``workflow:`` sets ``name``."""

    model_config = ConfigDict(extra="forbid", validate_by_name=False)

    name: str = Field(alias="workflow")
    dsl_version: Literal["2"]
    initial: StateName
    terminals: dict[StateName, Literal["succeeded", "failed", "cancelled"]] = Field(
        default_factory=dict
    )
    budget: LineBudget | None = None
    concurrency: LineConcurrency = Field(default_factory=LineConcurrency)
    stations: dict[StateName, Station]
    transitions: list[LineTransition] = Field(default_factory=list)  # type: ignore[assignment]

    @model_validator(mode="before")
    @classmethod
    def _no_v1_keys(cls, data: Any) -> Any:
        return _reject_keys(data, _V1_LINE_KEYS, "a line")

    @model_validator(mode="after")
    def _terminal_names(self) -> LineDefinition:
        for name, status in IMPLICIT_TERMINALS.items():
            if name in self.stations or self.terminals.get(name, status) != status:
                raise ValueError(f"{name!r} is a reserved terminal state")
        both = sorted(self.stations.keys() & self.terminals.keys())
        if both:
            raise ValueError(f"terminal states cannot carry a station: {both}")
        return self

    @property
    def all_terminals(self) -> dict[str, str]:
        """Declared terminals plus the implicit ``failed`` and ``cancelled``."""
        return {**IMPLICIT_TERMINALS, **self.terminals}


def parse_line(data: Mapping[str, Any]) -> LineDefinition:
    """Parse an already-loaded DSL v2 mapping. Raises ``pydantic.ValidationError``.

    shortcut: takes a mapping, not YAML text, because contracts does not depend on
    pyyaml (callers already load the YAML); add a text branch if that dependency lands.
    """
    return LineDefinition.model_validate(dict(data))


# --------------------------------------------------------------------------- #
# Service catalog (metadata only; bodies are bound on the worker)              #
# --------------------------------------------------------------------------- #


class _NoParams(_Strict):
    pass


class OpenPrParams(_Strict):
    draft: bool = True


@dataclass(frozen=True)
class ServiceEntry:
    name: str
    effect: Literal["read", "write_external"]
    idempotent: bool
    max_timeout_seconds: int
    max_attempts: int
    #: Must not declare secret fields; credentials come from ``credential``.
    params_model: type[BaseModel] = _NoParams
    inputs: Mapping[str, Trust] = field(default_factory=dict)
    #: name -> (type, trust). ``task`` only for values the service derives itself.
    outputs: Mapping[str, tuple[type, Trust]] = field(default_factory=dict)
    outcomes: frozenset[str] = frozenset()
    #: The subset of ``outcomes`` that is failure-class (see ``FAILURE_OUTCOMES``).
    failure_outcomes: frozenset[str] = frozenset()
    #: A vault reference name, or None.
    credential: str | None = None
    #: Merge-capable. No phase-1 entry is; ``no_merge`` gates any that ever is.
    merges: bool = False


SERVICE_CATALOG: dict[str, ServiceEntry] = {
    entry.name: entry
    for entry in (
        ServiceEntry(
            name="forge.prepare_worktree",
            effect="read",
            idempotent=True,
            max_timeout_seconds=900,
            max_attempts=3,
            inputs={"repo": "task"},
            credential="repo_host",
        ),
        ServiceEntry(
            name="forge.confirm_red",
            effect="read",
            idempotent=True,
            max_timeout_seconds=1800,
            max_attempts=2,
            outcomes=frozenset({"not_red"}),
            failure_outcomes=frozenset({"not_red"}),
        ),
        ServiceEntry(
            name="forge.run_checks",
            effect="read",
            idempotent=True,
            max_timeout_seconds=1800,
            max_attempts=2,
            outputs={"failure_summary": (str, "any")},
            outcomes=frozenset({"checks_failed"}),
            failure_outcomes=frozenset({"checks_failed"}),
        ),
        ServiceEntry(
            name="forge.open_pr",
            effect="write_external",
            idempotent=False,
            max_timeout_seconds=300,
            max_attempts=1,
            params_model=OpenPrParams,
            inputs={"task": "task"},
            outputs={"pr_ref": (str, "task")},
            credential="repo_host",
        ),
        ServiceEntry(
            name="forge.push_branch",
            effect="write_external",
            idempotent=True,
            max_timeout_seconds=300,
            max_attempts=3,
            inputs={"pr": "task"},
            credential="repo_host",
        ),
        ServiceEntry(
            name="forge.await_ci",
            effect="read",
            idempotent=True,
            max_timeout_seconds=7200,
            max_attempts=2,
            inputs={"pr": "task"},
            outputs={"failure_summary": (str, "any")},
            outcomes=frozenset({"checks_failed", "no_checks"}),
            failure_outcomes=frozenset({"checks_failed"}),
            credential="repo_host",
        ),
        ServiceEntry(
            name="forge.mark_pr_ready",
            effect="write_external",
            idempotent=True,
            max_timeout_seconds=300,
            max_attempts=3,
            inputs={"pr": "task"},
            credential="repo_host",
        ),
        ServiceEntry(
            name="forge.tracker_comment",
            effect="write_external",
            idempotent=False,
            max_timeout_seconds=300,
            max_attempts=1,
            inputs={"ticket": "task", "body": "any"},
            credential="tracker",
        ),
    )
}


def station_outcomes(station: Station) -> frozenset[str]:
    """Every outcome ``station`` can produce (an edge's ``when`` must be one)."""
    if isinstance(station, AgentStation):
        if station.decisions:
            return (AGENT_OUTCOMES - {SUCCEEDED}) | set(station.decisions)
        return AGENT_OUTCOMES
    if isinstance(station, HumanStation):
        return HUMAN_OUTCOMES
    entry = SERVICE_CATALOG.get(station.service)
    return SERVICE_OUTCOMES | (entry.outcomes if entry else frozenset())


def _success_outcomes(station: Station) -> frozenset[str]:
    if isinstance(station, AgentStation):
        return frozenset(station.decisions or (SUCCEEDED,))
    return frozenset({APPROVED if isinstance(station, HumanStation) else SUCCEEDED})


def _failure_outcomes(station: Station) -> frozenset[str]:
    entry = SERVICE_CATALOG.get(station.service) if isinstance(station, ServiceStation) else None
    return FAILURE_OUTCOMES | (entry.failure_outcomes if entry else frozenset())


def _outputs(station: Station | None) -> Mapping[str, tuple[type, Trust]]:
    if isinstance(station, AgentStation):
        return dict.fromkeys(AGENT_OUTPUTS, (str, "any"))
    if isinstance(station, ServiceStation):
        entry = SERVICE_CATALOG.get(station.service)
        return entry.outputs if entry else {}
    return {}


# --------------------------------------------------------------------------- #
# Feature invariants (copy of the editor's data; a workflow-engine test pins it) #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FeatureInvariant:
    id: str
    description: str
    from_state: str
    to_state: str | None
    required_signal: str
    on_event: str | None = None


#: Pure copy of ``forge_workflow.editor.validation.FEATURE_INVARIANTS``. On v2 they
#: apply whenever ``from_state`` appears in the graph, whatever the base.
FEATURE_INVARIANTS: tuple[FeatureInvariant, ...] = (
    FeatureInvariant(
        id="merge_human_gate",
        description=(
            "Merge must require human approval: the awaiting_review -> merged edge "
            "must carry the review_approved_by_human signal."
        ),
        from_state="awaiting_review",
        to_state="merged",
        required_signal="review_approved_by_human",
    ),
    FeatureInvariant(
        id="spec_gate",
        description="Leaving spec_review to spec_approved must require spec_approved_by_human.",
        from_state="spec_review",
        to_state="spec_approved",
        required_signal="spec_approved_by_human",
    ),
)


# --------------------------------------------------------------------------- #
# Validation                                                                   #
# --------------------------------------------------------------------------- #


class LineErrorCode(StrEnum):
    """Stable codes (spec K §4.1). Values follow the editor's ``IssueCode`` style."""

    STATE_WITHOUT_STATION = "state_without_station"
    DEAD_END_STATE = "dead_end_state"
    UNROUTED_SUCCESS = "unrouted_success"
    FAIL_OPEN_ROUTE = "fail_open_route"
    UNKNOWN_SERVICE = "unknown_service"
    STATION_PARAMS_INVALID = "station_params_invalid"
    UNKNOWN_OUTCOME = "unknown_outcome"
    UNKNOWN_REFERENCE = "unknown_reference"
    AGENT_STATION_UNBUDGETED = "agent_station_unbudgeted"
    INVALID_HUMAN_GATE = "invalid_human_gate"
    RETRY_GUARD_IN_V2 = "retry_guard_in_v2"
    #: A cycle of ``visit_limit`` edges: past their limits, its stations hand the
    #: run around forever without doing any work.
    VISIT_LIMIT_CYCLE = "visit_limit_cycle"
    # Same names and values as the editor's reachability / nondeterminism checks.
    UNREACHABLE_STATE = "unreachable_state"
    NONDETERMINISTIC_RULES = "nondeterministic_rules"
    #: ``invariant`` is no_merge, agent_no_write_tools, trusted_inputs or
    #: agent_outputs_not_guards.
    LINE_INVARIANT_VIOLATION = "line_invariant_violation"
    #: A ``FEATURE_INVARIANTS`` miss, as the editor reports it.
    PROTECTED_INVARIANT_VIOLATION = "protected_invariant_violation"


class LineError(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: LineErrorCode
    message: str
    state: str | None = None
    #: Index into ``transitions`` (the editor's edge id is ``e<index>``).
    edge: int | None = None
    invariant: str | None = None


def _edge(index: int, t: LineTransition) -> str:
    return f"edge #{index} ({t.from_state} -> {t.to_state} on {t.when!r})"


def _reach(
    start: str,
    transitions: list[LineTransition],
    skip: Callable[[LineTransition], bool] = lambda _t: False,
) -> set[str]:
    seen: set[str] = set()
    stack = [start]
    while stack:
        state = stack.pop()
        if state not in seen:
            seen.add(state)
            stack.extend(t.to_state for t in transitions if t.from_state == state and not skip(t))
    return seen


def validate_line(defn: LineDefinition) -> list[LineError]:
    """Return every graph error in ``defn`` (empty when valid). Pure: no I/O."""
    errors: list[LineError] = []

    def err(
        code: LineErrorCode,
        message: str,
        *,
        state: str | None = None,
        edge: int | None = None,
        invariant: str | None = None,
    ) -> None:
        errors.append(
            LineError(code=code, message=message, state=state, edge=edge, invariant=invariant)
        )

    stations = defn.stations
    terminals = defn.all_terminals
    edges = list(enumerate(defn.transitions))
    outgoing: dict[str, list[tuple[int, LineTransition]]] = {}
    for i, t in edges:
        outgoing.setdefault(t.from_state, []).append((i, t))
    states = {defn.initial, *stations, *defn.terminals}
    for t in defn.transitions:
        states |= {t.from_state, t.to_state}
    visit_limit_edges = [t for t in defn.transitions if t.when == "visit_limit"]

    # --- states: stations, dead ends, reachability ------------------------- #
    if defn.initial not in stations:
        err(
            LineErrorCode.STATE_WITHOUT_STATION,
            f"initial state {defn.initial!r} must be a station state",
            state=defn.initial,
        )
    for s in sorted(states - terminals.keys()):
        if s not in stations and s != defn.initial:
            err(
                LineErrorCode.STATE_WITHOUT_STATION,
                f"state {s!r} has no station and is not in terminals",
                state=s,
            )
        if s not in outgoing:
            err(
                LineErrorCode.DEAD_END_STATE,
                f"state {s!r} has no outgoing edge and is not in terminals",
                state=s,
            )
    reached = _reach(defn.initial, defn.transitions)
    for s in sorted({*stations, *defn.terminals} - reached):
        err(
            LineErrorCode.UNREACHABLE_STATE,
            f"state {s!r} is unreachable from {defn.initial!r}",
            state=s,
        )

    # --- edges: outcomes, guards, nondeterminism --------------------------- #
    seen: dict[tuple[str, str, str | None], int] = {}
    for i, t in edges:
        source = stations.get(t.from_state)
        if t.when in RETRY_GUARDS:
            err(
                LineErrorCode.RETRY_GUARD_IN_V2,
                f"{_edge(i, t)}: retry guards are DSL v1; bound loops with max_visits",
                state=t.from_state,
                edge=i,
            )
        elif source is None or t.when not in station_outcomes(source):
            err(
                LineErrorCode.UNKNOWN_OUTCOME,
                f"{_edge(i, t)}: {t.when!r} is not an outcome of {t.from_state!r}",
                state=t.from_state,
                edge=i,
            )
        if t.condition is not None:
            match = _CONDITION_RE.fullmatch(t.condition)
            guard = stations.get(match["state"]) if match else None
            if t.condition in RETRY_GUARDS:
                err(
                    LineErrorCode.RETRY_GUARD_IN_V2,
                    f"{_edge(i, t)}: retry guard {t.condition!r} is DSL v1",
                    state=t.from_state,
                    edge=i,
                )
            elif isinstance(guard, AgentStation):
                err(
                    LineErrorCode.LINE_INVARIANT_VIOLATION,
                    f"{_edge(i, t)}: agent output {t.condition!r} cannot be a guard",
                    state=t.from_state,
                    edge=i,
                    invariant="agent_outputs_not_guards",
                )
            elif not (
                match
                and isinstance(guard, ServiceStation)
                and _outputs(guard).get(match["key"], (None, "any"))[0] is bool
            ):
                err(
                    LineErrorCode.UNKNOWN_REFERENCE,
                    f"{_edge(i, t)}: condition {t.condition!r} must be <state>.<key> naming "
                    "a boolean output of a service station",
                    state=t.from_state,
                    edge=i,
                )
        key = (t.from_state, t.when, t.condition)
        if key in seen:
            err(
                LineErrorCode.NONDETERMINISTIC_RULES,
                f"{_edge(i, t)}: same when and condition as edge #{seen[key]}",
                state=t.from_state,
                edge=i,
            )
        else:
            seen[key] = i

    # --- stations ---------------------------------------------------------- #
    def check_ref(path: str, s: str, where: str) -> None:
        match = REFERENCE_RE.fullmatch(path)
        if match is None:
            err(
                LineErrorCode.UNKNOWN_REFERENCE,
                f"station {s!r} {where}: {path!r} is not a reference path",
                state=s,
            )
        elif match["state"] is not None and match["key"] not in _outputs(
            stations.get(match["state"])
        ):
            err(
                LineErrorCode.UNKNOWN_REFERENCE,
                f"station {s!r} {where}: {path!r} names no output of a station",
                state=s,
            )

    merge_states: list[str] = []
    for s, st in stations.items():
        if isinstance(st, AgentStation):
            for path in PLACEHOLDER_RE.findall(st.objective):
                check_ref(path, s, "objective")
            if defn.budget is None:
                err(
                    LineErrorCode.AGENT_STATION_UNBUDGETED,
                    f"agent station {s!r} needs a line budget",
                    state=s,
                )
            elif st.budget is not None and (
                st.budget.max_tokens > defn.budget.max_tokens
                or (
                    st.budget.max_usd is not None
                    and defn.budget.max_usd is not None
                    and st.budget.max_usd > defn.budget.max_usd
                )
            ):
                err(
                    LineErrorCode.AGENT_STATION_UNBUDGETED,
                    f"agent station {s!r} budget is above the line budget",
                    state=s,
                )
            # A skill profile only narrows these, so the role's tools are the bound.
            write_tools = sorted(
                (ROLE_TOOLS.get(st.role, frozenset()) - AGENT_STRIPPED_TOOLS)
                & AGENT_FORBIDDEN_TOOLS
            )
            if write_tools:
                err(
                    LineErrorCode.LINE_INVARIANT_VIOLATION,
                    f"agent station {s!r} (role {st.role.value}) would hold {write_tools}",
                    state=s,
                    invariant="agent_no_write_tools",
                )
        elif isinstance(st, HumanStation):
            if st.gate not in HUMAN_GATES:
                err(
                    LineErrorCode.INVALID_HUMAN_GATE,
                    f"human station {s!r} gate {st.gate.value!r} is not spec, plan or pr",
                    state=s,
                )
            for path in PLACEHOLDER_RE.findall(st.summary or ""):
                check_ref(path, s, "summary")
        else:
            for name, path in st.inputs.items():
                check_ref(path, s, f"input {name!r}")
            entry = SERVICE_CATALOG.get(st.service)
            if entry is None:
                err(
                    LineErrorCode.UNKNOWN_SERVICE,
                    f"service station {s!r}: {st.service!r} is not in the catalog",
                    state=s,
                )
            else:
                _check_service(s, st, entry, stations, err)
                if entry.merges:
                    merge_states.append(s)

        # Routing: success must be routed; after a failure, success needs this station again.
        out = outgoing.get(s, [])
        success = _success_outcomes(st)
        failure = _failure_outcomes(st)
        for outcome in sorted(success - {t.when for _, t in out}):
            err(
                LineErrorCode.UNROUTED_SUCCESS,
                f"station {s!r} has no edge on {outcome!r}",
                state=s,
            )
        not_through_s = [t for t in defn.transitions if t.from_state != s]
        for i, t in out:
            if t.when in UNROUTABLE_OUTCOMES:
                err(
                    LineErrorCode.FAIL_OPEN_ROUTE,
                    f"{_edge(i, t)}: {t.when!r} must take the fail default, not an edge",
                    state=s,
                    edge=i,
                )
            elif t.when in failure:
                # Judged by where the run can end, not by shared targets: a failure
                # may share a failed/cancelled terminal or a notify step with a
                # decision, but must not reach success without running s again.
                after = _reach(t.to_state, not_through_s) - {s}
                hits = sorted(x for x in after if terminals.get(x) == SUCCEEDED)
                if hits:
                    err(
                        LineErrorCode.FAIL_OPEN_ROUTE,
                        f"{_edge(i, t)}: reaches the succeeded terminal {hits[0]!r} "
                        f"without running {s!r} again",
                        state=s,
                        edge=i,
                    )
            if t.when == "visit_limit" and s in _reach(t.to_state, visit_limit_edges):
                err(
                    LineErrorCode.VISIT_LIMIT_CYCLE,
                    f"{_edge(i, t)}: visit_limit edges lead back to {s!r}, so the run "
                    "never ends once these stations are past their limits",
                    state=s,
                    edge=i,
                )

    # --- invariants -------------------------------------------------------- #
    if merge_states:
        # Paths that do not cross the approved edge of a pr gate.
        open_reach = _reach(
            defn.initial,
            defn.transitions,
            lambda t: (
                t.when == APPROVED
                and isinstance(g := stations.get(t.from_state), HumanStation)
                and g.gate is ApprovalGate.PR
            ),
        )
        for s in merge_states:
            if s in open_reach:
                err(
                    LineErrorCode.LINE_INVARIANT_VIOLATION,
                    f"merge-capable station {s!r} is reachable without a pr gate approval",
                    state=s,
                    invariant="no_merge",
                )

    for inv in FEATURE_INVARIANTS:
        if inv.from_state not in states:
            continue
        matching = [
            (i, t)
            for i, t in edges
            if t.from_state == inv.from_state
            and (inv.to_state is None or t.to_state == inv.to_state)
            and (inv.on_event is None or t.when == inv.on_event)
        ]
        if not matching:
            err(
                LineErrorCode.PROTECTED_INVARIANT_VIOLATION,
                f"{inv.description} (no matching edge {inv.from_state} -> {inv.to_state})",
                state=inv.from_state,
                invariant=inv.id,
            )
        for i, t in matching:
            if t.when != inv.required_signal:
                err(
                    LineErrorCode.PROTECTED_INVARIANT_VIOLATION,
                    f"{inv.description} ({_edge(i, t)} is missing {inv.required_signal!r})",
                    state=inv.from_state,
                    edge=i,
                    invariant=inv.id,
                )

    return errors


def _check_service(
    s: str,
    st: ServiceStation,
    entry: ServiceEntry,
    stations: Mapping[str, Station],
    err: Callable[..., None],
) -> None:
    code = LineErrorCode.STATION_PARAMS_INVALID
    try:
        entry.params_model.model_validate(st.params)
    except ValidationError as exc:
        detail = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        err(code, f"service station {s!r} params: {detail}", state=s)
    for name in sorted(st.inputs.keys() - entry.inputs.keys()):
        err(code, f"service station {s!r}: {entry.name} has no input {name!r}", state=s)
    if st.timeout_seconds is not None and st.timeout_seconds > entry.max_timeout_seconds:
        err(
            code,
            f"service station {s!r}: timeout_seconds is above {entry.max_timeout_seconds}",
            state=s,
        )
    max_attempts = entry.max_attempts if entry.idempotent else 1
    if st.retry is not None and st.retry.max_attempts > max_attempts:
        err(
            code,
            f"service station {s!r}: retry.max_attempts is above {max_attempts}"
            + ("" if entry.idempotent else f" ({entry.name} is not idempotent)"),
            state=s,
        )
    for name, path in st.inputs.items():
        if entry.inputs.get(name) == "task" and _trust(path, stations) != "task":
            err(
                LineErrorCode.LINE_INVARIANT_VIOLATION,
                f"service station {s!r} input {name!r} is trust: task but reads {path!r}",
                state=s,
                invariant="trusted_inputs",
            )


def _trust(path: str, stations: Mapping[str, Station]) -> Trust:
    """``task`` for a run fact or a ``trust: task`` service output; agent outputs are ``any``."""
    if path in TASK_TRUSTED_REFERENCES:
        return "task"
    match = REFERENCE_RE.fullmatch(path)
    if match is None or match["state"] is None:
        return "any"
    return _outputs(stations.get(match["state"])).get(match["key"], (str, "any"))[1]


__all__ = [
    "AGENT_FORBIDDEN_TOOLS",
    "AGENT_OUTCOMES",
    "AGENT_OUTPUTS",
    "AGENT_STRIPPED_TOOLS",
    "FAILURE_OUTCOMES",
    "FEATURE_INVARIANTS",
    "HUMAN_GATES",
    "HUMAN_OUTCOMES",
    "IMPLICIT_TERMINALS",
    "PLACEHOLDER_RE",
    "REFERENCE_RE",
    "SERVICE_CATALOG",
    "SERVICE_OUTCOMES",
    "STATE_NAME_PATTERN",
    "TASK_TRUSTED_REFERENCES",
    "UNROUTABLE_OUTCOMES",
    "AgentStation",
    "FeatureInvariant",
    "HumanStation",
    "LineBudget",
    "LineConcurrency",
    "LineDefinition",
    "LineError",
    "LineErrorCode",
    "LineTransition",
    "OpenPrParams",
    "ServiceEntry",
    "ServiceRetry",
    "ServiceStation",
    "Station",
    "parse_line",
    "station_outcomes",
    "validate_line",
]
