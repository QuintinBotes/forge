"""The Red-Team Gate's decision under risk (#194 criterion 6).

A class that requires red-team is satisfied only by a scan the change survived:
``not_run`` (#174) never satisfies it, whatever the reason no adversary ran.
"""

from __future__ import annotations

import uuid

import pytest

from scf_contracts import GatedChange
from scf_contracts.testing import RecordingRiskReader
from scf_workflow.red_team_gate import (
    REDTEAM_BLOCKED,
    REDTEAM_NOT_RUN,
    REDTEAM_SURVIVED,
    red_team_passes,
)

CHANGE = GatedChange(workspace_id=uuid.uuid4(), commit_sha="c" * 40)


def test_enforcing_holds_a_change_no_adversary_attacked() -> None:
    """The class tightens it: the workspace turned the scan off, which passes today."""
    reader = RecordingRiskReader("enforcing")
    assert red_team_passes(REDTEAM_NOT_RUN, passes=True, risk=reader, change=CHANGE) is False
    assert reader.observed == [{"gate": "red_team", "actual": "pass", "risk_decision": "block"}]


def test_enforcing_keeps_a_blocked_change_blocked() -> None:
    """The gate was already stricter and stays so."""
    reader = RecordingRiskReader("enforcing")
    assert red_team_passes(REDTEAM_BLOCKED, passes=False, risk=reader, change=CHANGE) is False
    assert reader.observed == [{"gate": "red_team", "actual": "block", "risk_decision": "block"}]


def test_a_survived_scan_satisfies_the_class() -> None:
    reader = RecordingRiskReader("enforcing")
    assert red_team_passes(REDTEAM_SURVIVED, passes=True, risk=reader, change=CHANGE) is True


def test_shadow_passes_an_unattacked_change_and_records_the_disagreement() -> None:
    reader = RecordingRiskReader("shadow")
    assert red_team_passes(REDTEAM_NOT_RUN, passes=True, risk=reader, change=CHANGE) is True
    assert reader.observed == [{"gate": "red_team", "actual": "pass", "risk_decision": "block"}]


@pytest.mark.parametrize("verdict", [REDTEAM_NOT_RUN, REDTEAM_SURVIVED])
def test_a_class_that_does_not_require_red_team_changes_nothing(verdict: str) -> None:
    reader = RecordingRiskReader("enforcing", red_team=False)
    assert red_team_passes(verdict, passes=True, risk=reader, change=CHANGE) is True


def test_without_risk_the_gate_decides_alone() -> None:
    assert red_team_passes(REDTEAM_NOT_RUN, passes=True) is True
