"""F19 container-sandboxing model: per-run sandbox instances.

A :class:`SandboxInstance` is the operational + audit record for one task's
sandbox (host worktree subprocess, or a locked-down Docker container). It drives
orphan reaping (``status`` / ``created_at`` + TTL, label-scoped ``container_name``)
and the run-trace ``sandbox`` lifecycle block. Secrets are never stored here:
``limits`` is a non-secret snapshot for audit; the BYOK model key and git
credentials never reach a sandbox and so never appear on this row.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from forge_db.base import WorkspaceScopedModel, enum_type, json_type
from forge_db.models.enums import (
    SandboxIsolationClass,
    SandboxKind,
    SandboxNetwork,
    SandboxStatus,
)

if TYPE_CHECKING:
    from forge_db.models.runs import AgentRun


class SandboxInstance(WorkspaceScopedModel):
    """One sandbox lifecycle row per agent run (F19)."""

    __tablename__ = "sandbox_instance"
    __table_args__ = (
        Index("ix_sandbox_instance_run", "agent_run_id"),
        Index("ix_sandbox_instance_status", "status"),
        Index("ix_sandbox_instance_isolation_class", "isolation_class"),
        Index(
            "ux_sandbox_instance_container_name",
            "container_name",
            unique=True,
            sqlite_where=text("container_name IS NOT NULL"),
            postgresql_where=text("container_name IS NOT NULL"),
        ),
    )

    agent_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent_run.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[SandboxKind] = mapped_column(
        enum_type(SandboxKind), default=SandboxKind.WORKTREE, nullable=False
    )
    container_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    container_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    image: Mapped[str | None] = mapped_column(String(512), nullable=True)
    network: Mapped[SandboxNetwork] = mapped_column(
        enum_type(SandboxNetwork), default=SandboxNetwork.NONE, nullable=False
    )
    status: Mapped[SandboxStatus] = mapped_column(
        enum_type(SandboxStatus), default=SandboxStatus.CREATING, nullable=False
    )
    exit_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    host_worktree_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    limits: Mapped[dict[str, Any]] = mapped_column(json_type(), default=dict, nullable=False)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # --- F34 kernel-boundary provenance (additive; null for pre-F34 rows) ---
    runtime: Mapped[str | None] = mapped_column(String(64), nullable=True)
    isolation_class: Mapped[SandboxIsolationClass] = mapped_column(
        enum_type(SandboxIsolationClass),
        default=SandboxIsolationClass.HOST_PROCESS,
        nullable=False,
        server_default=SandboxIsolationClass.HOST_PROCESS.value,
    )
    gvisor_platform: Mapped[str | None] = mapped_column(String(32), nullable=True)
    guest_kernel_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    vm_vcpus: Mapped[int | None] = mapped_column(Integer, nullable=True)
    vm_memory_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    boot_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    agent_run: Mapped[AgentRun] = relationship(back_populates="sandbox_instances")


__all__ = ["SandboxInstance"]
