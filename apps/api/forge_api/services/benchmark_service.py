"""Benchmark orchestration service (F35 §3.2).

Wires the pure ``forge_eval.benchmark`` core (frozen manifests, deterministic
scoring, replay verification, ranking) to persistence (``benchmark_suite`` /
``benchmark_submission``), secret redaction, and the audit log.

Foundation conformance (deviations from the F35 draft):

* No F12 ``EvalRunner``/Celery eval substrate exists in-tree, so *internal
  agent-driven benchmark runs* are PARKED; the load-bearing external path —
  submit report+bundles -> deterministic offline verification -> admin
  moderation -> public leaderboard — is fully implemented and synchronous
  (matching the in-process precedent of earlier slices).
* No MinIO object store exists in-tree, so replay bundles are persisted inline
  (JSON, size-capped by ``BENCHMARK_SUBMISSION_MAX_BYTES``) and the public
  "signed URL" affordance is an API-served, payload-free bundle download route.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from forge_api.observability.audit import AuditCategory, AuditLog
from forge_api.observability.redaction import redact_mapping, redact_text
from forge_api.schemas.benchmark import SubmitBenchmarkRequest
from forge_db.models.benchmark import BenchmarkSubmission, BenchmarkSuite
from forge_eval.benchmark import (
    BenchmarkContentHashMismatch,
    BenchmarkManifest,
    BenchmarkScore,
    BenchmarkScoring,
    LeaderboardRow,
    ReplayBundle,
    SubmissionStatus,
    VerificationResult,
    Visibility,
    compute_bundle_hash,
    compute_content_hash,
    load_manifest,
    rank_submissions,
    replay_bundles,
    verify_submission,
)
from forge_eval.golden import GoldenCase

#: Default packaged benchmark root (``forge_eval/benchmarks``).
_forge_eval_file = __import__("forge_eval").__file__
if _forge_eval_file is None:  # pragma: no cover - forge_eval is a regular package
    raise RuntimeError("forge_eval has no __file__; cannot locate packaged benchmarks")
DEFAULT_BENCHMARK_ROOT = Path(_forge_eval_file).parent / "benchmarks"

#: Statuses a public leaderboard may rank (flagged/rejected never appear).
PUBLIC_RANKABLE_STATUSES = (SubmissionStatus.scored.value, SubmissionStatus.verified.value)


# --------------------------------------------------------------------------- #
# Service errors (mapped to HTTP status codes by the routers)                   #
# --------------------------------------------------------------------------- #


class BenchmarkServiceError(Exception):
    """Base benchmark service error."""


class SuiteNotFoundError(BenchmarkServiceError):
    """No such (slug, version) suite (-> 404)."""


class SubmissionNotFoundError(BenchmarkServiceError):
    """No such submission visible to the caller (-> 404)."""


class SuiteContentDriftError(BenchmarkServiceError):
    """Suite hash drift: submitted or on-disk hash != frozen hash (-> 409)."""


class SubmissionTooLargeError(BenchmarkServiceError):
    """Submission exceeds BENCHMARK_SUBMISSION_MAX_BYTES (-> 413)."""


class NotVerifiedError(BenchmarkServiceError):
    """Publish requires a verified submission unless forced (-> 409)."""


class BenchmarkService:
    """Sync service over the F35 tables + the pure ``forge_eval.benchmark`` core."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        benchmark_root: Path | str | None = None,
        epsilon: float = 0.005,
        max_submission_bytes: int = 52_428_800,
        audit: AuditLog | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._root = Path(benchmark_root) if benchmark_root else DEFAULT_BENCHMARK_ROOT
        self._epsilon = epsilon
        self._max_bytes = max_submission_bytes
        self._audit = audit or AuditLog()

    # ------------------------------------------------------------------ #
    # Suites                                                              #
    # ------------------------------------------------------------------ #

    def register_suite(
        self,
        manifest: BenchmarkManifest,
        cases: list[GoldenCase],
        *,
        published: bool = True,
        created_by: uuid.UUID | None = None,
    ) -> BenchmarkSuite:
        """Register a frozen suite (idempotent for an identical content hash)."""
        if not manifest.frozen or not manifest.content_hash:
            raise BenchmarkServiceError("only frozen manifests can be registered")
        with self._session_factory() as session:
            existing = session.scalars(
                select(BenchmarkSuite).where(
                    BenchmarkSuite.slug == manifest.slug,
                    BenchmarkSuite.version == manifest.version,
                )
            ).first()
            if existing is not None:
                if existing.content_hash != manifest.content_hash:
                    raise SuiteContentDriftError(
                        f"suite {manifest.slug}@{manifest.version} already registered "
                        f"with {existing.content_hash}; content_hash mismatch; bump version"
                    )
                return existing
            suite = BenchmarkSuite(
                slug=manifest.slug,
                version=manifest.version,
                title=manifest.title,
                description=manifest.description,
                task_count=len(cases),
                primary_metric=manifest.scoring.primary_metric,
                scoring=manifest.scoring.model_dump(mode="json"),
                content_hash=manifest.content_hash,
                frozen=True,
                published=published,
                created_by=created_by,
            )
            session.add(suite)
            session.commit()
            session.refresh(suite)
            self._audit.record(
                category=AuditCategory.SYSTEM,
                action="benchmark.suite_registered",
                actor=f"user:{created_by}" if created_by else "system",
                target=f"{manifest.slug}@{manifest.version}",
                metadata={"content_hash": manifest.content_hash},
            )
            return suite

    def sync_suites_from_disk(self, *, published: bool = True) -> list[BenchmarkSuite]:
        """Register every frozen ``<slug>/<version>/manifest.yaml`` under the root."""
        registered: list[BenchmarkSuite] = []
        if not self._root.is_dir():
            return registered
        for manifest_path in sorted(self._root.glob("*/*/manifest.yaml")):
            manifest, cases = load_manifest(manifest_path.parent)
            if manifest.frozen:
                registered.append(self.register_suite(manifest, cases, published=published))
        return registered

    def list_suites(
        self, *, published_only: bool = False, public_only: bool = False
    ) -> list[BenchmarkSuite]:
        with self._session_factory() as session:
            stmt = select(BenchmarkSuite).order_by(BenchmarkSuite.slug, BenchmarkSuite.version)
            if published_only:
                stmt = stmt.where(BenchmarkSuite.published.is_(True))
            if public_only:
                # Never expose private per-repo Self-Eval suites publicly: only
                # global (workspace_id IS NULL), non-private suites are public.
                stmt = stmt.where(
                    BenchmarkSuite.workspace_id.is_(None),
                    BenchmarkSuite.private.is_(False),
                )
            return list(session.scalars(stmt).all())

    @staticmethod
    def is_public_suite(suite: BenchmarkSuite) -> bool:
        """True iff the suite may be exposed via the public (unauthenticated) API."""
        return suite.workspace_id is None and not suite.private

    def get_suite(self, slug: str, version: str) -> BenchmarkSuite:
        with self._session_factory() as session:
            suite = self._require_suite(session, slug, version)
            return suite

    def load_suite_cases(self, suite: BenchmarkSuite) -> tuple[BenchmarkScoring, list[GoldenCase]]:
        """Load the on-disk frozen cases, guarding against drift (AC7).

        Raises :class:`SuiteContentDriftError` when the on-disk case set no
        longer hashes to the registered ``content_hash``.
        """
        version_dir = self._root / suite.slug / suite.version
        try:
            manifest, cases = load_manifest(version_dir)
        except (FileNotFoundError, BenchmarkContentHashMismatch) as exc:
            raise SuiteContentDriftError(str(exc)) from exc
        scoring = manifest.scoring
        if compute_content_hash(cases, scoring) != suite.content_hash:
            raise SuiteContentDriftError(
                f"on-disk cases for {suite.slug}@{suite.version} no longer match the "
                "registered content_hash; bump version"
            )
        return scoring, cases

    # ------------------------------------------------------------------ #
    # Submissions                                                         #
    # ------------------------------------------------------------------ #

    def ingest_submission(
        self,
        slug: str,
        version: str,
        request: SubmitBenchmarkRequest,
        *,
        workspace_id: uuid.UUID | None,
        submitted_by: uuid.UUID | None,
        actor: str,
    ) -> BenchmarkSubmission:
        """External-contributor ingest (AC19): pending until verified."""
        payload_bytes = len(request.model_dump_json().encode("utf-8"))
        if payload_bytes > self._max_bytes:
            raise SubmissionTooLargeError(
                f"submission is {payload_bytes} bytes > cap {self._max_bytes}"
            )
        with self._session_factory() as session:
            suite = self._require_suite(session, slug, version)
            if request.suite_content_hash and request.suite_content_hash != suite.content_hash:
                raise SuiteContentDriftError(
                    f"submitted suite_content_hash {request.suite_content_hash} does not "
                    f"match the frozen suite hash {suite.content_hash}"
                )
            bundles = [
                b
                if b.content_hash
                else b.model_copy(
                    update={"content_hash": compute_bundle_hash(b.case_id, b.output_ids)}
                )
                for b in request.bundles
            ]
            submission = BenchmarkSubmission(
                benchmark_suite_id=suite.id,
                suite_content_hash=suite.content_hash,
                workspace_id=workspace_id,
                submitter_name=redact_text(request.submitter_name),
                submitter_org=redact_text(request.submitter_org) if request.submitter_org else None,
                submitter_contact=request.submitter_contact,
                model_label=redact_text(request.model_label),
                agent_mode=request.agent_mode,
                config=redact_mapping(request.config),
                forge_version=request.forge_version,
                composite_score=request.claimed.composite,
                scores=request.claimed.model_dump(mode="json"),
                replay_bundles=[b.model_dump(mode="json") for b in bundles],
                replay_content_hashes=[b.content_hash for b in bundles],
                status=SubmissionStatus.pending.value,
                visibility=Visibility.private.value,
                submitted_by=submitted_by,
            )
            session.add(submission)
            session.commit()
            session.refresh(submission)
        self._audit.record(
            category=AuditCategory.SYSTEM,
            action="benchmark.submitted",
            actor=actor,
            workspace_id=workspace_id,
            target=str(submission.id),
            metadata={"suite": f"{slug}@{version}", "claimed": request.claimed.composite},
        )
        return submission

    def verify(
        self, submission_id: uuid.UUID, *, workspace_id: uuid.UUID | None, actor: str
    ) -> tuple[BenchmarkSubmission, VerificationResult]:
        """Deterministic offline verification (AC8-AC11); fail-closed."""
        with self._session_factory() as session:
            submission = self._require_submission(session, submission_id, workspace_id)
            suite = session.get(BenchmarkSuite, submission.benchmark_suite_id)
            if suite is None:  # pragma: no cover - FK guarantees the parent
                raise SuiteNotFoundError(str(submission.benchmark_suite_id))
            if submission.suite_content_hash != suite.content_hash:
                raise SuiteContentDriftError(
                    "submission is bound to a different suite content_hash"
                )
            scoring, cases = self.load_suite_cases(suite)

            bundles = [ReplayBundle.model_validate(raw) for raw in submission.replay_bundles]
            claimed = BenchmarkScore.model_validate(submission.scores)
            report = replay_bundles(bundles, cases, scoring)
            result = verify_submission(
                claimed=claimed,
                reproduced_report=report,
                reproduced_bundles=bundles,
                claimed_bundle_hashes=[str(h) for h in submission.replay_content_hashes],
                scoring=scoring,
                cases=cases,
                epsilon=self._epsilon,
            )

            submission.verification = result.model_dump(mode="json")
            submission.verified = result.verified
            if result.verified:
                submission.verified_at = datetime.now(UTC)
                submission.status = SubmissionStatus.verified.value
            else:
                submission.status = SubmissionStatus.rejected.value
            session.commit()
            session.refresh(submission)
        self._audit.record(
            category=AuditCategory.SYSTEM,
            action="benchmark.verified" if result.verified else "benchmark.rejected",
            actor=actor,
            workspace_id=workspace_id,
            target=str(submission_id),
            status="ok" if result.verified else "rejected",
            detail="; ".join(result.reasons) or None,
        )
        return submission, result

    def publish(
        self,
        submission_id: uuid.UUID,
        *,
        workspace_id: uuid.UUID | None,
        moderator_id: uuid.UUID,
        force: bool = False,
        actor: str,
    ) -> BenchmarkSubmission:
        """Admin moderation gate (AC13): only verified entries go public."""
        with self._session_factory() as session:
            submission = self._require_submission(session, submission_id, workspace_id)
            if not submission.verified and not force:
                raise NotVerifiedError(
                    "submission is not verified; verify first or publish with force"
                )
            submission.visibility = Visibility.public.value
            submission.moderated_by = moderator_id
            session.commit()
            session.refresh(submission)
        self._audit.record(
            category=AuditCategory.SYSTEM,
            action="benchmark.published",
            actor=actor,
            workspace_id=workspace_id,
            target=str(submission_id),
            metadata={"force": force},
        )
        return submission

    def flag(
        self,
        submission_id: uuid.UUID,
        *,
        workspace_id: uuid.UUID | None,
        moderator_id: uuid.UUID,
        reason: str,
        actor: str,
    ) -> BenchmarkSubmission:
        """Flag a suspicious entry: removed from the board, kept for audit."""
        with self._session_factory() as session:
            submission = self._require_submission(session, submission_id, workspace_id)
            submission.status = SubmissionStatus.flagged.value
            submission.visibility = Visibility.private.value
            submission.moderated_by = moderator_id
            session.commit()
            session.refresh(submission)
        self._audit.record(
            category=AuditCategory.SYSTEM,
            action="benchmark.flagged",
            actor=actor,
            workspace_id=workspace_id,
            target=str(submission_id),
            detail=reason,
        )
        return submission

    def list_submissions(
        self, slug: str, version: str, *, workspace_id: uuid.UUID
    ) -> list[tuple[BenchmarkSubmission, BenchmarkSuite]]:
        with self._session_factory() as session:
            suite = self._require_suite(session, slug, version)
            rows = session.scalars(
                select(BenchmarkSubmission)
                .where(
                    BenchmarkSubmission.benchmark_suite_id == suite.id,
                    (BenchmarkSubmission.workspace_id == workspace_id)
                    | (BenchmarkSubmission.workspace_id.is_(None)),
                )
                .order_by(BenchmarkSubmission.submitted_at.desc())
            ).all()
            return [(row, suite) for row in rows]

    def get_submission(
        self, submission_id: uuid.UUID, *, workspace_id: uuid.UUID | None
    ) -> tuple[BenchmarkSubmission, BenchmarkSuite]:
        with self._session_factory() as session:
            submission = self._require_submission(session, submission_id, workspace_id)
            suite = session.get(BenchmarkSuite, submission.benchmark_suite_id)
            assert suite is not None  # FK-guaranteed
            return submission, suite

    # ------------------------------------------------------------------ #
    # Leaderboard (computed, not stored)                                  #
    # ------------------------------------------------------------------ #

    def leaderboard(
        self, slug: str, version: str, *, public_only: bool
    ) -> tuple[BenchmarkSuite, list[LeaderboardRow]]:
        with self._session_factory() as session:
            suite = self._require_suite(session, slug, version)
            if public_only and (not suite.published or not self.is_public_suite(suite)):
                # A private/workspace-scoped Self-Eval suite is invisible to the
                # public API even if published — 404 as if it does not exist.
                raise SuiteNotFoundError(f"{slug}@{version}")
            stmt = select(BenchmarkSubmission).where(
                BenchmarkSubmission.benchmark_suite_id == suite.id,
                BenchmarkSubmission.composite_score.is_not(None),
            )
            if public_only:
                stmt = stmt.where(
                    BenchmarkSubmission.visibility == Visibility.public.value,
                    BenchmarkSubmission.status.in_(PUBLIC_RANKABLE_STATUSES),
                )
            else:
                stmt = stmt.where(BenchmarkSubmission.status != SubmissionStatus.flagged.value)
            rows = session.scalars(stmt).all()
            unranked = [self._to_row(row) for row in rows]
        return suite, rank_submissions(unranked)

    def public_submission(
        self, slug: str, version: str, submission_id: uuid.UUID
    ) -> tuple[BenchmarkSubmission, BenchmarkSuite]:
        """A submission as visible on the public surface — 404 unless public."""
        with self._session_factory() as session:
            suite = self._require_suite(session, slug, version)
            submission = session.get(BenchmarkSubmission, submission_id)
            if (
                submission is None
                or submission.benchmark_suite_id != suite.id
                or submission.visibility != Visibility.public.value
                or submission.status not in PUBLIC_RANKABLE_STATUSES
                or not suite.published
                or not self.is_public_suite(suite)
            ):
                raise SubmissionNotFoundError(str(submission_id))
            return submission, suite

    # ------------------------------------------------------------------ #
    # Internals                                                           #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _to_row(row: BenchmarkSubmission) -> LeaderboardRow:
        score = BenchmarkScore.model_validate(row.scores) if row.scores else None
        return LeaderboardRow(
            rank=0,
            submission_id=row.id,
            model_label=row.model_label,
            agent_mode=row.agent_mode,
            composite_score=float(row.composite_score or 0.0),
            verified=row.verified,
            forge_version=row.forge_version,
            submitter_name=row.submitter_name,
            submitter_org=row.submitter_org,
            per_category=score.per_category if score else [],
            submitted_at=row.submitted_at,
        )

    @staticmethod
    def _require_suite(session: Session, slug: str, version: str) -> BenchmarkSuite:
        suite = session.scalars(
            select(BenchmarkSuite).where(
                BenchmarkSuite.slug == slug, BenchmarkSuite.version == version
            )
        ).first()
        if suite is None:
            raise SuiteNotFoundError(f"{slug}@{version}")
        return suite

    @staticmethod
    def _require_submission(
        session: Session, submission_id: uuid.UUID, workspace_id: uuid.UUID | None
    ) -> BenchmarkSubmission:
        """Workspace-scoped fetch: cross-workspace ids surface as 404, never a leak.

        Official/system submissions (``workspace_id IS NULL``) are visible to
        every authenticated workspace (they carry no tenant data).
        """
        submission = session.get(BenchmarkSubmission, submission_id)
        if submission is None or (
            submission.workspace_id is not None
            and workspace_id is not None
            and submission.workspace_id != workspace_id
        ):
            raise SubmissionNotFoundError(str(submission_id))
        return submission
