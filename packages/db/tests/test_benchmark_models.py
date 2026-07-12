"""Unit tests for the F35 benchmark models (AC22).

SQLite (in-memory) exercises ``Base.metadata.create_all`` — the same path the
0018 migration drives — asserting the two tables, the ``(slug, version)``
uniqueness guard, the leaderboard covering index, and the status/visibility
CHECK constraints.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from forge_db.base import Base
from forge_db.models import BenchmarkSubmission, BenchmarkSuite, Workspace


@pytest.fixture
def engine():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def _suite(**overrides) -> BenchmarkSuite:
    defaults = {
        "slug": "forge-swe",
        "version": "1.0.0",
        "title": "Forge SWE Benchmark v1",
        "task_count": 5,
        "scoring": {"metric_weights": {"retrieval.recall_at_k": 1.0}},
        "content_hash": "sha256:" + "a" * 64,
        "frozen": True,
    }
    defaults.update(overrides)
    return BenchmarkSuite(**defaults)


def test_create_all_produces_tables_and_leaderboard_index(engine) -> None:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert {"benchmark_suite", "benchmark_submission"} <= tables

    submission_indexes = {ix["name"] for ix in inspector.get_indexes("benchmark_submission")}
    assert "ix_benchmark_submission_leaderboard" in submission_indexes
    assert "ix_benchmark_submission_workspace_submitted" in submission_indexes

    suite_indexes = {ix["name"] for ix in inspector.get_indexes("benchmark_suite")}
    assert "ix_benchmark_suite_workspace_id" in suite_indexes


def test_suite_defaults_to_global_unscoped(engine) -> None:
    """F41: pre-existing/global suites keep workspace_id NULL and private=False."""
    with Session(engine) as session:
        suite = _suite()
        session.add(suite)
        session.commit()

        row = session.get(BenchmarkSuite, suite.id)
        assert row is not None
        assert row.workspace_id is None
        assert row.repo_id is None
        assert row.private is False


def test_suite_can_be_scoped_private_self_eval(engine) -> None:
    """F41: a minted Self-Eval Gate suite is private and workspace/repo scoped."""
    with Session(engine) as session:
        ws = Workspace(name="Acme", slug="acme")
        session.add(ws)
        session.flush()

        suite = _suite(
            slug="self-eval-acme",
            workspace_id=ws.id,
            repo_id="github:acme/widgets",
            private=True,
        )
        session.add(suite)
        session.commit()

        row = session.get(BenchmarkSuite, suite.id)
        assert row is not None
        assert row.workspace_id == ws.id
        assert row.repo_id == "github:acme/widgets"
        assert row.private is True


def test_suite_workspace_fk_cascades_on_delete(engine) -> None:
    fks = {fk.parent.name: fk.ondelete for fk in BenchmarkSuite.__table__.foreign_keys}
    assert fks["workspace_id"] == "CASCADE"


def test_slug_version_unique(engine) -> None:
    with Session(engine) as session:
        session.add(_suite())
        session.commit()
        session.add(_suite(title="dup"))
        with pytest.raises(IntegrityError):
            session.commit()


def test_submission_roundtrip_and_defaults(engine) -> None:
    with Session(engine) as session:
        ws = Workspace(name="Acme", slug="acme")
        suite = _suite()
        session.add_all([ws, suite])
        session.flush()

        submission = BenchmarkSubmission(
            benchmark_suite_id=suite.id,
            suite_content_hash=suite.content_hash,
            workspace_id=ws.id,
            submitter_name="community-dev",
            model_label="claude-opus / anthropic",
        )
        session.add(submission)
        session.commit()

        row = session.get(BenchmarkSubmission, submission.id)
        assert row is not None
        assert row.status == "pending"
        assert row.visibility == "private"
        assert row.verified is False
        assert row.composite_score is None
        assert row.agent_mode == "single_agent"
        assert row.submitted_at is not None


def test_official_submission_allows_null_workspace(engine) -> None:
    with Session(engine) as session:
        suite = _suite()
        session.add(suite)
        session.flush()
        official = BenchmarkSubmission(
            benchmark_suite_id=suite.id,
            suite_content_hash=suite.content_hash,
            workspace_id=None,
            submitter_name="forge-official",
            model_label="claude-sonnet / anthropic",
        )
        session.add(official)
        session.commit()
        assert official.workspace_id is None


def test_status_check_constraint_rejects_unknown(engine) -> None:
    with Session(engine) as session:
        suite = _suite()
        session.add(suite)
        session.flush()
        bad = BenchmarkSubmission(
            benchmark_suite_id=suite.id,
            suite_content_hash=suite.content_hash,
            submitter_name="x",
            model_label="y",
            status="bogus",
        )
        session.add(bad)
        with pytest.raises(IntegrityError):
            session.commit()


def test_fk_delete_rules_declared() -> None:
    """Suite deletion cascades to submissions; moderator/submitter FKs SET NULL.

    (SQLite does not enforce FKs by default, so the delete rules are asserted on
    the schema metadata — the DDL Postgres receives via create_all/0018.)
    """
    fks = {fk.parent.name: fk.ondelete for fk in BenchmarkSubmission.__table__.foreign_keys}
    assert fks["benchmark_suite_id"] == "CASCADE"
    assert fks["workspace_id"] == "CASCADE"
    assert fks["moderated_by"] == "SET NULL"
    assert fks["submitted_by"] == "SET NULL"


def test_submission_uuid_key_generated(engine) -> None:
    with Session(engine) as session:
        suite = _suite()
        session.add(suite)
        session.flush()
        assert isinstance(suite.id, uuid.UUID)
