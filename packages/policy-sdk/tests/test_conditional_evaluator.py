"""F29 — the :class:`ConditionalPolicyEvaluator` precedence ladder (ACs 1, 4-12, 15, 21)."""

from __future__ import annotations

import random
from datetime import UTC, datetime

import pytest

from forge_contracts import (
    Condition,
    ConditionalRule,
    ConditionGroup,
    ConditionOp,
    DecisionEffect,
    Policy,
    ReviewRules,
    RuleEffect,
    ToolCall,
    WriteRules,
)
from forge_policy import (
    ConditionalPolicyEvaluator,
    PolicyContext,
    RepoPolicyEvaluator,
)

# Saturday 2026-06-27 23:00Z (outside business hours) / Tuesday 2026-06-23 12:00Z (inside).
SAT_NIGHT = datetime(2026, 6, 27, 23, 0, tzinfo=UTC)
TUE_NOON = datetime(2026, 6, 23, 12, 0, tzinfo=UTC)


@pytest.fixture
def evaluator() -> ConditionalPolicyEvaluator:
    return ConditionalPolicyEvaluator()


# --------------------------------------------------------------------------- #
# AC1 — regression lock: a v1 policy evaluates identically to flat F04.        #
# --------------------------------------------------------------------------- #

_F04_MATRIX = [
    ToolCall(tool="write_code", path="app/main.py"),
    ToolCall(tool="write_code", path="secrets/db.json"),
    ToolCall(tool="write_file", path="app/certs/server.pem"),
    ToolCall(tool="write_file", path=".env"),
    ToolCall(tool="write_code", path="random/place/file.txt"),
    ToolCall(tool="write_code"),
    ToolCall(tool="write_file", path="../../etc/passwd"),
    ToolCall(tool="deploy_prod"),
    ToolCall(tool="shell", action="delete_files"),
    ToolCall(tool="read_repo"),
    ToolCall(tool="frobnicate_universe"),
    ToolCall(tool=""),
    ToolCall(tool="deploy", arguments={"environment": "production"}),
    ToolCall(tool="deploy", arguments={"environment": "dev"}),
]


def test_v1_policy_matches_f04_base(
    evaluator: ConditionalPolicyEvaluator, spec_policy: Policy
) -> None:
    base = RepoPolicyEvaluator()
    assert spec_policy.schema_version == 1 and not spec_policy.rules
    for call in _F04_MATRIX:
        flat = base.evaluate(call, spec_policy)
        conditional = evaluator.evaluate(call, spec_policy)
        assert conditional.effect is flat.effect
        assert conditional.reason == flat.reason
        assert conditional.matched_rule == flat.matched_rule
        assert conditional.requires_approval == flat.requires_approval
        assert conditional.severity == flat.severity
        assert conditional.conditional_matches == []


# --------------------------------------------------------------------------- #
# AC4 / AC5 — conditional deny tightens a base allow; condition-false passes.  #
# --------------------------------------------------------------------------- #


def test_conditional_deny_tightens_allow(
    evaluator: ConditionalPolicyEvaluator, canonical_policy: Policy
) -> None:
    d = evaluator.evaluate_in_context(
        ToolCall(tool="write_file", path="infra/x.tf"),
        canonical_policy,
        PolicyContext(branch="feature/x"),
    )
    assert d.effect is DecisionEffect.DENY
    assert d.severity == "critical"
    assert d.matched_rule == "rules[infra-writes-main-only]"
    assert d.base_effect is DecisionEffect.ALLOW
    assert d.conditional_matches[0].rule_id == "infra-writes-main-only"


def test_condition_false_passes_through(
    evaluator: ConditionalPolicyEvaluator, canonical_policy: Policy
) -> None:
    d = evaluator.evaluate_in_context(
        ToolCall(tool="write_file", path="infra/x.tf"),
        canonical_policy,
        PolicyContext(branch="main"),
    )
    assert d.effect is DecisionEffect.ALLOW
    assert d.conditional_matches == []


# --------------------------------------------------------------------------- #
# AC6 — gate escalates a base allow (time-conditional).                        #
# --------------------------------------------------------------------------- #


def test_gate_escalates_base_allow_outside_window(
    evaluator: ConditionalPolicyEvaluator, canonical_policy: Policy
) -> None:
    d = evaluator.evaluate_in_context(
        ToolCall(tool="deploy", arguments={"environment": "production"}),
        canonical_policy,
        PolicyContext(now=SAT_NIGHT),
    )
    assert d.effect is DecisionEffect.REQUIRES_APPROVAL
    assert d.requires_approval is True
    assert d.allowed is False
    assert d.base_effect is DecisionEffect.ALLOW
    assert d.matched_rule == "rules[deploy-prod-business-hours-only]"


def test_gate_does_not_fire_in_window(
    evaluator: ConditionalPolicyEvaluator, canonical_policy: Policy
) -> None:
    d = evaluator.evaluate_in_context(
        ToolCall(tool="deploy", arguments={"environment": "production"}),
        canonical_policy,
        PolicyContext(now=TUE_NOON),
    )
    assert d.effect is DecisionEffect.ALLOW
    assert d.conditional_matches == []


# --------------------------------------------------------------------------- #
# AC7 — bounded loosening (override_base on a non-critical base deny).         #
# --------------------------------------------------------------------------- #


def test_override_loosens_noncritical_deny(
    evaluator: ConditionalPolicyEvaluator, canonical_policy: Policy
) -> None:
    d = evaluator.evaluate_in_context(
        ToolCall(tool="run_command", arguments={"command": "terraform apply -auto-approve"}),
        canonical_policy,
        PolicyContext(environment="dev", actor_role="admin"),
    )
    assert d.effect is DecisionEffect.ALLOW
    assert d.base_effect is DecisionEffect.DENY
    assert d.matched_rule == "rules[terraform-apply-dev-admin]"


def test_override_denied_for_non_admin(
    evaluator: ConditionalPolicyEvaluator, canonical_policy: Policy
) -> None:
    d = evaluator.evaluate_in_context(
        ToolCall(tool="run_command", arguments={"command": "terraform apply -auto-approve"}),
        canonical_policy,
        PolicyContext(environment="dev", actor_role="member"),
    )
    assert d.effect is DecisionEffect.DENY
    assert d.allowed is False


# --------------------------------------------------------------------------- #
# AC8 / AC9 — override cannot defeat the critical floor.                       #
# --------------------------------------------------------------------------- #


def _override_everything_policy() -> Policy:
    return Policy(
        repo_id="r",
        schema_version=2,
        write_rules=WriteRules(allow=["app/**"], deny=["secrets/**", "*.pem"]),
        review_rules=ReviewRules(approval_required_for_merge=True),
        allowed_actions=["merge", "push", "write_file"],
        rules=[
            ConditionalRule(
                id="loosen-all",
                applies_to=["*"],
                effect=RuleEffect.ALLOW,
                override_base=True,
                reason="attempt to loosen everything",
            )
        ],
    )


def test_override_cannot_defeat_secret_floor(evaluator: ConditionalPolicyEvaluator) -> None:
    d = evaluator.evaluate_in_context(
        ToolCall(tool="write_file", path="secrets/x.pem"),
        _override_everything_policy(),
        PolicyContext(),
    )
    assert d.effect is DecisionEffect.DENY
    assert d.severity == "critical"
    assert d.base_effect is DecisionEffect.DENY
    assert d.conditional_matches[0].rule_id == "loosen-all"


def test_override_cannot_defeat_traversal(evaluator: ConditionalPolicyEvaluator) -> None:
    d = evaluator.evaluate_in_context(
        ToolCall(tool="write_file", path="../../etc/passwd"),
        _override_everything_policy(),
        PolicyContext(),
    )
    assert d.effect is DecisionEffect.DENY
    assert d.matched_rule == "path_traversal"


# --------------------------------------------------------------------------- #
# AC21 — the merge gate is immutable (human approval before merge — always).   #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("action", ["merge", "push"])
def test_override_cannot_defeat_merge_gate(
    evaluator: ConditionalPolicyEvaluator, action: str
) -> None:
    d = evaluator.evaluate_in_context(
        ToolCall(tool=action, arguments={"branch": "main"}),
        _override_everything_policy(),
        PolicyContext(),
    )
    assert d.effect is DecisionEffect.REQUIRES_APPROVAL
    assert d.requires_approval is True
    assert d.allowed is False
    assert d.conditional_matches[0].rule_id == "loosen-all"


def test_conditional_can_still_tighten_merge(evaluator: ConditionalPolicyEvaluator) -> None:
    policy = Policy(
        repo_id="r",
        schema_version=2,
        review_rules=ReviewRules(approval_required_for_merge=True),
        allowed_actions=["merge"],
        rules=[
            ConditionalRule(
                id="no-merge-friday",
                applies_to=["merge"],
                effect=RuleEffect.DENY,
                severity="warning",
                reason="no merges on a Friday",
            )
        ],
    )
    d = evaluator.evaluate_in_context(ToolCall(tool="merge"), policy, PolicyContext())
    assert d.effect is DecisionEffect.DENY
    assert d.matched_rule == "rules[no-merge-friday]"


# --------------------------------------------------------------------------- #
# AC10 / AC11 — deny precedence + deterministic priority ordering.             #
# --------------------------------------------------------------------------- #


def test_deny_beats_override(evaluator: ConditionalPolicyEvaluator) -> None:
    policy = Policy(
        repo_id="r",
        schema_version=2,
        rules=[
            ConditionalRule(
                id="allow-it",
                applies_to=["run_command"],
                effect=RuleEffect.ALLOW,
                override_base=True,
                reason="a",
            ),
            ConditionalRule(
                id="deny-it", applies_to=["run_command"], effect=RuleEffect.DENY, reason="d"
            ),
        ],
    )
    d = evaluator.evaluate_in_context(ToolCall(tool="run_command"), policy, PolicyContext())
    assert d.effect is DecisionEffect.DENY


@pytest.mark.parametrize("reverse", [False, True])
def test_priority_ordering_deterministic(
    evaluator: ConditionalPolicyEvaluator, reverse: bool
) -> None:
    high = ConditionalRule(
        id="high-prio", applies_to=["run_command"], effect=RuleEffect.DENY, reason="d", priority=10
    )
    low = ConditionalRule(
        id="low-prio", applies_to=["run_command"], effect=RuleEffect.DENY, reason="d", priority=20
    )
    order = [low, high] if reverse else [high, low]
    policy = Policy(repo_id="r", schema_version=2, rules=order)
    d = evaluator.evaluate_in_context(ToolCall(tool="run_command"), policy, PolicyContext())
    assert d.matched_rule == "rules[high-prio]"


# --------------------------------------------------------------------------- #
# AC12 — empty ``when`` + applies_to ["*"] fires for every action.            #
# --------------------------------------------------------------------------- #


def test_empty_when_always_matches(evaluator: ConditionalPolicyEvaluator) -> None:
    policy = Policy(
        repo_id="r",
        schema_version=2,
        rules=[
            ConditionalRule(
                id="block-all",
                applies_to=["*"],
                when=ConditionGroup(),
                effect=RuleEffect.DENY,
                reason="locked down",
            )
        ],
    )
    for call in (ToolCall(tool="read_repo"), ToolCall(tool="run_command"), ToolCall(tool="x")):
        d = evaluator.evaluate_in_context(call, policy, PolicyContext())
        assert d.effect is DecisionEffect.DENY
        assert d.matched_rule == "rules[block-all]"


def test_disabled_rule_does_not_fire(evaluator: ConditionalPolicyEvaluator) -> None:
    policy = Policy(
        repo_id="r",
        schema_version=2,
        allowed_actions=["read_repo"],
        rules=[
            ConditionalRule(
                id="off",
                applies_to=["read_repo"],
                effect=RuleEffect.DENY,
                reason="d",
                enabled=False,
            )
        ],
    )
    d = evaluator.evaluate_in_context(ToolCall(tool="read_repo"), policy, PolicyContext())
    assert d.effect is DecisionEffect.ALLOW
    assert d.conditional_matches == []


# --------------------------------------------------------------------------- #
# AC15 — totality: random valid inputs always return a Decision, never raise.  #
# --------------------------------------------------------------------------- #


def test_evaluate_is_total() -> None:
    rng = random.Random(29)
    evaluator = ConditionalPolicyEvaluator()
    actions = sorted(
        {"write_file", "deploy", "run_command", "merge", "read_repo", "spawn_subagent"}
    )
    ops = [ConditionOp.EQ, ConditionOp.NE, ConditionOp.MATCHES_GLOB, ConditionOp.CONTAINS]
    fields = ["branch", "environment", "path", "command", "actor_role", "task_kind"]
    effects = list(RuleEffect)
    for _ in range(400):
        rules = [
            ConditionalRule(
                id=f"r{i}",
                applies_to=[rng.choice(["*", *actions])],
                when=ConditionGroup(
                    match=rng.choice(["all", "any"]),
                    conditions=[
                        Condition(
                            field=rng.choice(fields),
                            op=rng.choice(ops),
                            value=rng.choice(["main", "infra/**", "x", "dev"]),
                        )
                    ],
                ),
                effect=rng.choice(effects),
                override_base=rng.choice([True, False]),
                reason="r",
            )
            for i in range(rng.randint(0, 4))
        ]
        policy = Policy(
            repo_id="r",
            schema_version=2 if rules else 1,
            write_rules=WriteRules(allow=["app/**"], deny=["secrets/**"]),
            allowed_actions=["read_repo", "deploy"],
            rules=rules,
        )
        ctx = PolicyContext(
            branch=rng.choice([None, "main", "feature/x"]),
            environment=rng.choice([None, "dev", "production"]),
            actor_role=rng.choice([None, "admin", "member"]),
            now=rng.choice([None, SAT_NIGHT, TUE_NOON]),
        )
        call = ToolCall(tool=rng.choice(actions), arguments={"command": "x", "path": "app/y"})
        decision = evaluator.evaluate_in_context(call, policy, ctx)
        assert decision.effect in set(DecisionEffect)
