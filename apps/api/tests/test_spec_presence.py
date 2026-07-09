"""Slice RT-4 — presence + cursors (Yjs awareness relay).

Four behaviours are pinned:

* **join broadcasts server-authoritative identity** — a co-editor's first
  awareness frame is relayed to the *other* participants with ``display_name``
  and ``color`` stamped from the authenticated principal (colour derived
  deterministically from ``user_id``), never from the wire;
* **leave removes it** — a disconnect broadcasts a null-state awareness frame
  and drops the peer from the room's presence view;
* **cursor relays** — a cursor/selection range rides through to co-editors;
* **spoof is overridden** — identity fields a client puts in its awareness state
  are discarded and replaced by the server's authoritative values.

Hermetic: drives :class:`SpecCollabRoom` directly with in-process fake
connections (mirrors ``test_spec_collab.py``); no Postgres, no live sockets.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from pycrdt import (
    Decoder,
    Encoder,
    YMessageType,
    create_awareness_message,
    read_message,
)
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.orm import Session, sessionmaker

from forge_api.realtime.spec_room import (
    SpecCollabRoom,
    color_for_user,
    display_name_for,
)
from forge_contracts import Requirement
from forge_db.base import Base
from forge_db.models import Workspace
from forge_spec import FileSpecEngine, spec_id_for_key

TEST_WORKSPACE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
EDITOR_A = uuid.UUID("00000000-0000-0000-0000-0000000000c1")
EDITOR_B = uuid.UUID("00000000-0000-0000-0000-0000000000c2")
EMAIL_A = "ada@acme.test"
EMAIL_B = "grace@acme.test"


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


@pytest.fixture
def room(
    spec_engine: FileSpecEngine,
    seeded_spec_id: uuid.UUID,
    db_factory: sessionmaker[Session],
) -> SpecCollabRoom:
    return SpecCollabRoom(
        workspace_id=TEST_WORKSPACE_ID,
        spec_id=seeded_spec_id,
        engine=spec_engine,
        session_factory=db_factory,
        quiesce_seconds=999,  # never auto-quiesce mid-test
    )


# --------------------------------------------------------------------------- #
# Awareness harness (drives the Yjs awareness wire format at the room)         #
# --------------------------------------------------------------------------- #


class _FakeConn:
    """A room participant that records the binary frames the room sends it."""

    def __init__(self) -> None:
        self.inbox: list[bytes] = []

    async def send_bytes(self, data: bytes) -> None:
        self.inbox.append(data)


def _awareness_frame(client_id: int, clock: int, state: dict | None) -> bytes:
    """Build a client -> server awareness frame (one client entry)."""
    encoder = Encoder()
    encoder.write_var_uint(1)
    encoder.write_var_uint(client_id)
    encoder.write_var_uint(clock)
    encoder.write_var_string(json.dumps(state))
    return create_awareness_message(encoder.to_bytes())


def _decode_presence_frames(inbox: list[bytes]) -> dict[int, dict | None]:
    """Merge every awareness frame in an inbox into a ``{client_id: state}`` map."""
    latest: dict[int, dict | None] = {}
    for frame in inbox:
        if not frame or frame[0] != YMessageType.AWARENESS:
            continue
        decoder = Decoder(read_message(frame[1:]))
        for _ in range(decoder.read_var_uint()):
            client_id = decoder.read_var_uint()
            decoder.read_var_uint()  # clock
            raw = decoder.read_var_string()
            state = json.loads(raw) if raw else None
            latest[client_id] = state if isinstance(state, dict) else None
    return latest


async def _settle() -> None:
    """Let the room's scheduled fan-out sends land on peer inboxes."""
    for _ in range(8):
        await asyncio.sleep(0)


# --------------------------------------------------------------------------- #
# 1. Join broadcasts server-authoritative identity                            #
# --------------------------------------------------------------------------- #


async def test_join_broadcasts_server_authoritative_identity(room: SpecCollabRoom) -> None:
    alice, bob = _FakeConn(), _FakeConn()
    await room.connect(alice)
    await room.connect(bob)
    bob.inbox.clear()  # ignore the sync handshake frames

    await room.receive(
        alice,
        _awareness_frame(101, 1, {"cursor": {"anchor": 0, "head": 0}}),
        can_write=True,
        user_id=EDITOR_A,
        email=EMAIL_A,
    )
    await _settle()

    # Bob (the co-editor) received Alice's presence with server-stamped identity.
    relayed = _decode_presence_frames(bob.inbox)
    assert 101 in relayed
    state = relayed[101]
    assert state is not None
    assert state["user_id"] == str(EDITOR_A)
    assert state["display_name"] == display_name_for(EDITOR_A, EMAIL_A)
    assert state["color"] == color_for_user(EDITOR_A)

    # The room's authoritative presence view agrees.
    presence = room.presence_states
    assert [p.user_id for p in presence] == [str(EDITOR_A)]
    assert presence[0].color == color_for_user(EDITOR_A)


async def test_join_is_not_echoed_to_origin(room: SpecCollabRoom) -> None:
    """Presence relays to co-editors only — the sender is not sent its own frame."""
    alice, bob = _FakeConn(), _FakeConn()
    await room.connect(alice)
    await room.connect(bob)
    alice.inbox.clear()
    bob.inbox.clear()

    await room.receive(
        alice, _awareness_frame(101, 1, {}), can_write=True, user_id=EDITOR_A, email=EMAIL_A
    )
    await _settle()

    assert _decode_presence_frames(alice.inbox) == {}
    assert 101 in _decode_presence_frames(bob.inbox)


# --------------------------------------------------------------------------- #
# 2. Leave removes presence                                                   #
# --------------------------------------------------------------------------- #


async def test_leave_removes_presence(room: SpecCollabRoom) -> None:
    alice, bob = _FakeConn(), _FakeConn()
    await room.connect(alice)
    await room.connect(bob)
    await room.receive(
        alice, _awareness_frame(101, 1, {}), can_write=True, user_id=EDITOR_A, email=EMAIL_A
    )
    await _settle()
    assert any(p.user_id == str(EDITOR_A) for p in room.presence_states)
    bob.inbox.clear()

    await room.disconnect(alice)
    await _settle()

    # Alice is gone from the room's view and Bob got a null-state leave frame.
    assert all(p.user_id != str(EDITOR_A) for p in room.presence_states)
    relayed = _decode_presence_frames(bob.inbox)
    assert relayed.get(101, "sentinel") is None


# --------------------------------------------------------------------------- #
# 3. Cursor / selection relays                                                #
# --------------------------------------------------------------------------- #


async def test_cursor_range_relays_to_co_editors(room: SpecCollabRoom) -> None:
    alice, bob = _FakeConn(), _FakeConn()
    await room.connect(alice)
    await room.connect(bob)
    bob.inbox.clear()

    await room.receive(
        alice,
        _awareness_frame(101, 1, {"cursor": {"anchor": 3, "head": 7}}),
        can_write=True,
        user_id=EDITOR_A,
        email=EMAIL_A,
    )
    await _settle()

    state = _decode_presence_frames(bob.inbox)[101]
    assert state is not None
    assert state["cursor"] == {"anchor": 3, "head": 7}

    presence = room.presence_states[0]
    assert presence.cursor is not None
    assert (presence.cursor.anchor, presence.cursor.head) == (3, 7)


# --------------------------------------------------------------------------- #
# 4. Spoofed identity is overridden by the server                             #
# --------------------------------------------------------------------------- #


async def test_spoofed_identity_is_overridden(room: SpecCollabRoom) -> None:
    alice, bob = _FakeConn(), _FakeConn()
    await room.connect(alice)
    await room.connect(bob)
    bob.inbox.clear()

    # Alice tries to impersonate Bob and paint herself a bespoke colour.
    await room.receive(
        alice,
        _awareness_frame(
            101,
            1,
            {
                "user_id": str(EDITOR_B),
                "display_name": "Totally Bob",
                "color": "#000000",
                "cursor": {"anchor": 1, "head": 2},
            },
        ),
        can_write=True,
        user_id=EDITOR_A,
        email=EMAIL_A,
    )
    await _settle()

    state = _decode_presence_frames(bob.inbox)[101]
    assert state is not None
    # Identity is Alice's server-authoritative values, not the spoofed ones.
    assert state["user_id"] == str(EDITOR_A)
    assert state["display_name"] == display_name_for(EDITOR_A, EMAIL_A)
    assert state["display_name"] != "Totally Bob"
    assert state["color"] == color_for_user(EDITOR_A)
    assert state["color"] != "#000000"
    # Only the (non-identity) cursor survived from the client.
    assert state["cursor"] == {"anchor": 1, "head": 2}


async def test_connection_cannot_overwrite_another_peers_slot(room: SpecCollabRoom) -> None:
    """A client_id owned by one connection can't be hijacked by another."""
    alice, bob, carol = _FakeConn(), _FakeConn(), _FakeConn()
    for conn in (alice, bob, carol):
        await room.connect(conn)

    # Alice registers client_id 101.
    await room.receive(
        alice, _awareness_frame(101, 1, {}), can_write=True, user_id=EDITOR_A, email=EMAIL_A
    )
    await _settle()
    carol.inbox.clear()

    # Bob tries to speak for Alice's client_id 101 — ignored, not relayed.
    await room.receive(
        bob, _awareness_frame(101, 2, {}), can_write=True, user_id=EDITOR_B, email=EMAIL_B
    )
    await _settle()

    assert _decode_presence_frames(carol.inbox) == {}
    # Alice's presence is untouched (still her identity).
    assert room.presence_states[0].user_id == str(EDITOR_A)


# --------------------------------------------------------------------------- #
# Identity derivation is stable + deterministic                               #
# --------------------------------------------------------------------------- #


def test_color_is_deterministic_and_stable() -> None:
    assert color_for_user(EDITOR_A) == color_for_user(EDITOR_A)
    assert color_for_user(EDITOR_A).startswith("#")


def test_display_name_prefers_email_local_part() -> None:
    assert display_name_for(EDITOR_A, EMAIL_A) == "ada"
    assert display_name_for(EDITOR_A, None) == f"user-{str(EDITOR_A)[:8]}"
