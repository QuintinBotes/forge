"""M1: GitHub Issues REST fixtures, mapping, webhooks and shared-client behavior."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from forge_contracts.enums import Direction
from forge_contracts.pm import ForgePriority, ForgeTask, HttpResponse, PMProvider, StatusCategory
from forge_integrations import GitHubClient, RetryPolicy, sign_github_payload
from forge_integrations.pm import GitHubIssuesAdapter, build_adapter
from forge_integrations.pm.errors import (
    ExternalNotFound,
    MappingError,
    PMAuthError,
    ProviderError,
    RateLimitError,
)

from .conftest import load_json


def task(**overrides) -> ForgeTask:
    return ForgeTask.model_validate(
        {
            "id": uuid4(),
            "key": "M1-1",
            "project_id": uuid4(),
            "title": "Updated title",
            "description_md": "Updated **body**",
            "status_category": "started",
            "priority": "high",
            "label_names": ["bug", "status:backlog", "priority:low"],
            "updated_at": datetime.now(UTC),
            **overrides,
        }
    )


async def test_fetch_fixture(github_issues_adapter):
    external = await github_issues_adapter.fetch_external("42")
    assert external.provider == PMProvider.github_issues
    assert external.external_id == "42"  # REST uses the repository number, not the global id.
    assert external.external_key == "acme/widgets#42"
    assert external.url == "https://github.com/acme/widgets/issues/42"
    assert external.description_md.startswith("## Acceptance criteria")
    assert external.status_category == StatusCategory.started
    assert external.priority_token == "priority:high"
    assert external.assignee_external_id == "octocat"
    assert external.external_updated_at == datetime(2026, 11, 7, 10, tzinfo=UTC)
    assert external.raw == {}


@pytest.mark.parametrize("status", list(StatusCategory))
@pytest.mark.parametrize("priority", list(ForgePriority))
async def test_default_maps_round_trip(
    status, priority, github_issues_adapter, github_issues_requests
):
    external = await github_issues_adapter.update_external(
        "42", task(status_category=status, priority=priority)
    )
    assert external.status_category == status
    assert github_issues_adapter.map_priority(external.priority_token, Direction.IN) == priority
    payload = json.loads(github_issues_requests[-1].content)
    assert payload["title"] == "Updated title"
    assert payload["body"] == "Updated **body**"
    assert "bug" in payload["labels"]
    assert len([label for label in payload["labels"] if label.startswith("priority:")]) == 1
    assert payload["state"] == ("closed" if status in ("completed", "canceled") else "open")


@pytest.mark.parametrize("status", list(StatusCategory))
@pytest.mark.parametrize("priority", list(ForgePriority))
async def test_configured_maps_round_trip(
    status, priority, github_issues_ctx, github_issues_transport
):
    ctx = github_issues_ctx.model_copy(
        update={
            "status_map": {value.value: f"workflow:{value.value}" for value in StatusCategory},
            "priority_map": {value.value: f"severity:{value.value}" for value in ForgePriority},
        }
    )
    with GitHubClient(transport=github_issues_transport) as client:
        adapter = GitHubIssuesAdapter(client, ctx)
        external = await adapter.update_external(
            "42", task(status_category=status, priority=priority)
        )
    assert external.status_category == status
    assert external.status_name == f"workflow:{status}"
    assert external.priority_token == f"severity:{priority}"
    assert adapter.map_status(external.status_name.upper(), Direction.IN) == status
    assert adapter.map_priority(external.priority_token.upper(), Direction.IN) == priority
    assert "status:backlog" not in external.labels
    assert "priority:low" not in external.labels


@pytest.mark.parametrize(
    "state,labels,expected",
    [
        ("open", [], "unstarted"),
        ("closed", [], "completed"),
        ("closed", ["status:in-progress"], "completed"),
        ("closed", ["status:canceled"], "canceled"),
        ("open", ["status:canceled"], "unstarted"),
        ("open", ["unknown", "priority:unknown"], "unstarted"),
    ],
)
async def test_state_fallback_and_stale_labels(state, labels, expected, github_issues_ctx):
    issue = {**load_json("github_issues/issue.json"), "state": state, "labels": labels}
    with GitHubClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=issue))
    ) as client:
        external = await GitHubIssuesAdapter(client, github_issues_ctx).fetch_external("42")
    assert external.status_category == expected
    assert external.priority_token is None


async def test_create_then_close_and_reopen(github_issues_adapter, github_issues_requests):
    created = await github_issues_adapter.create_external(task(status_category="completed"))
    assert created.status_category == StatusCategory.completed
    assert [request.method for request in github_issues_requests] == ["POST", "PATCH"]
    assert "state" not in json.loads(github_issues_requests[0].content)
    reopened = await github_issues_adapter.update_external("42", task(status_category="started"))
    assert reopened.status_category == StatusCategory.started
    assert json.loads(github_issues_requests[-1].content)["state"] == "open"


@pytest.mark.parametrize("status,state", [("started", "open"), ("canceled", "closed")])
async def test_native_state_overrides_round_trip(
    status, state, github_issues_ctx, github_issues_transport
):
    ctx = github_issues_ctx.model_copy(update={"status_map": {status: state}})
    with GitHubClient(transport=github_issues_transport) as client:
        adapter = GitHubIssuesAdapter(client, ctx)
        external = await adapter.update_external("42", task(status_category=status))
    assert external.status_category == status
    assert external.status_name == state


async def test_comment(github_issues_adapter, github_issues_requests):
    assert await github_issues_adapter.comment_external("42", "Run completed") == "1001"
    request = github_issues_requests[-1]
    assert request.method == "POST"
    assert request.url.path == "/repos/acme/widgets/issues/42/comments"
    assert json.loads(request.content) == {"body": "Run completed"}


async def test_list_skips_prs_even_with_null_pull_request_and_preserves_cursor(github_issues_ctx):
    issue = load_json("github_issues/issue.json")
    pull = {**load_json("github_issues/pull_request.json"), "pull_request": None}
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.params["page"] == "1":
            return httpx.Response(
                200,
                json=[pull],
                headers={
                    "Link": '<https://api.github.com/repos/acme/widgets/issues?page=2>; rel="next"'
                },
            )
        return httpx.Response(200, json=[issue])

    with GitHubClient(transport=httpx.MockTransport(handler)) as client:
        adapter = GitHubIssuesAdapter(client, github_issues_ctx)
        first, cursor = await adapter.list_external(limit=1)
        assert first == [] and cursor == "2"
        second, cursor = await adapter.list_external(cursor=cursor, limit=1)
    assert [item.external_id for item in second] == ["42"]
    assert cursor is None
    assert all(request.url.params["state"] == "all" for request in requests)


@pytest.mark.parametrize("operation", ["fetch", "update", "comment"])
async def test_pr_number_is_never_read_or_written_as_issue(
    operation, github_issues_adapter, github_issues_requests
):
    with pytest.raises(ExternalNotFound, match="pull requests"):
        if operation == "fetch":
            await github_issues_adapter.fetch_external("43")
        elif operation == "update":
            await github_issues_adapter.update_external("43", task())
        else:
            await github_issues_adapter.comment_external("43", "do not post")
    assert [request.method for request in github_issues_requests] == ["GET"]


@pytest.mark.parametrize(
    "action", ["opened", "edited", "labeled", "unlabeled", "closed", "reopened"]
)
def test_issue_webhooks_and_label_hint(action, github_issues_adapter):
    payload = {**load_json("github_issues/issue_labeled.json"), "action": action}
    raw = json.dumps(payload).encode()
    headers = {
        "X-GitHub-Event": "issues",
        "X-GitHub-Delivery": "delivery-42",
        "X-Hub-Signature-256": sign_github_payload("secret", raw),
    }
    assert github_issues_adapter.verify_webhook(raw, headers, "secret")
    event = github_issues_adapter.parse_webhook(raw, headers)
    assert event.event_type == ("issue.created" if action == "opened" else "issue.updated")
    assert event.external_id == "42"
    assert event.external_key == "acme/widgets#42"
    assert event.delivery_id == "delivery-42"
    assert event.signature_valid
    assert event.payload == {"action": action, "event": "issues", "label": "forge:run"}


@pytest.mark.parametrize("action", ["created", "edited", "deleted"])
def test_comment_webhooks_are_issue_update_hints(action, github_issues_adapter):
    raw = json.dumps({**load_json("github_issues/issue_comment.json"), "action": action}).encode()
    event = github_issues_adapter.parse_webhook(raw, {"x-github-event": "issue_comment"})
    assert event.external_id == "42"
    assert event.event_type == "issue.updated"
    assert "body" not in json.dumps(event.payload)
    assert (
        event.delivery_id
        == github_issues_adapter.parse_webhook(raw, {"x-github-event": "issue_comment"}).delivery_id
    )


@pytest.mark.parametrize("kind", ["issues", "issue_comment"])
def test_pr_webhooks_have_no_actionable_id(kind, github_issues_adapter):
    payload = load_json(
        "github_issues/issue_comment.json"
        if kind == "issue_comment"
        else "github_issues/issue_labeled.json"
    )
    payload["issue"]["pull_request"] = None
    event = github_issues_adapter.parse_webhook(
        json.dumps(payload).encode(), {"X-GitHub-Event": kind}
    )
    assert event.external_id is None and event.external_key is None


@pytest.mark.parametrize(
    "change,kind",
    [
        ({"repository": {"full_name": "other/repo"}}, "issues"),
        ({"action": "unsupported"}, "issues"),
        ({}, "pull_request"),
        ({"issue": {"number": "42"}}, "issues"),
    ],
)
def test_unrelated_webhooks_have_no_actionable_id(change, kind, github_issues_adapter):
    raw = json.dumps({**load_json("github_issues/issue_labeled.json"), **change}).encode()
    assert github_issues_adapter.parse_webhook(raw, {"X-GitHub-Event": kind}).external_id is None


def test_hmac_rejects_forged_tampered_missing_and_empty_secret(github_issues_adapter):
    raw = json.dumps(load_json("github_issues/issue_labeled.json")).encode()
    headers = {"X-Hub-Signature-256": sign_github_payload("secret", raw)}
    assert not github_issues_adapter.verify_webhook(raw + b" ", headers, "secret")
    assert not github_issues_adapter.verify_webhook(raw, headers, "wrong")
    assert not github_issues_adapter.verify_webhook(raw, {}, "secret")
    assert not github_issues_adapter.verify_webhook(
        raw, {"x-hub-signature-256": "sha1=bad"}, "secret"
    )
    assert not github_issues_adapter.verify_webhook(
        raw, {"x-hub-signature-256": sign_github_payload("", raw)}, ""
    )


@pytest.mark.parametrize("raw", [b"broken", b"[]", b"null", b'{"issue": "invalid"}'])
def test_malformed_webhook_rejected(raw, github_issues_adapter):
    with pytest.raises(ProviderError, match="invalid github issues webhook"):
        github_issues_adapter.parse_webhook(raw, {})


async def test_registry_uses_supplied_pm_transport(
    github_issues_ctx, github_issues_transport, github_issues_requests
):
    loop = asyncio.get_running_loop()
    async with httpx.AsyncClient(transport=github_issues_transport) as http:

        class Transport:
            async def request(self, method, url, *, headers=None, json=None, params=None):
                assert asyncio.get_running_loop() is loop
                response = await http.request(
                    method, url, headers=headers, json=json, params=params
                )
                return HttpResponse(
                    status_code=response.status_code,
                    json_body=response.json(),
                    headers=dict(response.headers),
                )

        adapter = build_adapter(
            "github_issues", Transport(), github_issues_ctx, auth_header="Bearer installation-token"
        )
        assert isinstance(adapter, GitHubIssuesAdapter)
        await adapter.create_external(task())
        await adapter.update_external("42", task(status_category="completed"))
        await adapter.comment_external("42", "Done")
        tasks, _ = await adapter.list_external()
        assert len(tasks) == 1
        assert (await adapter.get_connection_health()).status == "connected"
    assert all(
        request.headers["authorization"] == "Bearer installation-token"
        for request in github_issues_requests
    )


async def test_registry_preserves_github_retry_for_pm_rate_limits(
    github_issues_ctx, github_issues_transport
):
    attempts = []
    async with httpx.AsyncClient(transport=github_issues_transport) as http:

        class Transport:
            async def request(self, method, url, *, headers=None, json=None, params=None):
                attempts.append(method)
                if len(attempts) == 1:
                    raise RateLimitError("rate limited", retry_after=0)
                response = await http.request(
                    method, url, headers=headers, json=json, params=params
                )
                return HttpResponse(status_code=response.status_code, json_body=response.json())

        adapter = build_adapter("github_issues", Transport(), github_issues_ctx)
        assert (await adapter.fetch_external("42")).external_id == "42"
    assert attempts == ["GET", "GET"]


@pytest.fixture(scope="module")
def private_key():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


async def test_installation_token_refresh_retry_and_audit_redaction(github_issues_ctx, private_key):
    minted = []
    attempts = []
    audit = []
    sleeps = []
    tokens = ["ghs_fixture_token_one", "ghs_fixture_token_two"]

    def handler(request):
        if request.url.path == "/app/installations/987654/access_tokens":
            assert request.headers["authorization"].startswith("Bearer ey")
            minted.append(tokens[len(minted)])
            return httpx.Response(
                201, json={"token": minted[-1], "expires_at": "2099-01-01T00:00:00Z"}
            )
        attempts.append(request.headers["authorization"])
        if len(attempts) == 1:
            return httpx.Response(401, json={"message": tokens[0]})
        if len(attempts) == 2:
            return httpx.Response(503, json={"message": tokens[1]})
        return httpx.Response(200, json=load_json("github_issues/issue.json"))

    with GitHubClient.from_app(
        app_id="123456",
        private_key_pem=private_key,
        installation_id="987654",
        transport=httpx.MockTransport(handler),
        retry=RetryPolicy(max_attempts=3, jitter=False),
        audit_sink=audit.append,
        sleep=sleeps.append,
    ) as client:
        adapter = GitHubIssuesAdapter(client, github_issues_ctx)
        assert (await adapter.fetch_external("42")).external_id == "42"
    assert minted == tokens
    assert attempts == [f"Bearer {tokens[0]}", f"Bearer {tokens[1]}", f"Bearer {tokens[1]}"]
    assert len(sleeps) == 1
    assert len(audit) == 1 and audit[0].status == "ok"
    assert all(token not in str(audit) for token in tokens)
    assert "PRIVATE KEY" not in str(audit)


@pytest.mark.parametrize(
    "code,error",
    [(401, PMAuthError), (403, PMAuthError), (404, ExternalNotFound), (500, ProviderError)],
)
async def test_errors_and_health_do_not_leak_upstream_body(code, error, github_issues_ctx):
    secret = "ghs_do_not_leak"
    with GitHubClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(code, json={"message": secret}))
    ) as client:
        adapter = GitHubIssuesAdapter(client, github_issues_ctx)
        with pytest.raises(error) as exc:
            await adapter.fetch_external("42")
        assert secret not in str(exc.value)
        health = await adapter.get_connection_health()
        assert health.status == "error" and secret not in health.error


@pytest.mark.parametrize("bad_id", ["0", "-1", "../pulls/42", "https://evil.example", "42?x=1"])
async def test_invalid_ids_never_reach_transport(
    bad_id, github_issues_adapter, github_issues_requests
):
    with pytest.raises(ProviderError):
        await github_issues_adapter.fetch_external(bad_id)
    assert github_issues_requests == []


def test_unknown_mappings_fail(github_issues_adapter):
    for direction in Direction:
        with pytest.raises(MappingError):
            github_issues_adapter.map_status("unknown", direction)
        with pytest.raises(MappingError):
            github_issues_adapter.map_priority("unknown", direction)
