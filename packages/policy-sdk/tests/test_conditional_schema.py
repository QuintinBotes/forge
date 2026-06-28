"""F29 — conditional schema validation + JSON-Schema drift guard (ACs 2, 3, 20)."""

from __future__ import annotations

import json

import pydantic
import pytest

from forge_contracts import (
    Condition,
    ConditionalRule,
    ConditionGroup,
    ConditionOp,
    Decision,
    Policy,
    RuleEffect,
)
from forge_policy.schema import SCHEMA_PATH, policy_json_schema


def _rule(rule_id: str = "r1", **kw: object) -> ConditionalRule:
    kw.setdefault("effect", RuleEffect.DENY)
    kw.setdefault("reason", "because")
    return ConditionalRule(id=rule_id, **kw)  # type: ignore[arg-type]


# AC2 — rules require schema_version >= 2.
def test_rules_require_v2() -> None:
    with pytest.raises((ValueError, pydantic.ValidationError), match="schema_version"):
        Policy(repo_id="r", rules=[_rule()])
    # Same rules at v2 validate.
    Policy(repo_id="r", schema_version=2, rules=[_rule()])


# AC3 — duplicate ids / unknown field / unknown applies_to all fail.
def test_duplicate_rule_id_rejected() -> None:
    with pytest.raises((ValueError, pydantic.ValidationError), match="duplicate"):
        Policy(repo_id="r", schema_version=2, rules=[_rule("dup"), _rule("dup")])


def test_unknown_condition_field_rejected() -> None:
    bad = _rule(
        when=ConditionGroup(conditions=[Condition(field="nope", op=ConditionOp.EQ, value=1)])
    )
    with pytest.raises((ValueError, pydantic.ValidationError), match="condition field"):
        Policy(repo_id="r", schema_version=2, rules=[bad])


def test_unknown_applies_to_rejected() -> None:
    with pytest.raises((ValueError, pydantic.ValidationError), match="applies_to"):
        Policy(repo_id="r", schema_version=2, rules=[_rule(applies_to=["frobnicate"])])


def test_star_applies_to_is_allowed() -> None:
    Policy(repo_id="r", schema_version=2, rules=[_rule(applies_to=["*"])])


# AC1 (shape) — the additive Decision fields default empty.
def test_decision_additive_fields_default_empty() -> None:
    d = Decision()
    assert d.conditional_matches == []
    assert d.base_effect is None
    assert d.severity == "info"


# AC20 — the committed JSON schema matches the model (v2 drift guard).
def test_json_schema_v2_matches_committed() -> None:
    assert SCHEMA_PATH.is_file(), "policy.schema.json must be committed"
    committed = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert committed == policy_json_schema(), (
        "policy.schema.json is stale — regenerate with forge_policy.schema.write_schema()"
    )
    # The schema must describe the new conditional surface.
    assert "rules" in committed["properties"]
    assert "schema_version" in committed["properties"]
