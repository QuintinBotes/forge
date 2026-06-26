"""Integration SDK router stubs (filled by Task 1.13 — integration-sdk).

GitHub (repo sync, PRs, reviews, CI webhooks) and Slack notifications. The
webhook ingest route is intentionally outside the principal dependency so
provider-signed callbacks reach it (signature verification lands in Task 1.13).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from forge_api._stubs import NotImplementedResponse, eventual, not_implemented
from forge_api.deps import CurrentPrincipal, get_current_principal
from forge_contracts import (
    CIStatus,
    PullRequest,
    RepoSyncResult,
    SlackDeliveryResult,
)

router = APIRouter(
    prefix="/integration",
    tags=["integration"],
    responses={501: {"model": NotImplementedResponse}},
)

_R = "integration"


@router.post(
    "/github/repos/{connection_id}/sync",
    response_model=NotImplementedResponse,
    status_code=501,
    responses=eventual(RepoSyncResult, "Sync a connected repository."),
    dependencies=[Depends(get_current_principal)],
)
def sync_repo(connection_id: str, principal: CurrentPrincipal) -> NotImplementedResponse:
    return not_implemented(_R, "sync_repo")


@router.post(
    "/github/pull-requests",
    response_model=NotImplementedResponse,
    status_code=501,
    responses=eventual(PullRequest, "Open a pull request."),
    dependencies=[Depends(get_current_principal)],
)
def open_pr(principal: CurrentPrincipal) -> NotImplementedResponse:
    return not_implemented(_R, "open_pr")


@router.post(
    "/github/webhooks",
    response_model=NotImplementedResponse,
    status_code=501,
    responses=eventual(CIStatus, "Ingest a GitHub webhook (signature-verified)."),
)
def github_webhook() -> NotImplementedResponse:
    return not_implemented(_R, "parse_webhook")


@router.post(
    "/slack/notify",
    response_model=NotImplementedResponse,
    status_code=501,
    responses=eventual(SlackDeliveryResult, "Send a Slack notification."),
    dependencies=[Depends(get_current_principal)],
)
def slack_notify(principal: CurrentPrincipal) -> NotImplementedResponse:
    return not_implemented(_R, "notify")
