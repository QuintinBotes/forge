"""Access to the packaged built-in skill profiles (plan Task 1.11)."""

from __future__ import annotations

from importlib import resources

from forge_contracts import SkillProfile
from forge_skill.loader import load_profiles

__all__ = ["BUILTIN_PROFILE_NAMES", "builtin_profiles"]

_BUILTIN_FILE = "builtin_profiles.yaml"

# The seven engineering-discipline profiles shipped with Forge (FORGE_SPEC.md).
BUILTIN_PROFILE_NAMES: tuple[str, ...] = (
    "backend-tdd",
    "backend-fast",
    "frontend-ui",
    "incident-response",
    "spec-analyst",
    "security-review",
    "chore-fast",
)


def builtin_profiles() -> dict[str, SkillProfile]:
    """Load a fresh copy of the built-in profiles from the packaged YAML.

    A new dict of new ``SkillProfile`` instances is returned on every call so a
    caller mutating a resolved profile can never corrupt another registry.
    """
    text = resources.files(__package__).joinpath(_BUILTIN_FILE).read_text(encoding="utf-8")
    return load_profiles(text)
