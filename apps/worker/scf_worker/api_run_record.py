"""The run record over SCF's API, for a run process that holds only its run token (#257).

The second implementation of the run-record port (:class:`~scf_worker.run_record.RunRecord`,
#238). Each operation goes to the run's own run-record route (#256) with the token the
worker issued at launch, so a run process opens no database connection (spec 011 FR-011,
FR-013). A transport error, a server error or a ``429`` is retried a few times; a
refusal is not. An event keeps one ``Idempotency-Key`` across its attempts, so a retry
stores it once.
"""

from __future__ import annotations

import logging
import math
import time
import uuid
from typing import Any

import httpx

from scf_contracts import AgentRunResult, RunEventDraft, Step, events_for_step

__all__ = ["ATTEMPTS", "ApiRunRecord", "RunRecordError"]

logger = logging.getLogger("scf_worker.api_run_record")

#: Attempts per operation; the n-th retry waits n times the delay first, or what a
#: ``429``'s ``Retry-After`` asks, up to the cap.
ATTEMPTS = 3
RETRY_DELAY_SECONDS = 0.5
MAX_RETRY_AFTER_SECONDS = 10.0
TIMEOUT = httpx.Timeout(30.0, connect=5.0)


class RunRecordError(Exception):
    """The API refused an operation or could not be reached; the message names no credential."""


class ApiRunRecord:
    """The run-record port over ``/agent/runs/{run_id}/record/*``, for one run.

    The token names the run's workspace and the API holds it to that; ``workspace_id``
    is accepted only to match the in-process record's signature.
    """

    def __init__(
        self,
        run_id: uuid.UUID,
        workspace_id: uuid.UUID,
        *,
        api_url: str,
        token: str,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._run_id = run_id
        self._client = httpx.Client(
            base_url=f"{api_url.rstrip('/')}/agent/runs/{run_id}/record",
            headers={"Authorization": f"Bearer {token}"},
            timeout=TIMEOUT,
            transport=transport,
        )

    def _send(self, method: str, path: str, **kwargs: Any) -> tuple[httpx.Response, bool]:
        """The API's answer, and whether an earlier attempt may have reached the route.

        A transport error, a 5xx or a ``429`` is retried; any other answer is returned.
        ``RunRecordError`` once every attempt failed.
        """
        error, delay, in_doubt = "", 0.0, False
        for attempt in range(1, ATTEMPTS + 1):
            if delay:
                time.sleep(delay)
            delay = RETRY_DELAY_SECONDS * attempt
            try:
                response = self._client.request(method, path, **kwargs)
            except httpx.TransportError as exc:
                error, in_doubt = f"{type(exc).__name__}: {exc}", True
                continue
            if response.status_code == 429:  # the rate limiter, before the route ran
                delay = min(
                    _seconds(response.headers.get("Retry-After"), delay), MAX_RETRY_AFTER_SECONDS
                )
            elif response.status_code < 500:
                return response, in_doubt
            else:
                in_doubt = True
            error = f"HTTP {response.status_code}"
        raise RunRecordError(f"{method} {path} failed {ATTEMPTS} times, last with {error}")

    def _expect(self, status: int, method: str, path: str, **kwargs: Any) -> httpx.Response:
        response, _ = self._send(method, path, **kwargs)
        if response.status_code != status:
            raise RunRecordError(f"{method} {path} was refused: HTTP {response.status_code}")
        return response

    def inputs(self) -> dict[str, Any] | None:
        return dict(self._expect(200, "GET", "/inputs").json())

    def emit(self, draft: RunEventDraft) -> bool:
        """Append one event; ``False`` (logged, never raised): an event never fails the run."""
        key = str(uuid.uuid4())  # the same on every attempt, so a retry stores it once
        try:
            self._expect(
                204,
                "POST",
                "/events",
                json=draft.model_dump(mode="json"),
                headers={"Idempotency-Key": key},
            )
        except RunRecordError as exc:
            logger.warning("could not record %s of run %s: %s", draft.type.value, self._run_id, exc)
            return False
        return True

    def on_step(self, step: Step) -> None:
        # Sequential, so the events' numbers follow the steps.
        for draft in events_for_step(step):
            self.emit(draft)

    def record_handoff(self, result: AgentRunResult) -> bool:
        """Once; ``RunRecordError`` naming the status or transport error when not stored."""
        response, in_doubt = self._send("POST", "/handoff", json=result.model_dump(mode="json"))
        # With a live token a 409 means this run's handoff is stored: after an attempt
        # whose answer was lost, that attempt stored it.
        if response.status_code == 204 or (in_doubt and response.status_code == 409):
            return True
        raise RunRecordError(f"POST /handoff was refused: HTTP {response.status_code}")


def _seconds(value: str | None, default: float) -> float:
    """A ``Retry-After`` in seconds; ``default`` for a date or anything unreadable."""
    try:
        seconds = float(value or "")
    except ValueError:
        return default
    return seconds if 0 <= seconds < math.inf else default  # also refuses nan
