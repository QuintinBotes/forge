"""Human Approval System SDK (slice F36) — one gate primitive, six gate types.

The canonical Review & Approval Layer beneath the ``apps/api`` approvals router
and the ``apps/worker`` sweep tasks: domain models, the single server-side
authorization policy, the provider/hook registry, the approval service, and the
two gate primitives no other slice owns (``deploy``, ``policy_override`` with
its single-use grant). Pure domain — no FastAPI, no DB, no network.
"""

from __future__ import annotations

from forge_approval.audit import ApprovalAuditSink, NullAuditSink, RecordingAuditSink
from forge_approval.authorizer import (
    GATE_MIN_ROLE,
    ApprovalAuthorizer,
    AuthorizationError,
    DefaultPolicyReader,
    PolicyReader,
    quorum_met,
    required_approvals,
)
from forge_approval.codeowners import (
    CodeownersRule,
    CodeownersRuleset,
    parse_codeowners,
    required_owners_for_paths,
)
from forge_approval.delegation import DelegationDirectory, DelegationEntry
from forge_approval.escalation import (
    EscalationDecision,
    EscalationOutcome,
    SlaPolicy,
    route_escalation,
)
from forge_approval.events import (
    APPROVAL_REQUESTED_TOPIC,
    APPROVAL_RESOLVED_TOPIC,
    ActivityBus,
    ApprovalRequestedEvent,
    ApprovalResolvedEvent,
    InMemoryActivityBus,
)
from forge_approval.models import (
    ApprovalAction,
    ApprovalContext,
    ApprovalDecisionRecord,
    ApprovalDecisionRequest,
    ApprovalRequest,
    ApprovalResolution,
    ApprovalSummary,
    GateStatus,
    GateType,
    PolicyOverrideGrant,
    Principal,
    ResolutionOutcome,
    RiskFlag,
    Role,
)
from forge_approval.registry import (
    GateContextProvider,
    GateRegistry,
    GateResolutionHook,
    MissingProviderError,
    default_actions,
)
from forge_approval.repository import (
    AlreadyResolvedError,
    ApprovalNotFoundError,
    ApprovalRepository,
    DuplicateDecisionError,
    InMemoryApprovalRepository,
)
from forge_approval.requirements import GateRequirementResolver
from forge_approval.service import ApprovalService

__version__ = "0.1.0"

__all__ = [
    "APPROVAL_REQUESTED_TOPIC",
    "APPROVAL_RESOLVED_TOPIC",
    "GATE_MIN_ROLE",
    "ActivityBus",
    "AlreadyResolvedError",
    "ApprovalAction",
    "ApprovalAuditSink",
    "ApprovalAuthorizer",
    "ApprovalContext",
    "ApprovalDecisionRecord",
    "ApprovalDecisionRequest",
    "ApprovalNotFoundError",
    "ApprovalRepository",
    "ApprovalRequest",
    "ApprovalRequestedEvent",
    "ApprovalResolution",
    "ApprovalResolvedEvent",
    "ApprovalService",
    "ApprovalSummary",
    "AuthorizationError",
    "CodeownersRule",
    "CodeownersRuleset",
    "DefaultPolicyReader",
    "DelegationDirectory",
    "DelegationEntry",
    "DuplicateDecisionError",
    "EscalationDecision",
    "EscalationOutcome",
    "GateContextProvider",
    "GateRegistry",
    "GateRequirementResolver",
    "GateResolutionHook",
    "GateStatus",
    "GateType",
    "InMemoryActivityBus",
    "InMemoryApprovalRepository",
    "MissingProviderError",
    "NullAuditSink",
    "PolicyOverrideGrant",
    "PolicyReader",
    "Principal",
    "RecordingAuditSink",
    "ResolutionOutcome",
    "RiskFlag",
    "Role",
    "SlaPolicy",
    "default_actions",
    "parse_codeowners",
    "quorum_met",
    "required_approvals",
    "required_owners_for_paths",
    "route_escalation",
]
