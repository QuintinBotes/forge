"""Wiring for the Self-Eval Gate at the config-change API layer (A1).

The gate (``forge_eval.sweval.SelfEvalGate``) refuses a config change that
regresses a workspace's private-suite resolution rate below the recorded
baseline. Here we build the gate the AO settings router consults, composing the
real baseline lookup (:class:`SelfEvalService`, A2) with an injected
``eval_runner``.

Why the default ``eval_runner`` is a no-op: running a workspace's private suite
is a minutes-long, agent-driven job (A3's ``ProductionEvalRunner``) that lives
in the worker and must not block an HTTP request — and ``apps/api`` cannot import
``forge_worker`` without creating an import cycle. So at config-change time the
API gate has no fresh scorecard for the *proposed* config and no-ops, while the
gate MECHANISM (baseline lookup, regression block, force override, audit) is
fully wired and exercised in tests by injecting a runner. Establishing/refreshing
a baseline is the worker-owned ``POST /ao/self-eval/runs`` path (A4).
"""

from __future__ import annotations

import uuid
from typing import Any

from forge_api.db import get_session_factory
from forge_api.services.self_eval_service import SelfEvalService
from forge_eval.sweval import SelfEvalGate, SelfEvalScorecard

__all__ = ["build_self_eval_gate", "get_self_eval_gate"]


async def _unavailable_runner(_workspace_id: uuid.UUID, _config: Any) -> SelfEvalScorecard | None:
    """Default eval runner: no inline live eval at the API layer (see module doc)."""
    return None


def build_self_eval_gate(
    *,
    eval_runner: Any | None = None,
    session_factory: Any | None = None,
) -> SelfEvalGate:
    """Build the Self-Eval Gate with the real baseline lookup + an eval runner."""
    service = SelfEvalService(session_factory=session_factory or get_session_factory())
    return SelfEvalGate(
        eval_runner=eval_runner or _unavailable_runner,
        baseline_for=service.workspace_baseline,
    )


def get_self_eval_gate() -> SelfEvalGate:
    """FastAPI dependency: the default (no inline-eval) Self-Eval Gate.

    Overridden in tests (and by a self-hoster that injects a real runner) via
    ``app.dependency_overrides[get_self_eval_gate]``.
    """
    return build_self_eval_gate()
