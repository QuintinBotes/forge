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


async def bootstrap_namespace(
    client: Client, namespace: str, retention_days: int
) -> bool:
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
    print(f"temporal namespace {settings.temporal_namespace!r} {verb} "
          f"(retention {settings.temporal_retention_days}d)")
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="forge-cli")
    sub = parser.add_subparsers(dest="group", required=True)
    temporal = sub.add_parser("temporal", help="Temporal operator commands")
    tsub = temporal.add_subparsers(dest="command", required=True)
    tsub.add_parser("bootstrap", help="register the namespace + retention (idempotent)")
    replay = tsub.add_parser("replay", help="replay a workflow's history (determinism)")
    replay.add_argument("workflow_id")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = TemporalSettings()
    if args.group == "temporal" and args.command == "bootstrap":
        return asyncio.run(_bootstrap(settings))
    if args.group == "temporal" and args.command == "replay":
        return asyncio.run(_replay(settings, args.workflow_id))
    return 2  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
