"""Tests for ``PolicyEvaluator.evaluate`` (plan Task 1.10).

Covers the required cases from the plan:
- write to ``app/**`` allowed, to ``secrets/**`` denied,
- ``deploy_prod`` restricted,
- unknown action defaults deny,
plus write-glob precedence, the deploy-rules path, and the allow-list path.
"""

from __future__ import annotations

import pytest

from forge_contracts import (
    ApprovalGate,
    Decision,
    DecisionEffect,
    DeployRules,
    Policy,
    ToolCall,
    WriteRules,
)
from forge_policy import RepoPolicyEvaluator, evaluate


@pytest.fixture
def evaluator() -> RepoPolicyEvaluator:
    return RepoPolicyEvaluator()


# --------------------------------------------------------------------------- #
# Write rules (glob allow / deny)                                             #
# --------------------------------------------------------------------------- #


def test_write_to_allowed_path_is_allowed(
    evaluator: RepoPolicyEvaluator, spec_policy: Policy
) -> None:
    decision = evaluator.evaluate(ToolCall(tool="write_code", path="app/main.py"), spec_policy)
    assert isinstance(decision, Decision)
    assert decision.effect is DecisionEffect.ALLOW
    assert decision.allowed is True
    assert decision.matched_rule is not None
    assert "app/**" in decision.matched_rule


def test_write_to_tests_path_is_allowed(
    evaluator: RepoPolicyEvaluator, spec_policy: Policy
) -> None:
    decision = evaluator.evaluate(
        ToolCall(tool="write_code", path="tests/test_customers.py"), spec_policy
    )
    assert decision.allowed is True


def test_write_to_secrets_path_is_denied(
    evaluator: RepoPolicyEvaluator, spec_policy: Policy
) -> None:
    decision = evaluator.evaluate(ToolCall(tool="write_code", path="secrets/db.json"), spec_policy)
    assert decision.effect is DecisionEffect.DENY
    assert decision.allowed is False
    assert decision.matched_rule is not None
    assert "secrets/**" in decision.matched_rule


def test_write_to_pem_file_anywhere_is_denied(
    evaluator: RepoPolicyEvaluator, spec_policy: Policy
) -> None:
    decision = evaluator.evaluate(
        ToolCall(tool="write_file", path="app/certs/server.pem"), spec_policy
    )
    assert decision.effect is DecisionEffect.DENY
    assert "*.pem" in (decision.matched_rule or "")


def test_write_to_dotenv_is_denied(evaluator: RepoPolicyEvaluator, spec_policy: Policy) -> None:
    decision = evaluator.evaluate(ToolCall(tool="write_file", path=".env"), spec_policy)
    assert decision.effect is DecisionEffect.DENY


def test_write_deny_takes_precedence_over_allow(evaluator: RepoPolicyEvaluator) -> None:
    policy = Policy(
        repo_id="r",
        write_rules=WriteRules(allow=["app/**"], deny=["app/secrets/**"]),
    )
    decision = evaluator.evaluate(ToolCall(tool="write_code", path="app/secrets/token.txt"), policy)
    assert decision.effect is DecisionEffect.DENY
    assert "app/secrets/**" in (decision.matched_rule or "")


def test_write_to_unlisted_path_defaults_deny(
    evaluator: RepoPolicyEvaluator, spec_policy: Policy
) -> None:
    decision = evaluator.evaluate(
        ToolCall(tool="write_code", path="random/place/file.txt"), spec_policy
    )
    assert decision.effect is DecisionEffect.DENY


def test_write_without_path_defaults_deny(
    evaluator: RepoPolicyEvaluator, spec_policy: Policy
) -> None:
    decision = evaluator.evaluate(ToolCall(tool="write_code"), spec_policy)
    assert decision.effect is DecisionEffect.DENY


def test_write_path_from_arguments(evaluator: RepoPolicyEvaluator, spec_policy: Policy) -> None:
    decision = evaluator.evaluate(
        ToolCall(tool="write_file", arguments={"path": "app/api/v1.py"}), spec_policy
    )
    assert decision.allowed is True


# --------------------------------------------------------------------------- #
# Restricted / allowed actions                                               #
# --------------------------------------------------------------------------- #


def test_restricted_action_deploy_prod_is_denied(
    evaluator: RepoPolicyEvaluator, spec_policy: Policy
) -> None:
    decision = evaluator.evaluate(ToolCall(tool="deploy_prod"), spec_policy)
    assert decision.effect is DecisionEffect.DENY
    assert decision.allowed is False
    assert "restricted_actions" in (decision.matched_rule or "")


def test_restricted_action_via_action_field(
    evaluator: RepoPolicyEvaluator, spec_policy: Policy
) -> None:
    decision = evaluator.evaluate(ToolCall(tool="shell", action="delete_files"), spec_policy)
    assert decision.effect is DecisionEffect.DENY


def test_allowed_action_is_allowed(evaluator: RepoPolicyEvaluator, spec_policy: Policy) -> None:
    decision = evaluator.evaluate(ToolCall(tool="read_repo"), spec_policy)
    assert decision.effect is DecisionEffect.ALLOW
    assert "allowed_actions" in (decision.matched_rule or "")


def test_unknown_action_defaults_deny(evaluator: RepoPolicyEvaluator, spec_policy: Policy) -> None:
    decision = evaluator.evaluate(ToolCall(tool="frobnicate_universe"), spec_policy)
    assert decision.effect is DecisionEffect.DENY
    assert decision.allowed is False


def test_empty_tool_call_defaults_deny(evaluator: RepoPolicyEvaluator, spec_policy: Policy) -> None:
    decision = evaluator.evaluate(ToolCall(tool=""), spec_policy)
    assert decision.effect is DecisionEffect.DENY


# --------------------------------------------------------------------------- #
# Deploy rules                                                                #
# --------------------------------------------------------------------------- #


def test_deploy_to_restricted_environment_requires_approval(evaluator: RepoPolicyEvaluator) -> None:
    policy = Policy(
        repo_id="r",
        allowed_actions=["deploy"],
        deploy_rules=DeployRules(
            allow_agent_deploy=True,
            environments=["dev"],
            restricted_environments=["staging", "production"],
        ),
    )
    decision = evaluator.evaluate(
        ToolCall(tool="deploy", arguments={"environment": "production"}), policy
    )
    assert decision.effect is DecisionEffect.REQUIRES_APPROVAL
    assert decision.requires_approval is True
    assert decision.approval_gate is ApprovalGate.DEPLOY
    assert decision.allowed is False


def test_deploy_to_allowed_env_is_allowed_when_enabled(evaluator: RepoPolicyEvaluator) -> None:
    policy = Policy(
        repo_id="r",
        allowed_actions=["deploy"],
        deploy_rules=DeployRules(
            allow_agent_deploy=True,
            environments=["dev"],
            restricted_environments=["production"],
        ),
    )
    decision = evaluator.evaluate(ToolCall(tool="deploy", arguments={"environment": "dev"}), policy)
    assert decision.effect is DecisionEffect.ALLOW


def test_deploy_denied_when_agent_deploy_disabled(evaluator: RepoPolicyEvaluator) -> None:
    policy = Policy(
        repo_id="r",
        allowed_actions=["deploy"],
        deploy_rules=DeployRules(allow_agent_deploy=False, environments=["dev"]),
    )
    decision = evaluator.evaluate(ToolCall(tool="deploy", arguments={"environment": "dev"}), policy)
    assert decision.effect is DecisionEffect.DENY


def test_deploy_prod_inferred_from_name_denied_by_default(evaluator: RepoPolicyEvaluator) -> None:
    # No restricted_actions, default deploy_rules (allow_agent_deploy False).
    policy = Policy(repo_id="r", allowed_actions=["deploy_prod"])
    decision = evaluator.evaluate(ToolCall(tool="deploy_prod"), policy)
    assert decision.allowed is False


def test_deploy_to_restricted_env_requires_approval_even_when_disabled(
    evaluator: RepoPolicyEvaluator,
) -> None:
    policy = Policy(
        repo_id="r",
        allowed_actions=["deploy"],
        deploy_rules=DeployRules(
            allow_agent_deploy=False,
            restricted_environments=["production"],
        ),
    )
    decision = evaluator.evaluate(
        ToolCall(tool="deploy", arguments={"environment": "prod"}), policy
    )
    # 'prod' normalises to 'production' which is restricted.
    assert decision.effect is DecisionEffect.REQUIRES_APPROVAL
    assert decision.approval_gate is ApprovalGate.DEPLOY


# --------------------------------------------------------------------------- #
# Module-level convenience + determinism                                     #
# --------------------------------------------------------------------------- #


def test_module_level_evaluate(spec_policy: Policy) -> None:
    decision = evaluate(ToolCall(tool="write_code", path="app/main.py"), spec_policy)
    assert decision.allowed is True


def test_evaluate_is_pure_and_deterministic(
    evaluator: RepoPolicyEvaluator, spec_policy: Policy
) -> None:
    call = ToolCall(tool="write_code", path="secrets/x.key")
    d1 = evaluator.evaluate(call, spec_policy)
    d2 = evaluator.evaluate(call, spec_policy)
    assert d1.model_dump() == d2.model_dump()
    # ToolCall not mutated.
    assert call.path == "secrets/x.key"
