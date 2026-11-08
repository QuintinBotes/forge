"""GitHub issue hints; callers verify HMAC before parsing, as in other PM adapters."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from forge_contracts.pm import PMProvider, WebhookEvent
from forge_integrations.pm.errors import ProviderError
from forge_integrations.webhooks import verify_github_signature


def verify_github_issues(body: bytes, headers: dict[str, str], secret: str) -> bool:
    signature = {k.lower(): v for k, v in headers.items()}.get("x-hub-signature-256")
    return bool(secret) and verify_github_signature(secret, body, signature)


def parse_github_issues(body: bytes, headers: dict[str, str], *, repo: str) -> WebhookEvent:
    try:
        data = json.loads(body)
    except ValueError:
        raise ProviderError("invalid github issues webhook JSON") from None
    if not isinstance(data, dict):
        raise ProviderError("invalid github issues webhook object")
    lowered = {k.lower(): v for k, v in headers.items()}
    kind = lowered.get("x-github-event", "")
    action = data.get("action")
    issue = data.get("issue") or {}
    repository = data.get("repository") or {}
    label = data.get("label") or {}
    if not all(isinstance(value, dict) for value in (issue, repository, label)):
        raise ProviderError("invalid github issues webhook fields")
    supported = (
        kind == "issues"
        and action in ("opened", "edited", "labeled", "unlabeled", "closed", "reopened")
    ) or (kind == "issue_comment" and action in ("created", "edited", "deleted"))
    number = issue.get("number")
    repository_name = repository.get("full_name")
    actionable = (
        supported
        and "pull_request" not in issue
        and isinstance(repository_name, str)
        and repository_name.lower() == repo.lower()
        and isinstance(number, int)
        and not isinstance(number, bool)
        and number > 0
    )
    return WebhookEvent(
        provider=PMProvider.github_issues,
        delivery_id=lowered.get("x-github-delivery") or hashlib.sha256(body).hexdigest(),
        event_type="issue.created" if kind == "issues" and action == "opened" else "issue.updated",
        external_id=str(number) if actionable else None,
        external_key=f"{repo}#{number}" if actionable else None,
        signature_valid=True,
        received_at=datetime.now(UTC),
        payload={"action": action, "event": kind, "label": label.get("name")},
    )
