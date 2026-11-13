"""Spec K §4.1: line contracts, the service catalog and ``validate_line``.

Each error code and each line invariant has one minimal failing document, built
by changing one thing in ``_doc()`` (which validates clean).
"""

from __future__ import annotations

import copy
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import SecretBytes, SecretStr, ValidationError

from forge_contracts import ROLE_TOOLS, SubAgentRole
from forge_contracts.lines import (
    SERVICE_CATALOG,
    AgentStation,
    HumanStation,
    LineDefinition,
    LineError,
    LineErrorCode,
    LineTransition,
    ServiceEntry,
    ServiceStation,
    parse_line,
    station_outcomes,
    validate_line,
)

C = LineErrorCode
FIXTURE = Path(__file__).parent / "fixtures" / "implementation_loop.yaml"

AGENT: dict[str, Any] = {
    "kind": "agent",
    "role": "implementer",
    "skill_profile": "implementer",
    "context_template": "implementation",
    "objective": "Do {{task.title}}",
}
CHECKS: dict[str, Any] = {"kind": "service", "service": "forge.run_checks"}
GATE: dict[str, Any] = {"kind": "human", "gate": "pr", "sla_seconds": 3600}


def _t(src: str, dst: str, when: str = "succeeded", **extra: Any) -> dict[str, Any]:
    return {"from": src, "to": dst, "when": when, **extra}


def _doc(
    stations: dict[str, Any] | None = None,
    transitions: list[dict[str, Any]] | None = None,
    **top: Any,
) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "workflow": "mini",
        "dsl_version": "2",
        "initial": "work",
        "terminals": {"done": "succeeded"},
        "budget": {"max_tokens": 1000},
        "stations": stations if stations is not None else {"work": AGENT, "checks": CHECKS},
        "transitions": (
            transitions if transitions is not None else [_t("work", "checks"), _t("checks", "done")]
        ),
    }
    doc.update(top)
    return copy.deepcopy(doc)


def _errors(doc: dict[str, Any]) -> list[LineError]:
    return validate_line(parse_line(doc))


def _codes(doc: dict[str, Any]) -> set[LineErrorCode]:
    return {e.code for e in _errors(doc)}


def _invariants(doc: dict[str, Any]) -> set[str | None]:
    return {e.invariant for e in _errors(doc) if e.code is C.LINE_INVARIANT_VIOLATION}


# --------------------------------------------------------------------------- #
# The first line and the clean baseline                                        #
# --------------------------------------------------------------------------- #


def test_implementation_loop_parses_and_validates_clean() -> None:
    line = parse_line(yaml.safe_load(FIXTURE.read_text()))
    assert isinstance(line, LineDefinition)
    assert line.name == "implementation_loop"
    assert line.initial == "prepare"
    assert line.all_terminals == {
        "failed": "failed",
        "cancelled": "cancelled",
        "done_proposed": "succeeded",
        "ready_for_review": "succeeded",
    }
    assert line.budget is not None and line.budget.max_tokens == 3_000_000
    assert isinstance(line.stations["assess"], AgentStation)
    assert isinstance(line.stations["push"], ServiceStation)
    assert all(isinstance(t, LineTransition) for t in line.transitions)
    assert validate_line(line) == []


def test_baseline_doc_is_clean() -> None:
    assert _errors(_doc()) == []


def test_station_kinds_have_their_outcomes() -> None:
    line = parse_line(
        _doc(
            stations={
                "work": {**AGENT, "decisions": ["ship", "skip"]},
                "ci": {"kind": "service", "service": "forge.await_ci"},
                "gate": GATE,
            }
        )
    )
    assert station_outcomes(line.stations["work"]) >= {"ship", "skip", "failed"}
    assert "succeeded" not in station_outcomes(line.stations["work"])
    assert {"checks_failed", "no_checks", "not_configured"} <= station_outcomes(line.stations["ci"])
    assert station_outcomes(line.stations["gate"]) == {
        "approved",
        "rejected",
        "changes_requested",
        "expired",
        "visit_limit",
    }


# --------------------------------------------------------------------------- #
# One failing document per code                                                #
# --------------------------------------------------------------------------- #


def test_state_without_station_for_initial() -> None:
    errors = _errors(_doc(initial="done"))
    assert any(e.code is C.STATE_WITHOUT_STATION and e.state == "done" for e in errors)


def test_mistyped_to_is_dead_end_and_stationless() -> None:
    errors = _errors(_doc(transitions=[_t("work", "checks"), _t("checks", "dnoe")]))
    assert {(e.code, e.state) for e in errors} >= {
        (C.DEAD_END_STATE, "dnoe"),
        (C.STATE_WITHOUT_STATION, "dnoe"),
    }


def test_dead_end_station() -> None:
    errors = _errors(_doc(transitions=[_t("work", "checks")]))
    assert any(e.code is C.DEAD_END_STATE and e.state == "checks" for e in errors)


@pytest.mark.parametrize(
    ("stations", "transitions", "outcome"),
    [
        (
            {"work": {**AGENT, "decisions": ["ship", "skip"]}, "checks": CHECKS},
            [_t("work", "checks", "ship"), _t("checks", "done")],
            "skip",
        ),
        ({"work": AGENT, "checks": CHECKS}, [_t("work", "checks", "failed")], "succeeded"),
        ({"work": GATE}, [_t("work", "done", "changes_requested")], "approved"),
    ],
    ids=["decision", "succeeded", "approved"],
)
def test_unrouted_success(
    stations: dict[str, Any], transitions: list[dict[str, Any]], outcome: str
) -> None:
    errors = _errors(_doc(stations=stations, transitions=transitions))
    assert any(e.code is C.UNROUTED_SUCCESS and repr(outcome) in e.message for e in errors)


@pytest.mark.parametrize(
    ("stations", "extra"),
    [
        # lore #2374: failed routes to the success edge's target.
        ({"work": AGENT, "checks": CHECKS}, _t("work", "checks", "failed")),
        # A failure outcome routes to a succeeded terminal.
        ({"work": AGENT, "checks": CHECKS}, _t("work", "done", "timed_out")),
        ({"work": AGENT, "checks": CHECKS}, _t("work", "checks", "visit_limit")),
        ({"work": AGENT, "checks": GATE}, _t("checks", "done", "rejected")),
        ({"work": AGENT, "checks": GATE}, _t("checks", "done", "expired")),
        (
            {"work": AGENT, "checks": {"kind": "service", "service": "forge.await_ci"}},
            _t("checks", "done", "no_checks"),
        ),
        # Any edge on not_configured or budget_exhausted, wherever it leads.
        ({"work": AGENT, "checks": CHECKS}, _t("checks", "work", "not_configured")),
        ({"work": AGENT, "checks": CHECKS}, _t("work", "work", "budget_exhausted")),
    ],
    ids=[
        "failed-to-success-target",
        "timed_out-to-succeeded-terminal",
        "visit_limit-to-success-target",
        "rejected",
        "expired",
        "no_checks",
        "not_configured",
        "budget_exhausted",
    ],
)
def test_fail_open_route(stations: dict[str, Any], extra: dict[str, Any]) -> None:
    transitions = [_t("work", "checks"), _t("checks", "done", _success(stations["checks"]))]
    errors = _errors(_doc(stations=stations, transitions=[*transitions, extra]))
    assert [e.edge for e in errors if e.code is C.FAIL_OPEN_ROUTE] == [2]


def _success(station: dict[str, Any]) -> str:
    return "approved" if station["kind"] == "human" else "succeeded"


def test_failure_routed_elsewhere_is_not_fail_open() -> None:
    # Retrying the agent on a failed check is fine: it is not where success goes.
    doc = _doc(
        transitions=[_t("work", "checks"), _t("checks", "done"), _t("checks", "work", "failed")]
    )
    assert _errors(doc) == []


# The §4.5 line's edges: 6 confirm_red -> define_done on not_red, 10 local_checks ->
# implement on checks_failed, 13 await_ci -> implement on checks_failed, 14 mark_ready ->
# ready_for_review. Appended edges start at 15.
RELAY: dict[str, Any] = {"kind": "service", "service": "forge.prepare_worktree"}
NOTIFY: dict[str, Any] = {
    "kind": "service",
    "service": "forge.tracker_comment",
    "inputs": {"ticket": "trigger.external_ref"},
}


def _loop(
    stations: dict[str, Any] | None = None,
    retarget: dict[int, str] | None = None,
    add: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    doc: dict[str, Any] = yaml.safe_load(FIXTURE.read_text())
    doc["stations"].update(stations or {})
    for index, to in (retarget or {}).items():
        doc["transitions"][index]["to"] = to
    doc["transitions"].extend(add or [])
    return doc


@pytest.mark.parametrize(
    ("doc", "edges"),
    [
        # Transitive: the failure edge reaches success without its station running again.
        # The retry edges into implement (10, 13) then reach success through it too.
        (_loop(add=[_t("implement", "mark_ready", "failed")]), [10, 13, 15]),
        (_loop(add=[_t("implement", "propose_done", "failed")]), [10, 13, 15]),
        (_loop(add=[_t("implement", "push", "timed_out")]), [10, 15]),
        (_loop(add=[_t("implement", "mark_ready", "visit_limit")]), [10, 13, 15]),
        (_loop(add=[_t("local_checks", "await_ci", "failed")]), [15]),
        (_loop(add=[_t("confirm_red", "implement", "failed")]), [15]),
        (
            _loop(
                {"relay": RELAY},
                add=[_t("await_ci", "relay", "timed_out"), _t("relay", "mark_ready")],
            ),
            [15],
        ),
        # Catalog failure outcomes are failure-class.
        (_loop(retarget={13: "mark_ready"}), [13]),
        (_loop(retarget={10: "push"}), [10]),
        (_loop(retarget={6: "open_draft_pr"}), [6]),
        # Park outcomes and changes_requested are failure-class.
        (_loop(add=[_t("assess", "propose_done", "low_confidence")]), [15]),
        (_loop(add=[_t("implement", "ready_for_review", "awaiting_input")]), [10, 13, 15]),
        (
            _loop(
                {"gate": GATE},
                {14: "gate"},
                [
                    _t("gate", "ready_for_review", "approved"),
                    _t("gate", "done_proposed", "changes_requested"),
                ],
            ),
            [16],
        ),
        (
            _loop(
                {"gate": GATE, "notify": NOTIFY},
                {14: "gate"},
                [
                    _t("gate", "ready_for_review", "approved"),
                    _t("gate", "notify", "expired"),
                    _t("notify", "ready_for_review"),
                ],
            ),
            [16],
        ),
    ],
    ids=[
        "implement-failed-to-mark_ready",
        "implement-failed-to-propose_done",
        "implement-timed_out-to-push",
        "implement-visit_limit-to-mark_ready",
        "local_checks-failed-to-await_ci",
        "confirm_red-failed-to-implement",
        "await_ci-timed_out-via-relay",
        "await_ci-checks_failed-to-mark_ready",
        "local_checks-checks_failed-to-push",
        "confirm_red-not_red-to-open_draft_pr",
        "assess-low_confidence-to-propose_done",
        "implement-awaiting_input-to-ready_for_review",
        "gate-changes_requested-to-succeeded-terminal",
        "gate-expired-via-notify",
    ],
)
def test_fail_open_route_on_the_loop(doc: dict[str, Any], edges: list[int]) -> None:
    assert sorted((e.code, e.edge) for e in _errors(doc)) == [(C.FAIL_OPEN_ROUTE, i) for i in edges]


@pytest.mark.parametrize(
    "doc",
    [
        # local_checks -> implement and await_ci -> implement on checks_failed are in the fixture.
        _loop(),
        _loop(add=[_t("push", "implement", "failed")]),
        _loop(add=[_t("await_ci", "implement", "timed_out")]),
        _loop(add=[_t("confirm_red", "define_done", "failed")]),
        _loop(add=[_t("implement", "implement", "failed")]),
        _loop(add=[_t("implement", "failed", "failed")]),
        _loop(add=[_t("implement", "cancelled", "awaiting_input")]),
        _loop({"notify": NOTIFY}, add=[_t("push", "notify", "failed"), _t("notify", "failed")]),
    ],
    ids=[
        "fixture-retry-loops",
        "push-failed-to-implement",
        "await_ci-timed_out-to-implement",
        "confirm_red-failed-to-define_done",
        "self-loop",
        "failed-terminal",
        "cancelled-terminal",
        "notify-then-fail",
    ],
)
def test_failure_edges_that_rerun_the_station_are_clean(doc: dict[str, Any]) -> None:
    assert _errors(doc) == []


def test_unknown_service() -> None:
    doc = _doc(stations={"work": AGENT, "checks": {"kind": "service", "service": "forge.merge"}})
    assert C.UNKNOWN_SERVICE in _codes(doc)


@pytest.mark.parametrize(
    "checks",
    [
        {"kind": "service", "service": "forge.run_checks", "params": {"command": "rm -rf /"}},
        {"kind": "service", "service": "forge.open_pr", "params": {"draft": "maybe"}},
        {"kind": "service", "service": "forge.run_checks", "inputs": {"repo": "task.repo"}},
        {"kind": "service", "service": "forge.run_checks", "timeout_seconds": 1801},
        # Not idempotent: retry is forced to one attempt.
        {"kind": "service", "service": "forge.open_pr", "retry": {"max_attempts": 2}},
        # Idempotent: capped by the catalog.
        {"kind": "service", "service": "forge.push_branch", "retry": {"max_attempts": 4}},
    ],
    ids=["unknown-param", "bad-param-type", "unknown-input", "timeout", "retry-not-idem", "retry"],
)
def test_station_params_invalid(checks: dict[str, Any]) -> None:
    assert C.STATION_PARAMS_INVALID in _codes(_doc(stations={"work": AGENT, "checks": checks}))


@pytest.mark.parametrize(
    ("transitions"),
    [
        [_t("work", "checks"), _t("checks", "done"), _t("work", "done", "checks_failed")],
        [_t("work", "checks"), _t("checks", "done"), _t("done", "work")],
    ],
    ids=["not-an-agent-outcome", "edge-from-terminal"],
)
def test_unknown_outcome(transitions: list[dict[str, Any]]) -> None:
    assert C.UNKNOWN_OUTCOME in _codes(_doc(transitions=transitions))


@pytest.mark.parametrize(
    "doc",
    [
        _doc(stations={"work": {**AGENT, "objective": "Do {{task.secret}}"}, "checks": CHECKS}),
        _doc(stations={"work": {**AGENT, "objective": "Do {{ task.title }}"}, "checks": CHECKS}),
        _doc(stations={"work": {**AGENT, "objective": "{{outputs.work.nope}}"}, "checks": CHECKS}),
        _doc(
            stations={"work": {**AGENT, "objective": "{{outputs.ghost.branch}}"}, "checks": CHECKS}
        ),
        _doc(
            stations={
                "work": AGENT,
                "checks": {
                    "kind": "service",
                    "service": "forge.push_branch",
                    "inputs": {"pr": "outputs.work.summary_ref | upper"},
                },
            }
        ),
        _doc(
            stations={"work": AGENT, "checks": {**GATE, "summary": "PR {{outputs.checks.pr}}"}},
            transitions=[_t("work", "checks"), _t("checks", "done", "approved")],
        ),
        # Conditions: not <state>.<key>, and not a boolean service output.
        _doc(transitions=[_t("work", "checks", condition="green"), _t("checks", "done")]),
        _doc(
            transitions=[
                _t("work", "checks"),
                _t("checks", "done", condition="checks.failure_summary"),
            ]
        ),
    ],
    ids=[
        "unknown-task-field",
        "spaced-placeholder",
        "unknown-agent-output",
        "unknown-station",
        "expression",
        "human-has-no-outputs",
        "condition-shape",
        "condition-not-boolean",
    ],
)
def test_unknown_reference(doc: dict[str, Any]) -> None:
    assert C.UNKNOWN_REFERENCE in _codes(doc)


def test_boolean_service_output_is_a_valid_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    flagged = ServiceEntry(
        name="test.flag",
        effect="read",
        idempotent=True,
        max_timeout_seconds=60,
        max_attempts=1,
        outputs={"ok": (bool, "any")},
    )
    monkeypatch.setitem(SERVICE_CATALOG, "test.flag", flagged)
    doc = _doc(
        stations={"work": AGENT, "checks": {"kind": "service", "service": "test.flag"}},
        transitions=[_t("work", "checks"), _t("checks", "done", condition="checks.ok")],
    )
    assert _errors(doc) == []


@pytest.mark.parametrize(
    "doc",
    [
        _doc(budget=None),
        _doc(stations={"work": {**AGENT, "budget": {"max_tokens": 1001}}, "checks": CHECKS}),
        _doc(
            budget={"max_tokens": 1000, "max_usd": "5"},
            stations={
                "work": {**AGENT, "budget": {"max_tokens": 10, "max_usd": "5.01"}},
                "checks": CHECKS,
            },
        ),
    ],
    ids=["no-line-budget", "tokens-above-line", "usd-above-line"],
)
def test_agent_station_unbudgeted(doc: dict[str, Any]) -> None:
    errors = _errors(doc)
    assert any(e.code is C.AGENT_STATION_UNBUDGETED and e.state == "work" for e in errors)


def test_service_only_line_needs_no_budget() -> None:
    doc = _doc(
        initial="checks",
        budget=None,
        stations={"checks": CHECKS},
        transitions=[_t("checks", "done")],
    )
    assert _errors(doc) == []


@pytest.mark.parametrize("gate", ["deploy", "incident_remediation", "policy_override"])
def test_invalid_human_gate(gate: str) -> None:
    doc = _doc(
        stations={"work": AGENT, "checks": {**GATE, "gate": gate}},
        transitions=[_t("work", "checks"), _t("checks", "done", "approved")],
    )
    assert C.INVALID_HUMAN_GATE in _codes(doc)


@pytest.mark.parametrize(
    "transitions",
    [
        [
            _t("work", "checks"),
            _t("checks", "done"),
            _t("checks", "work", "retry_budget_remaining"),
        ],
        [_t("work", "checks"), _t("checks", "done", condition="retry_budget_exhausted")],
    ],
    ids=["when", "condition"],
)
def test_retry_guard_in_v2(transitions: list[dict[str, Any]]) -> None:
    assert C.RETRY_GUARD_IN_V2 in _codes(_doc(transitions=transitions))


def test_unreachable_state() -> None:
    doc = _doc(
        stations={"work": AGENT, "checks": CHECKS, "orphan": CHECKS},
        transitions=[_t("work", "checks"), _t("checks", "done"), _t("orphan", "done")],
    )
    assert [e.state for e in _errors(doc) if e.code is C.UNREACHABLE_STATE] == ["orphan"]


def test_nondeterministic_rules() -> None:
    doc = _doc(transitions=[_t("work", "checks"), _t("work", "done"), _t("checks", "done")])
    errors = _errors(doc)
    assert [(e.code, e.edge) for e in errors if e.code is C.NONDETERMINISTIC_RULES] == [
        (C.NONDETERMINISTIC_RULES, 1)
    ]


# --------------------------------------------------------------------------- #
# LINE_INVARIANTS and FEATURE_INVARIANTS                                       #
# --------------------------------------------------------------------------- #

MERGE = ServiceEntry(
    name="test.merge",
    effect="write_external",
    idempotent=False,
    max_timeout_seconds=60,
    max_attempts=1,
    merges=True,
)


def test_no_merge_without_pr_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(SERVICE_CATALOG, MERGE.name, MERGE)
    doc = _doc(
        stations={"work": AGENT, "checks": {"kind": "service", "service": MERGE.name}},
    )
    assert _invariants(doc) == {"no_merge"}


def test_no_merge_behind_pr_gate_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(SERVICE_CATALOG, MERGE.name, MERGE)
    merge = {"kind": "service", "service": MERGE.name}
    behind = _doc(
        stations={"work": AGENT, "gate": GATE, "merge": merge},
        transitions=[_t("work", "gate"), _t("gate", "merge", "approved"), _t("merge", "done")],
    )
    assert _errors(behind) == []
    # A plan gate is not enough, and neither is a side path around the pr gate.
    plan_gate = copy.deepcopy(behind)
    plan_gate["stations"]["gate"]["gate"] = "plan"
    assert _invariants(plan_gate) == {"no_merge"}
    around = copy.deepcopy(behind)
    around["transitions"].append(_t("gate", "merge", "changes_requested"))
    assert _invariants(around) == {"no_merge"}


def test_agent_no_write_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    # Today's ROLE_TOOLS give the implementer open_pr, which the station strips.
    assert "open_pr" in ROLE_TOOLS[SubAgentRole.IMPLEMENTER]
    assert _errors(_doc()) == []
    monkeypatch.setitem(
        ROLE_TOOLS, SubAgentRole.IMPLEMENTER, ROLE_TOOLS[SubAgentRole.IMPLEMENTER] | {"push"}
    )
    assert _invariants(_doc()) == {"agent_no_write_tools"}


def test_trusted_inputs() -> None:
    doc = _doc(
        stations={
            "work": AGENT,
            "checks": {
                "kind": "service",
                "service": "forge.open_pr",
                "inputs": {"task": "outputs.work.branch"},
            },
        }
    )
    assert _invariants(doc) == {"trusted_inputs"}


def test_agent_output_may_feed_trust_any_input() -> None:
    doc = _doc(
        stations={
            "work": AGENT,
            "checks": {
                "kind": "service",
                "service": "forge.tracker_comment",
                "inputs": {"ticket": "trigger.external_ref", "body": "outputs.work.summary_ref"},
            },
        }
    )
    assert _errors(doc) == []


@pytest.mark.parametrize(
    ("station", "inputs"),
    [
        ("prepare", {"repo": "task.description"}),
        ("push", {"pr": "task.title"}),
        ("propose_done", {"ticket": "task.description", "body": "outputs.assess.summary_ref"}),
        ("mark_ready", {"pr": "outputs.await_ci.failure_summary"}),
        ("push", {"pr": "outputs.local_checks.failure_summary"}),
        ("push", {"pr": "outputs.implement.branch"}),
    ],
    ids=[
        "repo-from-description",
        "pr-from-title",
        "ticket-from-description",
        "pr-from-await_ci-failure_summary",
        "pr-from-local_checks-failure_summary",
        "pr-from-agent-branch",
    ],
)
def test_trusted_inputs_on_the_loop(station: str, inputs: dict[str, str]) -> None:
    doc = _loop()
    doc["stations"][station]["inputs"] = inputs
    assert [(e.code, e.invariant, e.state) for e in _errors(doc)] == [
        (C.LINE_INVARIANT_VIOLATION, "trusted_inputs", station)
    ]


@pytest.mark.parametrize(
    "path", ["task.id", "task.key", "task.repo", "trigger.external_ref", "run.id"]
)
def test_run_facts_may_feed_trust_task_inputs(path: str) -> None:
    doc = _loop()
    doc["stations"]["propose_done"]["inputs"] = {"ticket": path, "body": "task.description"}
    assert _errors(doc) == []


def test_agent_outputs_not_guards() -> None:
    doc = _doc(transitions=[_t("work", "checks", condition="work.branch"), _t("checks", "done")])
    assert _invariants(doc) == {"agent_outputs_not_guards"}


@pytest.mark.parametrize(
    ("state", "invariant"), [("awaiting_review", "merge_human_gate"), ("spec_review", "spec_gate")]
)
def test_feature_invariants_apply_when_from_state_appears(state: str, invariant: str) -> None:
    doc = _doc(
        stations={"work": AGENT, state: GATE},
        transitions=[_t("work", state), _t(state, "done", "approved")],
    )
    errors = _errors(doc)
    assert {(e.code, e.invariant) for e in errors} == {(C.PROTECTED_INVARIANT_VIOLATION, invariant)}


# --------------------------------------------------------------------------- #
# Shape: parse errors                                                          #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("key", ["modes", "retry_policy", "escalation_policy", "extra_key"])
def test_line_rejects_v1_and_unknown_keys(key: str) -> None:
    with pytest.raises(ValidationError, match=key):
        parse_line(_doc(**{key: {}}))


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("action", "open_pr"),
        ("preconditions", ["repo_target_set"]),
        ("checks", ["lint"]),
        ("record", "x"),
        ("skill", "implementer"),
    ],
)
def test_transition_rejects_v1_keys(key: str, value: Any) -> None:
    with pytest.raises(ValidationError, match=key):
        parse_line(_doc(transitions=[_t("work", "checks", **{key: value}), _t("checks", "done")]))


@pytest.mark.parametrize(
    "transition",
    [
        {"from_state": "work", "to_state": "checks", "when": "succeeded"},
        {"from": "work", "to_state": "checks", "when": "succeeded"},
        {"from_state": "work", "to": "checks", "when": "succeeded"},
    ],
    ids=["both", "to_state", "from_state"],
)
def test_transition_takes_only_from_and_to(transition: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="_state"):
        parse_line(_doc(transitions=[transition, _t("checks", "done")]))


def test_line_name_key_is_workflow() -> None:
    doc = _doc(name="mini")
    del doc["workflow"]
    with pytest.raises(ValidationError, match="name"):
        parse_line(doc)


@pytest.mark.parametrize(
    ("stations", "top"),
    [
        ({"work": AGENT, "Checks": CHECKS}, {}),
        ({"work": AGENT, "c" * 65: CHECKS}, {}),
        ({"work": {**AGENT, "max_visits": 0}}, {}),
        ({"work": {**AGENT, "max_visits": 21}}, {}),
        ({"work": {**AGENT, "objective": "é" * 4097}}, {}),
        ({"work": {**AGENT, "model": 4}}, {}),
        ({"work": {**AGENT, "timeout_seconds": 14401}}, {}),
        ({"work": {**AGENT, "decisions": ["failed"]}}, {}),
        ({"work": {**AGENT, "decisions": ["ship", "ship"]}}, {}),
        ({"work": {**AGENT, "role": "overlord"}}, {}),
        ({"work": {**AGENT, "network": True}}, {}),
        ({"work": {**GATE, "required_approvals": 0}}, {}),
        ({"work": {**GATE, "required_approvals": 6}}, {}),
        ({"work": {**GATE, "sla_seconds": 299}}, {}),
        ({"work": {**GATE, "sla_seconds": 2_592_001}}, {}),
        ({"work": {**GATE, "gate": "release"}}, {}),
        ({"work": {"kind": "robot"}}, {}),
        ({"work": AGENT, "failed": CHECKS}, {}),
        ({"work": AGENT}, {"terminals": {"failed": "succeeded"}}),
        ({"work": AGENT, "done": CHECKS}, {}),
        ({"work": AGENT}, {"concurrency": {"max_active_runs": 51}}),
        ({"work": AGENT}, {"budget": {"max_tokens": 0}}),
        ({"work": AGENT}, {"dsl_version": "1"}),
    ],
    ids=[
        "state-name-case",
        "state-name-length",
        "max_visits-0",
        "max_visits-21",
        "objective-over-8KiB",
        "model-not-string",
        "agent-timeout",
        "decision-reuses-outcome",
        "decision-duplicate",
        "unknown-role",
        "no-network-field",
        "approvals-0",
        "approvals-6",
        "sla-low",
        "sla-high",
        "unknown-gate",
        "unknown-kind",
        "reserved-station-name",
        "reserved-terminal-remapped",
        "terminal-with-station",
        "concurrency",
        "budget-zero",
        "wrong-dsl-version",
    ],
)
def test_shape_errors(stations: dict[str, Any], top: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        parse_line(_doc(stations=stations, **top))


def test_objective_at_8kib_is_allowed() -> None:
    line = parse_line(_doc(stations={"work": {**AGENT, "objective": "x" * 8192}, "checks": CHECKS}))
    assert isinstance(line.stations["work"], AgentStation)


def test_human_station_defaults() -> None:
    gate = parse_line(_doc(stations={"work": GATE})).stations["work"]
    assert isinstance(gate, HumanStation)
    assert (gate.required_approvals, gate.max_visits) == (1, 3)


# --------------------------------------------------------------------------- #
# Service catalog                                                              #
# --------------------------------------------------------------------------- #


def test_catalog_has_the_phase_one_entries() -> None:
    assert {name: (e.effect, e.outcomes) for name, e in SERVICE_CATALOG.items()} == {
        "forge.prepare_worktree": ("read", frozenset()),
        "forge.confirm_red": ("read", frozenset({"not_red"})),
        "forge.run_checks": ("read", frozenset({"checks_failed"})),
        "forge.open_pr": ("write_external", frozenset()),
        "forge.push_branch": ("write_external", frozenset()),
        "forge.await_ci": ("read", frozenset({"checks_failed", "no_checks"})),
        "forge.mark_pr_ready": ("write_external", frozenset()),
        "forge.tracker_comment": ("write_external", frozenset()),
    }
    assert not any(e.merges for e in SERVICE_CATALOG.values())
    assert SERVICE_CATALOG["forge.run_checks"].outputs == {"failure_summary": (str, "any")}
    assert SERVICE_CATALOG["forge.await_ci"].outputs == {"failure_summary": (str, "any")}
    assert SERVICE_CATALOG["forge.open_pr"].outputs == {"pr_ref": (str, "task")}
    assert {n: e.failure_outcomes for n, e in SERVICE_CATALOG.items() if e.failure_outcomes} == {
        "forge.confirm_red": frozenset({"not_red"}),
        "forge.run_checks": frozenset({"checks_failed"}),
        "forge.await_ci": frozenset({"checks_failed"}),
    }
    assert all(e.failure_outcomes <= e.outcomes for e in SERVICE_CATALOG.values())


def test_catalog_params_have_no_secret_fields() -> None:
    for entry in SERVICE_CATALOG.values():
        for name, info in entry.params_model.model_fields.items():
            assert info.annotation not in (SecretStr, SecretBytes), (entry.name, name)
            assert not any(w in name for w in ("token", "secret", "password", "key")), name


def test_catalog_inputs_and_outputs_declare_trust() -> None:
    for entry in SERVICE_CATALOG.values():
        assert set(entry.inputs.values()) <= {"task", "any"}, entry.name
        assert {trust for _, trust in entry.outputs.values()} <= {"task", "any"}, entry.name


# --------------------------------------------------------------------------- #
# Purity                                                                       #
# --------------------------------------------------------------------------- #


def test_lines_import_pulls_in_no_engine_db_or_temporal() -> None:
    code = (
        "import sys, forge_contracts.lines\n"
        "bad = {m.split('.')[0] for m in sys.modules}\n"
        "bad &= {'forge_workflow', 'forge_db', 'temporalio'}\n"
        "print(sorted(bad))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "[]"


def _assess_with_out_of_scope(*add: dict[str, Any], **stations: Any) -> dict[str, Any]:
    doc = _loop(stations=stations, add=list(add))
    doc["stations"]["assess"]["decisions"] = ["already_done", "needs_work", "out_of_scope"]
    return doc


@pytest.mark.parametrize(
    "doc",
    [
        _assess_with_out_of_scope(
            _t("assess", "cancelled", "out_of_scope"), _t("assess", "cancelled", "failed")
        ),
        _assess_with_out_of_scope(
            _t("assess", "failed", "out_of_scope"), _t("assess", "failed", "timed_out")
        ),
        _assess_with_out_of_scope(
            _t("assess", "decline", "out_of_scope"),
            _t("assess", "decline", "failed"),
            _t("decline", "cancelled"),
            decline=NOTIFY,
        ),
    ],
    ids=["shared-cancelled-terminal", "shared-failed-terminal", "shared-notify-step"],
)
def test_a_failure_may_share_a_non_success_ending_with_a_decision(doc: dict[str, Any]) -> None:
    assert _errors(doc) == []


@pytest.mark.parametrize(
    ("doc", "edges"),
    [
        (_loop(add=[_t("implement", "implement", "visit_limit")]), [15]),
        (_loop(add=[_t("local_checks", "local_checks", "visit_limit")]), [15]),
        (
            _loop(
                stations={"n1": NOTIFY, "n2": NOTIFY},
                add=[
                    _t("implement", "n1", "visit_limit"),
                    _t("n1", "n2", "failed"),
                    _t("n1", "n2", "visit_limit"),
                    _t("n1", "cancelled"),
                    _t("n2", "n1", "visit_limit"),
                    _t("n2", "n1", "failed"),
                    _t("n2", "failed"),
                ],
            ),
            [17, 19],
        ),
    ],
    ids=["implement-self-loop", "local_checks-self-loop", "two-station-cycle"],
)
def test_visit_limit_cycles_are_refused(doc: dict[str, Any], edges: list[int]) -> None:
    errors = _errors(doc)
    assert [e.edge for e in errors if e.code is C.VISIT_LIMIT_CYCLE] == edges


def test_visit_limit_may_escalate_to_a_notify_step() -> None:
    doc = _loop(
        stations={"notify": NOTIFY},
        add=[_t("implement", "notify", "visit_limit"), _t("notify", "cancelled")],
    )
    assert _errors(doc) == []


@pytest.mark.parametrize("path", ["outputs.open_draft_pr.pr_ref\n", "task.repo\n", "run.id\n"])
def test_a_reference_with_a_trailing_newline_is_not_a_reference(path: str) -> None:
    doc = _loop()
    doc["stations"]["push"]["inputs"] = {"pr": path}
    assert C.UNKNOWN_REFERENCE in _codes(doc)
