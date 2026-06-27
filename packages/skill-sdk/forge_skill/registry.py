"""Skill-profile registry implementing the frozen ``SkillProfileRegistry`` Protocol.

Resolves profiles by name (built-ins by default, plus any registered overrides)
and injects a profile's behaviour into an ``AgentObjective`` (plan Task 1.11).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from forge_contracts import AgentObjective, SkillProfile
from forge_contracts.exceptions import UnknownSkillProfileError
from forge_skill.builtins import builtin_profiles
from forge_skill.injection import inject_profile
from forge_skill.loader import ProfileSource, load_profiles

__all__ = ["SkillProfileRegistry"]

# Accept profiles seeded either as a name->profile mapping or a bare iterable.
ProfileSeed = Mapping[str, SkillProfile] | Iterable[SkillProfile]


class SkillProfileRegistry:
    """Concrete implementation of the ``SkillProfileRegistry`` Protocol.

    By default the registry is pre-loaded with the seven built-in profiles;
    pass ``include_builtins=False`` for a registry seeded only with the profiles
    you provide. Later registrations override earlier ones by name, so a custom
    profile can shadow a built-in.
    """

    def __init__(
        self,
        profiles: ProfileSeed | None = None,
        *,
        include_builtins: bool = True,
    ) -> None:
        self._profiles: dict[str, SkillProfile] = {}
        if include_builtins:
            for profile in builtin_profiles().values():
                self.register(profile)
        if profiles is not None:
            seed = profiles.values() if isinstance(profiles, Mapping) else profiles
            for profile in seed:
                self.register(profile)

    # -- construction helpers --------------------------------------------- #

    @classmethod
    def from_yaml(
        cls, source: ProfileSource, *, include_builtins: bool = True
    ) -> SkillProfileRegistry:
        """Build a registry from a YAML profile collection (file/str/mapping)."""
        return cls(load_profiles(source), include_builtins=include_builtins)

    # -- mutation --------------------------------------------------------- #

    def register(self, profile: SkillProfile) -> None:
        """Add or replace a profile (keyed by ``profile.name``)."""
        self._profiles[profile.name] = profile

    # -- Protocol surface ------------------------------------------------- #

    def get(self, name: str) -> SkillProfile:
        """Resolve a profile by name; raise ``UnknownSkillProfileError`` if absent."""
        try:
            return self._profiles[name]
        except KeyError:
            known = ", ".join(sorted(self._profiles)) or "<none>"
            raise UnknownSkillProfileError(
                f"unknown skill profile {name!r}; known profiles: {known}"
            ) from None

    def inject(self, profile: SkillProfile, context: AgentObjective) -> AgentObjective:
        """Fold ``profile``'s behaviour into a copy of ``context``."""
        return inject_profile(profile, context)

    # -- introspection ---------------------------------------------------- #

    def names(self) -> list[str]:
        """Sorted list of registered profile names."""
        return sorted(self._profiles)

    def __contains__(self, name: object) -> bool:
        return name in self._profiles

    def __len__(self) -> int:
        return len(self._profiles)
