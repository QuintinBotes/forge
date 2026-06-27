"""Drift guard: every examples/automations/*.yaml parses + validates (AC20)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from forge_board.automation import AutomationRuleSpec, validate_rule
from forge_contracts.automation import AutomationTriggerType

EXAMPLES_DIR = Path(__file__).resolve().parents[3] / "examples" / "automations"
EXAMPLE_FILES = sorted(EXAMPLES_DIR.glob("*.yaml"))


def test_examples_dir_has_files() -> None:
    assert EXAMPLE_FILES, "expected community automation examples"


@pytest.mark.parametrize("path", EXAMPLE_FILES, ids=lambda p: p.name)
def test_example_parses_and_validates(path: Path) -> None:
    data = yaml.safe_load(path.read_text())
    spec = AutomationRuleSpec.model_validate(data)
    # Same validator the API uses (no project graph -> reference checks skipped).
    validate_rule(spec)


def test_canonical_example_matches_ac4() -> None:
    data = yaml.safe_load((EXAMPLES_DIR / "close-spec-tasks-on-merge.yaml").read_text())
    spec = AutomationRuleSpec.model_validate(data)
    assert spec.trigger.type is AutomationTriggerType.WORKFLOW_STATE_CHANGED
    assert spec.trigger.config["to_state"] == "merged"
    assert spec.actions[0].type.value == "close_linked_spec_tasks"
    assert spec.actions[0].exclude_trigger_task is True
