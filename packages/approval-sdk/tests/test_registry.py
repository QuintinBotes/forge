"""Unit tests — GateRegistry provider/hook lookup (F36 §7)."""

from __future__ import annotations

import pytest
from conftest import RecordingProvider

from forge_approval.models import ApprovalAction, GateType
from forge_approval.registry import (
    GateRegistry,
    MissingProviderError,
    default_actions,
)


def test_missing_provider_raises() -> None:
    registry = GateRegistry()
    with pytest.raises(MissingProviderError):
        registry.provider(GateType.PR)
    assert not registry.has_provider(GateType.PR)


def test_register_and_lookup_provider() -> None:
    registry = GateRegistry()
    provider = RecordingProvider(GateType.DEPLOY)
    registry.register_provider(provider)
    assert registry.provider(GateType.DEPLOY) is provider
    assert registry.has_provider(GateType.DEPLOY)


def test_emit_only_gate_has_no_hook() -> None:
    registry = GateRegistry()
    assert registry.hook(GateType.SPEC) is None


def test_default_actions_gate_correct() -> None:
    for gate_type in (GateType.SPEC, GateType.PLAN, GateType.PR, GateType.DEPLOY):
        assert ApprovalAction.ESCALATE not in default_actions(gate_type)
    for gate_type in (GateType.INCIDENT_REMEDIATION, GateType.POLICY_OVERRIDE):
        assert ApprovalAction.ESCALATE in default_actions(gate_type)
