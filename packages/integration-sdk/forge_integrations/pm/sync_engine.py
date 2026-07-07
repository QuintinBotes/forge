"""Provider-agnostic bidirectional sync engine (the heart of F18).

``PMSyncEngine`` maps Forge tasks <-> external issues, suppresses echo loops
(content-hash safety net; the activity-event origin tag is the worker's primary
signal), detects/resolves conflicts deterministically, and persists durable
links. It depends only on small Protocols (``LinkRepository`` / ``BoardWriter`` /
``AuditSink``) and a ``PMAdapter``, so the exact same engine serves every
provider and can be wired to the real board service or to in-memory fakes.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from forge_contracts.enums import Direction
from forge_contracts.pm import (
    ConflictPolicy,
    ExternalTask,
    ForgePriority,
    ForgeTask,
    PMAdapter,
    PMProvider,
    PMSyncState,
    StatusCategory,
    SyncDirection,
    SyncOutcome,
    WebhookEvent,
)
from forge_integrations.pm.errors import MappingError, ProviderError, SyncConflict
from forge_integrations.pm.hashing import external_content_hash, forge_content_hash

# --------------------------------------------------------------------------- #
# Records + Protocols                                                          #
# --------------------------------------------------------------------------- #


class LinkRecord(BaseModel):
    """In-engine view of a ``pm_task_link`` row."""

    model_config = ConfigDict(extra="ignore")

    id: UUID = Field(default_factory=uuid4)
    connection_id: UUID
    workspace_id: UUID
    forge_task_id: UUID
    provider: PMProvider
    external_id: str
    external_key: str = ""
    external_url: str = ""
    last_synced_at: datetime | None = None
    forge_version_at_sync: int | None = None
    external_updated_at_at_sync: datetime | None = None
    last_outbound_hash: str | None = None
    last_inbound_hash: str | None = None
    sync_state: PMSyncState = PMSyncState.synced
    conflict_detail: dict | None = None
    last_error: str | None = None


class ForgeTaskPatch(BaseModel):
    """Fields applied to the board when writing an inbound (external->forge) change."""

    model_config = ConfigDict(extra="ignore")

    project_id: UUID | None = None
    title: str
    description_md: str | None = None
    status_category: StatusCategory
    priority: ForgePriority
    assignee_email: str | None = None
    label_names: list[str] = Field(default_factory=list)


class LinkRepository(Protocol):
    def get(self, link_id: UUID) -> LinkRecord | None: ...
    def get_by_forge_task(self, connection_id: UUID, forge_task_id: UUID) -> LinkRecord | None: ...
    def get_by_external(self, connection_id: UUID, external_id: str) -> LinkRecord | None: ...
    def upsert(self, link: LinkRecord) -> LinkRecord: ...
    def delete(self, link_id: UUID) -> None: ...
    def list_by_state(self, connection_id: UUID, state: PMSyncState) -> list[LinkRecord]: ...


class BoardWriter(Protocol):
    """The board-write surface the engine needs (tagged ``source='pm_sync'``)."""

    def get(self, forge_task_id: UUID) -> ForgeTask | None: ...
    def create(self, patch: ForgeTaskPatch, *, source: dict) -> ForgeTask: ...
    def update(
        self,
        forge_task_id: UUID,
        patch: ForgeTaskPatch,
        *,
        expected_version: int | None,
        source: dict,
    ) -> ForgeTask: ...


class AuditSink(Protocol):
    def record(self, entry: dict) -> None: ...


# --------------------------------------------------------------------------- #
# In-memory implementations (tests + reference)                               #
# --------------------------------------------------------------------------- #


class InMemoryLinkRepository:
    def __init__(self) -> None:
        self._by_id: dict[UUID, LinkRecord] = {}

    def get(self, link_id: UUID) -> LinkRecord | None:
        rec = self._by_id.get(link_id)
        return rec.model_copy(deep=True) if rec else None

    def get_by_forge_task(self, connection_id: UUID, forge_task_id: UUID) -> LinkRecord | None:
        for rec in self._by_id.values():
            if rec.connection_id == connection_id and rec.forge_task_id == forge_task_id:
                return rec.model_copy(deep=True)
        return None

    def get_by_external(self, connection_id: UUID, external_id: str) -> LinkRecord | None:
        for rec in self._by_id.values():
            if rec.connection_id == connection_id and rec.external_id == external_id:
                return rec.model_copy(deep=True)
        return None

    def upsert(self, link: LinkRecord) -> LinkRecord:
        self._by_id[link.id] = link.model_copy(deep=True)
        return link.model_copy(deep=True)

    def delete(self, link_id: UUID) -> None:
        self._by_id.pop(link_id, None)

    def list_by_state(self, connection_id: UUID, state: PMSyncState) -> list[LinkRecord]:
        return [
            rec.model_copy(deep=True)
            for rec in self._by_id.values()
            if rec.connection_id == connection_id and rec.sync_state == state
        ]


class InMemoryAuditSink:
    def __init__(self) -> None:
        self.entries: list[dict] = []

    def record(self, entry: dict) -> None:
        self.entries.append(dict(entry))


class InMemoryBoardWriter:
    """A minimal in-memory board for engine unit tests; records source tags."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._store: dict[UUID, ForgeTask] = {}
        self._counter = 0
        self.created_sources: list[dict] = []
        self.updated_sources: list[dict] = []
        self._clock = clock or (lambda: datetime.now(UTC))

    def get(self, forge_task_id: UUID) -> ForgeTask | None:
        task = self._store.get(forge_task_id)
        return task.model_copy(deep=True) if task else None

    def create(self, patch: ForgeTaskPatch, *, source: dict) -> ForgeTask:
        self._counter += 1
        task_id = uuid4()
        task = ForgeTask(
            id=task_id,
            key=f"TASK-{self._counter}",
            project_id=patch.project_id or uuid4(),
            title=patch.title,
            description_md=patch.description_md,
            status_category=patch.status_category,
            priority=patch.priority,
            assignee_email=patch.assignee_email,
            label_names=list(patch.label_names),
            version=1,
            updated_at=self._clock(),
        )
        self._store[task_id] = task
        self.created_sources.append(dict(source))
        return task.model_copy(deep=True)

    def update(
        self,
        forge_task_id: UUID,
        patch: ForgeTaskPatch,
        *,
        expected_version: int | None,
        source: dict,
    ) -> ForgeTask:
        existing = self._store[forge_task_id]
        updated = existing.model_copy(
            update={
                "title": patch.title,
                "description_md": patch.description_md,
                "status_category": patch.status_category,
                "priority": patch.priority,
                "assignee_email": patch.assignee_email,
                "label_names": list(patch.label_names),
                "version": existing.version + 1,
                "updated_at": self._clock(),
            }
        )
        self._store[forge_task_id] = updated
        self.updated_sources.append(dict(source))
        return updated.model_copy(deep=True)

    # test helper
    def put(self, task: ForgeTask) -> None:
        self._store[task.id] = task.model_copy(deep=True)


# --------------------------------------------------------------------------- #
# Engine                                                                       #
# --------------------------------------------------------------------------- #

ECHO_SOURCE = "pm_sync"


class PMSyncEngine:
    def __init__(
        self,
        *,
        adapter: PMAdapter,
        links: LinkRepository,
        board: BoardWriter,
        audit: AuditSink,
        connection_id: UUID,
        workspace_id: UUID,
        forge_project_id: UUID,
        conflict_policy: ConflictPolicy = ConflictPolicy.newest_wins,
        sync_direction: SyncDirection = SyncDirection.bidirectional,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.adapter = adapter
        self.links = links
        self.board = board
        self.audit = audit
        self.connection_id = connection_id
        self.workspace_id = workspace_id
        self.forge_project_id = forge_project_id
        self.conflict_policy = conflict_policy
        self.sync_direction = sync_direction
        self._clock = clock or (lambda: datetime.now(UTC))

    # ------------------------------------------------------------------ #
    # public                                                             #
    # ------------------------------------------------------------------ #

    async def sync_out(self, forge_task: ForgeTask) -> SyncOutcome:
        if self.sync_direction == SyncDirection.inbound_only:
            return SyncOutcome(
                direction=Direction.OUT,
                forge_task_id=forge_task.id,
                action="no_change",
                detail={"reason": "inbound_only"},
            )

        link = self.links.get_by_forge_task(self.connection_id, forge_task.id)
        forge_hash = forge_content_hash(forge_task)

        if link is None:
            external = await self._create_external(forge_task)
            new_link = LinkRecord(
                connection_id=self.connection_id,
                workspace_id=self.workspace_id,
                forge_task_id=forge_task.id,
                provider=self.adapter.provider,
                external_id=external.external_id,
                external_key=external.external_key,
                external_url=external.url,
            )
            self._refresh(new_link, forge_task, external)
            self.links.upsert(new_link)
            return SyncOutcome(
                direction=Direction.OUT,
                forge_task_id=forge_task.id,
                external_id=external.external_id,
                action="created",
            )

        if forge_hash == link.last_outbound_hash:
            return SyncOutcome(
                direction=Direction.OUT,
                forge_task_id=forge_task.id,
                external_id=link.external_id,
                action="no_change",
            )

        # Forge changed. Did external change too since the last sync?
        external_now = await self.adapter.fetch_external(link.external_id)
        external_changed = (
            external_content_hash(external_now) != link.last_inbound_hash
            if link.last_inbound_hash is not None
            else False
        )
        if external_changed:
            return await self._resolve(
                link, forge_task=forge_task, external_task=external_now, origin=Direction.OUT
            )

        external = await self._update_external(link.external_id, forge_task)
        self._refresh(link, forge_task, external)
        self.links.upsert(link)
        return SyncOutcome(
            direction=Direction.OUT,
            forge_task_id=forge_task.id,
            external_id=link.external_id,
            action="updated",
        )

    async def sync_in(
        self, external_task: ExternalTask, event: WebhookEvent | None = None
    ) -> SyncOutcome:
        if self.sync_direction == SyncDirection.outbound_only:
            return SyncOutcome(
                direction=Direction.IN,
                external_id=external_task.external_id,
                action="skipped_echo",
                detail={"reason": "outbound_only"},
            )

        link = self.links.get_by_external(self.connection_id, external_task.external_id)
        external_hash = external_content_hash(external_task)

        if link is None:
            forge_task = self._apply_inbound_create(external_task)
            new_link = LinkRecord(
                connection_id=self.connection_id,
                workspace_id=self.workspace_id,
                forge_task_id=forge_task.id,
                provider=self.adapter.provider,
                external_id=external_task.external_id,
                external_key=external_task.external_key,
                external_url=external_task.url,
            )
            self._refresh(new_link, forge_task, external_task)
            self.links.upsert(new_link)
            return SyncOutcome(
                direction=Direction.IN,
                forge_task_id=forge_task.id,
                external_id=external_task.external_id,
                action="created",
            )

        if external_hash == link.last_inbound_hash:
            return SyncOutcome(
                direction=Direction.IN,
                forge_task_id=link.forge_task_id,
                external_id=external_task.external_id,
                action="skipped_echo",
            )

        # External changed. Did forge change too since the last sync?
        forge_now = self.board.get(link.forge_task_id)
        forge_changed = (
            forge_now is not None
            and link.last_outbound_hash is not None
            and forge_content_hash(forge_now) != link.last_outbound_hash
        )
        if forge_changed and forge_now is not None:
            return await self._resolve(
                link, forge_task=forge_now, external_task=external_task, origin=Direction.IN
            )

        forge_task = self._apply_inbound_update(link, external_task)
        self._refresh(link, forge_task, external_task)
        self.links.upsert(link)
        return SyncOutcome(
            direction=Direction.IN,
            forge_task_id=link.forge_task_id,
            external_id=external_task.external_id,
            action="updated",
        )

    async def resolve_conflict(
        self, link_id: UUID, winner: Literal["forge", "external"]
    ) -> SyncOutcome:
        link = self.links.get(link_id)
        if link is None:
            raise SyncConflict("link not found", link_id=str(link_id))
        detail = link.conflict_detail or {}
        if winner == "forge":
            forge_task = ForgeTask.model_validate(detail["forge"])
            external = await self._update_external(link.external_id, forge_task)
            self._refresh(link, forge_task, external)
            self.links.upsert(link)
            return SyncOutcome(
                direction=Direction.OUT,
                forge_task_id=forge_task.id,
                external_id=link.external_id,
                action="updated",
                winner="forge",
            )
        external_task = ExternalTask.model_validate(detail["external"])
        forge_task = self._apply_inbound_update(link, external_task)
        self._refresh(link, forge_task, external_task)
        self.links.upsert(link)
        return SyncOutcome(
            direction=Direction.IN,
            forge_task_id=link.forge_task_id,
            external_id=external_task.external_id,
            action="updated",
            winner="external",
        )

    # ------------------------------------------------------------------ #
    # conflict resolution                                                #
    # ------------------------------------------------------------------ #

    async def _resolve(
        self,
        link: LinkRecord,
        *,
        forge_task: ForgeTask,
        external_task: ExternalTask,
        origin: Direction,
    ) -> SyncOutcome:
        policy = self.conflict_policy
        winner: Literal["forge", "external"]
        if policy == ConflictPolicy.manual:
            link.sync_state = PMSyncState.conflict
            link.conflict_detail = {
                "forge": forge_task.model_dump(mode="json"),
                "external": external_task.model_dump(mode="json"),
            }
            self.links.upsert(link)
            return SyncOutcome(
                direction=origin,
                forge_task_id=forge_task.id,
                external_id=external_task.external_id,
                action="conflict",
            )
        if policy == ConflictPolicy.forge_wins:
            winner = "forge"
        elif policy == ConflictPolicy.external_wins:
            winner = "external"
        else:  # newest_wins
            winner = (
                "forge"
                if forge_task.updated_at >= external_task.external_updated_at
                else "external"
            )

        if winner == "forge":
            external = await self._update_external(link.external_id, forge_task)
            self._refresh(link, forge_task, external)
            self.links.upsert(link)
            return SyncOutcome(
                direction=Direction.OUT,
                forge_task_id=forge_task.id,
                external_id=link.external_id,
                action="updated",
                winner="forge",
            )
        forge_applied = self._apply_inbound_update(link, external_task)
        self._refresh(link, forge_applied, external_task)
        self.links.upsert(link)
        return SyncOutcome(
            direction=Direction.IN,
            forge_task_id=link.forge_task_id,
            external_id=external_task.external_id,
            action="updated",
            winner="external",
        )

    # ------------------------------------------------------------------ #
    # external + board writes (audited)                                  #
    # ------------------------------------------------------------------ #

    async def _create_external(self, forge_task: ForgeTask) -> ExternalTask:
        external = await self.adapter.create_external(forge_task)
        self._audit("create_external", Direction.OUT, forge_task, external)
        return external

    async def _update_external(self, external_id: str, forge_task: ForgeTask) -> ExternalTask:
        external = await self.adapter.update_external(external_id, forge_task)
        self._audit("update_external", Direction.OUT, forge_task, external)
        return external

    def _source_tag(self, link_id: UUID | None) -> dict:
        return {
            "source": ECHO_SOURCE,
            "connection_id": str(self.connection_id),
            "link_id": str(link_id) if link_id else None,
            "direction": "in",
        }

    def _apply_inbound_create(self, external_task: ExternalTask) -> ForgeTask:
        patch = self._external_to_patch(external_task)
        forge_task = self.board.create(patch, source=self._source_tag(None))
        self._audit("board_create", Direction.IN, forge_task, external_task)
        return forge_task

    def _apply_inbound_update(self, link: LinkRecord, external_task: ExternalTask) -> ForgeTask:
        patch = self._external_to_patch(external_task)
        current = self.board.get(link.forge_task_id)
        expected = current.version if current else None
        forge_task = self.board.update(
            link.forge_task_id,
            patch,
            expected_version=expected,
            source=self._source_tag(link.id),
        )
        self._audit("board_update", Direction.IN, forge_task, external_task)
        return forge_task

    # ------------------------------------------------------------------ #
    # helpers                                                            #
    # ------------------------------------------------------------------ #

    def _external_to_patch(self, external_task: ExternalTask) -> ForgeTaskPatch:
        category = external_task.status_category
        if category is None:
            try:
                category = StatusCategory(
                    self.adapter.map_status(external_task.status_name, Direction.IN)
                )
            except (MappingError, ValueError):
                category = StatusCategory.backlog
        priority = ForgePriority.none
        if external_task.priority_token is not None:
            try:
                priority = ForgePriority(
                    self.adapter.map_priority(external_task.priority_token, Direction.IN)
                )
            except (MappingError, ValueError):
                priority = ForgePriority.none
        return ForgeTaskPatch(
            project_id=self.forge_project_id,
            title=external_task.title,
            description_md=external_task.description_md,
            status_category=category,
            priority=priority,
            assignee_email=external_task.assignee_email,
            label_names=list(external_task.labels),
        )

    def _refresh(
        self, link: LinkRecord, forge_task: ForgeTask, external_task: ExternalTask
    ) -> None:
        link.forge_version_at_sync = forge_task.version
        link.external_updated_at_at_sync = external_task.external_updated_at
        link.last_outbound_hash = forge_content_hash(forge_task)
        link.last_inbound_hash = external_content_hash(external_task)
        link.last_synced_at = self._clock()
        link.sync_state = PMSyncState.synced
        link.conflict_detail = None
        link.last_error = None
        link.external_key = external_task.external_key or link.external_key
        link.external_url = external_task.url or link.external_url

    def _audit(
        self,
        operation: str,
        direction: Direction,
        forge_task: ForgeTask | None,
        external_task: ExternalTask | None,
    ) -> None:
        self.audit.record(
            {
                "operation": operation,
                "connection_id": str(self.connection_id),
                "provider": self.adapter.provider.value,
                "direction": direction.value,
                "forge_task_id": str(forge_task.id) if forge_task else None,
                "external_id": external_task.external_id if external_task else None,
                "payload_hash": (forge_content_hash(forge_task) if forge_task else None),
                "result": "ok",
                "at": self._clock().isoformat(),
            }
        )


__all__ = [
    "AuditSink",
    "BoardWriter",
    "ForgeTaskPatch",
    "InMemoryAuditSink",
    "InMemoryBoardWriter",
    "InMemoryLinkRepository",
    "LinkRecord",
    "LinkRepository",
    "PMSyncEngine",
    "ProviderError",
]
