"""Records + reads spec version snapshots (ss-versioning).

``FileSpecEngine`` is filesystem-backed and keeps no history: every save
overwrites ``manifest.yaml``/``spec.md`` in place. This service is the durable
side-channel the spec router calls on every save (``spec_create``,
``write_manifest``, ``write_spec_markdown``, ``write_spec_manifest_yaml``): it
appends an immutable :class:`~forge_db.models.SpecVersion` row carrying a full
snapshot, so Spec Studio can list a spec's version history and diff any two
versions even though the engine itself only ever holds the *current* state.

Workspace-scoped throughout (mirrors every other DB-backed repo in
``apps/api``): a version is only ever recorded, listed, or read for the
caller's own workspace.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from forge_contracts import SpecManifest
from forge_db.models import SpecVersion
from forge_spec import spec_id_for_key


def record_version(
    db: Session,
    *,
    workspace_id: uuid.UUID,
    manifest: SpecManifest,
    spec_md: str,
    manifest_yaml: str,
    created_by: uuid.UUID | None,
) -> SpecVersion:
    """Append the next version snapshot for ``manifest.id`` and commit it."""
    spec_id = spec_id_for_key(manifest.id)
    next_number = (
        db.execute(
            select(func.coalesce(func.max(SpecVersion.version_number), 0)).where(
                SpecVersion.workspace_id == workspace_id,
                SpecVersion.spec_id == spec_id,
            )
        ).scalar_one()
        + 1
    )
    version = SpecVersion(
        workspace_id=workspace_id,
        spec_id=spec_id,
        spec_key=manifest.id,
        version_number=next_number,
        name=manifest.name,
        status=manifest.status.value,
        manifest=manifest.model_dump(mode="json"),
        spec_md=spec_md,
        manifest_yaml=manifest_yaml,
        created_by=created_by,
    )
    db.add(version)
    db.commit()
    db.refresh(version)
    return version


def list_versions(db: Session, *, workspace_id: uuid.UUID, spec_id: uuid.UUID) -> list[SpecVersion]:
    """List a spec's versions, newest first."""
    stmt = (
        select(SpecVersion)
        .where(SpecVersion.workspace_id == workspace_id, SpecVersion.spec_id == spec_id)
        .order_by(SpecVersion.version_number.desc())
    )
    return list(db.execute(stmt).scalars())


def get_version(
    db: Session, *, workspace_id: uuid.UUID, spec_id: uuid.UUID, version_number: int
) -> SpecVersion | None:
    """Read one specific version snapshot, or ``None`` if unknown."""
    stmt = select(SpecVersion).where(
        SpecVersion.workspace_id == workspace_id,
        SpecVersion.spec_id == spec_id,
        SpecVersion.version_number == version_number,
    )
    return db.execute(stmt).scalar_one_or_none()


__all__ = ["get_version", "list_versions", "record_version"]
