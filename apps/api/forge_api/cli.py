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
    from sqlalchemy.orm import Session, sessionmaker
    from temporalio.client import Client

    from forge_policy import PolicyContext


async def bootstrap_namespace(client: Client, namespace: str, retention_days: int) -> bool:
    """Register ``namespace`` with ``retention_days`` retention, idempotently.

    Returns ``True`` if it was newly created, ``False`` if it already existed.
    """
    from google.protobuf.duration_pb2 import Duration
    from temporalio.api.workflowservice.v1 import RegisterNamespaceRequest
    from temporalio.service import RPCError, RPCStatusCode

    retention = Duration()
    retention.FromTimedelta(timedelta(days=retention_days))
    request = RegisterNamespaceRequest(
        namespace=namespace,
        workflow_execution_retention_period=retention,
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


def _sprint_session_factory() -> sessionmaker[Session]:
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


def _policy_context_from_args(args: argparse.Namespace) -> PolicyContext:
    """Build a :class:`PolicyContext` from the ``policy simulate`` CLI flags."""
    from datetime import datetime

    from forge_policy import PolicyContext

    now = None
    if args.now:
        now = datetime.fromisoformat(args.now.replace("Z", "+00:00"))
    return PolicyContext(
        branch=args.branch,
        base_branch=args.base_branch,
        environment=args.env,
        task_kind=args.task_kind,
        actor_role=args.actor_role,
        skill_profile=args.skill_profile,
        execution_mode=args.execution_mode,
        command=args.run_command,
        now=now,
    )


def _policy_simulate(args: argparse.Namespace) -> int:
    """Print the conditional decision + matched rules for one tool call."""
    from forge_contracts import ToolCall
    from forge_policy import ConditionalPolicyEvaluator, load_policy

    policy = load_policy(args.repo)
    arguments: dict[str, str] = {}
    if args.run_command:
        arguments["command"] = args.run_command
    if args.env:
        arguments["environment"] = args.env
    call = ToolCall(tool=args.action, action=args.action, path=args.path, arguments=arguments)
    decision = ConditionalPolicyEvaluator().evaluate_in_context(
        call, policy, _policy_context_from_args(args)
    )
    print(f"action={args.action} effect={decision.effect.value} severity={decision.severity}")
    print(f"  reason: {decision.reason}")
    if decision.base_effect is not None:
        print(f"  base_effect: {decision.base_effect.value}")
    if decision.requires_approval:
        print("  requires_approval: true")
    if decision.conditional_matches:
        print("  matched rules:")
        for match in decision.conditional_matches:
            print(f"    - {match.rule_id} ({match.effect.value}, {match.severity}): {match.reason}")
    return 0


def _policy_test(path: str) -> int:
    """Run a repo's ``.forge/policy.tests.yaml`` suite; exit 0 (pass) / 1 (fail)."""
    from forge_policy import load_policy, load_test_suite, run_policy_tests, suite_path_for
    from forge_policy.loader import resolve_policy_path

    policy = load_policy(path)
    suite_path = suite_path_for(resolve_policy_path(path))
    if not suite_path.is_file():
        print(f"no policy test suite found at {suite_path}")
        return 1
    report = run_policy_tests(policy, load_test_suite(suite_path))
    for failure in report.failures:
        print(f"FAIL {failure['name']}: expected={failure['expected']} actual={failure['actual']}")
    print(f"policy tests: {report.passed}/{report.total} passed")
    return 0 if report.ok else 1


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

    policy = sub.add_parser("policy", help="Conditional policy commands (F29)")
    psub = policy.add_subparsers(dest="command", required=True)
    simulate = psub.add_parser("simulate", help="simulate a tool-call decision in a context")
    simulate.add_argument("repo", help="repo directory or a policy .yaml file path")
    simulate.add_argument("--action", required=True, help="tool/action name (e.g. write_file)")
    simulate.add_argument("--path", default=None)
    simulate.add_argument("--command", default=None, dest="run_command")
    simulate.add_argument("--env", default=None, dest="env")
    simulate.add_argument("--branch", default=None)
    simulate.add_argument("--base-branch", default=None, dest="base_branch")
    simulate.add_argument("--task-kind", default=None, dest="task_kind")
    simulate.add_argument("--actor-role", default=None, dest="actor_role")
    simulate.add_argument("--skill-profile", default=None, dest="skill_profile")
    simulate.add_argument("--execution-mode", default=None, dest="execution_mode")
    simulate.add_argument("--now", default=None, help="UTC ISO-8601 eval clock")
    test = psub.add_parser("test", help="run .forge/policy.tests.yaml (exit 0/1)")
    test.add_argument("path", help="repo directory or a policy .yaml file path")
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
    if args.group == "policy":
        if args.command == "simulate":
            return _policy_simulate(args)
        if args.command == "test":
            return _policy_test(args.path)
    return 2  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
