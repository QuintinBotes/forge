"""F40-POL-GOVERNANCE — OOO approval delegation resolution."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from forge_approval import DelegationDirectory, DelegationEntry

ALICE = uuid.UUID("00000000-0000-0000-0000-0000000000d1")
BOB = uuid.UUID("00000000-0000-0000-0000-0000000000d2")
CAROL = uuid.UUID("00000000-0000-0000-0000-0000000000d3")

NOW = datetime(2026, 7, 8, 12, 0, tzinfo=UTC)


def test_no_delegation_resolves_to_self() -> None:
    directory = DelegationDirectory()
    assert directory.resolve(ALICE, NOW) == ALICE


def test_active_delegation_routes_to_delegate() -> None:
    directory = DelegationDirectory(entries=[DelegationEntry(user_id=ALICE, delegate_id=BOB)])
    assert directory.resolve(ALICE, NOW) == BOB


def test_delegation_chain_is_followed() -> None:
    directory = DelegationDirectory(
        entries=[
            DelegationEntry(user_id=ALICE, delegate_id=BOB),
            DelegationEntry(user_id=BOB, delegate_id=CAROL),
        ]
    )
    assert directory.resolve(ALICE, NOW) == CAROL


def test_cycle_is_broken() -> None:
    directory = DelegationDirectory(
        entries=[
            DelegationEntry(user_id=ALICE, delegate_id=BOB),
            DelegationEntry(user_id=BOB, delegate_id=ALICE),
        ]
    )
    # Must not loop forever; resolves to the last distinct hop.
    assert directory.resolve(ALICE, NOW) == BOB


def test_delegation_outside_window_is_inactive() -> None:
    entry = DelegationEntry(
        user_id=ALICE,
        delegate_id=BOB,
        starts_at=NOW + timedelta(days=1),
        ends_at=NOW + timedelta(days=3),
    )
    directory = DelegationDirectory(entries=[entry])
    assert directory.resolve(ALICE, NOW) == ALICE  # window not yet open
    assert directory.resolve(ALICE, NOW + timedelta(days=2)) == BOB  # inside window
    assert directory.resolve(ALICE, NOW + timedelta(days=5)) == ALICE  # window closed
