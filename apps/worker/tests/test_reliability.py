"""HARD-11: worker reliability primitives (hermetic, no broker).

Covers the ``ForgeTask`` base (acks_late + autoretry + SETNX dedup), the
``configure_reliability`` Celery knobs, and the env-driven settings — the
reliability half of the worker coverage gate.
"""

from __future__ import annotations

import pytest
from celery import Celery
from celery.exceptions import SoftTimeLimitExceeded

import forge_worker.reliability as reliability
from forge_worker.agent_runner import run_agent_task
from forge_worker.indexer import index_source_task
from forge_worker.reliability import (
    ForgeTask,
    InMemoryDedupBackend,
    RedisDedupBackend,
    TransientError,
    WorkerReliabilitySettings,
    configure_reliability,
    resolve_dedup_backend,
    set_dedup_backend,
    transient_errors,
)
from forge_worker.syncer import sync_source_task


@pytest.fixture(autouse=True)
def _fresh_dedup_backend() -> None:
    """Isolate each test with a fresh in-process dedup backend."""
    set_dedup_backend(InMemoryDedupBackend())
    yield
    set_dedup_backend(None)


# --------------------------------------------------------------------------- #
# WorkerReliabilitySettings / configure_reliability                            #
# --------------------------------------------------------------------------- #


def test_settings_from_env_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in [
        "FORGE_WORKER_PREFETCH_MULTIPLIER",
        "FORGE_WORKER_MAX_TASKS_PER_CHILD",
        "FORGE_TASK_SOFT_TIME_LIMIT",
        "FORGE_TASK_TIME_LIMIT",
        "FORGE_TASK_MAX_RETRIES",
        "FORGE_TASK_RETRY_BACKOFF",
        "FORGE_TASK_ACKS_LATE",
    ]:
        monkeypatch.delenv(var, raising=False)
    cfg = WorkerReliabilitySettings.from_env()
    assert cfg.prefetch_multiplier == 1
    assert cfg.max_tasks_per_child == 200
    assert cfg.task_soft_time_limit == 300
    assert cfg.task_time_limit == 360
    assert cfg.max_retries == 3
    assert cfg.retry_backoff is True
    assert cfg.acks_late is True


def test_settings_from_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORGE_WORKER_PREFETCH_MULTIPLIER", "4")
    monkeypatch.setenv("FORGE_WORKER_MAX_TASKS_PER_CHILD", "50")
    monkeypatch.setenv("FORGE_TASK_SOFT_TIME_LIMIT", "10")
    monkeypatch.setenv("FORGE_TASK_TIME_LIMIT", "20")
    monkeypatch.setenv("FORGE_TASK_MAX_RETRIES", "7")
    monkeypatch.setenv("FORGE_TASK_RETRY_BACKOFF", "false")
    monkeypatch.setenv("FORGE_TASK_ACKS_LATE", "no")
    cfg = WorkerReliabilitySettings.from_env()
    assert cfg.prefetch_multiplier == 4
    assert cfg.max_tasks_per_child == 50
    assert cfg.task_soft_time_limit == 10
    assert cfg.task_time_limit == 20
    assert cfg.max_retries == 7
    assert cfg.retry_backoff is False
    assert cfg.acks_late is False


def test_settings_from_env_bad_int_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORGE_TASK_MAX_RETRIES", "not-a-number")
    monkeypatch.setenv("FORGE_TASK_SOFT_TIME_LIMIT", "")
    cfg = WorkerReliabilitySettings.from_env()
    assert cfg.max_retries == 3
    assert cfg.task_soft_time_limit == 300


def test_configure_reliability_applies_celery_knobs() -> None:
    app = Celery("t")
    cfg = configure_reliability(
        app,
        WorkerReliabilitySettings(
            prefetch_multiplier=2,
            max_tasks_per_child=99,
            task_soft_time_limit=11,
            task_time_limit=22,
            acks_late=True,
            reject_on_worker_lost=True,
        ),
    )
    assert app.conf.worker_prefetch_multiplier == 2
    assert app.conf.worker_max_tasks_per_child == 99
    assert app.conf.task_soft_time_limit == 11
    assert app.conf.task_time_limit == 22
    assert app.conf.task_acks_late is True
    assert app.conf.task_reject_on_worker_lost is True
    assert cfg.prefetch_multiplier == 2


def test_configure_reliability_reads_env_when_no_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FORGE_WORKER_PREFETCH_MULTIPLIER", "3")
    app = Celery("t2")
    configure_reliability(app)
    assert app.conf.worker_prefetch_multiplier == 3


# --------------------------------------------------------------------------- #
# ForgeTask base config                                                        #
# --------------------------------------------------------------------------- #


def test_forgetask_reliability_attributes() -> None:
    assert ForgeTask.acks_late is True
    assert ForgeTask.reject_on_worker_lost is True
    assert TransientError in ForgeTask.autoretry_for
    assert ForgeTask.retry_backoff is True
    assert ForgeTask.retry_jitter is True


def test_forgetask_autoretries_transient_error_in_eager_mode() -> None:
    app = Celery("retry-eager")
    app.conf.task_always_eager = True
    app.conf.task_eager_propagates = False
    calls = {"n": 0}

    @app.task(bind=True, base=ForgeTask, max_retries=2, default_retry_delay=0)
    def flaky(self: ForgeTask) -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise TransientError("blip")
        return "ok"

    result = flaky.apply()
    # Two TransientError raises, third attempt succeeds -> 3 invocations total.
    assert calls["n"] == 3
    assert result.result == "ok"


def test_forgetask_does_not_retry_deterministic_error() -> None:
    app = Celery("deterministic")
    app.conf.task_always_eager = True
    app.conf.task_eager_propagates = False
    calls = {"n": 0}

    @app.task(bind=True, base=ForgeTask, max_retries=3, default_retry_delay=0)
    def boom(self: ForgeTask) -> str:
        calls["n"] += 1
        raise ValueError("deterministic")

    result = boom.apply()
    assert calls["n"] == 1  # not retried
    assert result.failed()


# --------------------------------------------------------------------------- #
# Dedup guard (is_duplicate / backends)                                        #
# --------------------------------------------------------------------------- #


def test_is_duplicate_first_then_replay() -> None:
    task = ForgeTask()
    task.name = "forge.test.dedup"
    assert task.is_duplicate("key-1") is False  # first sight claims the slot
    assert task.is_duplicate("key-1") is True  # replay is a duplicate
    assert task.is_duplicate("key-2") is False  # different key is independent


def test_is_duplicate_ignores_empty_key() -> None:
    task = ForgeTask()
    task.name = "forge.test.dedup"
    assert task.is_duplicate(None) is False
    assert task.is_duplicate("") is False
    # Repeated None never collapses (idempotency is opt-in).
    assert task.is_duplicate(None) is False


def test_dedup_key_is_namespaced_by_task_name() -> None:
    task = ForgeTask()
    task.name = "forge.agent.run"
    assert task.dedup_key("abc") == "forge:task:dedup:forge.agent.run:abc"


def test_redis_dedup_backend_setnx_semantics() -> None:
    class FakeRedis:
        def __init__(self) -> None:
            self.store: dict[str, str] = {}

        def set(self, key: str, value: str, *, nx: bool = False, ex: int | None = None):
            if nx and key in self.store:
                return None
            self.store[key] = value
            return True

    backend = RedisDedupBackend(FakeRedis())
    assert backend.seen("k", ttl_s=60) is False
    assert backend.seen("k", ttl_s=60) is True


def test_in_memory_dedup_ttl_eviction() -> None:
    backend = InMemoryDedupBackend()
    assert backend.seen("k", ttl_s=1) is False
    assert backend.seen("k", ttl_s=1) is True
    # Force expiry by shrinking the stored ttl via a re-key under a past window.
    backend._seen["k"] = 0.0  # simulate expired entry
    assert backend.seen("k", ttl_s=1) is False


def test_resolve_dedup_backend_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    set_dedup_backend(None)
    monkeypatch.setenv("FORGE_TASK_DEDUP_BACKEND", "memory")
    backend = resolve_dedup_backend()
    assert isinstance(backend, InMemoryDedupBackend)
    # Cached: a second resolve returns the same instance.
    assert resolve_dedup_backend() is backend


def test_build_dedup_backend_redis_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FORGE_TASK_DEDUP_BACKEND", raising=False)

    class _FakeRedisModule:
        class Redis:
            @staticmethod
            def from_url(url: str) -> object:
                return object()

    monkeypatch.setitem(__import__("sys").modules, "redis", _FakeRedisModule)
    backend = reliability._build_dedup_backend()
    assert isinstance(backend, RedisDedupBackend)


def test_build_dedup_backend_falls_back_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FORGE_TASK_DEDUP_BACKEND", raising=False)

    class _BoomModule:
        class Redis:
            @staticmethod
            def from_url(url: str) -> object:
                raise RuntimeError("no redis")

    monkeypatch.setitem(__import__("sys").modules, "redis", _BoomModule)
    backend = reliability._build_dedup_backend()
    assert isinstance(backend, InMemoryDedupBackend)


def test_transient_errors_introspection() -> None:
    assert TransientError in tuple(transient_errors())


# --------------------------------------------------------------------------- #
# Task-level enqueue dedup (agent / index / sync)                              #
# --------------------------------------------------------------------------- #


def test_run_agent_task_dedups_re_enqueue() -> None:
    task_id = "00000000-0000-0000-0000-0000000000c3"
    objective = {"objective": "do the thing", "task_id": task_id}
    first = run_agent_task(objective)
    assert first.get("deduplicated") is not True
    second = run_agent_task(objective)
    assert second == {"deduplicated": True, "idempotency_key": task_id}


def test_run_agent_task_explicit_idempotency_key() -> None:
    objective = {"objective": "x"}
    run_agent_task(objective, idempotency_key="IDEM-9")
    dup = run_agent_task(objective, idempotency_key="IDEM-9")
    assert dup == {"deduplicated": True, "idempotency_key": "IDEM-9"}


def test_run_agent_task_soft_time_limit_escalates(monkeypatch: pytest.MonkeyPatch) -> None:
    import forge_worker.agent_runner as ar

    def _raise(*_a, **_k):
        raise SoftTimeLimitExceeded

    monkeypatch.setattr(ar, "run_objective", _raise)
    out = run_agent_task({"objective": "runaway"}, idempotency_key="T-slow")
    assert out["escalated"] is True
    assert out["reason"] == "soft_time_limit_exceeded"


def test_index_source_task_dedups_re_enqueue(monkeypatch: pytest.MonkeyPatch) -> None:
    import forge_worker.indexer as idx

    def _build_boom():  # pragma: no cover - must not be reached on a duplicate
        raise AssertionError("build_knowledge_service must not run on a duplicate")

    monkeypatch.setattr(idx, "build_knowledge_service", _build_boom)
    # Pre-claim the idempotency slot (as a prior delivery would), then assert the
    # re-delivered enqueue short-circuits before building the service.
    assert index_source_task.is_duplicate("K") is False
    dup = index_source_task("s1", {"a.md": "x"}, idempotency_key="K")
    assert dup == {"deduplicated": True, "idempotency_key": "K"}


def test_sync_source_task_dedups_re_enqueue(monkeypatch: pytest.MonkeyPatch) -> None:
    import forge_worker.syncer as syncer

    def _build_boom():  # pragma: no cover - must not run on a duplicate
        raise AssertionError("build_knowledge_service must not run on a duplicate")

    monkeypatch.setattr(syncer, "build_knowledge_service", _build_boom)
    dup_key = "SYNC-1"
    assert sync_source_task.is_duplicate(dup_key) is False  # pre-claim
    dup = sync_source_task("s1", files={"a.md": "x"}, idempotency_key=dup_key)
    assert dup == {"deduplicated": True, "idempotency_key": dup_key}
