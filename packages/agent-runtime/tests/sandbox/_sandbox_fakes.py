"""Fake Docker client + object store for the F19 unit tier (no daemon).

Importable as a plain module (``import _sandbox_fakes``) — pytest puts this
directory on ``sys.path`` — so test modules and the local ``conftest`` share one
definition without relative-import gymnastics.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class FakeExecResult:
    exit_code: int = 0
    stdout: bytes = b""
    stderr: bytes = b""


class FakeContainer:
    def __init__(self, *, id: str, attrs: dict[str, Any] | None = None) -> None:
        self.id = id
        self.attrs = attrs or {"State": {"OOMKilled": False, "Status": "running"}}
        self.started = False
        self.removed = False
        self.killed = False

    def start(self) -> None:
        self.started = True

    def remove(self, *, force: bool = False) -> None:
        self.removed = True

    def reload(self) -> None:
        return None

    def kill(self) -> None:
        self.killed = True


class _ApiShim:
    def __init__(self, client: FakeDockerClient) -> None:
        self._client = client

    def exec_create(self, container_id: str, cmd: list[str], **kwargs: Any) -> dict[str, str]:
        self._client.exec_create_calls.append(
            {"container_id": container_id, "cmd": cmd, "kwargs": kwargs}
        )
        return {"Id": f"exec-{len(self._client.exec_create_calls)}"}

    def exec_start(self, exec_id: str, *, demux: bool = False) -> Any:
        result = self._client.next_exec()
        if demux:
            return (result.stdout, result.stderr)
        return result.stdout + result.stderr

    def exec_inspect(self, exec_id: str) -> dict[str, int]:
        result = self._client.last_exec_result or FakeExecResult()
        return {"ExitCode": result.exit_code}


class _ContainersShim:
    def __init__(self, client: FakeDockerClient) -> None:
        self._client = client

    def create(self, **kwargs: Any) -> FakeContainer:
        self._client.create_kwargs = kwargs
        container = FakeContainer(id=f"cid-{uuid.uuid4().hex[:12]}")
        self._client.created.append(container)
        return container

    def list(self, *, all: bool = False, filters: dict | None = None) -> list[FakeContainer]:
        self._client.list_calls.append({"all": all, "filters": filters})
        return list(self._client.existing)


class _Network:
    def __init__(self, name: str) -> None:
        self.name = name
        self.connected: list[Any] = []

    def connect(self, container: Any) -> None:
        self.connected.append(container)


class _NetworksShim:
    def __init__(self, client: FakeDockerClient) -> None:
        self._client = client

    def get(self, name: str) -> _Network:
        net = _Network(name)
        self._client.networks_got.append(net)
        return net


@dataclass
class FakeDockerClient:
    """A scriptable stand-in for ``docker.DockerClient``."""

    exec_results: list[FakeExecResult] = field(default_factory=list)
    existing: list[FakeContainer] = field(default_factory=list)
    create_kwargs: dict[str, Any] | None = None
    created: list[FakeContainer] = field(default_factory=list)
    exec_create_calls: list[dict[str, Any]] = field(default_factory=list)
    list_calls: list[dict[str, Any]] = field(default_factory=list)
    networks_got: list[_Network] = field(default_factory=list)
    last_exec_result: FakeExecResult | None = None
    pinged: bool = False

    def __post_init__(self) -> None:
        self.api = _ApiShim(self)
        self.containers = _ContainersShim(self)
        self.networks = _NetworksShim(self)
        self._exec_idx = 0

    def ping(self) -> bool:
        self.pinged = True
        return True

    def next_exec(self) -> FakeExecResult:
        if self._exec_idx < len(self.exec_results):
            result = self.exec_results[self._exec_idx]
            self._exec_idx += 1
        else:
            result = FakeExecResult()
        self.last_exec_result = result
        return result


class FakeObjectStore:
    """Captures artifact offloads to assert output capping/refs."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put(self, key: str, data: bytes, *, content_type: str = "text/plain") -> str:
        self.objects[key] = data
        return f"minio://artifacts/{key}"
