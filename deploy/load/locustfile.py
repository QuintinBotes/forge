"""HARD-11 — Locust API hot-path load test (Python alternative to the k6 script).

Read-heavy + write-heavy user classes driving the same hot paths as
``deploy/load/k6/api_hotpaths.js`` for operators who prefer Locust.

Usage (headless, manual/nightly)::

    locust -f deploy/load/locustfile.py --headless -u 20 -r 5 -t 3m \\
        --host http://localhost:8000

Set ``FORGE_LOAD_API_KEY`` to a workspace API key to exercise the authenticated
routes; without it only ``/health`` is driven (no external creds needed).
"""

from __future__ import annotations

import os

try:  # locust is an optional, load-only dependency (not in the workspace lock)
    from locust import HttpUser, between, task
except ModuleNotFoundError:  # pragma: no cover - only importable where locust is installed
    HttpUser = object  # type: ignore[assignment,misc]

    def between(_a: float, _b: float):  # type: ignore[misc]
        return None

    def task(*_args, **_kwargs):  # type: ignore[misc]
        def _decorator(fn):
            return fn

        return _decorator


_API_KEY = os.environ.get("FORGE_LOAD_API_KEY", "")


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_API_KEY}"} if _API_KEY else {}


class ReadHeavyUser(HttpUser):  # type: ignore[misc]
    """Mostly reads: liveness, board listing, knowledge search."""

    wait_time = between(0.5, 2.0)

    @task(5)
    def health(self) -> None:
        self.client.get("/health", name="health")

    @task(3)
    def board(self) -> None:
        if _API_KEY:
            self.client.get("/board/tasks", headers=_auth_headers(), name="board")

    @task(2)
    def search(self) -> None:
        if _API_KEY:
            self.client.post(
                "/knowledge/search",
                json={"query": "server config", "k": 5},
                headers=_auth_headers(),
                name="knowledge_search",
            )


class WriteHeavyUser(HttpUser):  # type: ignore[misc]
    """Mostly writes: sync/index/agent-run enqueue (auth required)."""

    wait_time = between(1.0, 3.0)

    @task
    def enqueue_agent_run(self) -> None:
        if _API_KEY:
            self.client.post(
                "/agent/runs",
                json={"objective": "load-test objective"},
                headers={**_auth_headers(), "Idempotency-Key": os.urandom(8).hex()},
                name="agent_enqueue",
            )
