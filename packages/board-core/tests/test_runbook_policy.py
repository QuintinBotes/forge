"""Tests for the runbook blast-radius safety helper (F17, AC7/AC10/AC19)."""

from __future__ import annotations

import random
import uuid

import pytest

from forge_board.incidents import assert_runbook_within_policy
from forge_contracts.incident import BlastRadius, Runbook, RunbookStep
from forge_skill import SkillProfileRegistry, to_directives

INCIDENT_DIRECTIVES = to_directives(SkillProfileRegistry().get("incident-response"))


def _runbook(*steps: RunbookStep) -> Runbook:
    return Runbook(incident_id=uuid.uuid4(), steps=list(steps))


def _step(action: str, blast: BlastRadius = BlastRadius.LOW, sid: str = "s1") -> RunbookStep:
    return RunbookStep(id=sid, order=1, title=action, action=action, blast_radius=blast)


def test_clean_low_blast_plan_ok() -> None:
    rb = _runbook(
        _step("read_logs", sid="s1"),
        _step("query_metrics", sid="s2"),
        _step("run_diagnostic_scripts", sid="s3"),
    )
    assert assert_runbook_within_policy(rb, INCIDENT_DIRECTIVES) == []


@pytest.mark.parametrize("action", ["deploy_prod", "delete_data", "modify_access_controls"])
def test_forbidden_action_flagged(action: str) -> None:
    rb = _runbook(_step(action, sid="bad"))
    assert assert_runbook_within_policy(rb, INCIDENT_DIRECTIVES) == ["bad"]


def test_high_blast_radius_flagged() -> None:
    rb = _runbook(_step("read_logs", blast=BlastRadius.HIGH, sid="hi"))
    assert assert_runbook_within_policy(rb, INCIDENT_DIRECTIVES) == ["hi"]


def test_allowlist_miss_flagged() -> None:
    rb = _runbook(_step("scale_service", sid="miss"))
    assert assert_runbook_within_policy(rb, INCIDENT_DIRECTIVES) == ["miss"]


def test_mixed_plan_reports_only_offenders() -> None:
    rb = _runbook(
        _step("read_logs", sid="ok"),
        _step("deploy_prod", sid="bad1"),
        _step("read_repo", blast=BlastRadius.MEDIUM, sid="bad2"),
    )
    assert set(assert_runbook_within_policy(rb, INCIDENT_DIRECTIVES)) == {"bad1", "bad2"}


def test_assert_runbook_within_policy_is_total() -> None:
    """Randomized totality fuzz: always returns list[str], never raises (AC19)."""
    rng = random.Random(1729)
    actions = [
        "read_logs",
        "query_metrics",
        "read_repo",
        "run_diagnostic_scripts",
        "deploy_prod",
        "delete_data",
        "modify_access_controls",
        "scale_service",
        "restart_service",
        "",
        "DEPLOY",
        "logs",
        "unknown_action",
    ]
    blasts = list(BlastRadius)
    for _ in range(2000):
        steps = [
            RunbookStep(
                id=f"s{i}",
                order=i,
                title="x",
                action=rng.choice(actions),
                blast_radius=rng.choice(blasts),
            )
            for i in range(rng.randint(0, 6))
        ]
        rb = Runbook(incident_id=uuid.uuid4(), steps=steps)
        result = assert_runbook_within_policy(rb, INCIDENT_DIRECTIVES)
        assert isinstance(result, list)
        assert all(isinstance(x, str) for x in result)
        # A forbidden or non-low-blast step must always be flagged.
        for step in steps:
            forbidden = step.action.strip().lower() in {
                "deploy_prod",
                "delete_data",
                "modify_access_controls",
                "deploy",
            }
            if forbidden or step.blast_radius is not BlastRadius.LOW:
                assert step.id in result
