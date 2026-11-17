"""The optional ``risk_rules`` policy block: loading, rejection, schema (#192, spec A §3.2, §8)."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pydantic
import pytest

from scf_contracts import Policy, RiskClass
from scf_policy import load_policy
from scf_policy.schema import SCHEMA_PATH

REPO_ROOT = Path(__file__).resolve().parents[3]
COMMITTED_POLICIES = sorted(
    p
    for p in [
        *(REPO_ROOT / "examples" / "policies").rglob("*.yaml"),
        *(Path(__file__).parent / "fixtures").glob("*.yaml"),
    ]
    if not p.name.endswith(".tests.yaml")
)

RISK_BLOCK = """
risk_rules:
  enforcement: shadow
  default: low
  fallback: critical
  derive:
    - paths: ["docs/**", "**/*.md"]
      class: minimal
    - id: billing
      paths: ["app/billing/**"]
      class: critical
  classes:
    minimal:  { approvals: 0, deploy: auto,  red_team: false, self_eval: false }
    critical: { approvals: 2, deploy: gated, red_team: true,  self_eval: true  }
"""


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "policy.yaml"
    path.write_text("repo_id: github.com/org/api\n" + textwrap.dedent(body), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# AC1 — every policy that loads today is unchanged                              #
# --------------------------------------------------------------------------- #


def test_committed_policies_are_found() -> None:
    assert len(COMMITTED_POLICIES) >= 9


@pytest.mark.parametrize("policy_path", COMMITTED_POLICIES, ids=lambda p: p.name)
def test_a_policy_without_risk_rules_loads_unchanged(policy_path: Path) -> None:
    policy = load_policy(policy_path)

    assert policy.risk_rules is None
    assert "risk_rules" not in policy.model_fields_set
    assert Policy.model_validate(policy.model_dump(exclude={"risk_rules"})) == policy


def test_risk_rules_need_no_schema_version_bump(tmp_path: Path) -> None:
    policy = load_policy(_write(tmp_path, RISK_BLOCK))

    assert policy.schema_version == 1
    assert policy.risk_rules is not None


# --------------------------------------------------------------------------- #
# AC7 — a valid block loads, an invalid one is rejected at load time            #
# --------------------------------------------------------------------------- #


def test_a_valid_risk_block_loads(tmp_path: Path) -> None:
    rules = load_policy(_write(tmp_path, RISK_BLOCK)).risk_rules

    assert rules is not None
    assert rules.enforcement == "shadow"
    assert rules.default is RiskClass.LOW
    assert rules.fallback is RiskClass.CRITICAL
    assert [r.risk_class for r in rules.derive] == [RiskClass.MINIMAL, RiskClass.CRITICAL]
    assert rules.derive[1].id == "billing"
    assert rules.classes[RiskClass.CRITICAL].red_team is True


def test_enforcement_defaults_to_shadow_and_fallback_to_critical(tmp_path: Path) -> None:
    rules = load_policy(_write(tmp_path, "risk_rules:\n  default: low\n")).risk_rules

    assert rules is not None
    assert rules.enforcement == "shadow"
    assert rules.fallback is RiskClass.CRITICAL


@pytest.mark.parametrize(
    ("body", "complaint"),
    [
        ("risk_rules:\n  default: severe\n", "default"),
        ("risk_rules:\n  default: low\n  fallback: severe\n", "fallback"),
        (
            "risk_rules:\n  default: low\n  derive:\n    - paths: [a/**]\n      class: severe\n",
            "class",
        ),
        (
            "risk_rules:\n  default: low\n  classes:\n    severe: { approvals: 1 }\n",
            "classes",
        ),
        ("risk_rules:\n  default: low\n  enforcement: audit\n", "enforcement"),
        ("risk_rules:\n  default: low\n  derive:\n    - class: high\n", "paths"),
        ("risk_rules:\n  default: low\n  derive:\n    - paths: []\n      class: high\n", "paths"),
        ("risk_rules:\n  enforcement: shadow\n", "default"),
        ("risk_rules:\n  default: low\n  derve: []\n", "derve"),
        (
            "risk_rules:\n  default: low\n  classes:\n    low: { deploy: canary }\n",
            "deploy",
        ),
        (
            "risk_rules:\n  default: low\n  classes:\n    low: { approvals: -1 }\n",
            "approvals",
        ),
    ],
)
def test_an_invalid_risk_block_is_rejected_at_load(
    tmp_path: Path, body: str, complaint: str
) -> None:
    with pytest.raises(pydantic.ValidationError, match=complaint):
        load_policy(_write(tmp_path, body))


# --------------------------------------------------------------------------- #
# AC7 — the committed JSON Schema describes the block                           #
# --------------------------------------------------------------------------- #


def test_the_committed_schema_describes_risk_rules() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    defs = schema["$defs"]

    assert "risk_rules" in schema["properties"]
    assert set(defs["RiskRules"]["properties"]) == {
        "enforcement",
        "default",
        "fallback",
        "derive",
        "classes",
    }
    assert set(defs["RiskClass"]["enum"]) == {"minimal", "low", "high", "critical"}
    assert set(defs["RiskRule"]["properties"]) == {"id", "paths", "class"}
