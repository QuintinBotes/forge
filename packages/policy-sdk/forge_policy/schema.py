"""Conditional-policy schema surface (F29).

The conditional models live in the frozen ``forge_contracts`` (the ``Policy``
model self-validates against ``POLICY_CONDITION_FIELDS`` / ``KNOWN_ACTIONS``), so
this module re-exports them under the ``forge_policy.schema`` name the slice
references and provides the committed-JSON-Schema drift guard (AC20).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from forge_contracts import (
    KNOWN_ACTIONS,
    POLICY_CONDITION_FIELDS,
    Condition,
    ConditionalMatch,
    ConditionalRule,
    ConditionGroup,
    ConditionOp,
    Decision,
    Policy,
    RuleEffect,
)

#: The committed JSON Schema regenerated from ``Policy.model_json_schema()``.
SCHEMA_PATH = Path(__file__).with_name("policy.schema.json")


def policy_json_schema() -> dict[str, Any]:
    """The canonical ``Policy`` JSON Schema (schema_version 2, incl. ``rules``)."""
    return Policy.model_json_schema()


def write_schema(path: Path | None = None) -> Path:
    """(Re)generate the committed ``policy.schema.json`` from the model."""
    target = path or SCHEMA_PATH
    target.write_text(
        json.dumps(policy_json_schema(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return target


__all__ = [
    "KNOWN_ACTIONS",
    "POLICY_CONDITION_FIELDS",
    "SCHEMA_PATH",
    "Condition",
    "ConditionGroup",
    "ConditionOp",
    "ConditionalMatch",
    "ConditionalRule",
    "Decision",
    "Policy",
    "RuleEffect",
    "policy_json_schema",
    "write_schema",
]
