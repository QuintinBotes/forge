"""SCF's factory records over its own MCP server (spec L seam 1, ADR-0015)."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.orm import Session, sessionmaker

from scf_api.db import get_db
from scf_api.deps import Principal, get_current_principal
from scf_api.main import create_app
from scf_api.observability.audit import AuditCategory, AuditLog
from scf_api.observability.redaction import redact_mapping
from scf_api.routers.agent import get_agent_store
from scf_api.routers.board import get_board_service
from scf_api.routers.mcp_server import get_mcp_server_audit_log
from scf_api.routers.spec import get_spec_engine
from scf_api.services.approval_service import build_gate_registry, get_approval_service
from scf_approval import (
    ApprovalAuthorizer,
    ApprovalService,
    InMemoryActivityBus,
    InMemoryApprovalRepository,
)
from scf_approval.models import (
    ApprovalAction,
    ApprovalDecisionRequest,
    GateType,
    Role,
)
from scf_approval.models import Principal as ApprovalPrincipal
from scf_approval.providers import InMemoryGrantStore
from scf_board import InMemoryBoardService
from scf_contracts import AgentRunResult, EpicDTO, RunStatus, UserRole
from scf_db.base import Base
from scf_db.models import Project, Workspace
from scf_mcp.transports.http import HttpMcpTransport
from scf_spec import FileSpecEngine, spec_id_for_key

WS = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
OTHER_WS = uuid.UUID("00000000-0000-0000-0000-0000000000a2")
FACTORY_TOOLS = {
    "spec_list",
    "spec_get",
    "board_items",
    "approvals_pending",
    "approval_precedents",
    "run_status",
    # Spec J: the context-assembly read tool (context_tools).
    "assemble_context",
    # Spec J §2.4.1 D-e: direct knowledge reads (knowledge_tools).
    "knowledge_search",
    "knowledge_document",
}
WORLD_TOOLS = {
    "world_search",
    "world_entity",
    "world_facts",
    "world_changes",
    "world_conflicts",
    "world_decision_drift",
    "world_propose",
}


class FakeRunStore:
    def __init__(self) -> None:
        self.runs: dict[tuple[uuid.UUID, uuid.UUID], AgentRunResult] = {}

    def get(self, run_id: uuid.UUID, *, workspace_id: uuid.UUID) -> AgentRunResult | None:
        return self.runs.get((run_id, workspace_id))


class World:
    """Shared state behind every client in one test."""

    def __init__(self, tmp_path: Path) -> None:
        engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        Base.metadata.create_all(engine)
        self.db: sessionmaker[Session] = sessionmaker(
            bind=engine, expire_on_commit=False, class_=Session
        )
        with self.db() as session:
            session.add_all(
                [
                    Workspace(id=WS, name="Acme", slug="acme"),
                    Workspace(id=OTHER_WS, name="Other", slug="other"),
                ]
            )
            session.commit()
        self.specs = {
            WS: FileSpecEngine(root=tmp_path / "ws"),
            OTHER_WS: FileSpecEngine(root=tmp_path / "other"),
        }
        self.boards = {WS: InMemoryBoardService(), OTHER_WS: InMemoryBoardService()}
        self.approvals = ApprovalService(
            InMemoryApprovalRepository(),
            build_gate_registry(InMemoryGrantStore()),
            ApprovalAuthorizer(),
            events=InMemoryActivityBus(),
            redactor=redact_mapping,
        )
        self.runs = FakeRunStore()
        self.audit = AuditLog()

    def client(self, role: UserRole = UserRole.ADMIN, ws: uuid.UUID = WS) -> TestClient:
        app: FastAPI = create_app()
        principal = Principal(
            user_id=uuid.uuid4(), workspace_id=ws, role=role, auth_method="test", scopes=["*"]
        )

        def _get_db() -> Iterator[Session]:
            with self.db() as session:
                yield session

        app.dependency_overrides[get_current_principal] = lambda: principal
        app.dependency_overrides[get_db] = _get_db
        app.dependency_overrides[get_spec_engine] = lambda: self.specs[ws]
        app.dependency_overrides[get_board_service] = lambda: self.boards[ws]
        app.dependency_overrides[get_approval_service] = lambda: self.approvals
        app.dependency_overrides[get_agent_store] = lambda: self.runs
        app.dependency_overrides[get_mcp_server_audit_log] = lambda: self.audit
        return TestClient(app)


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def _mcp(client: TestClient) -> HttpMcpTransport:
    return HttpMcpTransport("http://testserver/mcp", client=client)


def _structured(result: dict) -> dict:
    assert result.get("isError") is not True, result
    body = result["structuredContent"]
    assert json.loads(result["content"][0]["text"]) == body
    return body


def _provenance(body: dict) -> dict:
    assert body["observed_by"] == "forge"
    assert body["enforceable"] is False
    assert body["recorded_at"]
    return body["result"]


@pytest.mark.parametrize("role", [UserRole.MEMBER, UserRole.VIEWER])
def test_every_reader_sees_the_five_factory_tools(world: World, role: UserRole) -> None:
    with _mcp(world.client(role)) as mcp:
        names = {tool.name for tool in mcp.list_tools()}
    expected = WORLD_TOOLS | FACTORY_TOOLS
    if role is UserRole.VIEWER:
        expected -= {"world_propose"}
    assert names == expected


def test_spec_list_and_get_return_the_projects_specs(world: World) -> None:
    client = world.client()
    project_id = uuid.uuid4()
    with world.db() as session:
        session.add(Project(id=project_id, workspace_id=WS, name="Project", key="PROJ-1"))
        session.commit()
    created = client.post(
        "/spec/specs",
        json={
            "epic_id": str(uuid.uuid4()),
            "name": "Customer search",
            "requirements": [{"id": "R1", "text": "Search customers by name"}],
        },
    )
    assert created.status_code == 201, created.text
    key = created.json()["id"]
    # A spec joins a project through an epic carrying its uuid (as in the REST tests).
    world.boards[WS].create_epic(
        EpicDTO(project_id=project_id, title="Search", spec_id=spec_id_for_key(key))
    )

    with _mcp(client) as mcp:
        manifest = _provenance(_structured(mcp.call_tool("spec_get", {"spec_id": key})))
        assert manifest["id"] == key
        assert manifest["name"] == "Customer search"

        dashboard = _provenance(
            _structured(mcp.call_tool("spec_list", {"project_id": str(project_id)}))
        )
        assert dashboard["project_id"] == str(project_id)
        assert [spec["id"] for spec in dashboard["specs"]] == [key]


def test_board_items_returns_tasks_and_respects_limit(world: World) -> None:
    client = world.client()
    for title in ("one", "two", "three"):
        resp = client.post("/board/tasks", json={"title": title, "kind": "feature"})
        assert resp.status_code in (200, 201), resp.text
    with _mcp(client) as mcp:
        items = _provenance(_structured(mcp.call_tool("board_items", {"limit": 2})))["items"]
    assert len(items) == 2
    assert {item["title"] for item in items} <= {"one", "two", "three"}


def test_approvals_pending_lists_only_pending_requests(world: World) -> None:
    client = world.client()
    resp = client.post(
        "/approvals",
        json={
            "gate_type": "pr",
            "subject_type": "changeset",
            "subject_id": str(uuid.uuid4()),
            "requested_actor": f"agent:{uuid.uuid4()}",
            "title": "Merge the search PR",
        },
    )
    assert resp.status_code == 201, resp.text
    with _mcp(client) as mcp:
        items = _provenance(_structured(mcp.call_tool("approvals_pending", {})))["items"]
    assert [item["title"] for item in items] == ["Merge the search PR"]
    assert all(item["status"] == "pending" for item in items)


def _decided_hold(world: World, ws: uuid.UUID, action_type: str, note: str | None) -> uuid.UUID:
    """A held action a person rejected (rejecting runs nothing, so no store is needed)."""

    async def _go() -> uuid.UUID:
        request = await world.approvals.create(
            workspace_id=ws,
            gate_type=GateType.AUTOMATION_ACTION,
            subject_type="automation_action",
            subject_id=uuid.uuid4(),
            gate_payload={"action_type": action_type},
        )
        await world.approvals.resolve(
            request.id,
            ApprovalDecisionRequest(decision=ApprovalAction.REJECT, note=note),
            ApprovalPrincipal(kind="user", id=uuid.uuid4(), role=Role.ADMIN, workspace_id=ws),
            workspace_id=ws,
        )
        return request.id

    return asyncio.run(_go())


@pytest.mark.parametrize("role", [UserRole.VIEWER, UserRole.AGENT_RUNNER])
def test_approval_precedents_let_an_agent_cite_a_decision_and_its_reason(
    world: World, role: UserRole
) -> None:
    quoted = _decided_hold(world, WS, "webhook_post", "the relay is being replaced")
    silent = _decided_hold(world, WS, "webhook_post", None)
    _decided_hold(world, WS, "trigger_deploy", "another kind")
    _decided_hold(world, OTHER_WS, "webhook_post", "another tenant")

    with _mcp(world.client(role)) as mcp:
        args = {"action_type": "webhook_post"}
        items = _provenance(_structured(mcp.call_tool("approval_precedents", args)))["items"]

    assert [(i["approval_id"], i["reason"]) for i in items] == [
        (str(silent), None),
        (str(quoted), "the relay is being replaced"),
    ]
    assert all(i["decision_id"] and i["decision"] == "reject" for i in items)
    assert all(i["decider_id"] and i["decided_at"] for i in items)


def test_run_status_summarises_a_run(world: World) -> None:
    run_id = uuid.uuid4()
    world.runs.runs[(run_id, WS)] = AgentRunResult(
        run_id=run_id,
        status=RunStatus.SUCCEEDED,
        summary="Added customer search",
        confidence=0.9,
        artifacts={"pr_url": "https://github.com/acme/widgets/pull/7"},
    )
    with _mcp(world.client(UserRole.VIEWER)) as mcp:
        status = _provenance(_structured(mcp.call_tool("run_status", {"run_id": str(run_id)})))
    assert status["status"] == RunStatus.SUCCEEDED.value
    assert status["summary"] == "Added customer search"
    assert status["artifacts"]["pr_url"].endswith("/pull/7")
    assert status["steps"] == 0


def test_foreign_and_unknown_ids_are_in_band_errors(world: World) -> None:
    run_id = uuid.uuid4()
    world.runs.runs[(run_id, WS)] = AgentRunResult(run_id=run_id, status=RunStatus.SUCCEEDED)
    with _mcp(world.client(ws=OTHER_WS)) as mcp:
        foreign = mcp.call_tool("run_status", {"run_id": str(run_id)})
        unknown = mcp.call_tool("run_status", {"run_id": str(uuid.uuid4())})
        project = mcp.call_tool("spec_list", {"project_id": str(uuid.uuid4())})
    assert foreign["isError"] is True
    assert foreign["structuredContent"]["error"] == f"no agent run {run_id}"
    assert unknown["isError"] is True
    assert project["isError"] is True


def test_factory_calls_are_audited_once_each(world: World) -> None:
    with _mcp(world.client()) as mcp:
        mcp.call_tool("board_items", {})
        mcp.call_tool("approvals_pending", {})
    entries = world.audit.query(category=AuditCategory.MCP_CALL)
    assert [e.target for e in entries] == ["board_items", "approvals_pending"]
