"""Out-of-office (OOO) approval delegation (F40-POL-GOVERNANCE).

When a required approver is out of office, their pending gates must route to a
standing delegate rather than stall until the SLA expires them. This module holds
the delegation directory and the resolution that follows a delegation chain to the
first approver who is available at a given instant.

* a :class:`DelegationEntry` is active only within its ``[starts_at, ends_at)``
  window (open-ended when either bound is ``None``);
* :meth:`DelegationDirectory.resolve` follows the chain (A→B→C) until it reaches
  an available user, with cycle protection so a mutual OOO pair can never loop;
* resolution is pure and total (a dict lookup per hop, bounded by the member
  count), so it never blocks the beat.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

__all__ = [
    "DelegationDirectory",
    "DelegationEntry",
]


class DelegationEntry(BaseModel):
    """``user`` delegates approvals to ``delegate`` while OOO in the window."""

    user_id: UUID
    delegate_id: UUID
    starts_at: datetime | None = None
    ends_at: datetime | None = None

    def is_active(self, at: datetime) -> bool:
        """True if this delegation is in effect at instant ``at``."""
        if self.starts_at is not None and at < self.starts_at:
            return False
        return not (self.ends_at is not None and at >= self.ends_at)


class DelegationDirectory(BaseModel):
    """A workspace's OOO delegations, keyed by the delegating user."""

    entries: list[DelegationEntry] = Field(default_factory=list)

    def _active_delegate(self, user_id: UUID, at: datetime) -> UUID | None:
        for entry in self.entries:
            if entry.user_id == user_id and entry.is_active(at):
                return entry.delegate_id
        return None

    def resolve(self, user_id: UUID, at: datetime) -> UUID:
        """Follow the OOO chain from ``user_id`` to the first available approver.

        Returns ``user_id`` itself when they are not OOO. Cycle-safe: if the chain
        loops back to an already-visited user, resolution stops at the last
        distinct hop rather than looping forever.
        """
        current = user_id
        visited: set[UUID] = {current}
        while True:
            delegate = self._active_delegate(current, at)
            if delegate is None or delegate in visited:
                return current
            visited.add(delegate)
            current = delegate
