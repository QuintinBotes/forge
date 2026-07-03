"""gVisor (``runsc``) sandbox provider — userspace-kernel isolation (F34).

``GvisorSandboxProvider`` subclasses F19's :class:`ContainerSandboxProvider` and
differs only in the OCI runtime selected at container create (``runsc``, an
application kernel that services the guest's syscalls in userspace) plus a
preflight that requires the runtime to be registered with the daemon. **All**
F19 hardening — single-worktree mount, ``cap_drop: ALL``, non-root uid,
read-only rootfs, CPU/memory/PID limits, exec-with-timeout, OOM semantics,
output capping, teardown, orphan reaping — is inherited unchanged.

No silent downgrade: if ``runsc`` is not registered, ``preflight``/``create``
raise :class:`SandboxRuntimeUnavailable`; execution never falls back to runc or
a host subprocess.
"""

from __future__ import annotations

from typing import Any

from forge_agent.sandbox.base import SandboxRuntimeUnavailable
from forge_agent.sandbox.container import ContainerSandboxProvider, ContainerSandboxSession
from forge_agent.sandbox.runtime import detect_registered_runtimes
from forge_contracts import SandboxKind, SandboxSpec

DEFAULT_GVISOR_RUNTIME = "runsc"
DEFAULT_GVISOR_PLATFORM = "systrap"


class GvisorSandboxProvider(ContainerSandboxProvider):
    """Per-task Docker container under the gVisor ``runsc`` runtime."""

    kind = SandboxKind.GVISOR

    def __init__(
        self,
        *,
        gvisor_runtime: str = DEFAULT_GVISOR_RUNTIME,
        gvisor_platform: str = DEFAULT_GVISOR_PLATFORM,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._gvisor_runtime = gvisor_runtime
        self._gvisor_platform = gvisor_platform

    @property
    def runtime(self) -> str:
        return self._gvisor_runtime

    def preflight_check(self) -> None:
        """Require ``runsc`` registered with the daemon — never downgrade."""
        registered = detect_registered_runtimes(self._ensure_client())
        if self._gvisor_runtime not in registered:
            raise SandboxRuntimeUnavailable(
                f"gvisor sandbox requires OCI runtime {self._gvisor_runtime!r} "
                f"registered with the Docker daemon (found: {sorted(registered) or 'none'}); "
                "run deploy/scripts/install-runtimes.sh --gvisor on the daemon host"
            )

    def _create_kwargs(self, spec: SandboxSpec) -> dict[str, Any]:
        kw = super()._create_kwargs(spec)
        kw["runtime"] = spec.runtime or self._gvisor_runtime
        kw["environment"] = {
            **kw.get("environment", {}),
            "GVISOR_PLATFORM": spec.gvisor_platform or self._gvisor_platform,
        }
        return kw

    async def create(self, spec: SandboxSpec) -> ContainerSandboxSession:
        self.preflight_check()
        session = await super().create(spec)
        session.runtime = spec.runtime or self._gvisor_runtime
        return session


__all__ = ["DEFAULT_GVISOR_PLATFORM", "DEFAULT_GVISOR_RUNTIME", "GvisorSandboxProvider"]
