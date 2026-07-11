"""CoordinatorSettings env parsing + resume error path."""

from __future__ import annotations

import uuid

import pytest

from forge_coordinator import CoordinatorSettings, HumanResumeInput
from forge_coordinator.deps import CoordinatorDeps
from forge_coordinator.supervisor import Supervisor


def test_from_env_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MULTI_AGENT_ENABLED", "true")
    monkeypatch.setenv("MULTI_AGENT_MAX_PARALLEL_CAP", "6")
    monkeypatch.setenv("MULTI_AGENT_REVIEW_LOOP_BUDGET", "2")
    monkeypatch.setenv("MULTI_AGENT_FALLBACK_TO_SINGLE_AGENT", "yes")
    monkeypatch.setenv("MULTI_AGENT_CONFIDENCE_THRESHOLD", "0.6")
    s = CoordinatorSettings.from_env()
    assert s.enabled is True
    assert s.max_parallel_cap == 6
    assert s.review_loop_budget == 2
    assert s.fallback_to_single_agent is True
    assert s.confidence_threshold == 0.6


def test_from_env_defaults_and_bad_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MULTI_AGENT_ENABLED", raising=False)
    monkeypatch.setenv("MULTI_AGENT_MAX_PARALLEL_CAP", "not-an-int")
    s = CoordinatorSettings.from_env()
    assert s.enabled is False
    assert s.max_parallel_cap == 4  # falls back to default on a bad value


def test_resume_unknown_id_raises() -> None:
    sup = Supervisor(CoordinatorDeps(agent_factory=lambda _client: None))  # type: ignore[arg-type,misc]
    with pytest.raises(KeyError):
        sup.resume(uuid.uuid4(), HumanResumeInput(decision="approve"))
