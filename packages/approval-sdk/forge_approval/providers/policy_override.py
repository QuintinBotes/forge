"""The ``policy_override`` gate primitive (J5) + single-use grant.

An out-of-policy / ``requires_approval`` tool call is PAUSED, never executed;
this gate shows the exact attempted action, the blocking rules, and the agent's
rationale. Only an admin resolves it (enforced by :class:`ApprovalAuthorizer`).
On approve, the hook mints a single-use, short-TTL :class:`PolicyOverrideGrant`
bound to the exact action fingerprint; the paused call resumes ONCE via
``consume`` and the grant never broadens future scope (Build-Prompt #2).
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar, Protocol, runtime_checkable

from forge_approval.models import (
    ApprovalAction,
    ApprovalContext,
    ApprovalDecisionRequest,
    ApprovalRequest,
    GateType,
    PolicyOverrideGrant,
    Principal,
    ResolutionOutcome,
    RiskFlag,
)
from forge_approval.registry import default_actions

#: Domain signal name carried in the resolution outcome (NOT a bus event type).
POLICY_OVERRIDE_GRANTED_SIGNAL = "policy_override.granted"

#: Default grant TTL — short by design (spec: "single-use, short-TTL").
DEFAULT_GRANT_TTL = timedelta(minutes=15)


def action_fingerprint(tool_call: dict[str, Any]) -> str:
    """Stable SHA-256 fingerprint of the exact tool call being permitted."""
    canonical = json.dumps(tool_call, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@runtime_checkable
class PolicyOverrideGate(Protocol):
    """The frozen consumption contract (called by the F06/F29 resume path)."""

    async def consume(self, *, agent_run_id: uuid.UUID, action_fingerprint: str) -> bool:
        """Atomically check-and-consume a non-expired grant. ``True`` allows
        exactly this single call; ``False`` denies (no valid grant)."""
        ...


class InMemoryGrantStore:
    """Grant store honouring the single-active + single-use DB invariants."""

    def __init__(self) -> None:
        self._grants: list[PolicyOverrideGrant] = []
        self._lock = threading.Lock()

    def mint(self, grant: PolicyOverrideGrant) -> PolicyOverrideGrant:
        """Store a grant; at most one active per (agent_run_id, fingerprint)."""
        with self._lock:
            existing = self._find_active(
                grant.agent_run_id, grant.action_fingerprint, datetime.now(UTC)
            )
            if existing is not None:
                return existing.model_copy(deep=True)
            stored = grant.model_copy(deep=True)
            if stored.created_at is None:
                stored.created_at = datetime.now(UTC)
            self._grants.append(stored)
            return stored.model_copy(deep=True)

    async def consume(self, *, agent_run_id: uuid.UUID, action_fingerprint: str) -> bool:
        now = datetime.now(UTC)
        with self._lock:
            grant = self._find_active(agent_run_id, action_fingerprint, now)
            if grant is None:
                return False
            grant.consumed = True
            return True

    def all(self) -> list[PolicyOverrideGrant]:
        return [g.model_copy(deep=True) for g in self._grants]

    def _find_active(
        self, agent_run_id: uuid.UUID, fingerprint: str, now: datetime
    ) -> PolicyOverrideGrant | None:
        for grant in self._grants:
            if (
                grant.agent_run_id == agent_run_id
                and grant.action_fingerprint == fingerprint
                and not grant.consumed
                and grant.expires_at > now
            ):
                return grant
        return None


class PolicyOverrideGateProvider:
    """Central content: the attempted action, blocking rules, and severity."""

    gate_type: ClassVar[GateType] = GateType.POLICY_OVERRIDE

    async def build_context(
        self, request: ApprovalRequest, *, session: Any = None
    ) -> ApprovalContext:
        payload = dict(request.gate_payload)
        action = payload.get("action", {})
        blocked_by = payload.get("blocked_by", [])
        severity = payload.get("severity", "critical")
        if severity not in ("info", "warning", "critical"):
            severity = "critical"
        risk_flags = [
            RiskFlag(
                severity=severity,
                category="policy",
                message=f"blocked by policy rule '{rule}'",
                source="policy_engine",
            )
            for rule in blocked_by
        ] or [
            RiskFlag(
                severity=severity,
                category="policy",
                message="tool call requires an explicit policy override",
                source="policy_engine",
            )
        ]
        return ApprovalContext(
            approval_id=request.id,
            gate_type=request.gate_type,
            goal=request.title or "Out-of-policy tool call requires an admin override",
            confidence=payload.get("confidence"),
            risk_flags=risk_flags,
            run_trace_ref=(
                {"agent_run_id": str(request.agent_run_id)} if request.agent_run_id else None
            ),
            available_actions=self.available_actions(request),
            gate_payload={
                "action": action,
                "blocked_by": blocked_by,
                "severity": severity,
                "rationale": payload.get("rationale"),
                "action_fingerprint": payload.get("action_fingerprint"),
            },
        )

    def available_actions(self, request: ApprovalRequest) -> list[ApprovalAction]:
        return default_actions(request.gate_type)  # includes escalate


class PolicyOverrideResolutionHook:
    """Mints the single-use grant on approve; denial routes to a human."""

    gate_type: ClassVar[GateType] = GateType.POLICY_OVERRIDE

    def __init__(
        self, grants: InMemoryGrantStore, *, ttl: timedelta = DEFAULT_GRANT_TTL
    ) -> None:
        self._grants = grants
        self._ttl = ttl

    async def on_resolved(
        self,
        request: ApprovalRequest,
        decision: ApprovalDecisionRequest,
        actor: Principal,
        *,
        session: Any = None,
    ) -> ResolutionOutcome:
        if decision.decision is not ApprovalAction.APPROVE:
            return ResolutionOutcome(
                completed=True,
                follow_up_state="needs_human_input",
                details={"denied": True},
            )

        fingerprint = request.gate_payload.get("action_fingerprint")
        if request.agent_run_id is None or not isinstance(fingerprint, str) or not fingerprint:
            return ResolutionOutcome(
                completed=False,
                blocking_reasons=[
                    "override gate is missing agent_run_id or action_fingerprint;"
                    " no grant can be minted"
                ],
            )

        assert actor.id is not None  # authorizer guarantees a user principal
        grant = self._grants.mint(
            PolicyOverrideGrant(
                id=uuid.uuid4(),
                approval_request_id=request.id,
                agent_run_id=request.agent_run_id,
                action_fingerprint=fingerprint,
                granted_by=actor.id,
                expires_at=datetime.now(UTC) + self._ttl,
            )
        )
        return ResolutionOutcome(
            completed=True,
            follow_up_state="resume_once",
            details={
                "signal": POLICY_OVERRIDE_GRANTED_SIGNAL,
                "grant_id": str(grant.id),
                "action_fingerprint": fingerprint,
                "expires_at": grant.expires_at.isoformat(),
                "single_use": True,
            },
        )


__all__ = [
    "DEFAULT_GRANT_TTL",
    "POLICY_OVERRIDE_GRANTED_SIGNAL",
    "InMemoryGrantStore",
    "PolicyOverrideGate",
    "PolicyOverrideGateProvider",
    "PolicyOverrideResolutionHook",
    "action_fingerprint",
]
