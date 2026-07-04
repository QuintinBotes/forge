"""Shared helpers for Phase-0 router stubs.

Every feature route is pre-registered (plan Task 0.4) but not yet implemented:
it declares its eventual response model (for the OpenAPI contract Phase 1 fills)
and returns a typed :class:`NotImplementedResponse` with HTTP 501.

Phase-1 tasks edit only their own ``routers/<name>.py`` file: they replace the
``return not_implemented(...)`` body with a real handler and swap
``response_model`` for the eventual DTO. ``main.py`` is never touched.
"""

from __future__ import annotations

from typing import Any

from fastapi import status
from pydantic import BaseModel

#: HTTP status used by every unimplemented stub route.
STUB_STATUS_CODE = status.HTTP_501_NOT_IMPLEMENTED


class NotImplementedResponse(BaseModel):
    """The declared schema shape every stub route returns until Phase 1.

    ``status`` is a constant discriminator so clients (and tests) can detect a
    not-yet-implemented endpoint without string-matching the detail message.
    """

    status: str = "not_implemented"
    detail: str
    router: str
    operation: str


def not_implemented(router: str, operation: str) -> NotImplementedResponse:
    """Build the standard 501 payload for ``<router>.<operation>``."""
    return NotImplementedResponse(
        detail=(
            f"'{router}.{operation}' is registered but not implemented yet; "
            "it is filled in during Phase 1."
        ),
        router=router,
        operation=operation,
    )


def eventual(model: type[BaseModel], description: str) -> dict[int | str, dict[str, Any]]:
    """OpenAPI ``responses`` entry documenting a route's eventual 200 shape.

    Lets Phase-1 implementers (and API consumers) see the target DTO while the
    stub still returns :class:`NotImplementedResponse` at runtime.
    """
    return {200: {"model": model, "description": description}}
