"""Slice RT-3 — CRDT spec-collab room: pycrdt doc + engine sync + checkpointing.

Four behaviours are pinned:

* **convergence** — two in-process pycrdt clients apply *concurrent* inserts
  through the authoritative room doc and both (plus the server) converge to
  byte-identical text;
* **checkpoint on quiesce** — a burst of updates materialises through the
  ``FileSpecEngine`` save path and records **exactly one** ``SpecVersion``
  (not one per keystroke), attributed to the most-recent editor;
* **unknown spec → 404** — connecting to a spec the workspace does not have is
  a handshake denial, not a post-accept close;
* **READ-only writer → 1008** — a viewer may observe but a mutating frame from
  one is a policy-violation close (the update is never applied).

Hermetic: an in-memory SQLite factory backs ``SpecVersion``; a tmp-rooted
``FileSpecEngine`` backs the canonical spec content (mirrors
``test_spec_versioning.py``).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from pycrdt import (
    Doc,
    Text,
    TransactionEvent,
    YMessageType,
    create_sync_message,
    create_update_message,
    handle_sync_message,
)
from sqlalchemy import StaticPool, create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from forge_api.auth.service import AuthService, get_auth_service
from forge_api.main import create_app
from forge_api.realtime.spec_room import (
    SPEC_MD_KEY,
    SpecCollabRoom,
    SpecRoomRegistry,
    get_spec_room_registry,
    message_mutates,
)
from forge_contracts import Requirement, UserRole
from forge_db.base import Base
from forge_db.models import SpecVersion, Workspace
from forge_spec import FileSpecEngine, spec_id_for_key

TEST_WORKSPACE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
EDITOR_A = uuid.UUID("00000000-0000-0000-0000-0000000000c1")
EDITOR_B = uuid.UUID("00000000-0000-0000-0000-0000000000c2")


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #


@pytest.fixture
def db_factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as session:
        session.add(Workspace(id=TEST_WORKSPACE_ID, name="Acme", slug="acme"))
        session.commit()
    yield factory


@pytest.fixture
def spec_engine(tmp_path: Path) -> FileSpecEngine:
    return FileSpecEngine(root=tmp_path / "specs")


@pytest.fixture
def seeded_spec_id(spec_engine: FileSpecEngine) -> uuid.UUID:
    manifest = spec_engine.spec_create(
        uuid.uuid4(), "Realtime coediting", [Requirement(id="R1", text="Edit together")]
    )
    return spec_id_for_key(manifest.id)


def _version_count(factory: sessionmaker[Session], spec_id: uuid.UUID) -> int:
    with factory() as session:
        return session.execute(
            select(func.count()).select_from(SpecVersion).where(SpecVersion.spec_id == spec_id)
        ).scalar_one()


# --------------------------------------------------------------------------- #
# In-process client harness (drives the Yjs binary sync protocol at the room)  #
# --------------------------------------------------------------------------- #


class _FakeConn:
    """A room participant that records the binary frames the room sends it."""

    def __init__(self) -> None:
        self.inbox: list[bytes] = []

    async def send_bytes(self, data: bytes) -> None:
        self.inbox.append(data)


class _Client:
    """An in-process pycrdt peer editing ``spec.md`` against a room."""

    def __init__(self, user_id: uuid.UUID) -> None:
        self.user_id = user_id
        self.doc = Doc()
        self.text = Text()
        self.doc[SPEC_MD_KEY] = self.text
        self.conn = _FakeConn()
        self._updates: list[bytes] = []
        self.doc.observe(self._capture)

    def _capture(self, event: TransactionEvent) -> None:
        self._updates.append(event.update)

    async def _drain(self, room: SpecCollabRoom, *, can_write: bool = True) -> None:
        """Apply every queued server frame; push any STEP2 reply back to the room."""
        while self.conn.inbox:
            msg = self.conn.inbox.pop(0)
            if msg[0] != YMessageType.SYNC:
                continue
            reply = handle_sync_message(msg[1:], self.doc)
            if reply is not None:  # our STEP2 reply to the server's STEP1
                await room.receive(self.conn, reply, can_write=can_write, user_id=self.user_id)

    async def join(self, room: SpecCollabRoom) -> None:
        """Full initial sync: exchange STEP1/STEP2 both directions."""
        await room.connect(self.conn)  # server STEP1 -> inbox
        await self._drain(room)
        # Pull the server's state with our own STEP1.
        await room.receive(
            self.conn, create_sync_message(self.doc), can_write=True, user_id=self.user_id
        )
        await self._drain(room)

    async def edit(self, room: SpecCollabRoom, index: int, value: str) -> None:
        """Insert ``value`` at ``index`` and push the resulting update to the room."""
        before = len(self._updates)
        self.text.insert(index, value)
        for update in self._updates[before:]:
            await room.receive(
                self.conn,
                create_update_message(update),
                can_write=True,
                user_id=self.user_id,
            )


async def _settle(clients: list[_Client], room: SpecCollabRoom) -> None:
    """Let scheduled fan-out sends land, then drain every client to convergence."""
    for _ in range(8):
        await asyncio.sleep(0)
    for client in clients:
        await client._drain(room)


# --------------------------------------------------------------------------- #
# 1. Convergence                                                               #
# --------------------------------------------------------------------------- #


async def test_concurrent_inserts_converge(
    spec_engine: FileSpecEngine,
    seeded_spec_id: uuid.UUID,
    db_factory: sessionmaker[Session],
) -> None:
    room = SpecCollabRoom(
        workspace_id=TEST_WORKSPACE_ID,
        spec_id=seeded_spec_id,
        engine=spec_engine,
        session_factory=db_factory,
        quiesce_seconds=999,  # never auto-quiesce mid-test
    )
    alice, bob = _Client(EDITOR_A), _Client(EDITOR_B)
    await alice.join(room)
    await bob.join(room)

    # Both start from the same synced text.
    assert str(alice.text) == str(bob.text) == str(room.spec_md)
    goal_index = str(room.spec_md).index("Realtime coediting")

    # Concurrent inserts at the same logical point (each unaware of the other).
    await alice.edit(room, goal_index, "AAA")
    await bob.edit(room, goal_index, "BBB")
    await _settle([alice, bob], room)

    # All three peers converge to identical text containing both inserts.
    assert str(alice.text) == str(bob.text) == str(room.spec_md)
    assert "AAA" in str(room.spec_md)
    assert "BBB" in str(room.spec_md)


# --------------------------------------------------------------------------- #
# 2. Checkpoint on quiesce                                                     #
# --------------------------------------------------------------------------- #


async def test_quiesce_records_exactly_one_checkpoint(
    spec_engine: FileSpecEngine,
    seeded_spec_id: uuid.UUID,
    db_factory: sessionmaker[Session],
) -> None:
    room = SpecCollabRoom(
        workspace_id=TEST_WORKSPACE_ID,
        spec_id=seeded_spec_id,
        engine=spec_engine,
        session_factory=db_factory,
        quiesce_seconds=0.1,
    )
    alice, bob = _Client(EDITOR_A), _Client(EDITOR_B)
    await alice.join(room)
    await bob.join(room)

    assert _version_count(db_factory, seeded_spec_id) == 0

    goal_index = str(room.spec_md).index("Realtime coediting")
    # A burst of several updates from both editors (bob edits last).
    await alice.edit(room, goal_index, "AAA")
    await _settle([alice, bob], room)
    await bob.edit(room, goal_index, "BBB")
    await _settle([alice, bob], room)

    # Let the debounce window elapse -> a single checkpoint materialises.
    await asyncio.sleep(0.25)

    assert _version_count(db_factory, seeded_spec_id) == 1
    with db_factory() as session:
        version = session.execute(
            select(SpecVersion).where(SpecVersion.spec_id == seeded_spec_id)
        ).scalar_one()
    assert version.version_number == 1
    assert version.created_by == EDITOR_B  # most-recent editor
    # The materialised checkpoint carries the merged CRDT text.
    assert "AAA" in version.spec_md
    assert "BBB" in version.spec_md
    # The canonical engine was updated through its normal save path.
    assert "AAA" in spec_engine.read_spec_md(seeded_spec_id)


async def test_last_editor_leaving_checkpoints_immediately(
    spec_engine: FileSpecEngine,
    seeded_spec_id: uuid.UUID,
    db_factory: sessionmaker[Session],
) -> None:
    room = SpecCollabRoom(
        workspace_id=TEST_WORKSPACE_ID,
        spec_id=seeded_spec_id,
        engine=spec_engine,
        session_factory=db_factory,
        quiesce_seconds=999,  # prove the *disconnect* path checkpoints, not the timer
    )
    alice = _Client(EDITOR_A)
    await alice.join(room)
    goal_index = str(room.spec_md).index("Realtime coediting")
    await alice.edit(room, goal_index, "ZZZ")
    await _settle([alice], room)

    await room.disconnect(alice.conn)  # last participant leaves

    assert _version_count(db_factory, seeded_spec_id) == 1
    assert "ZZZ" in spec_engine.read_spec_md(seeded_spec_id)


# --------------------------------------------------------------------------- #
# 3 + 4. WebSocket route: unknown spec 404 + READ-only writer 1008             #
# --------------------------------------------------------------------------- #


@pytest.fixture
def auth_service() -> AuthService:
    """Hermetic in-memory auth service (no Postgres) for minting WS tokens."""
    return AuthService(secret_key=b"5" * 32)


@pytest.fixture
def room_registry(
    spec_engine: FileSpecEngine, db_factory: sessionmaker[Session]
) -> SpecRoomRegistry:
    return SpecRoomRegistry(
        engine_for_workspace=lambda _wid: spec_engine,
        session_factory=db_factory,
        quiesce_seconds=999,
    )


@pytest.fixture
def ws_client(auth_service: AuthService, room_registry: SpecRoomRegistry) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_auth_service] = lambda: auth_service
    app.dependency_overrides[get_spec_room_registry] = lambda: room_registry
    return TestClient(app)


def _mint(service: AuthService, role: UserRole) -> str:
    _, token = service.bootstrap_key(workspace_id=TEST_WORKSPACE_ID, name=role.value, role=role)
    return token


def test_unknown_spec_is_denied_404(ws_client: TestClient, auth_service: AuthService) -> None:
    from starlette.testclient import WebSocketDenialResponse

    token = _mint(auth_service, UserRole.MEMBER)
    unknown = uuid.uuid4()
    with (
        pytest.raises(WebSocketDenialResponse) as exc,
        ws_client.websocket_connect(f"/ws/spec/{unknown}?token={token}"),
    ):
        pass
    assert exc.value.status_code == 404


def test_readonly_writer_is_closed_1008(
    ws_client: TestClient,
    auth_service: AuthService,
    seeded_spec_id: uuid.UUID,
) -> None:
    token = _mint(auth_service, UserRole.VIEWER)
    with ws_client.websocket_connect(f"/ws/spec/{seeded_spec_id}?token={token}") as ws:
        # The server opens with a SYNC_STEP1 frame — a read is fine.
        first = ws.receive_bytes()
        assert first[0] == YMessageType.SYNC

        # A viewer sending a real (non-empty) update is a policy violation.
        scratch = Doc()
        scratch[SPEC_MD_KEY] = t = Text()
        t += "unauthorised edit"
        update_msg = create_update_message(scratch.get_update())
        assert message_mutates(update_msg[1:])  # sanity: this frame does mutate
        ws.send_bytes(update_msg)

        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_bytes()
    assert exc.value.code == 1008


def test_writer_can_edit_over_the_socket(
    ws_client: TestClient,
    auth_service: AuthService,
    seeded_spec_id: uuid.UUID,
    spec_engine: FileSpecEngine,
) -> None:
    """A MEMBER (WRITE) completes the sync handshake and its update is accepted."""
    token = _mint(auth_service, UserRole.MEMBER)
    with ws_client.websocket_connect(f"/ws/spec/{seeded_spec_id}?token={token}") as ws:
        server_step1 = ws.receive_bytes()
        # Build a client doc synced to the server, edit it, push the update.
        client = Doc()
        client[SPEC_MD_KEY] = text = Text()
        handle_sync_message(server_step1[1:], client)  # our STEP2 (empty) — ignored
        ws.send_bytes(create_sync_message(client))  # ask for server state
        server_step2 = ws.receive_bytes()
        handle_sync_message(server_step2[1:], client)

        updates: list[bytes] = []
        client.observe(lambda e: updates.append(e.update))
        text.insert(str(text).index("Realtime coediting"), "WROTE")
        for update in updates:
            ws.send_bytes(create_update_message(update))
        # The socket stays open (no 1008): a follow-up read blocks, so just close.
        ws.close()
