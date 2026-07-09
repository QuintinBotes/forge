"""``SpecVersion`` — an immutable snapshot of a spec taken on every save.

(ss-versioning) Spec Studio (F02's ``FileSpecEngine``) is filesystem-backed and
keeps no history: every ``write_manifest`` / ``save_spec_md`` /
``save_manifest_yaml`` overwrites ``manifest.yaml`` and ``spec.md`` in place, so
a spec's prior states are lost the moment it is edited. ``SpecVersion`` is the
DB-backed history: the API layer appends one row per save (see
``forge_api.routers.spec``'s ``_record_version``) carrying a full snapshot of
the manifest plus both rendered serializations, so the web Spec Studio can list
a spec's version history and diff any two versions.

Keyed by the engine's own deterministic ``spec_id`` (not a FK to
``spec_document``: that table is a separate, not-yet-wired projection — see
``forge_db.models.planning`` — and the engine is the actual source of truth
today). ``version_number`` is a per-``(workspace_id, spec_id)`` sequence
assigned by the recording service, 1-based and gapless.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Index, Integer, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from forge_db.base import WorkspaceScopedModel, json_type


class SpecVersion(WorkspaceScopedModel):
    """One immutable snapshot of a spec's manifest, taken on save."""

    __tablename__ = "spec_version"
    __table_args__ = (
        UniqueConstraint("workspace_id", "spec_id", "version_number", name="uq_spec_version_seq"),
        Index("ix_spec_version_spec_id", "spec_id"),
    )

    spec_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    spec_key: Mapped[str] = mapped_column(String(64), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    manifest: Mapped[dict[str, Any]] = mapped_column(json_type(), default=dict, nullable=False)
    spec_md: Mapped[str] = mapped_column(Text, nullable=False)
    manifest_yaml: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
