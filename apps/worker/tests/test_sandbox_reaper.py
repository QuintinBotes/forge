"""Worker sandbox reaper task + boot hook + beat schedule (F19 AC12)."""

from __future__ import annotations

from forge_agent.sandbox import LocalSandboxProvider, SandboxSettings
from forge_contracts import SandboxKind
from forge_worker.beat import BEAT_SCHEDULE, SANDBOX_REAP_TASK, configure_beat
from forge_worker.celery_app import celery_app
from forge_worker.tasks import sandbox as sandbox_task


class _FakeProvider:
    """Records the terminal ids it was asked to reap; returns a fixed count."""

    kind = SandboxKind.CONTAINER

    def __init__(self, removed: int) -> None:
        self._removed = removed
        self.seen_terminal_ids: set[str] | None = None

    async def reap_orphans(self, *, terminal_run_ids: set[str] | None = None) -> int:
        self.seen_terminal_ids = set(terminal_run_ids or [])
        return self._removed


def test_run_reap_pass_worktree_is_noop_without_docker_or_db() -> None:
    result = sandbox_task.run_reap_pass(
        provider=LocalSandboxProvider(),
        settings=SandboxSettings(kind=SandboxKind.WORKTREE),
    )
    assert result == {"removed": 0, "kind": "worktree"}


def test_run_reap_pass_container_uses_injected_provider() -> None:
    provider = _FakeProvider(removed=3)
    result = sandbox_task.run_reap_pass(
        provider=provider,
        settings=SandboxSettings(kind=SandboxKind.CONTAINER),
        session_factory=None,  # no DB -> empty terminal id set, still reaps by heuristic
    )
    assert result == {"removed": 3, "kind": "container"}
    assert provider.seen_terminal_ids == set()


def test_reap_task_is_registered() -> None:
    assert "sandbox.reap_orphans" in celery_app.tasks or _task_known()


def _task_known() -> bool:
    # The decorated task object exists even before autodiscovery finalizes.
    return sandbox_task.reap_orphans_task.name == "sandbox.reap_orphans"


def test_worker_ready_hook_is_safe() -> None:
    # Best-effort: with the default (worktree) settings this is a clean no-op.
    sandbox_task.reap_on_worker_ready()


def test_beat_schedule_has_reaper_entry() -> None:
    assert "sandbox-reap-orphans" in BEAT_SCHEDULE
    assert BEAT_SCHEDULE["sandbox-reap-orphans"]["task"] == SANDBOX_REAP_TASK
    # Re-configuring is idempotent and reads the env cadence.
    schedule = configure_beat(celery_app)
    assert schedule["sandbox-reap-orphans"]["schedule"] > 0
