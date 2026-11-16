"""F23 composition root: the traceability-projection repository seam.

:func:`build_projection_repository` selects the ``ProjectionRepository`` backend
via ``FORGE_PROJECTION_BACKEND`` (default ``memory``), exactly like the sibling
``FORGE_BOARD_BACKEND`` / ``FORGE_APPROVAL_BACKEND`` seams:

* ``memory`` (default) -> the hermetic
  :class:`~scf_spec.projection.InMemoryProjectionRepository` (the unit-test
  default; no Postgres, so every existing spec-engine test stays green untouched);
* ``db`` -> the durable
  :class:`~scf_api.services.projection_repository_db.SqlAlchemyProjectionRepository`
  bound to the shared session factory.

Both satisfy the same ``ProjectionRepository`` protocol, so the swap is
behaviour-preserving: the ``TraceabilityProjector`` / ``DashboardService`` read
and write through the port and never learn which backend they got. The DB import
is deferred so the default path never imports ``scf_db`` / opens a connection.
"""

from __future__ import annotations

from scf_spec import InMemoryProjectionRepository, ProjectionRepository

__all__ = ["build_projection_repository"]


def build_projection_repository() -> ProjectionRepository:
    """Return the projection repository selected by ``FORGE_PROJECTION_BACKEND``."""
    from scf_api.settings import get_settings

    if get_settings().projection_backend == "db":
        from scf_api.db import get_session_factory
        from scf_api.services.projection_repository_db import (
            SqlAlchemyProjectionRepository,
        )

        return SqlAlchemyProjectionRepository(get_session_factory())
    return InMemoryProjectionRepository()
