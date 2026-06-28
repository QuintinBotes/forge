"""F26 §8: derived velocity state is *advisory*.

No workflow gate or agent decision may read the velocity/burndown service, so this
contract test asserts the workflow + agent packages never import
``forge_board.sprint_service`` / the ``SprintVelocity`` projection.
"""

from __future__ import annotations

import pathlib

import forge_agent
import forge_workflow

_FORBIDDEN = ("sprint_service", "SprintVelocity", "sprint_velocity", "SprintBurndownSnapshot")


def _sources(module) -> list[pathlib.Path]:
    root = pathlib.Path(module.__file__).parent
    return list(root.rglob("*.py"))


def test_workflow_and_agent_do_not_read_velocity_projection() -> None:
    offenders: list[str] = []
    for module in (forge_workflow, forge_agent):
        for path in _sources(module):
            text = path.read_text(encoding="utf-8")
            for token in _FORBIDDEN:
                if token in text:
                    offenders.append(f"{path}: {token}")
    assert not offenders, f"velocity projection is advisory; unexpected refs: {offenders}"
