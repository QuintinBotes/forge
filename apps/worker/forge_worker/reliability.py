"""Worker reliability primitives (HARD-11).

Extends the existing Celery worker with the operational guarantees a self-hosted
operator needs to run Forge without losing or duplicating work:

* :class:`TransientError` — a marker exception for *retryable* failures (a DB /
  Redis / network blip). Deterministic failures raise their own exception type
  and surface immediately instead of burning the retry budget.
* :class:`ForgeTask` — a shared Celery task base that turns on ``acks_late`` (a
  task interrupted by SIGTERM / OOM is re-queued, not dropped), auto-retries
  :class:`TransientError` with exponential backoff + jitter, and exposes an
  idempotency guard (:meth:`ForgeTask.is_duplicate`) so a *re-delivered* message
  is a no-op rather than a double-run. ``acks_late`` is only safe paired with
  this dedup, so they ship together.
* :func:`configure_reliability` — applies the fair-dispatch / memory-bound /
  runaway-task Celery knobs, all env-driven so dev/test stay untouched.

Everything is default-on but degrades cleanly: the dedup guard falls back to an
in-process store when no Redis is reachable, so the unit suite needs no broker.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import celery

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = [
    "DedupBackend",
    "ForgeTask",
    "InMemoryDedupBackend",
    "RedisDedupBackend",
    "TransientError",
    "WorkerReliabilitySettings",
    "configure_reliability",
    "resolve_dedup_backend",
]


class TransientError(Exception):
    """A retryable failure (transient DB / Redis / network blip).

    Tasks that raise this are auto-retried by :class:`ForgeTask` with backoff;
    every other exception is treated as deterministic and surfaces at once.
    """


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class WorkerReliabilitySettings:
    """Env-driven Celery reliability knobs (``FORGE_``-prefixed, safe defaults)."""

    prefetch_multiplier: int = 1
    max_tasks_per_child: int = 200
    task_soft_time_limit: int = 300
    task_time_limit: int = 360
    max_retries: int = 3
    retry_backoff: bool = True
    acks_late: bool = True
    reject_on_worker_lost: bool = True

    @classmethod
    def from_env(cls) -> WorkerReliabilitySettings:
        """Resolve the reliability settings from the process environment."""
        return cls(
            prefetch_multiplier=_env_int("FORGE_WORKER_PREFETCH_MULTIPLIER", 1),
            max_tasks_per_child=_env_int("FORGE_WORKER_MAX_TASKS_PER_CHILD", 200),
            task_soft_time_limit=_env_int("FORGE_TASK_SOFT_TIME_LIMIT", 300),
            task_time_limit=_env_int("FORGE_TASK_TIME_LIMIT", 360),
            max_retries=_env_int("FORGE_TASK_MAX_RETRIES", 3),
            retry_backoff=_env_bool("FORGE_TASK_RETRY_BACKOFF", True),
            acks_late=_env_bool("FORGE_TASK_ACKS_LATE", True),
            reject_on_worker_lost=_env_bool("FORGE_TASK_REJECT_ON_WORKER_LOST", True),
        )


class DedupBackend(Protocol):
    """A SETNX-style dedup store keyed by an idempotency token."""

    def seen(self, key: str, *, ttl_s: int) -> bool:
        """Register ``key``; return ``True`` iff it was *already* present.

        The first caller for a key gets ``False`` (claimed the slot); every
        re-delivery inside the TTL gets ``True`` (duplicate — skip the work).
        """


class InMemoryDedupBackend:
    """Process-local dedup store (dev/test; no Redis).

    Thread-safe and TTL-aware so a re-enqueue after the window elapses is not a
    false duplicate. Not shared across worker processes — production uses
    :class:`RedisDedupBackend`; this keeps the unit suite broker-free.
    """

    def __init__(self) -> None:
        self._seen: dict[str, float] = {}  # key -> expiry monotonic ts
        self._lock = threading.Lock()

    def seen(self, key: str, *, ttl_s: int) -> bool:
        now = time.monotonic()
        with self._lock:
            self._evict(now)
            if key in self._seen:
                return True
            self._seen[key] = now + max(1, ttl_s)
            return False

    def _evict(self, now: float) -> None:
        expired = [k for k, exp in self._seen.items() if exp <= now]
        for k in expired:
            del self._seen[k]


class RedisDedupBackend:
    """Redis-backed dedup: ``SET key 1 NX EX ttl`` collapses re-deliveries."""

    def __init__(self, client: object) -> None:
        self._client = client

    def seen(self, key: str, *, ttl_s: int) -> bool:
        # redis-py returns True when the key was set (i.e. first sight), None
        # when NX failed (already present). Invert to "already seen".
        claimed = self._client.set(  # type: ignore[attr-defined]
            key, "1", nx=True, ex=max(1, ttl_s)
        )
        return not bool(claimed)


_DEDUP_LOCK = threading.Lock()
_DEDUP_BACKEND: DedupBackend | None = None


def resolve_dedup_backend() -> DedupBackend:
    """Return the process-wide dedup backend (Redis when reachable, else memory).

    Resolved once and cached. A Redis outage or a missing client degrades to the
    in-process backend rather than failing the task — dedup is a best-effort
    guard layered on top of the tasks' own content-hash idempotency.
    """
    global _DEDUP_BACKEND
    with _DEDUP_LOCK:
        if _DEDUP_BACKEND is not None:
            return _DEDUP_BACKEND
        _DEDUP_BACKEND = _build_dedup_backend()
        return _DEDUP_BACKEND


def set_dedup_backend(backend: DedupBackend | None) -> None:
    """Override the cached dedup backend (test seam)."""
    global _DEDUP_BACKEND
    with _DEDUP_LOCK:
        _DEDUP_BACKEND = backend


def _build_dedup_backend() -> DedupBackend:
    backend = os.environ.get("FORGE_TASK_DEDUP_BACKEND", "").strip().lower()
    if backend == "memory":
        return InMemoryDedupBackend()
    try:
        import redis

        url = os.environ.get("FORGE_REDIS_URL", "redis://localhost:6379/0")
        client = redis.Redis.from_url(url)
        return RedisDedupBackend(client)
    except Exception:
        # No redis client installed / unreachable — degrade to in-process.
        return InMemoryDedupBackend()


_DEDUP_TTL_S = _env_int("FORGE_TASK_DEDUP_TTL_SECONDS", 86_400)


class ForgeTask(celery.Task):
    """Shared reliability base for every Forge Celery task.

    ``acks_late`` re-queues a task interrupted mid-flight; :class:`TransientError`
    is auto-retried with exponential backoff + jitter; :meth:`is_duplicate`
    guards against a re-delivered message re-running non-idempotently.
    """

    autoretry_for = (TransientError,)
    retry_backoff = True
    retry_jitter = True
    # Celery's autoretry reads ``max_retries`` (via retry_kwargs it builds itself);
    # setting the class attribute is sufficient and avoids a mutable class default.
    max_retries = _env_int("FORGE_TASK_MAX_RETRIES", 3)
    acks_late = True
    reject_on_worker_lost = True

    #: Namespaced so a workspace/task tuple never collides with another task's key.
    dedup_prefix = "forge:task:dedup"

    def dedup_key(self, idem_key: str) -> str:
        """Namespace an idempotency token under this task's name."""
        return f"{self.dedup_prefix}:{self.name}:{idem_key}"

    def is_duplicate(self, idem_key: str | None, *, ttl_s: int | None = None) -> bool:
        """Return ``True`` when ``idem_key`` was already claimed for this task.

        ``None``/empty keys are never duplicates (idempotency is opt-in). The
        first claim within the TTL returns ``False`` and reserves the slot; every
        later delivery returns ``True`` so the caller can skip the side effect.
        """
        if not idem_key:
            return False
        backend = resolve_dedup_backend()
        return backend.seen(self.dedup_key(idem_key), ttl_s=ttl_s or _DEDUP_TTL_S)


def configure_reliability(
    app: celery.Celery, settings: WorkerReliabilitySettings | None = None
) -> WorkerReliabilitySettings:
    """Apply the env-driven reliability knobs to ``app``.

    Bounds in-flight work (fair prefetch), caps per-child task count (memory),
    sets soft/hard time limits (runaway tasks), and turns on ``acks_late`` +
    ``reject_on_worker_lost`` (interrupted tasks re-queue). Returns the resolved
    settings so callers/tests can assert what was applied.
    """
    cfg = settings or WorkerReliabilitySettings.from_env()
    app.conf.worker_prefetch_multiplier = cfg.prefetch_multiplier
    app.conf.worker_max_tasks_per_child = cfg.max_tasks_per_child
    app.conf.task_soft_time_limit = cfg.task_soft_time_limit
    app.conf.task_time_limit = cfg.task_time_limit
    app.conf.task_acks_late = cfg.acks_late
    app.conf.task_reject_on_worker_lost = cfg.reject_on_worker_lost
    app.conf.task_default_retry_delay = 1
    return cfg


def transient_errors() -> Iterable[type[BaseException]]:
    """The exception types treated as retryable (test/introspection seam)."""
    return (TransientError,)
