"""Forge CLI — Temporal operator commands (F25).

``forge-cli temporal bootstrap`` registers the deployment namespace with the
configured retention (idempotent: re-running succeeds cleanly). ``forge-cli
temporal replay <workflow_id>`` downloads a workflow's history and runs the
``Replayer`` for determinism debugging.

The ``temporal`` / ``temporal_visibility`` *databases* are created by the
auto-setup image (compose ``temporal`` service); this command only owns the
namespace + retention (the part Forge controls at runtime).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import timedelta
from typing import TYPE_CHECKING

from forge_workflow.temporal.config import TemporalSettings

if TYPE_CHECKING:
    from temporalio.client import Client


async def bootstrap_namespace(client: Client, namespace: str, retention_days: int) -> bool:
    """Register ``namespace`` with ``retention_days`` retention, idempotently.

    Returns ``True`` if it was newly created, ``False`` if it already existed.
    """
    from temporalio.api.workflowservice.v1 import RegisterNamespaceRequest
    from temporalio.service import RPCError, RPCStatusCode

    request = RegisterNamespaceRequest(
        namespace=namespace,
        workflow_execution_retention_period=timedelta(days=retention_days),
    )
    try:
        await client.service_client.workflow_service.register_namespace(request)
        return True
    except RPCError as exc:
        if exc.status == RPCStatusCode.ALREADY_EXISTS:
            return False  # idempotent re-run
        raise


async def _bootstrap(settings: TemporalSettings) -> int:
    from forge_workflow.temporal.client import get_temporal_client

    # Connect to the cluster's default namespace to issue the registration.
    client = await get_temporal_client(
        TemporalSettings(**{**settings.model_dump(), "temporal_namespace": "default"})
    )
    created = await bootstrap_namespace(
        client, settings.temporal_namespace, settings.temporal_retention_days
    )
    verb = "registered" if created else "already present"
    print(
        f"temporal namespace {settings.temporal_namespace!r} {verb} "
        f"(retention {settings.temporal_retention_days}d)"
    )
    return 0


async def _replay(settings: TemporalSettings, workflow_id: str) -> int:
    from temporalio.worker import Replayer

    from forge_workflow.temporal.client import build_data_converter, get_temporal_client
    from forge_workflow.temporal.worker import feature_workflow_runner
    from forge_workflow.temporal.workflows import FeatureWorkflow

    client = await get_temporal_client(settings)
    history = await client.get_workflow_handle(workflow_id).fetch_history()
    replayer = Replayer(
        workflows=[FeatureWorkflow],
        workflow_runner=feature_workflow_runner(),
        data_converter=build_data_converter(settings.temporal_codec_key),
    )
    await replayer.replay_workflow(history)
    print(f"replayed {workflow_id!r} with no non-determinism")
    return 0


def _sprint_session_factory():
    from forge_db import create_db_engine, create_session_factory, get_database_url

    return create_session_factory(create_db_engine(get_database_url()))


def _sprint_reconcile(sprint_id: str) -> int:
    """Rebuild a sprint's velocity rollup + burndown series from the event log."""
    import uuid

    from forge_board.sprint_service import SprintNotFound, SprintService

    service = SprintService(_sprint_session_factory())
    try:
        service.reconcile(sprint_id=uuid.UUID(sprint_id))
    except SprintNotFound:
        print(f"sprint {sprint_id!r} not found")
        return 1
    print(f"reconciled sprint {sprint_id!r}")
    return 0


def _sprint_velocity(project_id: str, *, as_json: bool) -> int:
    """Print a project's velocity dashboard."""
    import uuid

    from forge_board.sprint_service import SprintService
    from forge_db.models import Project

    factory = _sprint_session_factory()
    with factory() as session:
        project = session.get(Project, uuid.UUID(project_id))
        if project is None:
            print(f"project {project_id!r} not found")
            return 1
        workspace_id = project.workspace_id
    dashboard = SprintService(factory).velocity_dashboard(
        workspace_id=workspace_id, project_id=uuid.UUID(project_id), last=26
    )
    if as_json:
        print(dashboard.model_dump_json(indent=2))
        return 0
    summary = dashboard.summary
    print(f"velocity for project {project_id} ({summary.sprint_count} completed sprints)")
    for bar in dashboard.sprints:
        print(
            f"  {bar.name}: committed {bar.committed_points} / "
            f"completed {bar.completed_points} (predictability {bar.predictability})"
        )
    print(
        f"average={summary.average_velocity} rolling3={summary.rolling_3_velocity} "
        f"forecast={summary.forecast_low}/{summary.forecast_avg}/{summary.forecast_high}"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="forge-cli")
    sub = parser.add_subparsers(dest="group", required=True)
    temporal = sub.add_parser("temporal", help="Temporal operator commands")
    tsub = temporal.add_subparsers(dest="command", required=True)
    tsub.add_parser("bootstrap", help="register the namespace + retention (idempotent)")
    replay = tsub.add_parser("replay", help="replay a workflow's history (determinism)")
    replay.add_argument("workflow_id")

    sprint = sub.add_parser("sprint", help="Sprint velocity operator commands (F26)")
    ssub = sprint.add_subparsers(dest="command", required=True)
    reconcile = ssub.add_parser("reconcile", help="rebuild a sprint's rollup + burndown")
    reconcile.add_argument("sprint_id")
    velocity = ssub.add_parser("velocity", help="print a project's velocity dashboard")
    velocity.add_argument("project_id")
    velocity.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.group == "temporal":
        settings = TemporalSettings()
        if args.command == "bootstrap":
            return asyncio.run(_bootstrap(settings))
        if args.command == "replay":
            return asyncio.run(_replay(settings, args.workflow_id))
    if args.group == "sprint":
        if args.command == "reconcile":
            return _sprint_reconcile(args.sprint_id)
        if args.command == "velocity":
            return _sprint_velocity(args.project_id, as_json=args.as_json)
    return 2  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
