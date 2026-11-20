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

import logging
from collections import Counter
from collections.abc import Callable
from enum import StrEnum
from typing import Literal, Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

logger = logging.getLogger(__name__)

#: ``shadow`` records what risk would decide; ``enforcing`` lets it tighten gates.
RiskEnforcement = Literal["shadow", "enforcing"]
#: How a class wants deploys to run (consumed by milestone M10, recorded here).
RiskDeployMode = Literal["auto", "gated"]
#: The five shipping gates risk may tighten (spec A §5.4).
RiskGate = Literal["deploy", "merge", "approval", "red_team", "self_eval"]


class RiskClass(StrEnum):
    """How dangerous a change is, ordered ``minimal < low < high < critical``."""

    MINIMAL = "minimal"
    LOW = "low"
    HIGH = "high"
    CRITICAL = "critical"

    # str's own ordering is alphabetical, so compare by declaration order instead.
    def _rank(self) -> int:
        return list(RiskClass).index(self)

    @staticmethod
    def _rank_of(other: object) -> int | None:
        """A plain str (JSON, a database column) is read as a class; an unknown name raises."""
        return RiskClass(other)._rank() if isinstance(other, str) else None

    def __lt__(self, other: object) -> bool:
        rank = self._rank_of(other)
        return NotImplemented if rank is None else self._rank() < rank

    def __le__(self, other: object) -> bool:
        rank = self._rank_of(other)
        return NotImplemented if rank is None else self._rank() <= rank

    def __gt__(self, other: object) -> bool:
        rank = self._rank_of(other)
        return NotImplemented if rank is None else self._rank() > rank

    def __ge__(self, other: object) -> bool:
        rank = self._rank_of(other)
        return NotImplemented if rank is None else self._rank() >= rank


class _Model(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class _Strict(_Model):
    """Policy-authored blocks: a typo must not silently drop a rule."""

    model_config = ConfigDict(extra="forbid")


class RiskRule(_Strict):
    """One ``derive`` entry: a change touching any of ``paths`` is at least ``class``."""

    #: Optional; an unnamed rule is reported as ``derive[<index>]``.
    id: str | None = Field(default=None, min_length=1)
    paths: list[str] = Field(min_length=1)
    risk_class: RiskClass = Field(alias="class")

    @field_validator("paths")
    @classmethod
    def _matchable_paths(cls, paths: list[str]) -> list[str]:
        """Reject globs the matcher can never match, so a rule cannot silently do nothing.

        Repository paths are relative with no trailing ``/``. Rejected rather than
        rewritten, so risk rules keep matching exactly as conditional policy rules do.
        """
        for path in paths:
            if not path.strip():
                raise ValueError("risk rule path is blank")
            fixed = path.removeprefix("./").lstrip("/")
            if fixed.endswith("/"):
                fixed += "**"
            if fixed != path:
                raise ValueError(
                    f"risk rule path {path!r} can never match a repository path; use {fixed!r}"
                )
        return paths


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

    def rule_ids(self) -> list[str]:
        """Each ``derive`` rule's id, or ``derive[<index>]`` when the policy gave none."""
        return [rule.id or f"derive[{index}]" for index, rule in enumerate(self.derive)]

    @model_validator(mode="after")
    def _unique_rule_ids(self) -> RiskRules:
        counts = Counter(self.rule_ids())  # linear: a policy may carry many rules
        duplicates = sorted(rule_id for rule_id, count in counts.items() if count > 1)
        if duplicates:
            raise ValueError(f"duplicate risk rule ids {duplicates}")
        return self


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

    model_config = ConfigDict(str_strip_whitespace=True, frozen=True)

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

    @model_validator(mode="after")
    def _lowered_only_by_a_named_override(self) -> RiskEvaluation:
        named = bool(
            (self.override_approver or "").strip() and (self.override_reason or "").strip()
        )
        if self.final_class < self.derived_class and not named:
            raise ValueError(
                f"final class {self.final_class.value!r} is below the derived class "
                f"{self.derived_class.value!r} without an override approver and reason"
            )
        return self


class GateRequirements(_Model):
    """What a risk class demands of each shipping gate."""

    risk_class: RiskClass
    approvals: int
    deploy_mode: RiskDeployMode
    red_team: bool
    self_eval: bool


class GatedChange(_Model):
    """The change a shipping gate is deciding, by whatever identity the gate has (#194).

    A reader looks for its classification by the most specific identity given: the
    commit, then the agent run, then the workflow run.
    """

    workspace_id: UUID
    commit_sha: str | None = None
    agent_run_id: UUID | None = None
    workflow_run_id: UUID | None = None


class ClassifiedChange(_Model):
    """A change's stored classification as the gates see it."""

    classification_id: UUID
    workspace_id: UUID
    #: Read from the repository policy in force when the change was classified.
    enforcement: RiskEnforcement
    requirements: GateRequirements


@runtime_checkable
class RiskReader(Protocol):
    """What a shipping gate asks of risk; the gates never import ``scf_risk``."""

    def risk_for(self, change: GatedChange) -> ClassifiedChange | None:
        """The change's classification, or ``None`` when it has none."""
        ...

    def observe(
        self, risk: ClassifiedChange, *, gate: RiskGate, actual: str, risk_decision: str
    ) -> None:
        """Record a gate's own decision beside what the class requires of it."""
        ...


def tighten_approvals(existing: int, required: int) -> int:
    """An approval count: never fewer than either side asks for."""
    return max(existing, required)


def tighten_deploy(existing: RiskDeployMode, required: RiskDeployMode) -> RiskDeployMode:
    """A deploy mode: gated when either side gates it."""
    return "gated" if "gated" in (existing, required) else "auto"


def tighten_check(passes: bool, *, required: bool, satisfied: bool) -> bool:
    """A pass/block check: a required control that was not satisfied blocks; nothing unblocks."""
    return passes and (satisfied or not required)


def _label(decision: object) -> str:
    if isinstance(decision, bool):
        return "pass" if decision else "block"
    return str(decision)


def gate_decision[D](
    risk: RiskReader | None,
    change: GatedChange | None,
    gate: RiskGate,
    own: D,
    tighten: Callable[[D, GateRequirements], D],
) -> D:
    """A shipping gate's decision under risk (spec A §4.1, §5.4, §8).

    The gate decides ``own`` exactly as without risk. For a classified change the
    reader records ``own`` beside ``tighten(own, requirements)``; in shadow mode the
    gate returns ``own``, when enforcing the tightened decision. A reader that fails
    is logged and changes nothing; an observation that fails leaves the decision to
    the mode.
    """
    if risk is None or change is None:
        return own
    try:
        found = risk.risk_for(change)
        if found is None:
            return own
        tightened = tighten(own, found.requirements)
    except Exception:
        logger.exception("risk for the %s gate is unavailable; the gate decides alone", gate)
        return own
    try:
        risk.observe(found, gate=gate, actual=_label(own), risk_decision=_label(tightened))
    except Exception:
        logger.exception("could not record the %s gate's risk observation", gate)
    return tightened if found.enforcement == "enforcing" else own


__all__ = [
    "ChangeSignals",
    "ClassifiedChange",
    "DerivedRisk",
    "GateRequirements",
    "GatedChange",
    "RiskClass",
    "RiskClassRequirements",
    "RiskDeployMode",
    "RiskEnforcement",
    "RiskEvaluation",
    "RiskGate",
    "RiskOverride",
    "RiskReader",
    "RiskRule",
    "RiskRules",
    "gate_decision",
    "tighten_approvals",
    "tighten_check",
    "tighten_deploy",
]
