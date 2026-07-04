"""Workflow DSL parser: YAML -> validated :class:`WorkflowDefinition`.

The spec's DSL uses ``workflow:`` for the workflow name and ``from``/``to`` keys
on transitions; the contract DTO exposes ``name`` and the ``from``/``to``
aliases respectively, so parsing is a thin, validated mapping.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from forge_contracts import WorkflowDefinition
from forge_workflow.exceptions import WorkflowDefinitionError


def parse_definition(text: str) -> WorkflowDefinition:
    """Parse a YAML DSL string into a validated :class:`WorkflowDefinition`."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:  # pragma: no cover - defensive
        raise WorkflowDefinitionError(f"invalid workflow YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise WorkflowDefinitionError(
            f"workflow definition must be a mapping, got {type(raw).__name__}"
        )

    data: dict[str, Any] = dict(raw)
    # The DSL spells the name ``workflow:``; the DTO field is ``name``.
    if "name" not in data and "workflow" in data:
        data["name"] = data.pop("workflow")

    try:
        definition = WorkflowDefinition.model_validate(data)
    except ValidationError as exc:
        raise WorkflowDefinitionError(f"invalid workflow definition: {exc}") from exc

    # Build (and discard) the graph so structural problems surface at parse time.
    from forge_workflow.fsm import TransitionGraph

    TransitionGraph.from_definition(definition)
    return definition


def load_definition(source: str | Path) -> WorkflowDefinition:
    """Load a workflow definition from a path or a raw YAML string.

    A :class:`~pathlib.Path` is always read from disk. A ``str`` is treated as a
    filesystem path if it points at an existing file, otherwise as inline YAML.
    """
    if isinstance(source, Path):
        return parse_definition(source.read_text())

    try:
        candidate = Path(source)
        is_file = candidate.is_file()
    except OSError:
        is_file = False

    if is_file:
        return parse_definition(Path(source).read_text())
    return parse_definition(source)


__all__ = ["load_definition", "parse_definition"]
