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


class SpecReconcileWarning(UserWarning):
    """Emitted when ``spec.md`` and ``manifest.yaml`` diverge out-of-band.

    Both files are canonical serializations of the one ``SpecManifest``; the
    engine keeps them in lockstep on every write. If they are edited
    independently (e.g. by hand or by two tools) and no longer parse to the same
    manifest, the engine resolves the conflict by *last-write-wins* (the file
    with the newer mtime) and raises this warning so the divergence is visible
    rather than silently dropped.
    """


__all__ = ["SpecNotFoundError", "SpecReconcileWarning"]
