"""Every automatic action type declares its impact (#191, FR-026)."""

from __future__ import annotations

import pytest

from scf_contracts.automation import (
    ACTION_IMPACT,
    UNCLASSIFIED_IMPACT,
    ActionImpact,
    AutomationActionType,
    action_impact,
)


def test_every_action_type_has_one_declared_impact() -> None:
    assert set(ACTION_IMPACT) == set(AutomationActionType)


@pytest.mark.parametrize(
    "action_type",
    [
        AutomationActionType.TRIGGER_DEPLOY,
        AutomationActionType.AUTO_MERGE,
        AutomationActionType.WEBHOOK_POST,
    ],
)
def test_deploy_merge_and_webhook_are_irreversible_and_external(
    action_type: AutomationActionType,
) -> None:
    impact = action_impact(action_type)
    assert impact == ActionImpact(reversible=False, external=True)
    assert impact.needs_person


def test_an_external_issue_is_external_and_reversible() -> None:
    impact = action_impact(AutomationActionType.CREATE_EXTERNAL_ISSUE)
    assert impact == ActionImpact(reversible=True, external=True)
    assert not impact.needs_person


@pytest.mark.parametrize("action_type", ["not_a_type", "", "WEBHOOK_POST"])
def test_an_unknown_type_is_irreversible_and_external(action_type: str) -> None:
    impact = action_impact(action_type)
    assert impact is UNCLASSIFIED_IMPACT
    assert impact == ActionImpact(reversible=False, external=True)
    assert impact.needs_person


def test_a_known_type_is_found_by_its_string_value() -> None:
    assert action_impact("set_status") == ActionImpact(reversible=True, external=False)
