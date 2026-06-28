"""Unit tests for the Forge core data model (Task 0.2).

These exercise the SHARED SUBSTRATE every later package consumes:
- every spec entity has a SQLAlchemy model,
- UUID PKs + ``created_at``/``updated_at`` everywhere,
- workspace scoping on tenant tables,
- the full metadata creates on SQLite (vector/tsvector columns degrade), and
- pgvector / tsvector columns compile to their Postgres types (guarded).

No live database is required: SQLite (in-memory) is the unit-test backend and
the Postgres-specific behaviour is verified through dialect compilation.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session, configure_mappers

import forge_db.models as models
from forge_db.base import Base
from forge_db.models import (
    EMBEDDING_DIM,
    APIKey,
    KnowledgeSource,
    RetrievalChunk,
    User,
    Workspace,
)
from forge_db.models.enums import ChunkType
from forge_db.models.knowledge import CHUNK_TYPE_WEIGHTS

# The full spec Core Data Model — base entities + the F17 incident-workflow
# tables (incident_alert / incident_event / remediation_plan / postmortem /
# postmortem_action_item) that extend the model.
EXPECTED_MODELS = [
    "Workspace",
    "User",
    "APIKey",
    "RepositoryConnection",
    "MCPConnection",
    "PolicyProfile",
    "SkillProfile",
    "KnowledgeSource",
    "RetrievalChunk",
    "Project",
    "Constitution",
    "Epic",
    "SpecDocument",
    "Task",
    "Incident",
    "Sprint",
    "Milestone",
    "WorkflowRun",
    "AgentRun",
    "ApprovalRequest",
    "SubAgentRun",
    # F17 incident-workflow tables.
    "IncidentAlert",
    "IncidentEvent",
    "RemediationPlan",
    "Postmortem",
    "PostmortemActionItem",
    # F18 external PM-adapter tables.
    "PMConnection",
    "PMTaskLink",
    "PMWebhookDelivery",
    # F19 container-sandboxing table.
    "SandboxInstance",
    # F20 MCP sync-and-index tables.
    "KnowledgeSyncRun",
    "MCPIndexedResource",
    # F21 automation tables.
    "AutomationRule",
    "AutomationExecution",
    # F22 multi-repo execution tables.
    "PRGroup",
    "AgentRepoWorkspace",
    # F26 sprint-velocity tables.
    "SprintScopeEvent",
    "SprintBurndownSnapshot",
    "SprintVelocity",
]

# Tables that are NOT the tenant root and therefore must carry a workspace FK.
# ``pm_webhook_delivery`` is an inbound idempotency/audit ledger that must
# survive connection (and hence workspace) deletion, so it is intentionally not
# workspace-scoped (mirrors F03's webhook-delivery table).
NON_WORKSPACE_SCOPED = {"workspace", "pm_webhook_delivery"}


@pytest.fixture
def sqlite_engine():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_every_spec_entity_is_a_model() -> None:
    for name in EXPECTED_MODELS:
        assert hasattr(models, name), f"missing model export: {name}"
        model = getattr(models, name)
        assert hasattr(model, "__tablename__"), f"{name} is not a mapped class"


def test_table_set_is_exactly_the_spec_model() -> None:
    table_to_model = {getattr(models, n).__tablename__: n for n in EXPECTED_MODELS}
    assert set(Base.metadata.tables) == set(table_to_model)


def test_uuid_primary_keys_everywhere() -> None:
    for name in EXPECTED_MODELS:
        table = getattr(models, name).__table__
        pk_cols = list(table.primary_key.columns)
        assert len(pk_cols) == 1, f"{name} must have a single-column PK"
        assert pk_cols[0].name == "id", f"{name} PK must be 'id'"


def test_timestamp_columns_everywhere() -> None:
    for name in EXPECTED_MODELS:
        table = getattr(models, name).__table__
        assert "created_at" in table.c, f"{name} missing created_at"
        assert "updated_at" in table.c, f"{name} missing updated_at"


def test_workspace_scoping_on_tenant_tables() -> None:
    for name in EXPECTED_MODELS:
        table = getattr(models, name).__table__
        if table.name in NON_WORKSPACE_SCOPED:
            assert "workspace_id" not in table.c
            continue
        assert "workspace_id" in table.c, f"{name} missing workspace_id"
        fks = {fk.column.table.name for fk in table.c.workspace_id.foreign_keys}
        assert "workspace" in fks, f"{name}.workspace_id must FK to workspace"


def test_relationships_configure() -> None:
    # Forces SQLAlchemy to resolve every relationship() string target.
    configure_mappers()


def test_create_all_and_roundtrip_on_sqlite(sqlite_engine) -> None:
    inspector = inspect(sqlite_engine)
    created = set(inspector.get_table_names())
    expected = {getattr(models, n).__tablename__ for n in EXPECTED_MODELS}
    assert expected <= created

    with Session(sqlite_engine) as session:
        ws = Workspace(name="Acme", slug="acme")
        session.add(ws)
        session.flush()

        user = User(workspace_id=ws.id, email="dev@acme.test", name="Dev", role="admin")
        key = APIKey(
            workspace_id=ws.id,
            name="anthropic",
            kind="model_provider",
            provider="anthropic",
            key_prefix="sk-ant",
            encrypted_secret=b"ciphertext",
        )
        source = KnowledgeSource(
            workspace_id=ws.id,
            kind="repo",
            name="api repo",
            uri="github.com/org/api",
        )
        session.add_all([user, key, source])
        session.flush()

        chunk = RetrievalChunk(
            workspace_id=ws.id,
            knowledge_source_id=source.id,
            chunk_type=ChunkType.CODE,
            content="def handler():\n    return 1",
            path="app/main.py",
            start_line=1,
            end_line=2,
            content_hash="abc123",
            embedding=[0.0] * EMBEDDING_DIM,
            tsv="handler",
        )
        session.add(chunk)
        session.commit()

        assert isinstance(ws.id, uuid.UUID)
        assert ws.created_at is not None
        loaded = session.scalars(select(RetrievalChunk)).one()
        assert loaded.knowledge_source_id == source.id
        assert loaded.content_hash == "abc123"


def test_embedding_dim_is_set() -> None:
    assert isinstance(EMBEDDING_DIM, int)
    assert EMBEDDING_DIM == 1536


def test_chunk_type_weights_match_spec() -> None:
    # Spec "Chunk Types and Priority Weights" table.
    assert CHUNK_TYPE_WEIGHTS[ChunkType.README] == 1.3
    assert CHUNK_TYPE_WEIGHTS[ChunkType.POLICY] == 1.5
    assert CHUNK_TYPE_WEIGHTS[ChunkType.SPEC] == 1.4
    assert CHUNK_TYPE_WEIGHTS[ChunkType.SUMMARY] == 1.2
    assert CHUNK_TYPE_WEIGHTS[ChunkType.MARKDOWN] == 1.0
    assert CHUNK_TYPE_WEIGHTS[ChunkType.CODE] == 1.0
    assert CHUNK_TYPE_WEIGHTS[ChunkType.MCP_RESOURCE] == 1.0
    # Every chunk type has a weight.
    for ct in ChunkType:
        assert ct in CHUNK_TYPE_WEIGHTS


def test_pgvector_and_tsvector_compile_for_postgres() -> None:
    pg = postgresql.dialect()
    embedding_col = RetrievalChunk.__table__.c.embedding
    tsv_col = RetrievalChunk.__table__.c.tsv

    embedding_sql = embedding_col.type.compile(dialect=pg).lower()
    assert "vector" in embedding_sql
    assert str(EMBEDDING_DIM) in embedding_sql

    tsv_sql = tsv_col.type.compile(dialect=pg).lower()
    assert "tsvector" in tsv_sql


def test_pgvector_and_tsvector_degrade_for_sqlite() -> None:
    sqlite = create_engine("sqlite://").dialect
    embedding_col = RetrievalChunk.__table__.c.embedding
    tsv_col = RetrievalChunk.__table__.c.tsv
    # Must NOT emit the Postgres-only type names on SQLite.
    assert "vector" not in embedding_col.type.compile(dialect=sqlite).lower()
    assert "tsvector" not in tsv_col.type.compile(dialect=sqlite).lower()


def test_apikey_repr_redacts_secret() -> None:
    key = APIKey(
        workspace_id=uuid.uuid4(),
        name="anthropic",
        kind="model_provider",
        provider="anthropic",
        key_prefix="sk-ant",
        encrypted_secret=b"super-secret-ciphertext",
    )
    rendered = repr(key)
    assert "super-secret-ciphertext" not in rendered
    assert "ciphertext" not in rendered
