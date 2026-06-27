"""Inbound webhook parsing + signature verification.

Pure functions (no network): the API layer's webhook ingest route (Phase 2)
verifies a provider signature and maps the payload onto the frozen ``CIStatus``
DTO via :func:`parse_github_webhook`.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any

from forge_contracts import CheckResult, CIState, CIStatus, WebhookEvent

# --------------------------------------------------------------------------- #
# CI status mapping                                                            #
# --------------------------------------------------------------------------- #

# GitHub commit-status states map straight onto our CIState.
_STATUS_STATE: dict[str, CIState] = {
    "success": CIState.SUCCESS,
    "failure": CIState.FAILURE,
    "error": CIState.ERROR,
    "pending": CIState.PENDING,
}

# GitHub check-run / check-suite / workflow-run *conclusions* (only meaningful
# once status == "completed").
_CONCLUSION_STATE: dict[str, CIState] = {
    "success": CIState.SUCCESS,
    "neutral": CIState.SUCCESS,
    "skipped": CIState.SUCCESS,
    "failure": CIState.FAILURE,
    "timed_out": CIState.FAILURE,
    "cancelled": CIState.ERROR,
    "action_required": CIState.ERROR,
    "stale": CIState.ERROR,
    "startup_failure": CIState.ERROR,
}


def _status_state(value: str | None) -> CIState:
    return _STATUS_STATE.get((value or "").lower(), CIState.PENDING)


def _conclusion_state(status: str | None, conclusion: str | None) -> CIState:
    if (status or "").lower() != "completed":
        return CIState.PENDING
    return _CONCLUSION_STATE.get((conclusion or "").lower(), CIState.ERROR)


def _repo_full_name(payload: dict[str, Any]) -> str:
    repository = payload.get("repository") or {}
    return str(repository.get("full_name") or "")


def _parse_run_like(payload: dict[str, Any], key: str, repo: str) -> CIStatus:
    obj = payload.get(key) or {}
    state = _conclusion_state(obj.get("status"), obj.get("conclusion"))
    name = obj.get("name") or key
    url = obj.get("details_url") or obj.get("html_url")
    check = CheckResult(
        name=str(name),
        passed=state is CIState.SUCCESS,
        details=obj.get("conclusion"),
    )
    return CIStatus(
        repo=repo,
        sha=str(obj.get("head_sha") or ""),
        state=state,
        context=str(name),
        target_url=url,
        checks=[check],
    )


def parse_github_webhook(event: WebhookEvent) -> CIStatus:
    """Map a GitHub CI-related webhook onto a :class:`CIStatus`.

    Handles ``status``, ``check_run``, ``check_suite`` and ``workflow_run``
    events. Unknown event types resolve to a ``pending`` status rather than
    raising, so the ingest route can record-and-ignore non-CI deliveries.
    """
    payload = event.payload or {}
    repo = _repo_full_name(payload)
    event_type = (event.event_type or "").lower()

    if event_type == "status":
        return CIStatus(
            repo=repo,
            sha=str(payload.get("sha") or ""),
            state=_status_state(payload.get("state")),
            context=payload.get("context"),
            description=payload.get("description"),
            target_url=payload.get("target_url"),
        )

    if event_type in {"check_run", "check_suite", "workflow_run"}:
        return _parse_run_like(payload, event_type, repo)

    # Non-CI or unknown event: surface as pending so callers can no-op safely.
    return CIStatus(
        repo=repo,
        sha=str(payload.get("sha") or payload.get("after") or ""),
        state=CIState.PENDING,
        context=event_type or None,
    )


# --------------------------------------------------------------------------- #
# Signature verification                                                       #
# --------------------------------------------------------------------------- #


def sign_github_payload(secret: str, body: bytes) -> str:
    """Compute the ``X-Hub-Signature-256`` header value for ``body``."""
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_github_signature(
    secret: str, body: bytes, signature_header: str | None
) -> bool:
    """Constant-time verify a GitHub ``X-Hub-Signature-256`` header."""
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = sign_github_payload(secret, body)
    return hmac.compare_digest(expected, signature_header)


def verify_slack_signature(
    secret: str,
    timestamp: str,
    body: bytes | str,
    signature: str | None,
    *,
    max_skew_seconds: int = 300,
    now: float | None = None,
) -> bool:
    """Verify a Slack ``v0`` request signature with replay protection.

    Slack signs ``v0:{timestamp}:{body}`` with HMAC-SHA256. Requests older than
    ``max_skew_seconds`` are rejected to defeat replay attacks.
    """
    if not signature:
        return False
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    current = time.time() if now is None else now
    if abs(current - ts) > max_skew_seconds:
        return False
    raw = body.decode() if isinstance(body, bytes) else body
    basestring = f"v0:{timestamp}:{raw}".encode()
    expected = "v0=" + hmac.new(secret.encode(), basestring, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


__all__ = [
    "parse_github_webhook",
    "sign_github_payload",
    "verify_github_signature",
    "verify_slack_signature",
]
