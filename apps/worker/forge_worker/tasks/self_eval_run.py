"""Worker task: run a workspace's private Self-Eval suite and record the baseline (A4).

The un-parked "self-eval run": drives A3's :class:`ProductionEvalRunner` over a
workspace's private per-repo suite and persists the result as the baseline the
Self-Eval Gate (A1) blocks regressions against. It runs in the worker (never a
request path) because a real run is minutes-long and agent-driven.

It is an honest no-op until an operator has provisioned the prerequisites — a
published private suite, its on-disk case dir (``FORGE_BENCHMARK_DIR``), a local
git clone of its source repo (``FORGE_SELF_EVAL_REPO_ROOT/<repo_id>``), and a
BYOK model — because the foundation ships no repo-clone manager. Every missing
piece resolves to ``None`` and the task returns a ``scored: false`` reason
rather than fabricating a score. The live branches below need that provisioned
infrastructure (real repo clone + BYOK provider) to run, so they are excluded
from coverage — only the no-op path is exercised offline.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from typing import Any

from forge_worker.celery_app import celery_app
from forge_worker.self_eval_run import (
    ProductionEvalRunner,
    SelfEvalSuiteHandle,
    execute_self_eval_run,
)

__all__ = ["self_eval_run_task"]

_NO_SUITE_REASON = (
    "no runnable private suite (needs a published private suite + "
    "FORGE_BENCHMARK_DIR + FORGE_SELF_EVAL_REPO_ROOT/<repo_id> clone)"
)


def _resolve_private_suite(workspace_id: uuid.UUID) -> SelfEvalSuiteHandle | None:
    """Resolve a runnable private-suite handle for a workspace, or ``None``.

    ``None`` (the offline default) whenever the provisioning env vars are unset;
    otherwise delegates to the DB-backed resolver.
    """
    repo_root = os.environ.get("FORGE_SELF_EVAL_REPO_ROOT")
    benchmark_root = os.environ.get("FORGE_BENCHMARK_DIR")
    if not repo_root or not benchmark_root:
        return None
    return _query_private_suite(workspace_id, repo_root, benchmark_root)  # pragma: no cover


def _query_private_suite(  # pragma: no cover - needs a provisioned suite + repo clone
    workspace_id: uuid.UUID, repo_root: str, benchmark_root: str
) -> SelfEvalSuiteHandle | None:
    from sqlalchemy import select

    from forge_api.db import get_session_factory
    from forge_db.models.benchmark import BenchmarkSuite

    with get_session_factory()() as session:
        suite = session.scalars(
            select(BenchmarkSuite)
            .where(
                BenchmarkSuite.workspace_id == workspace_id,
                BenchmarkSuite.private.is_(True),
                BenchmarkSuite.published.is_(True),
            )
            .order_by(BenchmarkSuite.version.desc())
        ).first()
        if suite is None or not suite.repo_id:
            return None
        version_dir = Path(benchmark_root) / suite.slug / suite.version
        repo_path = Path(repo_root) / suite.repo_id.replace(":", "_").replace("/", "_")
        if not version_dir.is_dir() or not (repo_path / ".git").is_dir():
            return None
        return SelfEvalSuiteHandle(suite.id, str(version_dir), str(repo_path))


def _model_client_for(_config: Any) -> Any | None:  # pragma: no cover - needs BYOK env
    """Resolve a BYOK model client from the environment, or ``None`` (offline)."""
    from forge_agent.providers import (
        ModelClientConfig,
        ModelClientUnavailable,
        build_model_client,
    )

    cfg = ModelClientConfig.from_env()
    if cfg is None:
        return None
    try:
        return build_model_client(cfg)
    except ModelClientUnavailable:
        return None


def _run_and_record(  # pragma: no cover - live run needs repo clone + BYOK model
    handle: SelfEvalSuiteHandle,
    workspace_id: uuid.UUID,
    proposed_config: dict[str, Any],
    recorded_by: str | None,
) -> dict[str, Any]:
    from forge_agent.sandbox import LocalSandboxProvider
    from forge_api.db import get_session_factory
    from forge_api.services.self_eval_service import SelfEvalService

    runner = ProductionEvalRunner(
        resolve_suite=lambda _ws: handle,
        model_client_for=_model_client_for,
        sandbox_provider=LocalSandboxProvider(),
    )
    service = SelfEvalService(session_factory=get_session_factory())
    scorecard = asyncio.run(
        execute_self_eval_run(
            workspace_id=workspace_id,
            proposed_config=proposed_config,
            benchmark_suite_id=handle.benchmark_suite_id,
            runner=runner,
            record_baseline=service.record_baseline,
            recorded_by=uuid.UUID(recorded_by) if recorded_by else None,
        )
    )
    if scorecard is None:
        return {"scored": False, "reason": "runner produced no score (offline / no cases)"}
    return {
        "scored": True,
        "resolution_rate": scorecard.resolution_rate,
        "resolved": scorecard.resolved,
        "total": scorecard.total,
    }


@celery_app.task(name="forge.self_eval.run", queue="self_eval")
def self_eval_run_task(
    workspace_id: str,
    proposed_config: dict[str, Any],
    recorded_by: str | None = None,
) -> dict[str, Any]:
    """Prod seam: run the private suite for a config and record its baseline."""
    ws = uuid.UUID(workspace_id)
    handle = _resolve_private_suite(ws)
    if handle is None:
        return {"scored": False, "reason": _NO_SUITE_REASON}
    return _run_and_record(handle, ws, proposed_config, recorded_by)  # pragma: no cover
