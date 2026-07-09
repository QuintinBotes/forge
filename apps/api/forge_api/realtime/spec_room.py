"""CRDT spec-collab rooms: authoritative pycrdt docs + engine sync (slice RT-3).

The ``/ws/spec/{spec_id}`` channel co-edits a spec as a CRDT. Each
:class:`SpecCollabRoom` owns an *authoritative* :class:`pycrdt.Doc` holding a
:class:`pycrdt.Text` for ``spec.md`` (and one for ``manifest.yaml``), seeded from
the canonical :class:`~forge_spec.FileSpecEngine`. Connected clients speak the
Yjs binary sync protocol (SYNC_STEP1 / SYNC_STEP2 / SYNC_UPDATE) against that
doc; every applied update is fanned out to the room's *other* clients so all
peers converge.

The Y.Doc is ephemeral session state — the ``FileSpecEngine`` stays canonical.
On quiesce (``~1.5s`` with no updates, or the last editor leaving) the room
materialises ``spec.md`` back through the engine's normal save path and records
exactly **one** :class:`~forge_db.models.SpecVersion` checkpoint (not one per
keystroke), attributed to the most-recent editor — preserving the existing spec
version history.

Write gating lives at the router: a READ-only principal may observe, but a
mutating frame from one is a policy violation. :func:`message_mutates` is the
pure predicate the router uses to decide.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Callable
from functools import lru_cache
from typing import Protocol, runtime_checkable

from pycrdt import (
    Doc,
    Text,
    TransactionEvent,
    YMessageType,
    YSyncMessageType,
    create_sync_message,
    create_update_message,
    handle_sync_message,
    read_message,
)
from sqlalchemy.orm import Session, sessionmaker

from forge_api.services import spec_version_service
from forge_spec import FileSpecEngine

#: Y.Doc keys for the two canonical serializations co-edited in a room.
SPEC_MD_KEY = "spec.md"
MANIFEST_YAML_KEY = "manifest.yaml"

#: Debounce window (seconds): a room quiesces + checkpoints after this idle gap.
DEFAULT_QUIESCE_SECONDS = 1.5

#: An empty y-crdt update (no changes) — a fresh client's SYNC_STEP2 reply.
_EMPTY_UPDATE = b"\x00\x00"


@runtime_checkable
class WsSender(Protocol):
    """The single WebSocket capability a room needs: send a binary frame."""

    async def send_bytes(self, data: bytes) -> None: ...


def message_mutates(sync_body: bytes) -> bool:
    """Whether a SYNC message body carries a doc-mutating update.

    ``sync_body`` is a SYNC message with its leading :class:`YMessageType` byte
    already stripped, so ``sync_body[0]`` is the :class:`YSyncMessageType`.
    SYNC_STEP1 only *requests* state (a read); SYNC_STEP2 / SYNC_UPDATE carry
    updates. An empty update (a fresh client's step2 reply) is not a write.

    This is the pure predicate the router consults to reject a mutating frame
    from a READ-only principal (close 1008) without applying it.
    """
    if not sync_body:
        return False
    subtype = sync_body[0]
    if subtype not in (YSyncMessageType.SYNC_STEP2, YSyncMessageType.SYNC_UPDATE):
        return False
    try:
        update = read_message(sync_body[1:])
    except Exception:
        # Malformed but non-step1: treat as an attempted write (fail closed).
        return True
    return update != _EMPTY_UPDATE


class SpecCollabRoom:
    """An authoritative pycrdt doc for one ``(workspace, spec)`` co-editing session.

    Not thread-safe by design: FastAPI runs every socket handler on the one
    event loop, so the doc, the connection set, and the dirty flag are only ever
    touched from that loop. The observe callback fires *synchronously* inside
    :func:`handle_sync_message`, so ``_origin`` (set around each apply) reliably
    identifies the client whose frame produced an update — with no interleaving.
    """

    def __init__(
        self,
        *,
        workspace_id: uuid.UUID,
        spec_id: uuid.UUID,
        engine: FileSpecEngine,
        session_factory: Callable[[], Session] | sessionmaker[Session],
        quiesce_seconds: float = DEFAULT_QUIESCE_SECONDS,
    ) -> None:
        self.workspace_id = workspace_id
        self.spec_id = spec_id
        self._engine = engine
        self._session_factory = session_factory
        self._quiesce_seconds = quiesce_seconds

        self.doc: Doc = Doc()
        self.spec_md = Text()
        self.manifest_yaml = Text()
        self.doc[SPEC_MD_KEY] = self.spec_md
        self.doc[MANIFEST_YAML_KEY] = self.manifest_yaml
        # Seed from the canonical engine BEFORE observing, so the seed is neither
        # broadcast nor counted as a dirty edit. A missing spec raises
        # SpecNotFoundError here — the caller maps that to a 404.
        with self.doc.transaction():
            self.spec_md += engine.read_spec_md(spec_id)
            self.manifest_yaml += engine.read_manifest_yaml(spec_id)

        self._connections: set[WsSender] = set()
        self._last_editor: uuid.UUID | None = None
        self._dirty = False
        self._origin: WsSender | None = None
        self._origin_user: uuid.UUID | None = None
        self._quiesce_task: asyncio.Task[None] | None = None
        self._send_tasks: set[asyncio.Task[None]] = set()
        self._persist_lock = asyncio.Lock()
        self._sub = self.doc.observe(self._on_update)

    # -- connection lifecycle ---------------------------------------------- #

    @property
    def connection_count(self) -> int:
        """Number of live participants (tests / room-reaping)."""
        return len(self._connections)

    @property
    def last_editor(self) -> uuid.UUID | None:
        """User id of the most recent editor (the checkpoint's ``created_by``)."""
        return self._last_editor

    async def connect(self, conn: WsSender) -> None:
        """Register a participant and kick off the sync handshake (server STEP1)."""
        self._connections.add(conn)
        await conn.send_bytes(create_sync_message(self.doc))

    async def disconnect(self, conn: WsSender) -> None:
        """Drop a participant; checkpoint immediately when the last editor leaves."""
        self._connections.discard(conn)
        if conn is self._origin:
            self._origin = None
        if not self._connections:
            self._cancel_quiesce()
            await self.flush()

    async def receive(
        self,
        conn: WsSender,
        data: bytes,
        *,
        can_write: bool,
        user_id: uuid.UUID | None,
    ) -> bool:
        """Process one inbound frame from ``conn``.

        Returns ``False`` when the frame is a policy violation (a mutating update
        from a caller without WRITE) so the router can close 1008; ``True``
        otherwise. Non-SYNC frames (e.g. awareness) are ignored. A SYNC_STEP1
        request is answered with the doc's STEP2; an applied update is fanned out
        to the room's other clients by :meth:`_on_update` and (re)arms the
        quiesce timer.
        """
        if not data or data[0] != YMessageType.SYNC:
            return True
        body = data[1:]
        if not can_write and message_mutates(body):
            return False

        self._origin = conn
        self._origin_user = user_id
        try:
            reply = handle_sync_message(body, self.doc)
        finally:
            self._origin = None
            self._origin_user = None
        if reply is not None:
            await conn.send_bytes(reply)
        return True

    # -- update fan-out + quiesce ------------------------------------------ #

    def _on_update(self, event: TransactionEvent) -> None:
        """Fan an applied update out to the other clients and (re)arm quiesce.

        Fires synchronously inside :func:`handle_sync_message`, so ``_origin`` is
        the client whose frame produced this update; it is skipped (it already
        has the change). Runs on the event loop, so scheduling sends and the
        debounce timer via ``asyncio`` is safe.
        """
        self._dirty = True
        if self._origin_user is not None:
            self._last_editor = self._origin_user
        message = create_update_message(event.update)
        for conn in list(self._connections):
            if conn is self._origin:
                continue
            self._schedule_send(conn, message)
        self._arm_quiesce()

    def _schedule_send(self, conn: WsSender, data: bytes) -> None:
        async def _send() -> None:
            with contextlib.suppress(Exception):
                await conn.send_bytes(data)

        # Keep a reference so the fire-and-forget task is not GC'd mid-flight.
        task = asyncio.create_task(_send())
        self._send_tasks.add(task)
        task.add_done_callback(self._send_tasks.discard)

    def _arm_quiesce(self) -> None:
        self._cancel_quiesce()
        self._quiesce_task = asyncio.create_task(self._quiesce_after_idle())

    def _cancel_quiesce(self) -> None:
        if self._quiesce_task is not None:
            self._quiesce_task.cancel()
            self._quiesce_task = None

    async def _quiesce_after_idle(self) -> None:
        try:
            await asyncio.sleep(self._quiesce_seconds)
        except asyncio.CancelledError:
            return
        self._quiesce_task = None
        await self.flush()

    # -- materialise + checkpoint ------------------------------------------ #

    async def flush(self) -> None:
        """Materialise ``spec.md`` through the engine + record one checkpoint.

        A no-op when the doc is clean, so the debounce timer and the
        last-editor-leaves path can both call it without double-recording. The
        (sync, filesystem + DB) engine save runs off the event loop.
        """
        async with self._persist_lock:
            if not self._dirty:
                return
            text = str(self.spec_md)
            editor = self._last_editor
            self._dirty = False
            try:
                await asyncio.to_thread(self._persist, text, editor)
            except Exception:
                # A checkpoint is best-effort session state; a transient
                # parse/DB failure must not tear down the live socket. Re-mark
                # dirty so a later quiesce retries.
                self._dirty = True
                raise

    def _persist(self, text: str, editor: uuid.UUID | None) -> None:
        """Save materialised ``spec.md`` and append one SpecVersion (sync path)."""
        manifest = self._engine.save_spec_md(text)
        session = self._session_factory()
        try:
            spec_version_service.record_version(
                session,
                workspace_id=self.workspace_id,
                manifest=manifest,
                spec_md=self._engine.read_spec_md(self.spec_id),
                manifest_yaml=self._engine.read_manifest_yaml(self.spec_id),
                created_by=editor,
            )
        finally:
            session.close()


class SpecRoomRegistry:
    """Vends one :class:`SpecCollabRoom` per ``(workspace_id, spec_id)``.

    A room is created lazily on first connect (seeded from the workspace's
    engine) and reaped once its last participant leaves — so a later session
    re-seeds from the just-checkpointed canonical state. Rooms are keyed by
    ``(workspace_id, spec_id)`` for the same tenant isolation every Forge
    surface enforces.
    """

    def __init__(
        self,
        *,
        engine_for_workspace: Callable[[uuid.UUID], FileSpecEngine],
        session_factory: Callable[[], Session] | sessionmaker[Session],
        quiesce_seconds: float = DEFAULT_QUIESCE_SECONDS,
    ) -> None:
        self._engine_for_workspace = engine_for_workspace
        self._session_factory = session_factory
        self._quiesce_seconds = quiesce_seconds
        self._rooms: dict[tuple[uuid.UUID, uuid.UUID], SpecCollabRoom] = {}
        self._lock = asyncio.Lock()

    async def get_or_create(self, workspace_id: uuid.UUID, spec_id: uuid.UUID) -> SpecCollabRoom:
        """Return the room for ``(workspace, spec)``, creating + seeding it if new.

        Propagates :class:`~forge_spec.SpecNotFoundError` from the engine seed
        when ``spec_id`` resolves to no spec in the workspace (the router maps it
        to 404) — the room is not registered in that case.
        """
        key = (workspace_id, spec_id)
        async with self._lock:
            room = self._rooms.get(key)
            if room is None:
                room = SpecCollabRoom(
                    workspace_id=workspace_id,
                    spec_id=spec_id,
                    engine=self._engine_for_workspace(workspace_id),
                    session_factory=self._session_factory,
                    quiesce_seconds=self._quiesce_seconds,
                )
                self._rooms[key] = room
            return room

    async def release(self, room: SpecCollabRoom) -> None:
        """Reap a room once it has no live participants."""
        async with self._lock:
            if room.connection_count == 0:
                self._rooms.pop((room.workspace_id, room.spec_id), None)


@lru_cache(maxsize=1)
def _spec_room_registry_singleton() -> SpecRoomRegistry:
    from forge_api.db import get_session_factory
    from forge_api.routers.spec import get_spec_registry

    registry = get_spec_registry()
    return SpecRoomRegistry(
        engine_for_workspace=registry.for_workspace,
        session_factory=get_session_factory(),
    )


def get_spec_room_registry() -> SpecRoomRegistry:
    """Return the process-wide spec-collab room registry (overridable in tests)."""
    return _spec_room_registry_singleton()


__all__ = [
    "DEFAULT_QUIESCE_SECONDS",
    "MANIFEST_YAML_KEY",
    "SPEC_MD_KEY",
    "SpecCollabRoom",
    "SpecRoomRegistry",
    "WsSender",
    "get_spec_room_registry",
    "message_mutates",
]
