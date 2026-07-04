"""HARD-11: cover the Temporal worker entrypoint (F25) without a live Temporal.

``temporal_main`` is a side-effecty process entrypoint; its ``SessionPerCallStore``
delegates to the real ``SqlAlchemyWorkflowStore`` and its ``run``/``main`` wire a
``temporalio`` worker. We exercise the delegation + wiring with fakes so the
module's error/commit branches are covered hermetically.
"""

from __future__ import annotations

import uuid
from typing import ClassVar

import pytest

import forge_worker.temporal_main as tm


class _FakeSession:
    def __init__(self) -> None:
        self.committed = 0

    def __enter__(self) -> _FakeSession:
        return self

    def __exit__(self, *_a: object) -> bool:
        return False

    def commit(self) -> None:
        self.committed += 1


class _FakeStore:
    """Stand-in for SqlAlchemyWorkflowStore recording delegated calls."""

    last: ClassVar[dict[str, object]] = {}

    def __init__(self, session: object, workspace_id: uuid.UUID | None = None) -> None:
        self.session = session
        self.workspace_id = workspace_id

    def create(self, run: object) -> object:
        _FakeStore.last["create"] = run
        return run

    def get(self, run_id: uuid.UUID) -> str:
        _FakeStore.last["get"] = run_id
        return f"run-{run_id}"

    def update(self, run: object) -> object:
        _FakeStore.last["update"] = run
        return run

    def find_active_by_task(self, task_id: uuid.UUID) -> None:
        _FakeStore.last["find"] = task_id
        return None

    def list_by_task(self, task_id: uuid.UUID) -> list[object]:
        _FakeStore.last["list"] = task_id
        return []


@pytest.fixture
def patched_store(monkeypatch: pytest.MonkeyPatch) -> type[_FakeStore]:
    monkeypatch.setattr(tm, "SqlAlchemyWorkflowStore", _FakeStore)
    return _FakeStore


def test_session_per_call_store_delegates_and_commits(
    patched_store: type[_FakeStore],
) -> None:
    sessions: list[_FakeSession] = []

    def factory() -> _FakeSession:
        s = _FakeSession()
        sessions.append(s)
        return s

    store = tm.SessionPerCallStore(factory)  # type: ignore[arg-type]

    run = object()
    assert store.create(run) is run
    assert store.update(run) is run
    rid = uuid.uuid4()
    assert store.get(rid) == f"run-{rid}"
    tid = uuid.uuid4()
    assert store.find_active_by_task(tid) is None
    assert store.list_by_task(tid) == []

    # create + update commit; get/find/list are read-only (no commit).
    assert sum(s.committed for s in sessions) == 2
    assert patched_store.last["create"] is run


def test_build_activities_uses_session_per_call_store(
    patched_store: type[_FakeStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    def factory() -> _FakeSession:
        return _FakeSession()

    activities = tm.build_activities(factory)  # type: ignore[arg-type]
    assert activities is not None
    assert isinstance(activities.store, tm.SessionPerCallStore)


def test_build_activities_default_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    called = {"n": 0}

    def _fake_factory() -> object:
        called["n"] += 1
        return object()

    monkeypatch.setattr(tm, "create_session_factory", lambda: _fake_factory)
    monkeypatch.setattr(tm, "SqlAlchemyWorkflowStore", _FakeStore)
    activities = tm.build_activities()
    assert activities is not None


async def test_run_wires_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    ran = {"worker": False}

    class _FakeWorker:
        async def run(self) -> None:
            ran["worker"] = True

    async def _fake_client(_settings: object) -> str:
        return "client"

    monkeypatch.setattr(tm, "get_temporal_client", _fake_client)
    monkeypatch.setattr(tm, "build_activities", lambda: "activities")
    monkeypatch.setattr(tm, "build_temporal_worker", lambda *a, **k: _FakeWorker())

    await tm.run(tm.TemporalSettings())
    assert ran["worker"] is True


def test_main_invokes_run(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    async def _fake_run(settings: object = None) -> None:
        calls["n"] += 1

    monkeypatch.setattr(tm, "run", _fake_run)
    tm.main()
    assert calls["n"] == 1
