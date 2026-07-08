"""Spec engine router (Task 1.7 — spec-engine; wired in Phase 2 Task 2.1).

Covers the SDD lifecycle: constitution -> spec_create -> clarify -> plan ->
approve -> tasks -> validate, plus manifest read/write.

Handlers delegate to a process-wide filesystem-backed
:class:`~forge_spec.FileSpecEngine` rooted at ``Settings.spec_root``. The
``spec_id``/``task_id`` path params are the engine's deterministic uuids
(``forge_spec.spec_id_for_key`` / ``task_id_for``). Errors map to HTTP: an
unresolved spec/task uuid -> 404; a gate violation (e.g. generating tasks before
the spec is approved) -> 409.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from forge_api.auth.rbac import Permission
from forge_api.deps import DbSession, Principal, get_current_principal
from forge_api.routers._rbac import require_permission
from forge_api.routers.board import BoardServiceDep
from forge_api.settings import get_settings
from forge_contracts import (
    BoardFilter,
    Constitution,
    Requirement,
    SpecManifest,
    TaskDTO,
    ValidationReport,
)
from forge_contracts.exceptions import SpecGateError
from forge_db.models import Project
from forge_spec import FileSpecEngine, SpecNotFoundError

router = APIRouter(
    prefix="/spec",
    tags=["spec"],
    dependencies=[Depends(get_current_principal)],
)

# Authorization gates as route dependencies (the engine is already workspace
# scoped, so handlers need not capture the principal). Mutating the SDD lifecycle
# is WRITE; reading a manifest is READ. A read-only viewer is denied writes.
WriteGate = Depends(require_permission(Permission.WRITE))
ReadGate = Depends(require_permission(Permission.READ))


# --------------------------------------------------------------------------- #
# Per-workspace engine registry (tenant isolation)                             #
# --------------------------------------------------------------------------- #


class SpecEngineRegistry:
    """Vends a :class:`FileSpecEngine` rooted under a per-workspace subdirectory.

    The engine uses a single ``spec_root`` with deterministic spec ids, so two
    tenants creating the same epic/name would otherwise collide on disk and read
    each other's specs. Rooting each workspace at ``<spec_root>/<workspace_id>``
    gives real filesystem isolation between tenants.
    """

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self._engines: dict[uuid.UUID, FileSpecEngine] = {}

    def for_workspace(self, workspace_id: uuid.UUID) -> FileSpecEngine:
        engine = self._engines.get(workspace_id)
        if engine is None:
            engine = FileSpecEngine(root=self._root / str(workspace_id))
            self._engines[workspace_id] = engine
        return engine


@lru_cache(maxsize=1)
def _spec_registry_singleton() -> SpecEngineRegistry:
    return SpecEngineRegistry(get_settings().spec_root)


def get_spec_registry() -> SpecEngineRegistry:
    """Return the process-wide spec-engine registry (override in tests via DI)."""
    return _spec_registry_singleton()


def get_spec_engine(
    principal: Annotated[Principal, Depends(get_current_principal)],
    registry: Annotated[SpecEngineRegistry, Depends(get_spec_registry)],
) -> FileSpecEngine:
    """Return the spec engine for the caller's workspace (override in tests)."""
    return registry.for_workspace(principal.workspace_id)


EngineDep = Annotated[FileSpecEngine, Depends(get_spec_engine)]


# --------------------------------------------------------------------------- #
# Error mapping + request bodies                                              #
# --------------------------------------------------------------------------- #


@contextmanager
def _spec_errors() -> Iterator[None]:
    """Translate spec domain exceptions into HTTP error responses."""
    try:
        yield
    except SpecNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except SpecGateError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


class ConstitutionInitRequest(BaseModel):
    """Body for ``POST /spec/constitution``."""

    project_id: uuid.UUID
    principles: list[str] | None = None


class SpecCreateRequest(BaseModel):
    """Body for ``POST /spec/specs``."""

    epic_id: uuid.UUID
    name: str
    requirements: list[Requirement] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Routes                                                                      #
# --------------------------------------------------------------------------- #


@router.post(
    "/constitution",
    response_model=Constitution,
    status_code=status.HTTP_201_CREATED,
    dependencies=[WriteGate],
)
def constitution_init(engine: EngineDep, request: ConstitutionInitRequest) -> Constitution:
    """Initialise a project constitution."""
    return engine.constitution_init(request.project_id, request.principles)


@router.post(
    "/specs",
    response_model=SpecManifest,
    status_code=status.HTTP_201_CREATED,
    dependencies=[WriteGate],
)
def spec_create(engine: EngineDep, request: SpecCreateRequest) -> SpecManifest:
    """Create a draft spec for an epic."""
    return engine.spec_create(request.epic_id, request.name, request.requirements)


@router.get("/specs/{spec_id}", response_model=SpecManifest, dependencies=[ReadGate])
def read_manifest(engine: EngineDep, spec_id: uuid.UUID) -> SpecManifest:
    """Read a spec manifest by its deterministic uuid."""
    with _spec_errors():
        return engine.read_manifest(spec_id)


@router.put("/specs/{spec_id}", response_model=SpecManifest, dependencies=[WriteGate])
def write_manifest(engine: EngineDep, spec_id: uuid.UUID, manifest: SpecManifest) -> SpecManifest:
    """Persist (create or update) a spec manifest."""
    with _spec_errors():
        return engine.write_manifest(manifest)


@router.post("/specs/{spec_id}/clarify", response_model=SpecManifest, dependencies=[WriteGate])
def spec_clarify(engine: EngineDep, spec_id: uuid.UUID) -> SpecManifest:
    """Run the clarification pass."""
    with _spec_errors():
        return engine.spec_clarify(spec_id)


@router.post("/specs/{spec_id}/plan", response_model=SpecManifest, dependencies=[WriteGate])
def spec_plan(engine: EngineDep, spec_id: uuid.UUID) -> SpecManifest:
    """Generate the technical plan + ADRs."""
    with _spec_errors():
        return engine.spec_plan(spec_id)


@router.post("/specs/{spec_id}/approve", response_model=SpecManifest, dependencies=[WriteGate])
def approve_spec(engine: EngineDep, spec_id: uuid.UUID) -> SpecManifest:
    """Approve a spec (the human gate); moves it to ``approved``."""
    with _spec_errors():
        return engine.approve_spec(spec_id)


@router.post("/specs/{spec_id}/tasks", response_model=list[TaskDTO], dependencies=[WriteGate])
def spec_tasks(engine: EngineDep, spec_id: uuid.UUID) -> list[TaskDTO]:
    """Generate implementation tasks from an approved spec (gated)."""
    with _spec_errors():
        return engine.spec_tasks(spec_id)


@router.post(
    "/tasks/{task_id}/validate",
    response_model=ValidationReport,
    dependencies=[WriteGate],
)
def validate(engine: EngineDep, task_id: uuid.UUID) -> ValidationReport:
    """Validate a task against its spec (requirement-to-test traceability)."""
    with _spec_errors():
        return engine.validate(task_id)


# --------------------------------------------------------------------------- #
# F23 spec-validation dashboard: GET /projects/{project_id}/specs             #
# --------------------------------------------------------------------------- #
#
# The web dashboard's degraded "Live specs are unavailable" state exists
# because this route has never been implemented. It projects the F23
# dashboard shape {project_id, constitution, specs[]}: each spec is its
# FileSpecEngine manifest enriched with its latest (F23 traceability
# projection) validation report. A project's specs are discovered via the
# board's epics (``epic.spec_id`` -> the engine's deterministic spec uuid) --
# the only place an epic/spec is linked back to a project today.


class SpecOverview(SpecManifest):
    """A spec manifest enriched with its latest validation report (dashboard row)."""

    validation: ValidationReport | None = None


class SpecDashboard(BaseModel):
    """The spec-validation dashboard projection for a project."""

    project_id: uuid.UUID
    constitution: Constitution | None = None
    specs: list[SpecOverview] = Field(default_factory=list)


#: A distinct router (no ``/spec`` prefix) for the project-scoped dashboard read.
project_router = APIRouter(tags=["spec"], dependencies=[Depends(get_current_principal)])


@project_router.get(
    "/projects/{project_id}/specs",
    response_model=SpecDashboard,
    dependencies=[ReadGate],
)
def project_spec_overview(
    project_id: uuid.UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    engine: EngineDep,
    board: BoardServiceDep,
    db: DbSession,
) -> SpecDashboard:
    """The F23 spec-validation dashboard projection for a project.

    404s when the project does not exist in the caller's workspace (no
    existence leak beyond that); a project with no linked specs yet returns an
    empty ``specs`` list rather than 404.
    """
    project = db.get(Project, project_id)
    if project is None or project.workspace_id != principal.workspace_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")

    epics = board.list_epics(BoardFilter(project_id=project_id))
    seen: set[uuid.UUID] = set()
    spec_ids: list[uuid.UUID] = []
    for epic in epics:
        if epic.spec_id is not None and epic.spec_id not in seen:
            seen.add(epic.spec_id)
            spec_ids.append(epic.spec_id)

    specs: list[SpecOverview] = []
    for spec_id in spec_ids:
        try:
            manifest = engine.read_manifest(spec_id)
        except SpecNotFoundError:
            continue
        validation = engine.latest_validation(spec_id)
        specs.append(SpecOverview(**manifest.model_dump(), validation=validation))

    constitution = engine.read_constitution(project_id)
    return SpecDashboard(project_id=project_id, constitution=constitution, specs=specs)


__all__ = [
    "ConstitutionInitRequest",
    "SpecCreateRequest",
    "SpecDashboard",
    "SpecEngineRegistry",
    "SpecOverview",
    "get_spec_engine",
    "get_spec_registry",
    "project_router",
    "router",
]
