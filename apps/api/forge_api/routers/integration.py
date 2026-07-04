"""Integration SDK router (Task 1.13 — integration-sdk; wired in Phase 2 Task 2.1).

GitHub (repo sync, PRs, CI webhooks) and Slack notifications.

* ``POST /integration/github/repos/{connection_id}/sync`` — sync a connected repo.
* ``POST /integration/github/pull-requests``               — open a pull request.
* ``POST /integration/github/webhooks``                    — ingest a GitHub
  webhook and return the parsed :class:`~forge_contracts.CIStatus` (pure; the
  only route outside the principal dependency so provider callbacks reach it).
* ``POST /integration/slack/notify``                       — send a Slack message.

The GitHub/Slack clients are built from configuration and injected via FastAPI
dependencies, so tests drive them with an ``httpx.MockTransport`` (no live calls)
and production supplies real BYOK credentials. Upstream client failures map to
HTTP 502. The webhook parser is fully offline (no network).
"""

from __future__ import annotations

import uuid
from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import ValidationError

from forge_api.auth.rbac import Permission
from forge_api.deps import Principal
from forge_api.observability.audit import AuditCategory, AuditLog
from forge_api.observability.redaction import redact_mapping, redact_text
from forge_api.routers._rbac import require_permission
from forge_api.settings import get_settings
from forge_contracts import (
    CIStatus,
    HealthResult,
    PullRequest,
    PullRequestRequest,
    RepositoryConnection,
    RepoSyncResult,
    SlackDeliveryResult,
    SlackMessage,
    WebhookEvent,
)
from forge_integrations import (
    AuditSink,
    GitHubAuditEvent,
    GitHubClient,
    GitHubError,
    SlackError,
    SlackNotifier,
    load_private_key,
    parse_github_webhook,
    verify_github_signature,
)

router = APIRouter(
    prefix="/integration",
    tags=["integration"],
)

# Authorization: syncing a repo, opening a PR, and sending a Slack message are
# all WRITE operations (a read-only viewer is denied). The webhook route is
# deliberately ungated by RBAC — it is authenticated by HMAC signature instead.
WriteGate = Depends(require_permission(Permission.WRITE))
WriterDep = Annotated[Principal, Depends(require_permission(Permission.WRITE))]
ReadGate = Depends(require_permission(Permission.READ))


# --------------------------------------------------------------------------- #
# Repository-connection store (server-side, per-workspace)                      #
# --------------------------------------------------------------------------- #


class RepoConnectionStore:
    """Server-side registry of which repos a workspace may sync.

    The sync route must NOT trust a caller-supplied :class:`RepositoryConnection`
    (the server holds the privileged GitHub token, so a caller-controlled
    ``full_name`` is a confused-deputy / cross-tenant vector). Connections are
    resolved here by ``connection_id`` scoped to the caller's workspace; the
    DB-backed store is swapped in behind the same dependency in production.
    """

    def __init__(self) -> None:
        self._by_ws: dict[uuid.UUID, dict[uuid.UUID, RepositoryConnection]] = {}

    def register(
        self, workspace_id: uuid.UUID, connection: RepositoryConnection
    ) -> RepositoryConnection:
        if connection.id is None:
            connection.id = uuid.uuid4()
        self._by_ws.setdefault(workspace_id, {})[connection.id] = connection
        return connection

    def get(self, workspace_id: uuid.UUID, connection_id: uuid.UUID) -> RepositoryConnection | None:
        return self._by_ws.get(workspace_id, {}).get(connection_id)


@lru_cache(maxsize=1)
def _repo_connection_store_singleton() -> RepoConnectionStore:
    return RepoConnectionStore()


def get_repo_connection_store() -> RepoConnectionStore:
    """Return the process-wide repo-connection store (override in tests via DI)."""
    return _repo_connection_store_singleton()


RepoConnStoreDep = Annotated[RepoConnectionStore, Depends(get_repo_connection_store)]


# --------------------------------------------------------------------------- #
# Client dependencies (overridable for tests / BYOK swap)                      #
# --------------------------------------------------------------------------- #


@lru_cache(maxsize=1)
def _integration_audit_log() -> AuditLog:
    """Process-wide audit log for integration operations (swappable in tests)."""
    return AuditLog()


def _github_audit_sink(audit_log: AuditLog) -> AuditSink:
    """Adapt a :class:`GitHubAuditEvent` onto the immutable audit log.

    Redaction is the single source of truth here (HARD-01 §8): ``detail`` runs
    through :func:`redact_text` and the metadata mapping through
    :func:`redact_mapping` so a token/JWT/PEM that ever slipped into a message is
    scrubbed before it can reach an audit row.
    """

    def _sink(event: GitHubAuditEvent) -> None:
        audit_log.record(
            category=AuditCategory.INTEGRATION,
            action=f"github.{event.action}",
            target=event.repo,
            status=event.status,
            payload_hash=event.payload_hash,
            latency_ms=event.latency_ms,
            detail=redact_text(event.detail) if event.detail else None,
            metadata=redact_mapping(
                {"status_code": event.status_code} if event.status_code else {}
            ),
        )

    return _sink


@lru_cache(maxsize=1)
def _github_client_singleton() -> GitHubClient | None:
    """Build the process-wide GitHub client from configuration.

    Prefers real GitHub App auth (JWT + minted installation tokens) when
    ``github_app_id`` + ``github_installation_id`` are configured; falls back to
    a static ``github_token`` for dev/back-compat. Returns ``None`` when neither
    is configured so write routes fail closed with ``501 Not Configured`` rather
    than silently faking a client.
    """
    settings = get_settings()
    if settings.github_app_id and settings.github_installation_id:
        try:
            private_key_pem = load_private_key(settings.github_app_private_key_path)
        except GitHubError:
            # Missing/unreadable key -> treat as not-configured (fail closed). The
            # error deliberately carries only the path, never the key (AC7).
            return None
        return GitHubClient.from_app(
            app_id=settings.github_app_id,
            private_key_pem=private_key_pem,
            installation_id=settings.github_installation_id,
            base_url=settings.github_api_url,
            audit_sink=_github_audit_sink(_integration_audit_log()),
        )
    if settings.github_token:
        return GitHubClient(token=settings.github_token, base_url=settings.github_api_url)
    return None


@lru_cache(maxsize=1)
def _slack_notifier_singleton() -> SlackNotifier:
    settings = get_settings()
    return SlackNotifier(token=settings.slack_token, default_channel=settings.slack_default_channel)


def get_github_client_optional() -> GitHubClient | None:
    """Return the GitHub client, or ``None`` when GitHub is not configured."""
    return _github_client_singleton()


def get_github_client(
    client: Annotated[GitHubClient | None, Depends(get_github_client_optional)],
) -> GitHubClient:
    """Return the process-wide GitHub client, failing closed when unconfigured.

    Overridable in tests via DI. Unconfigured write routes get ``501`` rather
    than a silent no-auth client.
    """
    if client is None:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="GitHub integration is not configured",
        )
    return client


def get_slack_notifier() -> SlackNotifier:
    """Return the process-wide Slack notifier (override in tests via DI)."""
    return _slack_notifier_singleton()


def get_github_webhook_secret() -> str | None:
    """Return the configured GitHub webhook signing secret (overridable in tests)."""
    return get_settings().github_webhook_secret


GitHubDep = Annotated[GitHubClient, Depends(get_github_client)]
GitHubOptionalDep = Annotated[GitHubClient | None, Depends(get_github_client_optional)]
SlackDep = Annotated[SlackNotifier, Depends(get_slack_notifier)]
WebhookSecretDep = Annotated[str | None, Depends(get_github_webhook_secret)]


# --------------------------------------------------------------------------- #
# Routes                                                                      #
# --------------------------------------------------------------------------- #


@router.post(
    "/github/repos/{connection_id}/sync",
    response_model=RepoSyncResult,
)
def sync_repo(
    client: GitHubDep,
    store: RepoConnStoreDep,
    principal: WriterDep,
    connection_id: uuid.UUID,
) -> RepoSyncResult:
    """Sync a connected repository (full or incremental by last-synced sha).

    The repository is resolved **server-side** from ``connection_id`` scoped to
    the caller's workspace — never from the request body — so a caller cannot
    point the server's privileged token at an arbitrary or another tenant's repo.
    An unknown or foreign connection id is reported as 404.
    """
    connection = store.get(principal.workspace_id, connection_id)
    if connection is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="repository connection not found",
        )
    try:
        return client.sync_repo(connection)
    except GitHubError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


@router.post(
    "/github/pull-requests",
    response_model=PullRequest,
    status_code=status.HTTP_201_CREATED,
    dependencies=[WriteGate],
)
def open_pr(client: GitHubDep, request: PullRequestRequest) -> PullRequest:
    """Open a pull request (optionally requesting reviewers / labels)."""
    try:
        return client.open_pr(request)
    except GitHubError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


@router.get(
    "/github/health",
    response_model=HealthResult,
    dependencies=[ReadGate],
)
def github_health(client: GitHubOptionalDep) -> HealthResult:
    """Live GitHub reachability probe (HARD-01 J2).

    With App creds configured this mints an installation token and hits
    ``/rate_limit``, turning the previously-mocked check into a real probe. When
    GitHub is not configured it reports ``healthy:false`` with a redacted reason
    (never the key). ``READ`` permission is required.
    """
    if client is None:
        return HealthResult(
            healthy=False,
            status="not_configured",
            message="GitHub integration is not configured",
        )
    return client.health()


@router.post("/github/webhooks", response_model=CIStatus)
async def github_webhook(
    request: Request,
    secret: WebhookSecretDep,
    x_hub_signature_256: Annotated[str | None, Header()] = None,
) -> CIStatus:
    """Ingest a GitHub webhook and return the parsed CI status (offline; pure).

    This route is intentionally outside the principal dependency so provider
    callbacks reach it, which makes the HMAC signature the *only* trust boundary.
    The raw body is verified against the configured webhook secret
    (``X-Hub-Signature-256``) before it is parsed; a missing/invalid signature —
    or an unconfigured secret — is rejected (fail-closed), so forged CI/status
    events cannot drive workflow transitions.
    """
    body = await request.body()
    if not secret or not verify_github_signature(secret, body, x_hub_signature_256):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing webhook signature",
        )
    try:
        event = WebhookEvent.model_validate_json(body)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="invalid webhook payload",
        ) from exc
    return parse_github_webhook(event)


@router.post(
    "/slack/notify",
    response_model=SlackDeliveryResult,
    dependencies=[WriteGate],
)
def slack_notify(notifier: SlackDep, message: SlackMessage) -> SlackDeliveryResult:
    """Send a Slack notification."""
    try:
        return notifier.notify(message)
    except SlackError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


__all__ = [
    "RepoConnectionStore",
    "get_github_client",
    "get_github_client_optional",
    "get_github_webhook_secret",
    "get_repo_connection_store",
    "get_slack_notifier",
    "router",
]
