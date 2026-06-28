"""DSL drift guard (AC23) — bundled vs example parse equal; names registered."""

from __future__ import annotations

from pathlib import Path

from forge_deploy.effects import KNOWN_EFFECTS
from forge_deploy.engine import load_deployment_definition
from forge_deploy.guards import default_guard_registry

_REPO_ROOT = Path(__file__).resolve().parents[3]
_BUNDLED = (
    Path(__file__).resolve().parents[1]
    / "forge_deploy"
    / "definitions"
    / "deployment_promotion.yaml"
)
_EXAMPLE = _REPO_ROOT / "examples" / "deployments" / "deployment_promotion.yaml"


def test_bundled_equals_example() -> None:
    bundled = load_deployment_definition(_BUNDLED)
    example = load_deployment_definition(_EXAMPLE)
    assert bundled.model_dump() == example.model_dump()


def test_every_guard_and_effect_registered() -> None:
    definition = load_deployment_definition()
    guards = default_guard_registry()
    for rule in definition.transitions:
        for guard in rule.guards:
            assert guard in guards, f"unregistered guard {guard!r}"
        for effect in rule.effects:
            assert effect in KNOWN_EFFECTS, f"unknown effect {effect!r}"


def test_cancel_edges_injected_for_non_terminal_states() -> None:
    definition = load_deployment_definition()
    cancel_from = {r.from_state for r in definition.transitions if r.event == "cancel"}
    assert {"requested", "gate_evaluating", "awaiting_approval", "approved"} <= cancel_from
    # Terminal states get no cancel edge.
    assert "succeeded" not in cancel_from
    assert "cancelled" not in cancel_from
