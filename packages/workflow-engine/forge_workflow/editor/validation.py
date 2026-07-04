"""Server-authoritative, multi-issue workflow validation (F28).

Collects *every* problem in one pass (never fail-fast) so the editor can render
the full issue list. The ERROR set is a **superset** of the foundation's
``parse_definition`` / ``TransitionGraph.validate`` failures (AC 5 parity): any
graph with zero ERROR issues parses cleanly, and every parse failure surfaces at
least one ERROR. Editor-only stricter checks (unregistered names, reachability,
dead-ends, protected invariants) add ERRORs/WARNINGs beyond the engine.

Deviation from the slice doc: the foundation has no guard/effect *registries*, so
the registered vocabulary is a :class:`~forge_workflow.editor.catalog.Vocabulary`
(scanned from the bundled definitions), and protected invariants are phrased
against the real ``default_feature`` DSL signals (``review_approved_by_human``,
``spec_approved_by_human``) rather than the idealized ``merge_ready`` /
``approval_granted:spec`` guards — see slice notes.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from forge_workflow.editor.catalog import Vocabulary
from forge_workflow.editor.graph import (
    TERMINAL_STATES,
    TransitionEdge,
    WorkflowGraph,
    edge_triggers,
)

#: A non-terminal dead-end here is tolerated (it is an explicit human gate).
_DEAD_END_EXEMPT: frozenset[str] = frozenset({"needs_human_input"})


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


class IssueCode(StrEnum):
    UNKNOWN_STATE = "unknown_state"
    UNKNOWN_EVENT = "unknown_event"
    UNREGISTERED_GUARD = "unregistered_guard"
    UNREGISTERED_PRECONDITION = "unregistered_precondition"
    UNREGISTERED_EFFECT = "unregistered_effect"
    UNKNOWN_SKILL = "unknown_skill"
    NO_INITIAL_STATE = "no_initial_state"
    UNREACHABLE_STATE = "unreachable_state"
    DEAD_END_STATE = "dead_end_state"
    NONDETERMINISTIC_RULES = "nondeterministic_rules"
    DUPLICATE_EDGE = "duplicate_edge"
    PROTECTED_INVARIANT_VIOLATION = "protected_invariant_violation"


class ValidationIssue(BaseModel):
    code: IssueCode
    severity: Severity
    message: str
    node_id: str | None = None
    edge_id: str | None = None
    invariant_id: str | None = None


class ProtectedInvariant(BaseModel):
    """An edge selector + a trigger signal that must be present on every match.

    Adapted to the foundation DSL: ``required_signal`` must appear in the matching
    edge's trigger tokens (``action`` + ``when``).
    """

    id: str
    description: str
    applies_when_base_in: list[str] = Field(default_factory=list)
    from_state: str
    to_state: str | None = None
    on_event: str | None = None
    required_signal: str


#: Non-negotiable invariants for feature-class workflows (spec: "human approval
#: before merge — always"; "no implementation run without an approved spec").
FEATURE_INVARIANTS: list[ProtectedInvariant] = [
    ProtectedInvariant(
        id="merge_human_gate",
        description=(
            "Merge must require human approval: the awaiting_review -> merged edge "
            "must carry the review_approved_by_human signal."
        ),
        applies_when_base_in=["default_feature"],
        from_state="awaiting_review",
        to_state="merged",
        required_signal="review_approved_by_human",
    ),
    ProtectedInvariant(
        id="spec_gate",
        description=(
            "Leaving spec_review to spec_approved must require spec_approved_by_human."
        ),
        applies_when_base_in=["default_feature"],
        from_state="spec_review",
        to_state="spec_approved",
        required_signal="spec_approved_by_human",
    ),
]


def _matches(edge: TransitionEdge, inv: ProtectedInvariant) -> bool:
    if edge.from_state != inv.from_state:
        return False
    if inv.to_state is not None and edge.to_state != inv.to_state:
        return False
    return not (inv.on_event is not None and inv.on_event not in edge_triggers(edge))


def collect_validation_issues(
    graph: WorkflowGraph,
    *,
    vocabulary: Vocabulary,
    skill_names: set[str] | None = None,
    base_bundled_name: str | None = None,
    invariants: list[ProtectedInvariant] | None = None,
) -> list[ValidationIssue]:
    """Run all checks; return every issue (errors + warnings)."""
    issues: list[ValidationIssue] = []
    skills = skill_names or set()
    node_ids = {n.id for n in graph.nodes}

    # --- no transitions / no initial state -------------------------------- #
    if not graph.edges:
        issues.append(
            ValidationIssue(
                code=IssueCode.NO_INITIAL_STATE,
                severity=Severity.ERROR,
                message="workflow has no transitions",
            )
        )

    indegree: dict[str, int] = dict.fromkeys(node_ids, 0)
    outdegree: dict[str, int] = dict.fromkeys(node_ids, 0)

    # --- per-edge structural + registry checks ---------------------------- #
    seen_exact: set[tuple[str, frozenset[str], str | None]] = set()
    by_state_trigger: dict[tuple[str, str], list[tuple[str | None, str]]] = {}

    for edge in graph.edges:
        for state in (edge.from_state, edge.to_state):
            if not state:
                issues.append(
                    ValidationIssue(
                        code=IssueCode.UNKNOWN_STATE,
                        severity=Severity.ERROR,
                        message="transition has an empty from/to state",
                        edge_id=edge.id,
                    )
                )
            elif state not in node_ids:
                issues.append(
                    ValidationIssue(
                        code=IssueCode.UNKNOWN_STATE,
                        severity=Severity.ERROR,
                        message=f"transition references unknown state {state!r}",
                        edge_id=edge.id,
                        node_id=state,
                    )
                )
        if edge.from_state in outdegree:
            outdegree[edge.from_state] += 1
        if edge.to_state in indegree and edge.to_state != edge.from_state:
            indegree[edge.to_state] += 1

        # effect (action)
        if edge.action and edge.action not in vocabulary.effects:
            issues.append(
                ValidationIssue(
                    code=IssueCode.UNREGISTERED_EFFECT,
                    severity=Severity.ERROR,
                    message=f"unregistered effect {edge.action!r}",
                    edge_id=edge.id,
                )
            )
        # condition (guard)
        if edge.condition and edge.condition not in vocabulary.guards:
            issues.append(
                ValidationIssue(
                    code=IssueCode.UNREGISTERED_GUARD,
                    severity=Severity.ERROR,
                    message=f"unregistered guard {edge.condition!r}",
                    edge_id=edge.id,
                )
            )
        # when signals (events or guard signals)
        when_tokens = (
            [edge.when] if isinstance(edge.when, str) else list(edge.when or [])
        )
        known = vocabulary.events | vocabulary.guards
        for token in when_tokens:
            if token not in known:
                issues.append(
                    ValidationIssue(
                        code=IssueCode.UNKNOWN_EVENT,
                        severity=Severity.ERROR,
                        message=f"unknown event/signal {token!r}",
                        edge_id=edge.id,
                    )
                )
        # preconditions
        for precondition in edge.preconditions:
            if precondition not in vocabulary.preconditions:
                issues.append(
                    ValidationIssue(
                        code=IssueCode.UNREGISTERED_PRECONDITION,
                        severity=Severity.ERROR,
                        message=f"unregistered precondition {precondition!r}",
                        edge_id=edge.id,
                    )
                )
        # skill (warning only)
        if edge.skill and skills and edge.skill not in skills:
            issues.append(
                ValidationIssue(
                    code=IssueCode.UNKNOWN_SKILL,
                    severity=Severity.WARNING,
                    message=f"unknown skill {edge.skill!r}",
                    edge_id=edge.id,
                )
            )

        # duplicate / nondeterministic
        triggers = edge_triggers(edge)
        key = (edge.from_state, frozenset(triggers), edge.condition)
        if key in seen_exact:
            issues.append(
                ValidationIssue(
                    code=IssueCode.DUPLICATE_EDGE,
                    severity=Severity.ERROR,
                    message=(
                        f"duplicate transition from {edge.from_state!r} "
                        f"on {sorted(triggers)!r}"
                    ),
                    edge_id=edge.id,
                )
            )
        else:
            seen_exact.add(key)
            for token in triggers:
                by_state_trigger.setdefault((edge.from_state, token), []).append(
                    (edge.condition, edge.id)
                )

    # --- nondeterminism: same (from, trigger) + same condition ------------ #
    for (from_state, token), entries in by_state_trigger.items():
        seen_conditions: dict[str | None, str] = {}
        for condition, edge_id in entries:
            if condition in seen_conditions:
                issues.append(
                    ValidationIssue(
                        code=IssueCode.NONDETERMINISTIC_RULES,
                        severity=Severity.ERROR,
                        message=(
                            f"nondeterministic rules from {from_state!r} on "
                            f"{token!r} (indistinguishable conditions)"
                        ),
                        edge_id=edge_id,
                    )
                )
            else:
                seen_conditions[condition] = edge_id

    # --- initial / reachability / dead-ends ------------------------------- #
    if graph.edges and node_ids:
        initials = [s for s in node_ids if indegree.get(s, 0) == 0]
        if not initials:
            issues.append(
                ValidationIssue(
                    code=IssueCode.NO_INITIAL_STATE,
                    severity=Severity.ERROR,
                    message="no initial state (every state has an inbound edge)",
                )
            )

        reachable = _reachable(graph, initials)
        for node in graph.nodes:
            if node.id not in reachable:
                issues.append(
                    ValidationIssue(
                        code=IssueCode.UNREACHABLE_STATE,
                        severity=Severity.WARNING,
                        message=f"state {node.id!r} is unreachable from an initial state",
                        node_id=node.id,
                    )
                )
            if (
                outdegree.get(node.id, 0) == 0
                and node.id not in TERMINAL_STATES
                and node.id not in _DEAD_END_EXEMPT
            ):
                issues.append(
                    ValidationIssue(
                        code=IssueCode.DEAD_END_STATE,
                        severity=Severity.ERROR,
                        message=f"non-terminal state {node.id!r} has no outgoing transition",
                        node_id=node.id,
                    )
                )

    # --- protected invariants --------------------------------------------- #
    for inv in invariants or []:
        if base_bundled_name not in inv.applies_when_base_in:
            continue
        matching = [e for e in graph.edges if _matches(e, inv)]
        if not matching:
            issues.append(
                ValidationIssue(
                    code=IssueCode.PROTECTED_INVARIANT_VIOLATION,
                    severity=Severity.ERROR,
                    message=(
                        f"{inv.description} (no matching edge "
                        f"{inv.from_state} -> {inv.to_state})"
                    ),
                    invariant_id=inv.id,
                    node_id=inv.from_state,
                )
            )
            continue
        for edge in matching:
            if inv.required_signal not in edge_triggers(edge):
                issues.append(
                    ValidationIssue(
                        code=IssueCode.PROTECTED_INVARIANT_VIOLATION,
                        severity=Severity.ERROR,
                        message=(
                            f"{inv.description} (edge is missing "
                            f"{inv.required_signal!r})"
                        ),
                        invariant_id=inv.id,
                        edge_id=edge.id,
                    )
                )

    return issues


def _reachable(graph: WorkflowGraph, initials: list[str]) -> set[str]:
    adjacency: dict[str, list[str]] = {n.id: [] for n in graph.nodes}
    for edge in graph.edges:
        if edge.from_state in adjacency:
            adjacency[edge.from_state].append(edge.to_state)
    reachable: set[str] = set()
    stack = list(initials)
    while stack:
        current = stack.pop()
        if current in reachable:
            continue
        reachable.add(current)
        stack.extend(adjacency.get(current, []))
    return reachable


def error_count(issues: list[ValidationIssue]) -> int:
    return sum(1 for i in issues if i.severity is Severity.ERROR)


def warning_count(issues: list[ValidationIssue]) -> int:
    return sum(1 for i in issues if i.severity is Severity.WARNING)


def has_errors(issues: list[ValidationIssue]) -> bool:
    return error_count(issues) > 0


__all__ = [
    "FEATURE_INVARIANTS",
    "IssueCode",
    "ProtectedInvariant",
    "Severity",
    "ValidationIssue",
    "collect_validation_issues",
    "error_count",
    "has_errors",
    "warning_count",
]
