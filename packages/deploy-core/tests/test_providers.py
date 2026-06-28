"""Deploy provider behaviour — github/webhook map status; null records."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from forge_deploy.errors import ProviderError
from forge_deploy.providers import (
    GitHubActionsProvider,
    GitHubDeploymentsProvider,
    NullDeployProvider,
    WebhookCommandProvider,
)
from forge_deploy.schemas import DeployHandle, DeployRequest, DeployStatus


def _req(**kw: Any) -> DeployRequest:
    base = {
        "deployment_id": uuid.uuid4(),
        "repo_id": "github.com/org/api",
        "environment": "staging",
        "commit_sha": "abc123",
        "config": {},
    }
    base.update(kw)
    return DeployRequest(**base)


def test_null_records_trigger_and_returns_handle() -> None:
    provider = NullDeployProvider()
    handle = provider.trigger(_req())
    assert handle.provider == "null"
    assert len(provider.triggered) == 1


def test_null_scripted_status_sequence() -> None:
    provider = NullDeployProvider(
        statuses=[
            DeployStatus(state="in_progress", finished=False),
            DeployStatus(state="success", finished=True),
        ]
    )
    handle = provider.trigger(_req())
    assert provider.get_status(handle).state == "in_progress"
    assert provider.get_status(handle).state == "success"
    assert provider.get_status(handle).state == "success"  # sticks on last


class _FakeGHClient:
    def __init__(self) -> None:
        self.dispatched: list[dict[str, Any]] = []
        self.created: list[dict[str, Any]] = []

    def dispatch_workflow(self, repo_id, *, workflow_file, ref, inputs):
        self.dispatched.append(
            {"repo_id": repo_id, "workflow_file": workflow_file, "inputs": inputs}
        )
        return {"id": "run-1", "html_url": "https://gh/run/1"}

    def create_deployment(self, repo_id, *, ref, environment, payload):
        self.created.append({"repo_id": repo_id, "ref": ref, "environment": environment})
        return {"id": "dep-9", "url": "https://gh/dep/9"}

    def get_deployment_status(self, repo_id, external_id):
        return {"state": "success", "description": "ok"}


def test_github_actions_dispatches_once_and_maps_status() -> None:
    client = _FakeGHClient()
    provider = GitHubActionsProvider(client)
    handle = provider.trigger(_req(config={"workflow_file": "deploy.yml", "ref": "main"}))
    assert len(client.dispatched) == 1
    assert handle.external_id == "run-1"
    status = provider.get_status(handle)
    assert status.state == "success" and status.finished is True


def test_github_deployments_creates_and_supports_rollback() -> None:
    client = _FakeGHClient()
    provider = GitHubDeploymentsProvider(client)
    handle = provider.trigger(_req(config={"environment": "production"}))
    assert client.created[0]["environment"] == "production"
    assert handle.external_id == "dep-9"
    assert provider.supports_native_rollback() is True


class _FakeWebhook:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def post(self, url, *, method, headers, json):
        self.calls.append({"url": url, "headers": headers, "json": json})
        return {"id": "wh-1", "url": url}


def test_webhook_posts_with_secret_header() -> None:
    client = _FakeWebhook()
    provider = WebhookCommandProvider(client)
    provider.trigger(
        _req(config={"url": "https://hooks/deploy", "secret": "s3cr3t", "secret_header": "X-Sig"})
    )
    assert client.calls[0]["headers"]["X-Sig"] == "s3cr3t"


def test_webhook_requires_url() -> None:
    provider = WebhookCommandProvider(_FakeWebhook())
    with pytest.raises(ProviderError):
        provider.trigger(_req(config={}))


def test_github_actions_error_normalized() -> None:
    class _Boom:
        def dispatch_workflow(self, *a, **k):
            raise RuntimeError("boom")

        def create_deployment(self, *a, **k):  # pragma: no cover
            raise RuntimeError("boom")

        def get_deployment_status(self, *a, **k):  # pragma: no cover
            return {}

    with pytest.raises(ProviderError):
        GitHubActionsProvider(_Boom()).trigger(_req())


def test_handle_roundtrip() -> None:
    h = DeployHandle(provider="null", external_id="x")
    assert h.url is None
