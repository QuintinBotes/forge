"""Skill *directives* — the structural permission projection of a skill profile.

The slice spec references an F11 ``SkillDirectives`` / ``skill_permits_action``
/ ``ACTION_ALIASES`` surface that was never built in the foundation; this module
supplies it as a thin, pure projection over the existing
:class:`forge_contracts.SkillProfile`. It is what the incident path uses to
enforce least-privilege ("read-only by default") structurally rather than via
prompts: the runtime executes a tool *iff* ``skill_permits_action(...).allowed``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from forge_contracts import SkillProfile
from forge_contracts.incident import BlastRadius, blast_rank

__all__ = [
    "ACTION_ALIASES",
    "KNOWN_ACTIONS",
    "PermitDecision",
    "SkillDirectives",
    "normalize_action",
    "skill_permits_action",
    "to_directives",
]

#: Canonical semantic actions a runbook step / tool call may name.
KNOWN_ACTIONS: frozenset[str] = frozenset(
    {
        "read_logs",
        "query_metrics",
        "read_repo",
        "read_knowledge",
        "run_diagnostic_scripts",
        "query_mcp",
        "write_spec",
        "deploy_prod",
        "delete_data",
        "modify_access_controls",
        "restart_service",
        "scale_service",
        "rollback_deploy",
        "rotate_credentials",
    }
)

#: Aliases mapping common tool names onto canonical actions.
ACTION_ALIASES: dict[str, str] = {
    "logs": "read_logs",
    "read_log": "read_logs",
    "get_logs": "read_logs",
    "metrics": "query_metrics",
    "query_metric": "query_metrics",
    "read_metrics": "query_metrics",
    "repo": "read_repo",
    "read_code": "read_repo",
    "diagnostic": "run_diagnostic_scripts",
    "run_diagnostics": "run_diagnostic_scripts",
    "mcp_query": "query_mcp",
    "deploy": "deploy_prod",
    "deploy_production": "deploy_prod",
    "delete": "delete_data",
    "drop_data": "delete_data",
    "modify_acl": "modify_access_controls",
    "change_access": "modify_access_controls",
}


def normalize_action(action: str) -> str:
    """Resolve an action name (or alias) to its canonical form."""
    key = (action or "").strip().lower()
    return ACTION_ALIASES.get(key, key)


@dataclass(frozen=True)
class SkillDirectives:
    """The enforceable permission envelope projected from a skill profile."""

    name: str
    approval_before_action: bool = False
    max_blast_radius: BlastRadius | None = None
    allowed_actions: frozenset[str] = field(default_factory=frozenset)
    forbidden_actions: frozenset[str] = field(default_factory=frozenset)
    #: Whether the profile mandates a review gate (drives F27 MAKER_CHECKER
    #: pattern selection). Projected from ``SkillProfile.review_required``.
    review_required: bool = False


@dataclass(frozen=True)
class PermitDecision:
    """The outcome of evaluating an action against skill directives."""

    allowed: bool
    reason: str
    requires_approval: bool = False
    severity: str = "info"


def to_directives(profile: SkillProfile) -> SkillDirectives:
    """Project a :class:`SkillProfile` into its enforceable directives."""
    raw_blast = profile.max_blast_radius
    blast: BlastRadius | None = None
    if raw_blast is not None:
        try:
            blast = BlastRadius(str(raw_blast).strip().lower())
        except ValueError:
            blast = None
    return SkillDirectives(
        name=profile.name,
        approval_before_action=profile.requires_human_approval_before_action,
        max_blast_radius=blast,
        allowed_actions=frozenset(normalize_action(a) for a in profile.allowed_actions),
        forbidden_actions=frozenset(normalize_action(a) for a in profile.forbidden_actions),
        review_required=profile.review_required,
    )


def skill_permits_action(directives: SkillDirectives, action: str) -> PermitDecision:
    """Decide whether ``action`` is permitted under ``directives``.

    A forbidden action is denied with ``requires_approval=True`` and
    ``severity="critical"`` (it can never be silently allowed). An allowlist miss
    (a non-empty allowlist that does not cover the action) is denied — the agent
    can never widen its own scope. Otherwise the action is allowed.
    """
    canonical = normalize_action(action)
    if canonical in directives.forbidden_actions:
        return PermitDecision(
            allowed=False,
            reason=f"action {canonical!r} is forbidden by skill {directives.name!r}",
            requires_approval=True,
            severity="critical",
        )
    if directives.allowed_actions and canonical not in directives.allowed_actions:
        return PermitDecision(
            allowed=False,
            reason=(f"action {canonical!r} is not in the allowlist for skill {directives.name!r}"),
            requires_approval=False,
            severity="high",
        )
    return PermitDecision(allowed=True, reason="permitted")


def blast_within(value: str | BlastRadius, cap: BlastRadius | None) -> bool:
    """True when ``value``'s blast radius does not exceed ``cap`` (None == no cap)."""
    if cap is None:
        return True
    return blast_rank(value) <= blast_rank(cap)
