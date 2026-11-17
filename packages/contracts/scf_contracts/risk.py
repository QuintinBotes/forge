"""Risk classification contracts (spec A §3, §5.2; ``specs/004-risk-classification``).

A change's risk class is *derived* from facts about its diff and the policy's
``risk_rules`` block. A declared class may raise it; lowering it needs a named
approver and a reason. ``scf_risk`` computes; these models only carry data, so
any package can read a classification without depending on ``scf_risk``.

The rule models validate on construction, so a policy whose ``risk_rules`` names
an unknown class or enforcement mode, or a rule without paths, is rejected when
the policy is loaded, never when a change is classified.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: ``shadow`` records what risk would decide; ``enforcing`` lets it tighten gates.
RiskEnforcement = Literal["shadow", "enforcing"]
#: How a class wants deploys to run (consumed by milestone M10, recorded here).
RiskDeployMode = Literal["auto", "gated"]


class RiskClass(StrEnum):
    """How dangerous a change is, ordered ``minimal < low < high < critical``."""

    MINIMAL = "minimal"
    LOW = "low"
    HIGH = "high"
    CRITICAL = "critical"

    # str's own ordering is alphabetical, so compare by declaration order instead.
    def _rank(self) -> int:
        return list(RiskClass).index(self)

    def __lt__(self, other: object) -> bool:
        return self._rank() < other._rank() if isinstance(other, RiskClass) else NotImplemented

    def __le__(self, other: object) -> bool:
        return self._rank() <= other._rank() if isinstance(other, RiskClass) else NotImplemented

    def __gt__(self, other: object) -> bool:
        return self._rank() > other._rank() if isinstance(other, RiskClass) else NotImplemented

    def __ge__(self, other: object) -> bool:
        return self._rank() >= other._rank() if isinstance(other, RiskClass) else NotImplemented


class _Model(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class _Strict(_Model):
    """Policy-authored blocks: a typo must not silently drop a rule."""

    model_config = ConfigDict(extra="forbid")


class RiskRule(_Strict):
    """One ``derive`` entry: a change touching any of ``paths`` is at least ``class``."""

    #: Optional; an unnamed rule is reported as ``derive[<index>]``.
    id: str | None = None
    paths: list[str] = Field(min_length=1)
    risk_class: RiskClass = Field(alias="class")


class RiskClassRequirements(_Strict):
    """What the policy demands of the shipping gates for one class."""

    approvals: int = Field(default=0, ge=0)
    deploy: RiskDeployMode = "auto"
    red_team: bool = False
    self_eval: bool = False


class RiskRules(_Strict):
    """The ``risk_rules`` block of the repository policy."""

    enforcement: RiskEnforcement = "shadow"
    default: RiskClass
    #: Applied when the diff facts are unavailable; fails closed by default.
    fallback: RiskClass = RiskClass.CRITICAL
    derive: list[RiskRule] = Field(default_factory=list)
    classes: dict[RiskClass, RiskClassRequirements] = Field(default_factory=dict)


class ChangeSignals(_Model):
    """Facts read from a diff. Deliberately nothing about who wrote it (spec FR-008)."""

    paths: list[str] = Field(default_factory=list)
    migration_present: bool = False
    touches_contracts: bool = False
    lines_added: int = 0
    lines_removed: int = 0
    repo_count: int = 1
    #: False when the diff could not be read; the classifier then applies the fallback.
    available: bool = True
    unavailable_reason: str | None = None


class DerivedRisk(_Model):
    """The floor a change's diff puts under its risk class."""

    risk_class: RiskClass
    #: Ids of the matching ``derive`` rules, in policy order.
    matched_rule_ids: list[str] = Field(default_factory=list)
    fallback_applied: bool = False
    reason: str | None = None


class RiskOverride(_Model):
    """A named person's permission to declare a class below the derived floor."""

    model_config = ConfigDict(str_strip_whitespace=True)

    approver: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class RiskEvaluation(_Model):
    """The class a change ships under, and how it got there."""

    derived_class: RiskClass
    declared_class: RiskClass | None = None
    final_class: RiskClass
    matched_rule_ids: list[str] = Field(default_factory=list)
    fallback_applied: bool = False
    override_approver: str | None = None
    override_reason: str | None = None


class GateRequirements(_Model):
    """What a risk class demands of each shipping gate."""

    risk_class: RiskClass
    approvals: int
    deploy_mode: RiskDeployMode
    red_team: bool
    self_eval: bool


__all__ = [
    "ChangeSignals",
    "DerivedRisk",
    "GateRequirements",
    "RiskClass",
    "RiskClassRequirements",
    "RiskDeployMode",
    "RiskEnforcement",
    "RiskEvaluation",
    "RiskOverride",
    "RiskRule",
    "RiskRules",
]
