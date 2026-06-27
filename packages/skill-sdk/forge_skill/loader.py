"""YAML loaders for Forge skill profiles (plan Task 1.11).

Skill profiles are plain YAML so community contributions need no code. A profile
document may be supplied three ways and all three are accepted everywhere:

* a filesystem ``Path`` (or path-like string) to a ``.yaml`` file,
* a raw YAML ``str``,
* an already-parsed ``Mapping``.

Two shapes are supported for a *collection* of profiles:

* the spec's wrapped form (a top-level ``skill_profiles:`` mapping), and
* a flat mapping of ``name -> profile body``.

In both collection shapes the mapping key becomes the profile's ``name`` (an
explicit ``name`` inside the body wins if present).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from forge_contracts import SkillProfile

__all__ = ["load_profile", "load_profiles"]

# Top-level key under which the spec nests its profile collection.
_WRAPPER_KEY = "skill_profiles"

# A source we accept for any loader entrypoint.
ProfileSource = str | Path | Mapping[str, Any]


def _read_yaml(source: ProfileSource) -> Any:
    """Resolve a profile source to parsed Python data.

    A ``str``/``Path`` that names an existing file is read from disk; any other
    string is treated as raw YAML content.
    """
    if isinstance(source, Mapping):
        return dict(source)
    if isinstance(source, str | Path):
        text: str
        try:
            candidate = Path(source)
            text = candidate.read_text(encoding="utf-8") if candidate.is_file() else str(source)
        except (OSError, ValueError):
            # e.g. embedded NULs or an over-long "path": treat as raw YAML.
            text = str(source)
        return yaml.safe_load(text)
    raise TypeError(f"unsupported profile source: {type(source)!r}")


def _coerce_profile(name: str, body: Any) -> SkillProfile:
    """Validate one ``name -> body`` entry into a ``SkillProfile``."""
    data: dict[str, Any] = dict(body or {})
    data.setdefault("name", name)
    return SkillProfile.model_validate(data)


def load_profiles(source: ProfileSource) -> dict[str, SkillProfile]:
    """Load a collection of skill profiles keyed by name.

    Accepts the wrapped (``skill_profiles:``) or flat mapping shape.
    """
    data = _read_yaml(source)
    if not isinstance(data, Mapping):
        raise ValueError("skill profile document must be a mapping of profiles")

    if _WRAPPER_KEY in data and isinstance(data[_WRAPPER_KEY], Mapping):
        data = data[_WRAPPER_KEY]

    profiles: dict[str, SkillProfile] = {}
    for name, body in data.items():
        profile = _coerce_profile(str(name), body)
        profiles[profile.name] = profile
    return profiles


def load_profile(source: ProfileSource) -> SkillProfile:
    """Load a single skill profile document (must carry its own ``name``)."""
    data = _read_yaml(source)
    if not isinstance(data, Mapping):
        raise ValueError("skill profile document must be a mapping")
    return SkillProfile.model_validate(dict(data))
