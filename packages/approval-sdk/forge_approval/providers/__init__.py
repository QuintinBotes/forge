"""F36-owned gate primitives: ``deploy`` and ``policy_override``.

These two gates have no other owning slice; their producers (F31 promotions,
F06/F29 policy interrupts) only ever call ``ApprovalService.create``.
"""

from __future__ import annotations

from forge_approval.providers.deploy import DeployGateProvider, DeployResolutionHook
from forge_approval.providers.policy_override import (
    GrantStore,
    InMemoryGrantStore,
    PolicyOverrideGate,
    PolicyOverrideGateProvider,
    PolicyOverrideResolutionHook,
    action_fingerprint,
)

__all__ = [
    "DeployGateProvider",
    "DeployResolutionHook",
    "GrantStore",
    "InMemoryGrantStore",
    "PolicyOverrideGate",
    "PolicyOverrideGateProvider",
    "PolicyOverrideResolutionHook",
    "action_fingerprint",
]
