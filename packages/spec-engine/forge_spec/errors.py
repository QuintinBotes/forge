"""Spec-engine-local exceptions.

``SpecGateError`` (the gate violation type) lives in ``forge_contracts`` and is
re-exported from the package root; lookup failures use the package-local
:class:`SpecNotFoundError`, which subclasses the shared ``ForgeError`` base so
callers can still catch the whole Forge error family.
"""

from __future__ import annotations

from forge_contracts import ForgeError


class SpecNotFoundError(ForgeError, KeyError):
    """Raised when a spec or task uuid does not resolve to an on-disk spec."""


__all__ = ["SpecNotFoundError"]
