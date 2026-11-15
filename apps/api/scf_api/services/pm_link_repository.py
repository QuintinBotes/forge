"""F18 composition root: the PM-sync link-repository seam.

:func:`build_link_repository` selects the ``LinkRepository`` backend that a
:class:`~scf_integrations.pm.sync_engine.PMSyncEngine` links durably through,
via ``FORGE_PM_LINK_BACKEND`` (default ``memory``), exactly like the sibling
``FORGE_BOARD_BACKEND`` / ``FORGE_APPROVAL_BACKEND`` / ``FORGE_PROJECTION_BACKEND``
seams:

* ``memory`` (default) -> the hermetic
  :class:`~scf_integrations.pm.sync_engine.InMemoryLinkRepository` (the
  unit-test default; no Postgres, so every existing sync-engine test stays green
  untouched);
* ``db`` -> the durable
  :class:`~scf_api.services.pm_link_repository_db.DbLinkRepository` bound to the
  shared session factory (mapping onto the ``pm_task_link`` table).

Both satisfy the same ``LinkRepository`` protocol, so the swap is
behaviour-preserving: ``PMSyncEngine`` reads and writes through the port and
never learns which backend it got. The DB import is deferred so the default path
never imports ``scf_db`` / opens a connection.
"""

from __future__ import annotations

from scf_integrations.pm.sync_engine import InMemoryLinkRepository, LinkRepository

__all__ = ["build_link_repository"]


def build_link_repository() -> LinkRepository:
    """Return the link repository selected by ``FORGE_PM_LINK_BACKEND``."""
    from scf_api.settings import get_settings

    if get_settings().pm_link_backend == "db":
        from scf_api.db import get_session_factory
        from scf_api.services.pm_link_repository_db import DbLinkRepository

        return DbLinkRepository(get_session_factory())
    return InMemoryLinkRepository()
