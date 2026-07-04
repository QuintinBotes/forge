"""Authenticated benchmark management router (F35) — ``/api/v1/benchmarks``.

Reads require ``member`` (READ); external submission ingest requires WRITE
(so the read-only ``viewer``/``agent-runner`` roles cannot submit, AC18);
verify / publish / flag are ``admin``-only moderation actions. All access is
scoped by the authenticated principal's workspace — a cross-workspace id
surfaces as 404, never a leak.

Internal agent-driven benchmark runs (``POST /runs``) are PARKED: the in-tree
foundation has no F12 ``EvalRunner``/agent-eval substrate to drive them, and a
fake run would violate PARK-DON'T-FAKE. The external submit -> verify ->
moderate -> rank path is fully implemented.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from forge_api.auth.rbac import Permission
from forge_api.db import get_session_factory
from forge_api.deps import Principal, get_current_principal
from forge_api.routers._rbac import require_permission
from forge_api.schemas.benchmark import (
    BenchmarkSuiteOut,
    FlagRequest,
    PublishRequest,
    SubmissionOut,
    SubmitBenchmarkRequest,
    VerifyResponse,
)
from forge_api.services.benchmark_service import (
    BenchmarkService,
    NotVerifiedError,
    SubmissionNotFoundError,
    SubmissionTooLargeError,
    SuiteContentDriftError,
    SuiteNotFoundError,
)
from forge_api.settings import get_settings
from forge_db.models.benchmark import BenchmarkSubmission, BenchmarkSuite
from forge_eval.benchmark import BenchmarkScore, SubmissionStatus, VerificationResult, Visibility

router = APIRouter(
    prefix="/benchmarks",
    tags=["benchmarks"],
    dependencies=[Depends(get_current_principal)],
)

ReaderDep = Annotated[Principal, Depends(require_permission(Permission.READ))]
WriterDep = Annotated[Principal, Depends(require_permission(Permission.WRITE))]
AdminDep = Annotated[Principal, Depends(require_permission(Permission.ADMIN))]


def get_benchmark_service() -> BenchmarkService:
    """Build the benchmark service (overridable in tests via DI)."""
    settings = get_settings()
    return BenchmarkService(
        session_factory=get_session_factory(),
        benchmark_root=settings.benchmark_dir,
        epsilon=settings.benchmark_verify_epsilon,
        max_submission_bytes=settings.benchmark_submission_max_bytes,
    )


ServiceDep = Annotated[BenchmarkService, Depends(get_benchmark_service)]


def _suite_dto(suite: BenchmarkSuite) -> BenchmarkSuiteOut:
    return BenchmarkSuiteOut(
        slug=suite.slug,
        version=suite.version,
        title=suite.title,
        description=suite.description,
        task_count=suite.task_count,
        primary_metric=suite.primary_metric,
        content_hash=suite.content_hash,
        frozen=suite.frozen,
        published=suite.published,
    )


def _submission_dto(row: BenchmarkSubmission, suite: BenchmarkSuite) -> SubmissionOut:
    return SubmissionOut(
        id=row.id,
        benchmark_slug=suite.slug,
        benchmark_version=suite.version,
        model_label=row.model_label,
        agent_mode=row.agent_mode,
        forge_version=row.forge_version,
        composite_score=float(row.composite_score)
        if row.composite_score is not None
        else None,
        scores=BenchmarkScore.model_validate(row.scores) if row.scores else None,
        status=SubmissionStatus(row.status),
        visibility=Visibility(row.visibility),
        verified=row.verified,
        verification=VerificationResult.model_validate(row.verification)
        if row.verification
        else None,
        submitter_name=row.submitter_name,
        submitter_org=row.submitter_org,
        submitted_at=row.submitted_at,
    )


@router.get("", response_model=list[BenchmarkSuiteOut])
def list_suites(service: ServiceDep, principal: ReaderDep) -> list[BenchmarkSuiteOut]:
    return [_suite_dto(s) for s in service.list_suites()]


# NOTE: the /submissions/{id} routes are declared BEFORE /{slug}/{version} —
# Starlette matches in declaration order and "submissions" would otherwise be
# captured as a {slug}.
@router.get("/submissions/{submission_id}", response_model=SubmissionOut)
def get_submission(
    service: ServiceDep, principal: ReaderDep, submission_id: uuid.UUID
) -> SubmissionOut:
    try:
        row, suite = service.get_submission(
            submission_id, workspace_id=principal.workspace_id
        )
    except SubmissionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return _submission_dto(row, suite)


@router.get("/{slug}/{version}", response_model=BenchmarkSuiteOut)
def get_suite(
    service: ServiceDep, principal: ReaderDep, slug: str, version: str
) -> BenchmarkSuiteOut:
    try:
        return _suite_dto(service.get_suite(slug, version))
    except SuiteNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post(
    "/{slug}/{version}/submissions",
    response_model=SubmissionOut,
    status_code=status.HTTP_201_CREATED,
)
def submit(
    service: ServiceDep,
    principal: WriterDep,
    slug: str,
    version: str,
    body: SubmitBenchmarkRequest,
) -> SubmissionOut:
    try:
        row = service.ingest_submission(
            slug,
            version,
            body,
            workspace_id=principal.workspace_id,
            submitted_by=principal.user_id,
            actor=f"user:{principal.user_id}",
        )
    except SuiteNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except SuiteContentDriftError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except SubmissionTooLargeError as exc:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail=str(exc)
        ) from exc
    suite = service.get_suite(slug, version)
    return _submission_dto(row, suite)


@router.get("/{slug}/{version}/submissions", response_model=list[SubmissionOut])
def list_submissions(
    service: ServiceDep, principal: ReaderDep, slug: str, version: str
) -> list[SubmissionOut]:
    try:
        rows = service.list_submissions(slug, version, workspace_id=principal.workspace_id)
    except SuiteNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return [_submission_dto(row, suite) for row, suite in rows]


@router.post("/submissions/{submission_id}/verify", response_model=VerifyResponse)
def verify(
    service: ServiceDep, principal: AdminDep, submission_id: uuid.UUID
) -> VerifyResponse:
    try:
        row, result = service.verify(
            submission_id,
            workspace_id=principal.workspace_id,
            actor=f"user:{principal.user_id}",
        )
    except SubmissionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except SuiteContentDriftError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return VerifyResponse(
        submission_id=row.id,
        verification=result,
        status=SubmissionStatus(row.status),
    )


@router.post("/submissions/{submission_id}/publish", response_model=SubmissionOut)
def publish(
    service: ServiceDep,
    principal: AdminDep,
    submission_id: uuid.UUID,
    body: PublishRequest | None = None,
) -> SubmissionOut:
    try:
        row = service.publish(
            submission_id,
            workspace_id=principal.workspace_id,
            moderator_id=principal.user_id,
            force=bool(body and body.force),
            actor=f"user:{principal.user_id}",
        )
    except SubmissionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except NotVerifiedError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    _, suite = service.get_submission(submission_id, workspace_id=principal.workspace_id)
    return _submission_dto(row, suite)


@router.post("/submissions/{submission_id}/flag", response_model=SubmissionOut)
def flag(
    service: ServiceDep,
    principal: AdminDep,
    submission_id: uuid.UUID,
    body: FlagRequest,
) -> SubmissionOut:
    try:
        row = service.flag(
            submission_id,
            workspace_id=principal.workspace_id,
            moderator_id=principal.user_id,
            reason=body.reason,
            actor=f"user:{principal.user_id}",
        )
    except SubmissionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    _, suite = service.get_submission(submission_id, workspace_id=principal.workspace_id)
    return _submission_dto(row, suite)
