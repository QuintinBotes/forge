"""Unit tests — policy-override grants: single-use, TTL, fingerprint (AC#14)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from forge_approval.models import PolicyOverrideGrant
from forge_approval.providers.policy_override import (
    InMemoryGrantStore,
    action_fingerprint,
)

RUN_ID = uuid.uuid4()
FINGERPRINT = action_fingerprint({"tool": "shell", "action": "run", "arguments": {"cmd": "rm"}})


def _grant(expires_in: timedelta = timedelta(minutes=15)) -> PolicyOverrideGrant:
    return PolicyOverrideGrant(
        id=uuid.uuid4(),
        approval_request_id=uuid.uuid4(),
        agent_run_id=RUN_ID,
        action_fingerprint=FINGERPRINT,
        granted_by=uuid.uuid4(),
        expires_at=datetime.now(UTC) + expires_in,
    )


async def test_grant_single_use() -> None:
    """AC#14: consume returns True exactly once, then False."""
    store = InMemoryGrantStore()
    store.mint(_grant())
    assert await store.consume(agent_run_id=RUN_ID, action_fingerprint=FINGERPRINT) is True
    assert await store.consume(agent_run_id=RUN_ID, action_fingerprint=FINGERPRINT) is False


async def test_grant_expired_denies() -> None:
    store = InMemoryGrantStore()
    store.mint(_grant(expires_in=timedelta(minutes=-1)))
    assert await store.consume(agent_run_id=RUN_ID, action_fingerprint=FINGERPRINT) is False


async def test_fingerprint_mismatch_denies() -> None:
    store = InMemoryGrantStore()
    store.mint(_grant())
    other = action_fingerprint({"tool": "shell", "action": "run", "arguments": {"cmd": "ls"}})
    assert await store.consume(agent_run_id=RUN_ID, action_fingerprint=other) is False
    # ... and the original grant is still intact (mismatch consumed nothing).
    assert await store.consume(agent_run_id=RUN_ID, action_fingerprint=FINGERPRINT) is True


async def test_agent_run_mismatch_denies() -> None:
    store = InMemoryGrantStore()
    store.mint(_grant())
    assert await store.consume(agent_run_id=uuid.uuid4(), action_fingerprint=FINGERPRINT) is False


def test_mint_is_idempotent_while_active() -> None:
    """At most one active grant per (agent_run_id, fingerprint)."""
    store = InMemoryGrantStore()
    first = store.mint(_grant())
    second = store.mint(_grant())
    assert second.id == first.id
    assert len(store.all()) == 1


def test_fingerprint_is_stable_and_shape_sensitive() -> None:
    call = {"tool": "shell", "action": "run", "arguments": {"cmd": "rm -rf /tmp/x"}}
    assert action_fingerprint(call) == action_fingerprint(dict(call))
    changed = {**call, "arguments": {"cmd": "rm -rf /"}}
    assert action_fingerprint(call) != action_fingerprint(changed)
