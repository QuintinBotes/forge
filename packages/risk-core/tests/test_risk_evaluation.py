"""Declared class against the derived floor, and per-class gate requirements (#192, spec A §3.1)."""

from __future__ import annotations

import pydantic
import pytest

from scf_contracts import (
    DerivedRisk,
    GateRequirements,
    RiskClass,
    RiskEvaluation,
    RiskOverride,
    RiskRules,
)
from scf_risk.errors import RiskDeEscalationError
from scf_risk.evaluation import evaluate
from scf_risk.requirements import requirements_for

HIGH_FLOOR = DerivedRisk(risk_class=RiskClass.HIGH, matched_rule_ids=["auth"])


# --------------------------------------------------------------------------- #
# evaluate (AC5)                                                                #
# --------------------------------------------------------------------------- #


def test_no_declaration_takes_the_floor() -> None:
    result = evaluate(HIGH_FLOOR, None, None)

    assert result.final_class is RiskClass.HIGH
    assert result.derived_class is RiskClass.HIGH
    assert result.declared_class is None
    assert result.matched_rule_ids == ["auth"]


@pytest.mark.parametrize("declared", [RiskClass.HIGH, RiskClass.CRITICAL])
def test_a_declaration_at_or_above_the_floor_is_accepted(declared: RiskClass) -> None:
    result = evaluate(HIGH_FLOOR, declared, None)

    assert result.final_class is declared
    assert result.declared_class is declared
    assert result.override_approver is None
    assert result.override_reason is None


def test_an_unused_override_is_not_recorded() -> None:
    result = evaluate(HIGH_FLOOR, RiskClass.CRITICAL, RiskOverride(approver="ana", reason="r"))

    assert result.final_class is RiskClass.CRITICAL
    assert result.override_approver is None
    assert result.override_reason is None


@pytest.mark.parametrize("declared", [RiskClass.MINIMAL, RiskClass.LOW])
def test_a_declaration_below_the_floor_without_override_is_refused(declared: RiskClass) -> None:
    with pytest.raises(RiskDeEscalationError) as refused:
        evaluate(HIGH_FLOOR, declared, None)

    assert refused.value.derived is RiskClass.HIGH
    assert refused.value.declared is declared
    assert "high" in str(refused.value)
    assert declared.value in str(refused.value)


def test_a_declaration_below_the_floor_with_override_applies_and_records_who_and_why() -> None:
    override = RiskOverride(approver="ana@example.com", reason="docs-only hotfix, reviewed")

    result = evaluate(HIGH_FLOOR, RiskClass.LOW, override)

    assert result.final_class is RiskClass.LOW
    assert result.derived_class is RiskClass.HIGH
    assert result.declared_class is RiskClass.LOW
    assert result.override_approver == "ana@example.com"
    assert result.override_reason == "docs-only hotfix, reviewed"


def test_the_fallback_flag_is_carried_into_the_evaluation() -> None:
    derived = DerivedRisk(risk_class=RiskClass.CRITICAL, fallback_applied=True, reason="no diff")

    assert evaluate(derived, None, None).fallback_applied is True


@pytest.mark.parametrize(
    ("approver", "reason"), [("", "why"), ("   ", "why"), ("ana", ""), ("ana", " \t\n")]
)
def test_an_override_with_a_blank_approver_or_reason_is_rejected(
    approver: str, reason: str
) -> None:
    with pytest.raises(pydantic.ValidationError):
        RiskOverride(approver=approver, reason=reason)


def test_a_declared_class_given_as_a_string_is_ordered_as_a_class() -> None:
    critical_floor = DerivedRisk(risk_class=RiskClass.CRITICAL)

    with pytest.raises(RiskDeEscalationError):
        evaluate(HIGH_FLOOR, "low", None)
    with pytest.raises(RiskDeEscalationError):
        evaluate(critical_floor, "minimal", None)
    assert evaluate(HIGH_FLOOR, "critical", None).final_class is RiskClass.CRITICAL
    assert evaluate(HIGH_FLOOR, "low", RiskOverride(approver="ana", reason="r")).declared_class is (
        RiskClass.LOW
    )


@pytest.mark.parametrize(
    ("approver", "reason"), [(None, None), ("   ", "why"), ("ana", ""), (None, "why")]
)
def test_an_evaluation_cannot_lower_the_class_without_a_named_override(
    approver: str | None, reason: str | None
) -> None:
    with pytest.raises(pydantic.ValidationError, match="override"):
        RiskEvaluation(
            derived_class="critical",
            final_class="minimal",
            override_approver=approver,
            override_reason=reason,
        )


def test_an_evaluation_with_a_named_override_or_no_lowering_is_valid() -> None:
    lowered = RiskEvaluation(
        derived_class="critical",
        final_class="minimal",
        override_approver="ana",
        override_reason="r",
    )
    raised = RiskEvaluation(derived_class="low", final_class="high")

    assert lowered.final_class is RiskClass.MINIMAL
    assert raised.final_class is RiskClass.HIGH


def test_an_override_cannot_be_changed_after_it_is_made() -> None:
    override = RiskOverride(approver="ana", reason="reviewed")

    with pytest.raises(pydantic.ValidationError):
        override.approver = "   "  # type: ignore[misc]
    assert override.approver == "ana"


# --------------------------------------------------------------------------- #
# requirements_for (AC6)                                                        #
# --------------------------------------------------------------------------- #

RULES = RiskRules.model_validate(
    {
        "default": "low",
        "classes": {
            "minimal": {"approvals": 0, "deploy": "auto", "red_team": False, "self_eval": False},
            "low": {"approvals": 1, "deploy": "auto", "red_team": False, "self_eval": True},
            "high": {"approvals": 2, "deploy": "gated", "red_team": False, "self_eval": True},
            "critical": {"approvals": 2, "deploy": "gated", "red_team": True, "self_eval": True},
        },
    }
)


def test_requirements_follow_the_configured_class() -> None:
    assert requirements_for(RiskClass.CRITICAL, RULES) == GateRequirements(
        risk_class=RiskClass.CRITICAL,
        approvals=2,
        deploy_mode="gated",
        red_team=True,
        self_eval=True,
    )
    assert requirements_for(RiskClass.LOW, RULES) == GateRequirements(
        risk_class=RiskClass.LOW, approvals=1, deploy_mode="auto", red_team=False, self_eval=True
    )


def test_an_unconfigured_class_adds_no_requirement() -> None:
    rules = RiskRules(default=RiskClass.LOW)

    assert requirements_for(RiskClass.HIGH, rules) == GateRequirements(
        risk_class=RiskClass.HIGH, approvals=0, deploy_mode="auto", red_team=False, self_eval=False
    )


def test_an_unconfigured_class_takes_the_nearest_configured_class_below_it() -> None:
    only_high = RiskRules.model_validate(
        {"default": "low", "classes": {"high": {"approvals": 2, "deploy": "gated"}}}
    )
    low_and_critical = RiskRules.model_validate(
        {
            "default": "low",
            "classes": {
                "low": {"approvals": 1, "self_eval": True},
                "critical": {"approvals": 3, "deploy": "gated", "red_team": True},
            },
        }
    )

    # A fallback-to-critical change must not get fewer controls than a high one.
    assert requirements_for(RiskClass.CRITICAL, only_high) == GateRequirements(
        risk_class=RiskClass.CRITICAL,
        approvals=2,
        deploy_mode="gated",
        red_team=False,
        self_eval=False,
    )
    assert requirements_for(RiskClass.LOW, only_high) == GateRequirements(
        risk_class=RiskClass.LOW, approvals=0, deploy_mode="auto", red_team=False, self_eval=False
    )
    assert requirements_for(RiskClass.HIGH, low_and_critical) == GateRequirements(
        risk_class=RiskClass.HIGH, approvals=1, deploy_mode="auto", red_team=False, self_eval=True
    )
