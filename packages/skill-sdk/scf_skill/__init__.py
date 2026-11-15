"""Skill-profile loader, registry, and agent behavior injection.

Public surface (plan Task 1.11):

* :class:`SkillProfileRegistry` — implements the frozen ``SkillProfileRegistry``
  Protocol (``get`` / ``inject``); pre-loaded with the built-in profiles.
* :func:`builtin_profiles` / :data:`BUILTIN_PROFILE_NAMES` — the seven shipped
  engineering-discipline profiles.
* :func:`load_profiles` / :func:`load_profile` — plain-YAML loaders.
* :func:`inject_profile` — fold a profile's behaviour into an ``AgentObjective``.
"""

from __future__ import annotations

from scf_skill.builtins import BUILTIN_PROFILE_NAMES, builtin_profiles
from scf_skill.directives import (
    ACTION_ALIASES,
    KNOWN_ACTIONS,
    PermitDecision,
    SkillDirectives,
    blast_within,
    normalize_action,
    skill_permits_action,
    to_directives,
)
from scf_skill.injection import inject_profile
from scf_skill.loader import load_profile, load_profiles
from scf_skill.registry import SkillProfileRegistry

__version__ = "0.1.0"

__all__ = [
    "ACTION_ALIASES",
    "BUILTIN_PROFILE_NAMES",
    "KNOWN_ACTIONS",
    "PermitDecision",
    "SkillDirectives",
    "SkillProfileRegistry",
    "__version__",
    "blast_within",
    "builtin_profiles",
    "inject_profile",
    "load_profile",
    "load_profiles",
    "normalize_action",
    "skill_permits_action",
    "to_directives",
]
