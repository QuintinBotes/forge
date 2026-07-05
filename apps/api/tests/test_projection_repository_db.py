"""Postgres integration tests for :class:`SqlAlchemyProjectionRepository` (F23).

Exercises the DB-backed traceability-projection repository against a real
pgvector Postgres via the shared ``pg_engine`` fixture (root ``conftest.py``):
the full ``ProjectionRepository`` protocol end-to-end — a wholesale link-set
round-trip (every ``CriterionLinkRecord`` field), the monotonic
``projection_version`` bump (``SELECT ... FOR UPDATE`` upsert), a full
``SpecRollupRecord`` round-trip (coverage ratios, enum status, epic id,
timestamp), project-scoped ``list_rollups`` / ``list_links`` filtering + ordering,
the ``(spec_id)`` / ``(spec_id, criterion_ext_id)`` unique constraints, durability
across repository instances, byte-for-byte parity with the in-memory store, and
structural conformance to the same protocol the in-memory store implements. Skips
cleanly (parked) when no Postgres is reachable; runs under
``FORGE_TEST_DATABASE_URL`` (pgvector :5433) in the gate.

Each behaviour mirrors the in-memory contract, so both backends are proven to
satisfy the same protocol identically.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from forge_api.services.projection_repository_db import SqlAlchemyProjectionRepository
from forge_db.base import Base
from forge_db.models import (
    Project,
    SpecDocument,
    TraceabilitySpecRollup,
    Workspace,
)
from forge_spec import InMemoryProjectionRepository, ProjectionRepository
from forge_spec.dashboard_schemas import (
    CellStatus,
    CriterionLinkRecord,
    SpecRollupRecord,
    ValidationStatus,
)

pytestmark = [pytest.mark.postgres, pytest.mark.usefixtures("pg_engine")]

_PROTOCOL_METHODS = (
    "replace_spec_links",
    "upsert_rollup",
    "get_rollup",
    "get_projection_version",
    "list_rollups",
    "get_links",
    "list_links",
)

_VALIDATED_AT = datetime(2026, 2, 3, 4, 5, 6, tzinfo=UTC)


@pytest.fixture
def factory(pg_engine) -> Iterator[sessionmaker[Session]]:
    Base.metadata.create_all(pg_engine)
    try:
        yield sessionmaker(bind=pg_engine, expire_on_commit=False, class_=Session)
    finally:
        Base.metadata.drop_all(pg_engine)


@pytest.fixture
def repo(factory: sessionmaker[Session]) -> SqlAlchemyProjectionRepository:
    return SqlAlchemyProjectionRepository(factory)


class _Ids:
    """A seeded workspace with two projects, each carrying one spec_document."""

    def __init__(self) -> None:
        self.ws = uuid.uuid4()
        self.proj_a = uuid.uuid4()
        self.proj_b = uuid.uuid4()
        self.spec_a1 = uuid.uuid4()
        self.spec_a2 = uuid.uuid4()
        self.spec_b1 = uuid.uuid4()


@pytest.fixture
def ids(factory: sessionmaker[Session]) -> _Ids:
    seeded = _Ids()
    with factory() as session:
        session.add(
            Workspace(id=seeded.ws, name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
        )
        session.flush()
        for pid, key in ((seeded.proj_a, "PA"), (seeded.proj_b, "PB")):
            session.add(
                Project(id=pid, workspace_id=seeded.ws, name=f"Project {key}", key=key)
            )
        session.flush()
        specs = (
            (seeded.spec_a1, seeded.proj_a, "SPEC-A1"),
            (seeded.spec_a2, seeded.proj_a, "SPEC-A2"),
            (seeded.spec_b1, seeded.proj_b, "SPEC-B1"),
        )
        for sid, pid, key in specs:
            session.add(
                SpecDocument(
                    id=sid,
                    workspace_id=seeded.ws,
                    project_id=pid,
                    spec_key=key,
                    name=f"{key} name",
                )
            )
        session.commit()
    return seeded


def _link(
    ids: _Ids,
    *,
    spec_id: uuid.UUID,
    project_id: uuid.UUID,
    spec_key: str,
    criterion: str,
    status: CellStatus = CellStatus.VALIDATED,
    **kw,
) -> CriterionLinkRecord:
    base = {
        "workspace_id": str(ids.ws),
        "project_id": str(project_id),
        "spec_id": str(spec_id),
        "spec_key": spec_key,
        "criterion_ext_id": criterion,
        "criterion_text": f"{criterion} text",
        "status": status,
    }
    base.update(kw)
    return CriterionLinkRecord(**base)


def _lk(
    ids: _Ids, sid: uuid.UUID, pid: uuid.UUID, key: str, crit: str, **kw
) -> CriterionLinkRecord:
    """Positional shorthand for :func:`_link` (keeps list literals within width)."""
    return _link(ids, spec_id=sid, project_id=pid, spec_key=key, criterion=crit, **kw)


def _rollup(
    ids: _Ids,
    *,
    spec_id: uuid.UUID,
    project_id: uuid.UUID,
    spec_key: str,
    **kw,
) -> SpecRollupRecord:
    base = {
        "workspace_id": str(ids.ws),
        "project_id": str(project_id),
        "spec_id": str(spec_id),
        "spec_key": spec_key,
        "spec_name": f"{spec_key} name",
        "spec_status": "active",
        "total_requirements": 4,
        "covered_requirements": 3,
        "total_criteria": 4,
        "validated_criteria": 2,
        "failed_criteria": 1,
        "uncovered_criteria": 1,
        "claimed_criteria": 0,
        "stale_criteria": 0,
        "requirement_coverage": 0.75,
        "acceptance_criteria_coverage": 0.5,
        "validation_status": ValidationStatus.PARTIAL,
        "gap_count": 2,
    }
    base.update(kw)
    return SpecRollupRecord(**base)


# --------------------------------------------------------------------------- #
# Protocol conformance                                                        #
# --------------------------------------------------------------------------- #


def test_repo_satisfies_projection_repository_protocol(
    repo: SqlAlchemyProjectionRepository,
) -> None:
    for name in _PROTOCOL_METHODS:
        assert callable(getattr(repo, name)), name
    # A structural stand-in for the (non-runtime-checkable) Protocol: the DB repo
    # is assignable where a ``ProjectionRepository`` is expected.
    port: ProjectionRepository = repo
    assert port is repo


# --------------------------------------------------------------------------- #
# Links: wholesale round-trip + replace semantics                             #
# --------------------------------------------------------------------------- #


def test_replace_and_get_links_roundtrip(
    repo: SqlAlchemyProjectionRepository, ids: _Ids
) -> None:
    link = _link(
        ids,
        spec_id=ids.spec_a1,
        project_id=ids.proj_a,
        spec_key="SPEC-A1",
        criterion="A1",
        status=CellStatus.VALIDATED,
        requirement_ext_ids=["R1", "R3"],
        satisfied=True,
        test_refs=["t::one", "t::two"],
        diff_refs=["d1"],
        task_ids=["TASK-1"],
        pr_numbers=[7, 9],
        report_spec_version=4,
        current_spec_version=5,
        last_validated_at=_VALIDATED_AT,
    )
    repo.replace_spec_links(str(ids.spec_a1), [link])

    got = repo.get_links(str(ids.spec_a1))
    assert len(got) == 1
    assert got[0] == link


def test_replace_spec_links_is_wholesale(
    repo: SqlAlchemyProjectionRepository, ids: _Ids
) -> None:
    first = [
        _lk(ids, ids.spec_a1, ids.proj_a, "SPEC-A1", c)
        for c in ("A1", "A2")
    ]
    repo.replace_spec_links(str(ids.spec_a1), first)
    assert [link_.criterion_ext_id for link_ in repo.get_links(str(ids.spec_a1))] == ["A1", "A2"]

    # Wholesale replace: the old set is gone, only the new row remains.
    second = [
        _lk(ids, ids.spec_a1, ids.proj_a, "SPEC-A1", "A3")
    ]
    repo.replace_spec_links(str(ids.spec_a1), second)
    assert [link_.criterion_ext_id for link_ in repo.get_links(str(ids.spec_a1))] == ["A3"]


def test_replace_with_empty_clears_links(
    repo: SqlAlchemyProjectionRepository, ids: _Ids
) -> None:
    repo.replace_spec_links(
        str(ids.spec_a1),
        [_lk(ids, ids.spec_a1, ids.proj_a, "SPEC-A1", "A1")],
    )
    repo.replace_spec_links(str(ids.spec_a1), [])
    assert repo.get_links(str(ids.spec_a1)) == []


def test_get_links_ordered_by_criterion(
    repo: SqlAlchemyProjectionRepository, ids: _Ids
) -> None:
    unordered = [
        _lk(ids, ids.spec_a1, ids.proj_a, "SPEC-A1", c)
        for c in ("A3", "A1", "A2")
    ]
    repo.replace_spec_links(str(ids.spec_a1), unordered)
    assert [link_.criterion_ext_id for link_ in repo.get_links(str(ids.spec_a1))] == [
        "A1",
        "A2",
        "A3",
    ]


def test_get_links_absent_or_non_uuid(repo: SqlAlchemyProjectionRepository) -> None:
    assert repo.get_links(str(uuid.uuid4())) == []
    assert repo.get_links("not-a-uuid") == []


# --------------------------------------------------------------------------- #
# Rollup: round-trip + monotonic projection_version                           #
# --------------------------------------------------------------------------- #


def test_upsert_rollup_bumps_version_monotonically(
    repo: SqlAlchemyProjectionRepository, ids: _Ids
) -> None:
    rollup = _rollup(ids, spec_id=ids.spec_a1, project_id=ids.proj_a, spec_key="SPEC-A1")
    assert repo.get_projection_version(str(ids.spec_a1)) == 0

    assert repo.upsert_rollup(rollup) == 1
    assert repo.upsert_rollup(rollup) == 2
    assert repo.upsert_rollup(rollup) == 3
    assert repo.get_projection_version(str(ids.spec_a1)) == 3


def test_upsert_rollup_keeps_single_row(
    repo: SqlAlchemyProjectionRepository, ids: _Ids, factory: sessionmaker[Session]
) -> None:
    rollup = _rollup(ids, spec_id=ids.spec_a1, project_id=ids.proj_a, spec_key="SPEC-A1")
    repo.upsert_rollup(rollup)
    repo.upsert_rollup(rollup)
    with factory() as session:
        count = session.execute(
            select(func.count())
            .select_from(TraceabilitySpecRollup)
            .where(TraceabilitySpecRollup.spec_id == ids.spec_a1)
        ).scalar_one()
    assert count == 1


def test_rollup_full_field_roundtrip(
    repo: SqlAlchemyProjectionRepository, ids: _Ids
) -> None:
    epic_id = str(uuid.uuid4())
    rollup = _rollup(
        ids,
        spec_id=ids.spec_a1,
        project_id=ids.proj_a,
        spec_key="SPEC-A1",
        epic_id=epic_id,
        uncovered_requirement_ext_ids=["R2", "R4"],
        validation_status=ValidationStatus.FAILING,
        requirement_coverage=0.25,
        acceptance_criteria_coverage=1.0,
        last_validated_at=_VALIDATED_AT,
    )
    repo.upsert_rollup(rollup)

    got = repo.get_rollup(str(ids.spec_a1))
    assert got == rollup
    assert got is not None
    assert got.epic_id == epic_id
    assert got.validation_status is ValidationStatus.FAILING
    assert got.uncovered_requirement_ext_ids == ["R2", "R4"]


def test_get_rollup_and_version_absent(repo: SqlAlchemyProjectionRepository) -> None:
    assert repo.get_rollup(str(uuid.uuid4())) is None
    assert repo.get_rollup("not-a-uuid") is None
    assert repo.get_projection_version(str(uuid.uuid4())) == 0
    assert repo.get_projection_version("not-a-uuid") == 0


def test_upsert_updates_row_data(
    repo: SqlAlchemyProjectionRepository, ids: _Ids
) -> None:
    repo.upsert_rollup(
        _rollup(ids, spec_id=ids.spec_a1, project_id=ids.proj_a, spec_key="SPEC-A1", gap_count=2)
    )
    repo.upsert_rollup(
        _rollup(
            ids,
            spec_id=ids.spec_a1,
            project_id=ids.proj_a,
            spec_key="SPEC-A1",
            gap_count=0,
            validation_status=ValidationStatus.PASSING,
        )
    )
    got = repo.get_rollup(str(ids.spec_a1))
    assert got is not None
    assert got.gap_count == 0
    assert got.validation_status is ValidationStatus.PASSING


# --------------------------------------------------------------------------- #
# Project-scoped list reads: filtering + ordering                             #
# --------------------------------------------------------------------------- #


def test_list_rollups_filters_by_project_and_orders(
    repo: SqlAlchemyProjectionRepository, ids: _Ids
) -> None:
    # proj_a has two specs (seeded out of key order), proj_b has one.
    repo.upsert_rollup(
        _rollup(ids, spec_id=ids.spec_a2, project_id=ids.proj_a, spec_key="SPEC-A2")
    )
    repo.upsert_rollup(
        _rollup(ids, spec_id=ids.spec_a1, project_id=ids.proj_a, spec_key="SPEC-A1")
    )
    repo.upsert_rollup(
        _rollup(ids, spec_id=ids.spec_b1, project_id=ids.proj_b, spec_key="SPEC-B1")
    )

    rollups_a = repo.list_rollups(str(ids.proj_a))
    assert [r.spec_key for r in rollups_a] == ["SPEC-A1", "SPEC-A2"]  # ordered by spec_key
    assert {r.project_id for r in rollups_a} == {str(ids.proj_a)}

    rollups_b = repo.list_rollups(str(ids.proj_b))
    assert [r.spec_key for r in rollups_b] == ["SPEC-B1"]

    assert repo.list_rollups(str(uuid.uuid4())) == []
    assert repo.list_rollups("not-a-uuid") == []


def test_list_links_filters_by_project_and_orders(
    repo: SqlAlchemyProjectionRepository, ids: _Ids
) -> None:
    repo.replace_spec_links(
        str(ids.spec_a2),
        [_lk(ids, ids.spec_a2, ids.proj_a, "SPEC-A2", "B1")],
    )
    repo.replace_spec_links(
        str(ids.spec_a1),
        [
            _lk(ids, ids.spec_a1, ids.proj_a, "SPEC-A1", "A2"),
            _lk(ids, ids.spec_a1, ids.proj_a, "SPEC-A1", "A1"),
        ],
    )
    repo.replace_spec_links(
        str(ids.spec_b1),
        [_lk(ids, ids.spec_b1, ids.proj_b, "SPEC-B1", "A1")],
    )

    links_a = repo.list_links(str(ids.proj_a))
    # Ordered by (spec_key, criterion_ext_id); only proj_a's links.
    assert [(link_.spec_key, link_.criterion_ext_id) for link_ in links_a] == [
        ("SPEC-A1", "A1"),
        ("SPEC-A1", "A2"),
        ("SPEC-A2", "B1"),
    ]
    assert {link_.project_id for link_ in links_a} == {str(ids.proj_a)}

    assert [link_.spec_key for link_ in repo.list_links(str(ids.proj_b))] == ["SPEC-B1"]
    assert repo.list_links("not-a-uuid") == []


# --------------------------------------------------------------------------- #
# Durability + parity                                                         #
# --------------------------------------------------------------------------- #


def test_durable_across_repository_instances(
    factory: sessionmaker[Session], ids: _Ids
) -> None:
    writer = SqlAlchemyProjectionRepository(factory)
    writer.replace_spec_links(
        str(ids.spec_a1),
        [_lk(ids, ids.spec_a1, ids.proj_a, "SPEC-A1", "A1")],
    )
    version = writer.upsert_rollup(
        _rollup(ids, spec_id=ids.spec_a1, project_id=ids.proj_a, spec_key="SPEC-A1")
    )

    reader = SqlAlchemyProjectionRepository(factory)
    assert reader.get_projection_version(str(ids.spec_a1)) == version
    assert reader.get_rollup(str(ids.spec_a1)) is not None
    assert [link_.criterion_ext_id for link_ in reader.get_links(str(ids.spec_a1))] == ["A1"]


def test_parity_with_in_memory_store(
    repo: SqlAlchemyProjectionRepository, ids: _Ids
) -> None:
    mem: ProjectionRepository = InMemoryProjectionRepository()
    links = [
        _link(
            ids,
            spec_id=ids.spec_a1,
            project_id=ids.proj_a,
            spec_key="SPEC-A1",
            criterion="A1",
            satisfied=True,
            test_refs=["t::a"],
            pr_numbers=[3],
            last_validated_at=_VALIDATED_AT,
        ),
        _lk(ids, ids.spec_a1, ids.proj_a, "SPEC-A1", "A2"),
    ]
    rollup = _rollup(
        ids,
        spec_id=ids.spec_a1,
        project_id=ids.proj_a,
        spec_key="SPEC-A1",
        last_validated_at=_VALIDATED_AT,
    )
    for backend in (repo, mem):
        backend.replace_spec_links(str(ids.spec_a1), links)
        v1 = backend.upsert_rollup(rollup)
        v2 = backend.upsert_rollup(rollup)
        assert (v1, v2) == (1, 2)

    assert repo.get_projection_version(str(ids.spec_a1)) == mem.get_projection_version(
        str(ids.spec_a1)
    )
    assert repo.get_rollup(str(ids.spec_a1)) == mem.get_rollup(str(ids.spec_a1))
    assert sorted(repo.get_links(str(ids.spec_a1)), key=lambda link_: link_.criterion_ext_id) == (
        sorted(mem.get_links(str(ids.spec_a1)), key=lambda link_: link_.criterion_ext_id)
    )
    assert repo.list_rollups(str(ids.proj_a)) == mem.list_rollups(str(ids.proj_a))
