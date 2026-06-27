"""AC12 — orphan reaping selection + provider/dispatch logic."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from _sandbox_fakes import FakeContainer, FakeDockerClient

from forge_agent.sandbox import (
    ContainerSandboxProvider,
    LocalSandboxProvider,
    reap_orphans,
    select_orphans,
)


def _container(run_id: str, *, status: str = "running", age_seconds: int = 0) -> FakeContainer:
    created = (datetime.now(UTC) - timedelta(seconds=age_seconds)).isoformat()
    return FakeContainer(
        id=f"cid-{run_id}",
        attrs={
            "State": {"Status": status, "OOMKilled": False},
            "Created": created,
            "Config": {"Labels": {"forge.sandbox": "true", "forge.agent_run_id": run_id}},
        },
    )


def test_select_orphans_matrix() -> None:
    now = datetime.now(UTC)
    exited = _container("a", status="exited")
    terminal_run = _container("b", status="running")
    healthy = _container("c", status="running", age_seconds=10)
    expired = _container("d", status="running", age_seconds=100_000)

    selected = select_orphans(
        [exited, terminal_run, healthy, expired],
        now=now,
        max_ttl_seconds=21600,
        terminal_run_ids={"b"},
    )
    assert exited in selected
    assert terminal_run in selected
    assert expired in selected
    assert healthy not in selected


async def test_container_provider_reaps_and_returns_count() -> None:
    client = FakeDockerClient()
    client.existing = [
        _container("a", status="exited"),
        _container("c", status="running", age_seconds=10),
    ]
    provider = ContainerSandboxProvider(client=client)
    removed = await provider.reap_orphans(terminal_run_ids=set())
    assert removed == 1
    assert client.existing[0].removed is True
    # The reap query is label-scoped to forge.sandbox=true.
    assert client.list_calls[0]["filters"] == {"label": "forge.sandbox=true"}


async def test_reap_orphans_dispatch_local_is_zero() -> None:
    assert await reap_orphans(LocalSandboxProvider()) == 0


async def test_reap_orphans_dispatch_container() -> None:
    client = FakeDockerClient()
    client.existing = [_container("a", status="exited")]
    provider = ContainerSandboxProvider(client=client)
    assert await reap_orphans(provider, terminal_run_ids=["a"]) == 1
