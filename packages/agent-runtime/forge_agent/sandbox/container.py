"""Per-task Docker container sandbox provider (F19 — V2 isolation).

``ContainerSandboxProvider`` runs allow-listed commands inside a disposable,
locked-down container whose filesystem is exactly the run's worktree subpath
mounted at ``/workspace``. Hardening (all applied at create): non-root uid/gid,
read-only root fs + tmpfs scratch, ``cap_drop: ALL``, ``no-new-privileges``,
CPU/memory/PID limits, ``network=none`` by default. The worker reaches the daemon
only through ``docker-socket-proxy`` (``DOCKER_HOST=tcp://docker-proxy:2375``); it
never touches the raw socket.

The Docker SDK is imported lazily so the package imports without a daemon (unit
tests inject a fake client). A real, unreachable daemon raises
:class:`SandboxStartupError` — never a silent downgrade to host execution.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import shlex
import time
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from forge_agent.sandbox.base import (
    ArtifactStore,
    SandboxExecError,
    SandboxStartupError,
)
from forge_agent.sandbox.output import cap_output
from forge_contracts import CommandOutput, SandboxKind, SandboxNetwork, SandboxSpec

SANDBOX_LABEL = "forge.sandbox"
_KILL_GRACE_SECONDS = 10


def _load_docker() -> Any:
    """Import the docker SDK or raise a startup error (never a silent fallback)."""
    try:
        import docker  # type: ignore[import-untyped]
    except ModuleNotFoundError as exc:  # pragma: no cover - dep is declared
        raise SandboxStartupError("docker SDK is not installed") from exc
    return docker


def _container_name(agent_run_id: uuid.UUID) -> str:
    return f"forge-sbx-{agent_run_id}"


def _build_create_kwargs(spec: SandboxSpec) -> dict[str, Any]:
    """The exact ``containers.create`` kwargs (hardening matrix)."""
    docker = _load_docker()
    # The docker SDK Mount does not yet expose ``subpath`` (Engine >= 26.1's
    # ``volume-subpath``); inject ``VolumeOptions.Subpath`` directly. Mount is a
    # plain dict subclass, so the extra key serialises through to the daemon.
    mount = docker.types.Mount(
        target="/workspace",
        source=spec.worktree_volume,
        type="volume",
        read_only=False,
    )
    if spec.worktree_subpath:
        mount["VolumeOptions"] = {"Subpath": spec.worktree_subpath}
    environment = dict(spec.env)
    network_mode = "none" if spec.network is SandboxNetwork.NONE else None
    return {
        "image": spec.image,
        "command": ["tini", "--", "sleep", "infinity"],
        "name": _container_name(spec.agent_run_id),
        "labels": {
            SANDBOX_LABEL: "true",
            "forge.agent_run_id": str(spec.agent_run_id),
            "forge.workspace_id": str(spec.workspace_id),
        },
        "user": f"{spec.run_as_uid}:{spec.run_as_gid}",
        "working_dir": "/workspace",
        "mounts": [mount],
        "network_mode": network_mode,
        "read_only": True,
        "tmpfs": {
            "/tmp": f"size={spec.limits.tmpfs_mb}m",
            "/home/forge": "size=64m",
        },
        "mem_limit": f"{spec.limits.memory_mb}m",
        "nano_cpus": int(spec.limits.cpus * 1_000_000_000),
        "pids_limit": spec.limits.pids_limit,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "environment": environment,
        "auto_remove": False,
        "detach": True,
    }


class ContainerSandboxSession:
    """A live container bound to one worktree; runs many ``exec``s then tears down."""

    kind = SandboxKind.CONTAINER

    def __init__(
        self,
        spec: SandboxSpec,
        *,
        client: Any,
        container: Any,
        artifact_store: ArtifactStore | None = None,
        output_cap_bytes: int = 262144,
    ) -> None:
        self._spec = spec
        self._client = client
        self._container = container
        self._artifact_store = artifact_store
        self._output_cap_bytes = output_cap_bytes
        self.sandbox_id = str(getattr(container, "id", "") or "")
        self.workspace_dir = "/workspace"
        self.host_worktree_path = spec.host_worktree_path

    async def setup(self) -> None:
        for command in self._spec.setup_commands:
            await self.run(
                command,
                cwd=self.workspace_dir,
                timeout_s=self._spec.exec_timeout_seconds,
            )

    async def teardown(self, *, reason: str = "completed") -> None:
        def _remove() -> None:
            # Best-effort; the reaper backstops any container that survives.
            with contextlib.suppress(Exception):
                self._container.remove(force=True)

        await asyncio.to_thread(_remove)

    async def run(
        self,
        command: str,
        *,
        cwd: str,
        timeout_s: int,
        env: Mapping[str, str] | None = None,
    ) -> CommandOutput:
        quoted = shlex.quote(command)
        wrapped = f"timeout --kill-after={_KILL_GRACE_SECONDS}s {timeout_s}s sh -lc {quoted}"
        exec_cmd = ["/bin/sh", "-lc", wrapped]
        started = time.monotonic()
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(self._exec_sync, exec_cmd, cwd, env),
                timeout=timeout_s + _KILL_GRACE_SECONDS + 5,
            )
        except TimeoutError:
            # The container-side `timeout` wedged; kill the container as a backstop.
            await asyncio.to_thread(self._kill)
            result = {"exit_code": 124, "stdout": b"", "stderr": b"", "timed_out": True}
        except Exception as exc:
            raise SandboxExecError(f"exec failed: {exc}") from exc

        duration_ms = int((time.monotonic() - started) * 1000)
        exit_code = int(result["exit_code"])
        timed_out = bool(result.get("timed_out")) or exit_code == 124
        oom_killed = await asyncio.to_thread(self._read_oom)

        stdout, stdout_ref = cap_output(
            _to_text(result["stdout"]),
            cap_bytes=self._output_cap_bytes,
            store=self._artifact_store,
            key=self._artifact_key("stdout"),
        )
        stderr, stderr_ref = cap_output(
            _to_text(result["stderr"]),
            cap_bytes=self._output_cap_bytes,
            store=self._artifact_store,
            key=self._artifact_key("stderr"),
        )
        return CommandOutput(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
            timed_out=timed_out,
            oom_killed=oom_killed,
            stdout_artifact_ref=stdout_ref,
            stderr_artifact_ref=stderr_ref,
            sandbox_kind=SandboxKind.CONTAINER,
            container_id=self.sandbox_id or None,
        )

    # -- daemon calls (run off-thread) ------------------------------------- #
    def _exec_sync(
        self, exec_cmd: list[str], cwd: str, env: Mapping[str, str] | None
    ) -> dict[str, Any]:
        api = self._client.api
        created = api.exec_create(
            self._container.id,
            exec_cmd,
            workdir=cwd,
            environment=dict(env) if env is not None else None,
            user=f"{self._spec.run_as_uid}:{self._spec.run_as_gid}",
        )
        exec_id = created["Id"] if isinstance(created, dict) else created
        out = api.exec_start(exec_id, demux=True)
        stdout_b, stderr_b = out if isinstance(out, tuple) else (out, b"")
        inspected = api.exec_inspect(exec_id)
        exit_code = inspected.get("ExitCode")
        return {
            "exit_code": 0 if exit_code is None else int(exit_code),
            "stdout": stdout_b or b"",
            "stderr": stderr_b or b"",
            "timed_out": False,
        }

    def _read_oom(self) -> bool:
        try:
            self._container.reload()
            return bool(self._container.attrs.get("State", {}).get("OOMKilled"))
        except Exception:
            return False

    def _kill(self) -> None:
        with contextlib.suppress(Exception):
            self._container.kill()

    def _artifact_key(self, stream: str) -> str:
        return f"sandbox/{self._spec.agent_run_id}/{uuid.uuid4().hex}.{stream}.log"


class ContainerSandboxProvider:
    """Builds + reaps per-task Docker container sandboxes (``container`` isolation)."""

    kind = SandboxKind.CONTAINER

    def __init__(
        self,
        *,
        docker_host: str = "tcp://docker-proxy:2375",
        client: Any = None,
        egress_network: str = "forge_sandbox_egress",
        artifact_store: ArtifactStore | None = None,
        output_cap_bytes: int = 262144,
        max_ttl_seconds: int = 21600,
    ) -> None:
        self._docker_host = docker_host
        self._client = client
        self._egress_network = egress_network
        self._artifact_store = artifact_store
        self._output_cap_bytes = output_cap_bytes
        self._max_ttl_seconds = max_ttl_seconds

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        docker = _load_docker()
        try:
            self._client = docker.DockerClient(base_url=self._docker_host)
            self._client.ping()
        except Exception as exc:
            raise SandboxStartupError(
                f"docker daemon unreachable at {self._docker_host}: {exc}"
            ) from exc
        return self._client

    async def create(self, spec: SandboxSpec) -> ContainerSandboxSession:
        if spec.image is None:
            raise SandboxStartupError("container sandbox requires an image")
        client = self._ensure_client()
        kwargs = _build_create_kwargs(spec)

        def _create_and_start() -> Any:
            container = client.containers.create(**kwargs)
            if spec.network is SandboxNetwork.EGRESS:
                self._attach_egress(client, container)
            container.start()
            return container

        try:
            container = await asyncio.to_thread(_create_and_start)
        except SandboxStartupError:
            raise
        except Exception as exc:
            raise SandboxStartupError(f"failed to start sandbox container: {exc}") from exc

        session = ContainerSandboxSession(
            spec,
            client=client,
            container=container,
            artifact_store=self._artifact_store,
            output_cap_bytes=self._output_cap_bytes,
        )
        await session.setup()
        return session

    def _attach_egress(self, client: Any, container: Any) -> None:
        try:
            network = client.networks.get(self._egress_network)
            network.connect(container)
        except Exception as exc:
            raise SandboxStartupError(
                f"failed to attach egress network {self._egress_network}: {exc}"
            ) from exc

    async def reap_orphans(self, *, terminal_run_ids: set[str] | None = None) -> int:
        client = self._ensure_client()

        def _reap() -> int:
            containers = client.containers.list(
                all=True, filters={"label": f"{SANDBOX_LABEL}=true"}
            )
            now = datetime.now(UTC)
            removed = 0
            for container in select_orphans(
                containers,
                now=now,
                max_ttl_seconds=self._max_ttl_seconds,
                terminal_run_ids=terminal_run_ids or set(),
            ):
                try:
                    container.remove(force=True)
                    removed += 1
                except Exception:
                    continue
            return removed

        return await asyncio.to_thread(_reap)


def _to_text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


def _container_age_seconds(container: Any, now: datetime) -> float:
    created = getattr(container, "attrs", {}).get("Created") if container else None
    if not created:
        return 0.0
    # Docker emits RFC3339 with up to nanosecond precision; clamp to microseconds.
    text = re.sub(r"(\.\d{6})\d*", r"\1", str(created)).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except (ValueError, TypeError):
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return (now - parsed).total_seconds()


def select_orphans(
    containers: list[Any],
    *,
    now: datetime,
    max_ttl_seconds: int,
    terminal_run_ids: set[str],
) -> list[Any]:
    """Pure selection: which sandbox containers should be reaped.

    A container is an orphan when it is already exited/dead, OR its run is terminal
    (``forge.agent_run_id`` in ``terminal_run_ids``), OR it is older than the TTL.
    """
    orphans: list[Any] = []
    for container in containers:
        attrs = getattr(container, "attrs", {}) or {}
        status = str(attrs.get("State", {}).get("Status", "")).lower()
        labels = attrs.get("Config", {}).get("Labels", {}) or {}
        run_id = labels.get("forge.agent_run_id")
        is_terminal_status = status in {"exited", "dead", "removing"}
        is_terminal_run = run_id is not None and run_id in terminal_run_ids
        is_expired = _container_age_seconds(container, now) > max_ttl_seconds
        if is_terminal_status or is_terminal_run or is_expired:
            orphans.append(container)
    return orphans


__all__ = [
    "SANDBOX_LABEL",
    "ContainerSandboxProvider",
    "ContainerSandboxSession",
    "select_orphans",
]
