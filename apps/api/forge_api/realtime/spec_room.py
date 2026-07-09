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

Presence + cursors (slice RT-4) ride the same socket as ephemeral Yjs
*awareness* frames (``YMessageType.AWARENESS``). The room relays a co-editor's
join/leave and cursor/selection to the *other* participants, but **stamps
identity server-side**: the ``display_name`` and per-user ``color`` come from
the authenticated principal (colour derived deterministically from ``user_id``),
so a client cannot spoof another user's identity — any identity fields in the
inbound awareness state are discarded and only the cursor is carried through.
Presence is never persisted, never audited, and scoped to the room's tenant.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import uuid
from collections.abc import Callable, Sequence
from functools import lru_cache
from typing import Any, Protocol, runtime_checkable

from pycrdt import (
    Decoder,
    Doc,
    Encoder,
    Text,
    TransactionEvent,
    YMessageType,
    YSyncMessageType,
    create_awareness_message,
    create_sync_message,
    create_update_message,
    handle_sync_message,
    read_message,
)
from sqlalchemy.orm import Session, sessionmaker

from forge_api.services import spec_version_service
from forge_contracts import CursorRange, PresenceState
from forge_spec import FileSpecEngine

#: Y.Doc keys for the two canonical serializations co-edited in a room.
SPEC_MD_KEY = "spec.md"
MANIFEST_YAML_KEY = "manifest.yaml"

#: Debounce window (seconds): a room quiesces + checkpoints after this idle gap.
DEFAULT_QUIESCE_SECONDS = 1.5

#: An empty y-crdt update (no changes) — a fresh client's SYNC_STEP2 reply.
_EMPTY_UPDATE = b"\x00\x00"

#: Stable, high-contrast collaborator colours. A user is assigned one
#: deterministically from a hash of their ``user_id`` so the same person shows
#: the same colour to every co-editor across sessions and processes. These are
#: transport data (a value stamped onto presence), not web design tokens.
_PRESENCE_COLORS = (
    "#2563eb",  # blue
    "#16a34a",  # green
    "#db2777",  # pink
    "#d97706",  # amber
    "#7c3aed",  # violet
    "#0891b2",  # cyan
    "#dc2626",  # red
    "#4f46e5",  # indigo
)


def color_for_user(user_id: uuid.UUID) -> str:
    """Deterministically map a user to a stable presence colour.

    Uses a salted-independent digest (``hashlib``, not the process-salted
    ``hash()``) so the colour is identical for the same user across every
    process and restart — a client can never influence it.
    """
    digest = hashlib.sha256(str(user_id).encode()).digest()
    return _PRESENCE_COLORS[digest[0] % len(_PRESENCE_COLORS)]


def display_name_for(user_id: uuid.UUID, email: str | None) -> str:
    """Server-authoritative human label for a collaborator.

    Prefers the verified email's local part; falls back to a short, stable
    ``user-<id-prefix>`` handle. Derived from the authenticated principal, never
    from client-supplied awareness state.
    """
    if email:
        local = email.split("@", 1)[0].strip()
        if local:
            return local
    return f"user-{str(user_id)[:8]}"


def _decode_awareness_update(update: bytes) -> list[tuple[int, int, dict[str, Any] | None]]:
    """Decode a Yjs awareness update into ``(client_id, clock, state|None)`` rows.

    A ``null`` state marks a client clearing its presence (a disconnect/leave).
    """
    decoder = Decoder(update)
    count = decoder.read_var_uint()
    rows: list[tuple[int, int, dict[str, Any] | None]] = []
    for _ in range(count):
        client_id = decoder.read_var_uint()
        clock = decoder.read_var_uint()
        raw = decoder.read_var_string()
        state = json.loads(raw) if raw else None
        rows.append((client_id, clock, state if isinstance(state, dict) else None))
    return rows


def _encode_awareness_update(rows: Sequence[tuple[int, int, dict[str, Any] | None]]) -> bytes:
    """Encode ``(client_id, clock, state|None)`` rows into a Yjs awareness update."""
    encoder = Encoder()
    encoder.write_var_uint(len(rows))
    for client_id, clock, state in rows:
        encoder.write_var_uint(client_id)
        encoder.write_var_uint(clock)
        encoder.write_var_string(json.dumps(state))
    return encoder.to_bytes()


def _cursor_from_state(state: dict[str, Any]) -> CursorRange | None:
    """Extract a validated cursor/selection from client awareness state, if any."""
    cursor = state.get("cursor")
    if not isinstance(cursor, dict):
        return None
    try:
        return CursorRange.model_validate(cursor)
    except Exception:
        return None


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

        # Ephemeral presence (never persisted). Keyed by the Yjs awareness
        # client_id each peer picks; we track which connection owns a client_id
        # so a leave removes exactly that peer's presence and one connection can
        # never overwrite another's slot.
        self._presence: dict[int, PresenceState] = {}
        self._client_owner: dict[int, WsSender] = {}
        self._awareness_clock: dict[int, int] = {}

    # -- connection lifecycle ---------------------------------------------- #

    @property
    def connection_count(self) -> int:
        """Number of live participants (tests / room-reaping)."""
        return len(self._connections)

    @property
    def last_editor(self) -> uuid.UUID | None:
        """User id of the most recent editor (the checkpoint's ``created_by``)."""
        return self._last_editor

    @property
    def presence_states(self) -> list[PresenceState]:
        """Server-authoritative view of every live collaborator's presence."""
        return list(self._presence.values())

    async def connect(self, conn: WsSender) -> None:
        """Register a participant and kick off the sync handshake (server STEP1).

        A newcomer is also handed the room's current presence snapshot (the
        already-stamped state of every existing co-editor) so their UI can paint
        peers immediately, before anyone next moves their cursor.
        """
        self._connections.add(conn)
        await conn.send_bytes(create_sync_message(self.doc))
        if self._presence:
            rows = [
                (client_id, self._awareness_clock.get(client_id, 1), state.model_dump(mode="json"))
                for client_id, state in self._presence.items()
            ]
            await conn.send_bytes(create_awareness_message(_encode_awareness_update(rows)))

    async def disconnect(self, conn: WsSender) -> None:
        """Drop a participant; checkpoint immediately when the last editor leaves.

        Any presence this connection owned is removed and a leave (null-state)
        awareness frame is broadcast to the remaining co-editors so its cursor
        stops rendering.
        """
        self._connections.discard(conn)
        if conn is self._origin:
            self._origin = None
        self._drop_presence(conn)
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
        email: str | None = None,
    ) -> bool:
        """Process one inbound frame from ``conn``.

        Returns ``False`` when the frame is a policy violation (a mutating update
        from a caller without WRITE) so the router can close 1008; ``True``
        otherwise. An AWARENESS frame relays presence/cursor to co-editors with
        server-stamped identity (never write-gated — even a viewer shows
        presence). A SYNC_STEP1 request is answered with the doc's STEP2; an
        applied update is fanned out to the room's other clients by
        :meth:`_on_update` and (re)arms the quiesce timer.
        """
        if not data:
            return True
        if data[0] == YMessageType.AWARENESS:
            self._relay_awareness(conn, data[1:], user_id=user_id, email=email)
            return True
        if data[0] != YMessageType.SYNC:
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

    # -- presence + cursors (awareness relay) ------------------------------ #

    def _relay_awareness(
        self,
        conn: WsSender,
        body: bytes,
        *,
        user_id: uuid.UUID | None,
        email: str | None,
    ) -> None:
        """Relay an inbound awareness frame, stamping server-authoritative identity.

        Identity (``user_id``/``display_name``/``color``) is taken from the
        authenticated principal, not the wire — any identity fields the client
        sent are discarded and only the cursor/selection is carried through, so
        no peer can impersonate another. A ``null`` state relays a self-clear.
        The corrected update is fanned out to the room's *other* participants.
        """
        if user_id is None:
            return
        try:
            rows = _decode_awareness_update(read_message(body))
        except Exception:
            return

        display_name = display_name_for(user_id, email)
        color = color_for_user(user_id)
        corrected: list[tuple[int, int, dict[str, Any] | None]] = []
        for client_id, clock, state in rows:
            owner = self._client_owner.get(client_id)
            if owner is not None and owner is not conn:
                # A connection may only speak for its own awareness client_ids;
                # ignore any attempt to overwrite another peer's slot.
                continue
            self._client_owner[client_id] = conn
            self._awareness_clock[client_id] = clock
            if state is None:
                self._presence.pop(client_id, None)
                self._client_owner.pop(client_id, None)
                corrected.append((client_id, clock, None))
                continue
            presence = PresenceState(
                user_id=str(user_id),
                display_name=display_name,
                color=color,
                cursor=_cursor_from_state(state),
            )
            self._presence[client_id] = presence
            corrected.append((client_id, clock, presence.model_dump(mode="json")))

        if not corrected:
            return
        message = create_awareness_message(_encode_awareness_update(corrected))
        for other in list(self._connections):
            if other is conn:
                continue
            self._schedule_send(other, message)

    def _drop_presence(self, conn: WsSender) -> None:
        """Remove a connection's presence and broadcast the leave to co-editors."""
        owned = [client_id for client_id, owner in self._client_owner.items() if owner is conn]
        if not owned:
            return
        removals: list[tuple[int, int, dict[str, Any] | None]] = []
        for client_id in owned:
            self._client_owner.pop(client_id, None)
            self._presence.pop(client_id, None)
            clock = self._awareness_clock.get(client_id, 0) + 1
            self._awareness_clock[client_id] = clock
            removals.append((client_id, clock, None))
        message = create_awareness_message(_encode_awareness_update(removals))
        for other in list(self._connections):
            self._schedule_send(other, message)

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
    "color_for_user",
    "display_name_for",
    "get_spec_room_registry",
    "message_mutates",
]
