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
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from forge_api.auth.rbac import Permission
from forge_api.deps import DbSession, Principal, get_current_principal
from forge_api.routers._rbac import require_permission
from forge_api.routers.board import BoardServiceDep
from forge_api.services import spec_version_service
from forge_api.services.spec_draft_service import SpecDraft, draft_spec
from forge_api.services.spec_import_service import SpecImport, SpecImportRequest, import_spec
from forge_api.settings import get_settings
from forge_contracts import (
    BoardFilter,
    Constitution,
    ModelClient,
    Requirement,
    SpecManifest,
    TaskDTO,
    ValidationReport,
)
from forge_contracts.exceptions import SpecGateError
from forge_db.models import Project, SpecVersion
from forge_orchestration_policy import Tier
from forge_spec import (
    FileSpecEngine,
    ManifestDiff,
    SpecNotFoundError,
    diff_manifest,
    diff_markdown,
    spec_id_for_key,
)

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


def _record_version(
    engine: FileSpecEngine,
    db: DbSession,
    principal: Principal,
    manifest: SpecManifest,
) -> SpecVersion:
    """Snapshot ``manifest`` (+ its rendered serializations) as the next version.

    Called after every successful save (``spec_create`` / ``write_manifest`` /
    ``write_spec_markdown`` / ``write_spec_manifest_yaml``); reads back the
    just-persisted ``spec.md``/``manifest.yaml`` (always in sync post-save)
    rather than re-rendering them independently, so the recorded snapshot is
    byte-identical to what a reader of the engine sees right now.
    """
    spec_id = spec_id_for_key(manifest.id)
    spec_md = engine.read_spec_md(spec_id)
    manifest_yaml = engine.read_manifest_yaml(spec_id)
    return spec_version_service.record_version(
        db,
        workspace_id=principal.workspace_id,
        manifest=manifest,
        spec_md=spec_md,
        manifest_yaml=manifest_yaml,
        created_by=principal.user_id,
    )


class ConstitutionInitRequest(BaseModel):
    """Body for ``POST /spec/constitution``."""

    project_id: uuid.UUID
    principles: list[str] | None = None


class SpecCreateRequest(BaseModel):
    """Body for ``POST /spec/specs``."""

    epic_id: uuid.UUID
    name: str
    requirements: list[Requirement] = Field(default_factory=list)


class TextContent(BaseModel):
    """Body for the ``spec.md`` / ``manifest.yaml`` write endpoints."""

    content: str


class DraftSpecRequest(BaseModel):
    """Body for ``POST /spec/draft`` (BYOK AI spec drafting; draft-only)."""

    goal: str = Field(min_length=1, description="One-line engineering goal to draft a spec for.")
    epic_id: uuid.UUID | None = None
    #: Optional project whose constitution seeds the spec-authoring prompt.
    project_id: uuid.UUID | None = None


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
def spec_create(
    engine: EngineDep,
    request: SpecCreateRequest,
    db: DbSession,
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> SpecManifest:
    """Create a draft spec for an epic (recorded as version 1)."""
    manifest = engine.spec_create(request.epic_id, request.name, request.requirements)
    _record_version(engine, db, principal, manifest)
    return manifest


@router.get("/specs/{spec_id}", response_model=SpecManifest, dependencies=[ReadGate])
def read_manifest(engine: EngineDep, spec_id: uuid.UUID) -> SpecManifest:
    """Read a spec manifest by its deterministic uuid."""
    with _spec_errors():
        return engine.read_manifest(spec_id)


@router.put("/specs/{spec_id}", response_model=SpecManifest, dependencies=[WriteGate])
def write_manifest(
    engine: EngineDep,
    spec_id: uuid.UUID,
    manifest: SpecManifest,
    db: DbSession,
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> SpecManifest:
    """Persist (create or update) a spec manifest; records a new version."""
    with _spec_errors():
        updated = engine.write_manifest(manifest)
    _record_version(engine, db, principal, updated)
    return updated


@router.get(
    "/specs/{spec_id}/markdown",
    dependencies=[ReadGate],
    response_class=PlainTextResponse,
)
def read_spec_markdown(engine: EngineDep, spec_id: uuid.UUID) -> PlainTextResponse:
    """Read the spec's ``spec.md`` prose serialization (always kept in sync)."""
    with _spec_errors():
        text = engine.read_spec_md(spec_id)
    return PlainTextResponse(text)


@router.put("/specs/{spec_id}/markdown", response_model=SpecManifest, dependencies=[WriteGate])
def write_spec_markdown(
    engine: EngineDep,
    spec_id: uuid.UUID,
    body: TextContent,
    db: DbSession,
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> SpecManifest:
    """Save a spec edited as ``spec.md`` prose; re-renders ``manifest.yaml`` to match.

    The spec being edited must already exist at ``spec_id`` (404 otherwise);
    the document's own frontmatter id governs which spec is written, mirroring
    ``PUT /spec/specs/{spec_id}``. Records a new version on success.
    """
    with _spec_errors():
        engine.read_manifest(spec_id)
        updated = engine.save_spec_md(body.content)
    _record_version(engine, db, principal, updated)
    return updated


@router.get(
    "/specs/{spec_id}/manifest",
    dependencies=[ReadGate],
    response_class=PlainTextResponse,
)
def read_spec_manifest_yaml(engine: EngineDep, spec_id: uuid.UUID) -> PlainTextResponse:
    """Read the spec's ``manifest.yaml`` serialization (always kept in sync)."""
    with _spec_errors():
        text = engine.read_manifest_yaml(spec_id)
    return PlainTextResponse(text)


@router.put("/specs/{spec_id}/manifest", response_model=SpecManifest, dependencies=[WriteGate])
def write_spec_manifest_yaml(
    engine: EngineDep,
    spec_id: uuid.UUID,
    body: TextContent,
    db: DbSession,
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> SpecManifest:
    """Save a spec edited as ``manifest.yaml``; re-renders ``spec.md`` to match.

    Unlike the markdown endpoint, this may also *create* a new spec: when no
    spec resolves to ``spec_id`` yet, the YAML's own id governs where it is
    written (mirroring ``PUT /spec/specs/{spec_id}``'s create-or-update
    semantics). Records a new version on success.
    """
    with _spec_errors():
        updated = engine.save_manifest_yaml(body.content)
    _record_version(engine, db, principal, updated)
    return updated


@router.get(
    "/constitution/{project_id}",
    response_model=Constitution,
    dependencies=[ReadGate],
)
def read_constitution(engine: EngineDep, project_id: uuid.UUID) -> Constitution:
    """Read a project's constitution; 404 if it was never initialised."""
    constitution = engine.read_constitution(project_id)
    if constitution is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="constitution not found")
    return constitution


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
# ss-versioning: spec version history + diff                                  #
# --------------------------------------------------------------------------- #
#
# A version is recorded (see ``_record_version``) on every save through the
# editing endpoints above. These read-only routes list a spec's version
# history and diff any two of its versions — both the raw ``spec.md`` prose
# (line-level) and the structured manifest (id-keyed adds/removes/changes per
# list field). Versions are looked up by ``spec_id`` (the same deterministic
# uuid as everywhere else in this router) + a 1-based ``version_number``.


class SpecVersionSummary(BaseModel):
    """One row of a spec's version history (no snapshot payload)."""

    version_number: int
    name: str
    status: str
    created_at: str
    created_by: uuid.UUID | None = None


class SpecVersionDetail(SpecVersionSummary):
    """A single version's full snapshot."""

    manifest: SpecManifest
    spec_md: str
    manifest_yaml: str


class SpecVersionDiff(BaseModel):
    """The diff between two versions of a spec."""

    from_version: int
    to_version: int
    markdown: list[Any] = Field(default_factory=list)
    manifest: ManifestDiff


def _version_summary(version: SpecVersion) -> SpecVersionSummary:
    return SpecVersionSummary(
        version_number=version.version_number,
        name=version.name,
        status=version.status,
        created_at=version.created_at.isoformat(),
        created_by=version.created_by,
    )


@router.get(
    "/specs/{spec_id}/versions",
    response_model=list[SpecVersionSummary],
    dependencies=[ReadGate],
)
def list_spec_versions(
    spec_id: uuid.UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: DbSession,
) -> list[SpecVersionSummary]:
    """List a spec's versions, newest first (empty if never saved)."""
    versions = spec_version_service.list_versions(
        db, workspace_id=principal.workspace_id, spec_id=spec_id
    )
    return [_version_summary(v) for v in versions]


def _get_version_or_404(
    db: DbSession, principal: Principal, spec_id: uuid.UUID, version_number: int
) -> SpecVersion:
    version = spec_version_service.get_version(
        db, workspace_id=principal.workspace_id, spec_id=spec_id, version_number=version_number
    )
    if version is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"spec version {version_number} not found",
        )
    return version


@router.get(
    "/specs/{spec_id}/versions/{version_number}",
    response_model=SpecVersionDetail,
    dependencies=[ReadGate],
)
def read_spec_version(
    spec_id: uuid.UUID,
    version_number: int,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: DbSession,
) -> SpecVersionDetail:
    """Read one version's full snapshot (manifest + both serializations)."""
    version = _get_version_or_404(db, principal, spec_id, version_number)
    return SpecVersionDetail(
        **_version_summary(version).model_dump(),
        manifest=SpecManifest.model_validate(version.manifest),
        spec_md=version.spec_md,
        manifest_yaml=version.manifest_yaml,
    )


@router.get(
    "/specs/{spec_id}/versions/{from_version}/diff/{to_version}",
    response_model=SpecVersionDiff,
    dependencies=[ReadGate],
)
def diff_spec_versions(
    spec_id: uuid.UUID,
    from_version: int,
    to_version: int,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: DbSession,
) -> SpecVersionDiff:
    """Diff two versions of a spec: line-level markdown + structured manifest."""
    older = _get_version_or_404(db, principal, spec_id, from_version)
    newer = _get_version_or_404(db, principal, spec_id, to_version)
    markdown_diff = diff_markdown(older.spec_md, newer.spec_md)
    manifest_diff = diff_manifest(
        SpecManifest.model_validate(older.manifest), SpecManifest.model_validate(newer.manifest)
    )
    return SpecVersionDiff(
        from_version=from_version,
        to_version=to_version,
        markdown=[line.model_dump() for line in markdown_diff],
        manifest=manifest_diff,
    )


# --------------------------------------------------------------------------- #
# ss-draft: BYOK AI spec drafting (POST /spec/draft)                          #
# --------------------------------------------------------------------------- #
#
# Uses the ao-model-router to pick a model for the workspace's BYOK provider,
# resolves the HARD-02 ModelClient (env/vault key) bound to that model, and
# streams a spec.md draft seeded with the project constitution. Draft-only:
# nothing is persisted. The binding is a single overridable dependency so tests
# inject a mocked ModelClient (no live key / network).

#: The Adaptive Orchestration tier used for spec authoring. Drafting a spec is
#: high-leverage work, so it routes to the senior model by default.
_DRAFT_TIER: Tier = "senior"


@dataclass(frozen=True)
class DraftModelBinding:
    """A resolved model client + the router-chosen model for a draft call."""

    client: ModelClient
    model: str


def get_draft_binding(
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> DraftModelBinding:
    """Resolve the BYOK model client + router-chosen model for spec drafting.

    The provider comes from the workspace's ``FORGE_MODEL_*`` env config; the
    ``ao-model-router`` maps the spec-authoring tier to a concrete model on that
    provider; the HARD-02 client is then resolved with the workspace's BYOK key
    bound to that model. Overridden in tests to inject a mocked client.
    """
    from forge_agent.providers import ModelClientConfig, ModelClientError
    from forge_agent.providers.router import ModelRouter
    from forge_api.auth.service import get_auth_service

    config = ModelClientConfig.from_env()
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "no model provider configured for spec drafting; set FORGE_MODEL_PROVIDER "
                "and a BYOK key"
            ),
        )
    model = ModelRouter(provider=config.provider).resolve(_DRAFT_TIER)
    try:
        client = get_auth_service().resolve_model_client(principal.workspace_id, model=model)
    except ModelClientError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    return DraftModelBinding(client=client, model=model)


DraftBindingDep = Annotated[DraftModelBinding, Depends(get_draft_binding)]


@router.post("/draft", response_model=SpecDraft, dependencies=[WriteGate])
def draft_spec_endpoint(
    engine: EngineDep, binding: DraftBindingDep, request: DraftSpecRequest
) -> SpecDraft:
    """Draft a ``spec.md`` from a one-line goal via the BYOK model (draft-only).

    Seeds the spec-authoring prompt with the project constitution (when a
    ``project_id`` resolving to one is supplied), streams the draft, and returns
    a parsed :class:`SpecManifest` preview plus token/cost accounting. Nothing is
    persisted — a human refines the draft via the spec-editing endpoints.
    """
    from forge_agent.providers import ModelClientError

    constitution = (
        engine.read_constitution(request.project_id) if request.project_id is not None else None
    )
    try:
        return draft_spec(
            binding.client,
            goal=request.goal,
            model=binding.model,
            constitution=constitution,
            epic_id=request.epic_id,
        )
    except ModelClientError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


# --------------------------------------------------------------------------- #
# ss-import: external spec import (POST /spec/import)                        #
# --------------------------------------------------------------------------- #
#
# Turns an existing markdown or YAML spec (pasted or uploaded from outside
# Forge) into a spec.md draft — parse/normalize only, no model call. Draft-only
# like ``POST /spec/draft``: nothing is persisted; a human refines the result
# via the normal spec-editing endpoints.


@router.post("/import", response_model=SpecImport, dependencies=[WriteGate])
def import_spec_endpoint(request: SpecImportRequest) -> SpecImport:
    """Import an external markdown/YAML spec as a ``spec.md`` draft (draft-only)."""
    return import_spec(request.content, source_format=request.source_format)


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
    "SpecImport",
    "SpecImportRequest",
    "SpecOverview",
    "SpecVersionDetail",
    "SpecVersionDiff",
    "SpecVersionSummary",
    "TextContent",
    "get_spec_engine",
    "get_spec_registry",
    "project_router",
    "router",
]
