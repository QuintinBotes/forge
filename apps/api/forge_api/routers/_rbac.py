"""Authorization dependencies for the feature routers (Phase-2 bug fix r2).

The feature routers authenticate via ``get_current_principal`` but historically
performed **no authorization** — any authenticated caller (including a read-only
``viewer`` or the ``agent-runner`` identity) could drive writes, runs, and the
human approval gate. This module closes that gap by exposing a
:func:`require_permission` factory that the routers attach per-route.

It deliberately builds on :func:`forge_api.deps.get_current_principal` (the same
dependency the routers and the test-suite override) rather than the auth
service's own ``require_permission`` (which keys off a *different* dependency),
so the RBAC gate composes cleanly with the existing authentication wiring and
with ``app.dependency_overrides`` in tests.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, HTTPException, status

from forge_api.auth.rbac import Permission, PermissionDeniedError, ensure
from forge_api.deps import Principal, get_current_principal

# Annotated alias so ``Depends`` lives in the type, not in an argument default
# (the latter trips ruff's B008); matches ``forge_api.auth.service`` style.
_AuthedPrincipal = Annotated[Principal, Depends(get_current_principal)]


def require_permission(permission: Permission) -> Callable[[Principal], Principal]:
    """Build a dependency that authenticates then enforces ``permission``.

    Yields the authenticated :class:`Principal` (so handlers can scope by
    ``workspace_id`` / capture the decider identity), raising HTTP 403 when the
    caller's role lacks ``permission``. Authentication (401) is handled upstream
    by :func:`get_current_principal`.
    """

    def _dependency(principal: _AuthedPrincipal) -> Principal:
        try:
            ensure(principal.role, permission)
        except PermissionDeniedError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
        return principal

    return _dependency


__all__ = ["require_permission"]
