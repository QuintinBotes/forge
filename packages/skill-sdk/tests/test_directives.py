"""Tests for the skill-directives projection (F17 — F11 gap-fill)."""

from __future__ import annotations

from forge_contracts.incident import BlastRadius
from forge_skill import (
    SkillProfileRegistry,
    blast_within,
    normalize_action,
    skill_permits_action,
    to_directives,
)


def _incident_directives():
    profile = SkillProfileRegistry().get("incident-response")
    return to_directives(profile)


def test_incident_response_directives_allowlist() -> None:
    directives = _incident_directives()
    assert directives.approval_before_action is True
    assert directives.max_blast_radius is BlastRadius.LOW
    assert directives.allowed_actions == frozenset(
        {"read_logs", "query_metrics", "read_repo", "run_diagnostic_scripts"}
    )
    assert {"deploy_prod", "delete_data", "modify_access_controls"} <= directives.forbidden_actions


def test_forbidden_action_is_denied_critical() -> None:
    directives = _incident_directives()
    for action in ("deploy_prod", "delete_data", "modify_access_controls"):
        decision = skill_permits_action(directives, action)
        assert decision.allowed is False
        assert decision.requires_approval is True
        assert decision.severity == "critical"


def test_allowed_action_permitted() -> None:
    directives = _incident_directives()
    for action in ("read_logs", "query_metrics", "read_repo", "run_diagnostic_scripts"):
        assert skill_permits_action(directives, action).allowed is True


def test_allowlist_miss_denied() -> None:
    directives = _incident_directives()
    decision = skill_permits_action(directives, "scale_service")
    assert decision.allowed is False
    assert decision.severity == "high"


def test_alias_normalisation() -> None:
    directives = _incident_directives()
    # "deploy" is an alias of forbidden "deploy_prod".
    assert normalize_action("deploy") == "deploy_prod"
    assert skill_permits_action(directives, "deploy").allowed is False
    # "logs" is an alias of allowed "read_logs".
    assert skill_permits_action(directives, "logs").allowed is True


def test_blast_within_cap() -> None:
    assert blast_within(BlastRadius.LOW, BlastRadius.LOW) is True
    assert blast_within(BlastRadius.MEDIUM, BlastRadius.LOW) is False
    assert blast_within(BlastRadius.HIGH, BlastRadius.LOW) is False
    assert blast_within(BlastRadius.HIGH, None) is True
