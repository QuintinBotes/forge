"""Fixtures for the F35 benchmark router/service tests.

In-memory SQLite (StaticPool, shared across the app worker thread), a tmp
benchmark root holding a small *frozen* suite (hash computed at fixture time so
it can never drift from the loader), and a :class:`BenchmarkService` injected
via dependency override. Role-parametrized clients mirror the marketplace/F31
harness.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.orm import Session, sessionmaker

from forge_api.deps import Principal, get_current_principal
from forge_api.main import create_app
from forge_api.observability.audit import AuditLog
from forge_api.routers.benchmarks import get_benchmark_service
from forge_api.routers.public_leaderboard import rate_limiter
from forge_api.services.benchmark_service import BenchmarkService
from forge_api.settings import Settings
from forge_contracts import UserRole
from forge_db.base import Base
from forge_eval.benchmark import (
    BenchmarkScore,
    compute_benchmark_score,
    compute_content_hash,
    load_manifest,
    make_bundle,
    replay_bundles,
)

WS_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
OTHER_WS_ID = uuid.UUID("00000000-0000-0000-0000-0000000000c3")
ADMIN_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b1")
MEMBER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b2")

SLUG = "fixture"
VERSION = "1.0.0"

#: case_id -> (expected ids, faithful outputs)
CASES: dict[str, tuple[list[str], list[str]]] = {
    "case-auth": (
        ["auth.py::refresh", "auth.py::expiry"],
        ["auth.py::refresh", "auth.py::expiry", "noise"],
    ),
    "case-board": (["board.py::cursor"], ["noise", "board.py::cursor"]),
    "case-task": (["REQ-1", "REQ-2"], ["REQ-1", "REQ-2"]),
}


def _write_suite(root: Path) -> None:
    suite_dir = root / SLUG / VERSION
    (suite_dir / "cases").mkdir(parents=True)
    case_payload = {
        "cases": [
            {
                "id": "case-auth",
                "query": "expired token?",
                "expected_ids": CASES["case-auth"][0],
                "kind": "retrieval",
                "tags": ["retrieval"],
            },
            {
                "id": "case-board",
                "query": "cursor pagination?",
                "expected_ids": CASES["case-board"][0],
                "kind": "retrieval",
                "tags": ["retrieval"],
            },
            {
                "id": "case-task",
                "query": "add pagination",
                "expected_ids": CASES["case-task"][0],
                "kind": "agent_task",
                "tags": ["agent_task"],
                "metadata": {"expected_terminal_state": "pr_opened"},
            },
        ]
    }
    (suite_dir / "cases" / "all.yaml").write_text(yaml.safe_dump(case_payload))
    manifest = {
        "slug": SLUG,
        "version": VERSION,
        "title": "Fixture benchmark",
        "description": "F35 API test suite",
        "schema_version": 1,
        "frozen": False,
        "scoring": {
            "primary_metric": "benchmark.composite",
            "metric_weights": {
                "retrieval.recall_at_k": 0.5,
                "retrieval.mrr": 0.3,
                "agent.requirement_satisfaction_rate": 0.2,
            },
            "category_field": "tags",
            "k": 5,
        },
        "case_files": ["cases/all.yaml"],
    }
    manifest_path = suite_dir / "manifest.yaml"
    manifest_path.write_text(yaml.safe_dump(manifest))
    loaded, cases = load_manifest(suite_dir)
    manifest["frozen"] = True
    manifest["content_hash"] = compute_content_hash(cases, loaded.scoring)
    manifest_path.write_text(yaml.safe_dump(manifest))


@pytest.fixture
def benchmark_root(tmp_path: Path) -> Path:
    root = tmp_path / "benchmarks"
    _write_suite(root)
    return root


@pytest.fixture
def session_factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    try:
        yield sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture
def audit_log() -> AuditLog:
    return AuditLog()


@pytest.fixture
def service(
    session_factory: sessionmaker[Session], benchmark_root: Path, audit_log: AuditLog
) -> BenchmarkService:
    svc = BenchmarkService(
        session_factory=session_factory,
        benchmark_root=benchmark_root,
        epsilon=0.005,
        max_submission_bytes=64_000,
        audit=audit_log,
    )
    svc.sync_suites_from_disk(published=True)
    return svc


@pytest.fixture
def test_settings() -> Settings:
    return Settings(
        public_leaderboard_enabled=True,
        leaderboard_public_rate_limit=1000,
        leaderboard_cache_ttl_seconds=60,
    )


def _principal(role: UserRole, workspace_id: uuid.UUID = WS_ID) -> Principal:
    user_ids = {UserRole.ADMIN: ADMIN_ID, UserRole.MEMBER: MEMBER_ID}
    return Principal(
        user_id=user_ids.get(role, uuid.uuid4()),
        workspace_id=workspace_id,
        role=role,
        email=f"{role.value}@forge.local",
        auth_method="test",
        scopes=["*"],
    )


@pytest.fixture
def make_client(
    service: BenchmarkService, test_settings: Settings
) -> Iterator[Callable[..., TestClient]]:
    clients: list[TestClient] = []

    def _make(
        role: UserRole | None = UserRole.ADMIN,
        workspace_id: uuid.UUID = WS_ID,
        settings: Settings | None = None,
    ) -> TestClient:
        app = create_app(settings or test_settings)
        app.dependency_overrides[get_benchmark_service] = lambda: service
        if role is not None:
            principal = _principal(role, workspace_id)
            app.dependency_overrides[get_current_principal] = lambda: principal
        client = TestClient(app)
        clients.append(client)
        return client

    rate_limiter.reset()
    try:
        yield _make
    finally:
        rate_limiter.reset()
        for client in clients:
            client.close()


def faithful_submission(bundle_outputs: dict[str, list[str]] | None = None) -> dict:
    """Build a submit payload whose claimed score matches its bundles exactly."""
    outputs = bundle_outputs or {cid: out for cid, (_exp, out) in CASES.items()}
    bundles = [make_bundle(cid, out) for cid, out in outputs.items()]
    # Recompute the claimed score the same way verification will.
    from forge_eval.benchmark import BenchmarkScoring
    from forge_eval.golden import GoldenCase

    scoring = BenchmarkScoring(
        metric_weights={
            "retrieval.recall_at_k": 0.5,
            "retrieval.mrr": 0.3,
            "agent.requirement_satisfaction_rate": 0.2,
        },
        k=5,
    )
    cases = [
        GoldenCase(
            id="case-auth",
            query="q",
            expected_ids=CASES["case-auth"][0],
            kind="retrieval",
            tags=["retrieval"],
        ),
        GoldenCase(
            id="case-board",
            query="q",
            expected_ids=CASES["case-board"][0],
            kind="retrieval",
            tags=["retrieval"],
        ),
        GoldenCase(
            id="case-task",
            query="q",
            expected_ids=CASES["case-task"][0],
            kind="agent_task",
            tags=["agent_task"],
            metadata={"expected_terminal_state": "pr_opened"},
        ),
    ]
    report = replay_bundles(bundles, cases, scoring)
    claimed: BenchmarkScore = compute_benchmark_score(report, scoring, cases)
    return {
        "submitter_name": "community-dev",
        "submitter_org": "OSS Collective",
        "submitter_contact": "secret-contact@example.com",
        "model_label": "claude-opus / anthropic",
        "agent_mode": "single_agent",
        "forge_version": "3.0.0",
        "config": {"provider": "anthropic", "api_key": "sk-ant-SECRETSECRET123"},
        "claimed": claimed.model_dump(mode="json"),
        "bundles": [b.model_dump(mode="json") for b in bundles],
    }
