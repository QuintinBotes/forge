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

from forge_skill.builtins import BUILTIN_PROFILE_NAMES, builtin_profiles
from forge_skill.injection import inject_profile
from forge_skill.loader import load_profile, load_profiles
from forge_skill.registry import SkillProfileRegistry

__version__ = "0.1.0"

__all__ = [
    "BUILTIN_PROFILE_NAMES",
    "SkillProfileRegistry",
    "__version__",
    "builtin_profiles",
    "inject_profile",
    "load_profile",
    "load_profiles",
]
