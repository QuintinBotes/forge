"""Public, unauthenticated, read-only leaderboard router (F35 §3.2).

Deliberately **not** in ``FEATURE_ROUTERS`` (those carry the auth dependency):
``forge_api.main.create_app`` includes this router at the app root **only when
``FORGE_PUBLIC_LEADERBOARD_ENABLED=true``** — the self-hosted default is
``false``, so every ``/public/*`` path 404s and internal eval data never leaks
(AC16). The router is structurally read-only (GET only), per-IP rate-limited
(AC17), cache-fronted, and serializes exclusively through the payload-free
``Public*`` models — ``submitter_contact``, raw config, and raw payloads are
unrepresentable in the response schema (AC15).
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from forge_api.routers.benchmarks import get_benchmark_service
from forge_api.schemas.benchmark import (
    PublicBenchmarkOut,
    PublicLeaderboard,
    PublicLeaderboardEntry,
    PublicSubmissionDetail,
)
from forge_api.services.benchmark_service import (
    BenchmarkService,
    SubmissionNotFoundError,
    SuiteNotFoundError,
)
from forge_api.settings import get_settings
from forge_eval.benchmark import BenchmarkScore

router = APIRouter(prefix="/public", tags=["public-leaderboard"])

_RATE_WINDOW_SECONDS = 60.0


class _SlidingWindowRateLimiter:
    """Tiny in-process per-IP sliding-window limiter (public surface DoS bound)."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, *, now: float | None = None) -> bool:
        stamp = time.monotonic() if now is None else now
        with self._lock:
            window = self._hits.setdefault(key, deque())
            while window and stamp - window[0] >= _RATE_WINDOW_SECONDS:
                window.popleft()
            if len(window) >= limit:
                return False
            window.append(stamp)
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


#: Process-wide limiter; tests reset it between cases.
rate_limiter = _SlidingWindowRateLimiter()


def _enforce_rate_limit(request: Request) -> None:
    settings = get_settings()
    client_ip = request.client.host if request.client else "unknown"
    if not rate_limiter.allow(client_ip, settings.leaderboard_public_rate_limit):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="public leaderboard rate limit exceeded",
        )


def _cache_headers(response: Response) -> None:
    ttl = get_settings().leaderboard_cache_ttl_seconds
    response.headers["Cache-Control"] = f"public, max-age={ttl}"


RateLimited = Depends(_enforce_rate_limit)
ServiceDep = Annotated[BenchmarkService, Depends(get_benchmark_service)]


@router.get(
    "/benchmarks",
    response_model=list[PublicBenchmarkOut],
    dependencies=[RateLimited],
)
def list_public_benchmarks(
    service: ServiceDep, response: Response
) -> list[PublicBenchmarkOut]:
    _cache_headers(response)
    return [
        PublicBenchmarkOut(
            slug=s.slug,
            version=s.version,
            title=s.title,
            description=s.description,
            task_count=s.task_count,
            primary_metric=s.primary_metric,
            content_hash=s.content_hash,
        )
        for s in service.list_suites(published_only=True)
    ]


@router.get(
    "/leaderboard/{slug}/{version}",
    response_model=PublicLeaderboard,
    dependencies=[RateLimited],
)
def public_leaderboard(
    service: ServiceDep, response: Response, slug: str, version: str
) -> PublicLeaderboard:
    try:
        suite, rows = service.leaderboard(slug, version, public_only=True)
    except SuiteNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    _cache_headers(response)
    return PublicLeaderboard(
        slug=suite.slug,
        version=suite.version,
        title=suite.title,
        primary_metric=suite.primary_metric,
        content_hash=suite.content_hash,
        generated_at=datetime.now(UTC),
        entries=[
            PublicLeaderboardEntry(
                rank=row.rank,
                model_label=row.model_label,
                agent_mode=row.agent_mode,
                composite_score=row.composite_score,
                verified=row.verified,
                forge_version=row.forge_version,
                submitter_name=row.submitter_name,
                submitter_org=row.submitter_org,
                per_category=row.per_category,
                submitted_at=row.submitted_at,
                submission_id=row.submission_id,
            )
            for row in rows
        ],
    )


@router.get(
    "/leaderboard/{slug}/{version}/submissions/{submission_id}",
    response_model=PublicSubmissionDetail,
    dependencies=[RateLimited],
)
def public_submission_detail(
    service: ServiceDep,
    response: Response,
    slug: str,
    version: str,
    submission_id: uuid.UUID,
) -> PublicSubmissionDetail:
    try:
        row, suite = service.public_submission(slug, version, submission_id)
    except (SuiteNotFoundError, SubmissionNotFoundError) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    _cache_headers(response)
    bundles_url = f"/public/leaderboard/{slug}/{version}/submissions/{row.id}/bundles"
    return PublicSubmissionDetail(
        submission_id=row.id,
        slug=suite.slug,
        version=suite.version,
        model_label=row.model_label,
        agent_mode=row.agent_mode,
        forge_version=row.forge_version,
        composite_score=float(row.composite_score or 0.0),
        verified=row.verified,
        scores=BenchmarkScore.model_validate(row.scores),
        submitter_name=row.submitter_name,
        submitter_org=row.submitter_org,
        submitted_at=row.submitted_at,
        reproduce_command=(
            f"forge-cli bench verify --suite-dir benchmarks/{slug}/{version} "
            f"--submission submission-{row.id}.json"
        ),
        replay_bundle_urls=[bundles_url],
    )


@router.get(
    "/leaderboard/{slug}/{version}/submissions/{submission_id}/bundles",
    dependencies=[RateLimited],
)
def public_submission_bundles(
    service: ServiceDep,
    response: Response,
    slug: str,
    version: str,
    submission_id: uuid.UUID,
) -> dict[str, Any]:
    """Payload-free reproduce artifact: the exact input `bench verify` consumes.

    Bundles contain only case ids + ordered output ids (no prompts, diffs, or
    tool outputs) — downloading this file + the frozen suite reproduces the
    composite offline.
    """
    try:
        row, _suite = service.public_submission(slug, version, submission_id)
    except (SuiteNotFoundError, SubmissionNotFoundError) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    _cache_headers(response)
    return {
        "submission_id": str(row.id),
        "claimed": row.scores,
        "claimed_bundle_hashes": row.replay_content_hashes,
        "bundles": row.replay_bundles,
    }
