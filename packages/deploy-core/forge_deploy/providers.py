"""Deploy providers — the pluggable port that triggers an actual deploy.

Every provider implements the :class:`DeployProvider` Protocol; all external I/O
(GitHub API, HTTP webhook) goes through an injected client so the engine and its
tests never make real network calls. Status is normalized to
``pending|in_progress|success|failure|error``.

The methods are synchronous to match Forge's sync SQLAlchemy session substrate
(the slice doc sketches ``async def``; the foundation is sync — deviation noted).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from forge_deploy.errors import ProviderError
from forge_deploy.schemas import DeployHandle, DeployRequest, DeployStatus


@runtime_checkable
class DeployProvider(Protocol):
    name: str

    def trigger(self, req: DeployRequest) -> DeployHandle: ...
    def get_status(self, handle: DeployHandle) -> DeployStatus: ...
    def supports_native_rollback(self) -> bool: ...


@runtime_checkable
class GitHubDeployClient(Protocol):
    """Narrow client surface the GitHub providers depend on (F03 adapter alias).

    Injected so no real GitHub call happens in tests.
    """

    def create_deployment(
        self, repo_id: str, *, ref: str, environment: str, payload: dict[str, Any]
    ) -> dict[str, Any]: ...

    def dispatch_workflow(
        self, repo_id: str, *, workflow_file: str, ref: str, inputs: dict[str, Any]
    ) -> dict[str, Any]: ...

    def get_deployment_status(self, repo_id: str, external_id: str) -> dict[str, Any]: ...


@runtime_checkable
class WebhookClient(Protocol):
    def post(
        self, url: str, *, method: str, headers: dict[str, str], json: dict[str, Any]
    ) -> dict[str, Any]: ...


_GITHUB_STATE_MAP = {
    "queued": "pending",
    "pending": "pending",
    "in_progress": "in_progress",
    "success": "success",
    "failure": "failure",
    "error": "error",
    "inactive": "failure",
}


class NullDeployProvider:
    """Test double: records triggers and returns scripted statuses; no I/O."""

    name = "null"

    def __init__(self, statuses: list[DeployStatus] | None = None) -> None:
        self.triggered: list[DeployRequest] = []
        self._statuses = list(statuses or [DeployStatus(state="success", finished=True)])
        self._idx = 0

    def trigger(self, req: DeployRequest) -> DeployHandle:
        self.triggered.append(req)
        return DeployHandle(provider=self.name, external_id=f"null-{len(self.triggered)}", url=None)

    def get_status(self, handle: DeployHandle) -> DeployStatus:
        if self._idx < len(self._statuses) - 1:
            status = self._statuses[self._idx]
            self._idx += 1
            return status
        return self._statuses[-1]

    def supports_native_rollback(self) -> bool:
        return False


class GitHubActionsProvider:
    """Triggers a deploy via Actions ``workflow_dispatch`` on the injected client."""

    name = "github_actions"

    def __init__(self, client: GitHubDeployClient) -> None:
        self._client = client

    def trigger(self, req: DeployRequest) -> DeployHandle:
        cfg = req.config
        workflow_file = cfg.get("workflow_file", "deploy.yml")
        ref = cfg.get("ref", req.commit_sha)
        inputs = dict(cfg.get("inputs", {}))
        inputs.setdefault("environment", req.environment)
        inputs.setdefault("commit_sha", req.commit_sha)
        try:
            result = self._client.dispatch_workflow(
                req.repo_id, workflow_file=workflow_file, ref=ref, inputs=inputs
            )
        except Exception as exc:
            raise ProviderError(f"github_actions dispatch failed: {exc}") from exc
        return DeployHandle(
            provider=self.name,
            external_id=str(result.get("id", "")),
            url=result.get("html_url"),
        )

    def get_status(self, handle: DeployHandle) -> DeployStatus:
        raw = self._client.get_deployment_status(handle.external_id, handle.external_id)
        return _map_github_status(raw)

    def supports_native_rollback(self) -> bool:
        return False


class GitHubDeploymentsProvider:
    """Triggers a deploy via the GitHub Deployments API on the injected client."""

    name = "github_deployments"

    def __init__(self, client: GitHubDeployClient) -> None:
        self._client = client

    def trigger(self, req: DeployRequest) -> DeployHandle:
        cfg = req.config
        environment = cfg.get("environment", req.environment)
        try:
            result = self._client.create_deployment(
                req.repo_id,
                ref=req.commit_sha,
                environment=environment,
                payload=dict(cfg.get("payload", {})),
            )
        except Exception as exc:
            raise ProviderError(f"github_deployments create failed: {exc}") from exc
        return DeployHandle(
            provider=self.name,
            external_id=str(result.get("id", "")),
            url=result.get("url"),
        )

    def get_status(self, handle: DeployHandle) -> DeployStatus:
        raw = self._client.get_deployment_status(handle.external_id, handle.external_id)
        return _map_github_status(raw)

    def supports_native_rollback(self) -> bool:
        return True


class WebhookCommandProvider:
    """Triggers a deploy by POSTing to a configured webhook with a secret header."""

    name = "webhook"

    def __init__(self, client: WebhookClient) -> None:
        self._client = client

    def trigger(self, req: DeployRequest) -> DeployHandle:
        cfg = req.config
        url = cfg.get("url")
        if not url:
            raise ProviderError("webhook provider requires a 'url'")
        method = cfg.get("method", "POST")
        headers = dict(cfg.get("headers", {}))
        secret = cfg.get("secret")
        if secret:
            headers[cfg.get("secret_header", "X-Forge-Signature")] = secret
        body = {
            "deployment_id": str(req.deployment_id),
            "repo_id": req.repo_id,
            "environment": req.environment,
            "commit_sha": req.commit_sha,
            "artifact_ref": req.artifact_ref,
        }
        try:
            result = self._client.post(url, method=method, headers=headers, json=body)
        except Exception as exc:
            raise ProviderError(f"webhook deploy failed: {exc}") from exc
        return DeployHandle(
            provider=self.name,
            external_id=str(result.get("id", str(req.deployment_id))),
            url=result.get("url", url),
        )

    def get_status(self, handle: DeployHandle) -> DeployStatus:
        # Webhook providers report terminal status via the provider callback.
        return DeployStatus(state="in_progress", finished=False)

    def supports_native_rollback(self) -> bool:
        return False


def _map_github_status(raw: dict[str, Any]) -> DeployStatus:
    state = _GITHUB_STATE_MAP.get(str(raw.get("state", "pending")), "pending")
    finished = state in {"success", "failure", "error"}
    return DeployStatus(state=state, detail=raw.get("description"), finished=finished)


__all__ = [
    "DeployProvider",
    "GitHubActionsProvider",
    "GitHubDeployClient",
    "GitHubDeploymentsProvider",
    "NullDeployProvider",
    "WebhookClient",
    "WebhookCommandProvider",
]
