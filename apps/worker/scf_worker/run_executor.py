"""Where an agent run's loop runs: the run executor setting and seam (#239).

``FORGE_RUN_EXECUTOR`` is ``inline`` (the loop runs inside the worker's Celery
task), ``docker`` (one container per run) or ``kubernetes`` (one Job per run). A
non-inline executor launches one unit per run through :class:`RunExecutor`; the
lifecycle around it (launch, close-out, reconciliation) is in
:mod:`scf_worker.launched_runs`. Spec 011 FR-014, FR-021, FR-022.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, NoReturn, Protocol

from scf_contracts import SandboxKind

__all__ = [
    "INLINE",
    "RUN_EXECUTORS",
    "RUN_EXECUTOR_ENV",
    "SANDBOX_BACKEND_ENV",
    "ExecutorUnavailable",
    "RunExecutor",
    "UnavailableExecutor",
    "UnitState",
    "build_executor",
    "check_run_executor",
    "configured_executor",
]

RUN_EXECUTOR_ENV = "FORGE_RUN_EXECUTOR"
#: Where container-backed sandbox tiers run (#233); absent means the container daemon.
SANDBOX_BACKEND_ENV = "FORGE_SANDBOX_BACKEND"
INLINE = "inline"
#: Each non-inline executor is named after the platform its sandbox backend must match.
RUN_EXECUTORS = (INLINE, "docker", "kubernetes")


class ExecutorUnavailable(Exception):
    """The executor's runtime cannot launch a unit."""


@dataclass(frozen=True)
class UnitState:
    """A unit as its executor sees it: running, exited with a code, or missing."""

    phase: Literal["running", "exited", "missing"]
    exit_code: int | None = None


class RunExecutor(Protocol):
    """Launches and tracks one unit per agent run."""

    name: str

    def preflight(self) -> None:
        """Raise :class:`ExecutorUnavailable` when no unit can be launched."""
        ...

    def launch(self, run_id: uuid.UUID, workspace_id: uuid.UUID, *, env: Mapping[str, str]) -> str:
        """Start the unit that runs one run's loop and return its identifier at once.

        ``env`` is the unit's whole environment, and the one place per-unit
        credentials go; the database credential is never in it.
        """
        ...

    def state(self, unit: str) -> UnitState: ...

    def remove(self, unit: str) -> None:
        """Stop and remove the unit; a missing unit is already removed."""
        ...

    def list_units(self) -> dict[str, uuid.UUID]:
        """Every unit this executor launched, with its run, for reaping."""
        ...


@dataclass
class UnavailableExecutor:
    """An executor that is not built yet: every call refuses, so no run falls back inline."""

    name: str

    def preflight(self) -> NoReturn:
        raise ExecutorUnavailable(f"the {self.name} run executor is not available in this release")

    def launch(self, run_id: uuid.UUID, workspace_id: uuid.UUID, *, env: Mapping[str, str]) -> str:
        self.preflight()

    def state(self, unit: str) -> UnitState:
        self.preflight()

    def remove(self, unit: str) -> None:
        self.preflight()

    def list_units(self) -> dict[str, uuid.UUID]:
        self.preflight()


def build_executor(name: str) -> RunExecutor:
    """The executor ``name`` selects."""
    # shortcut: docker and kubernetes refuse every launch until #240 and #241 build them.
    return UnavailableExecutor(name)


def configured_executor(env: Mapping[str, str] | None = None) -> str:
    """The executor ``FORGE_RUN_EXECUTOR`` names; ``ValueError`` naming the setting otherwise."""
    raw = (os.environ if env is None else env).get(RUN_EXECUTOR_ENV, "").strip()
    # Unset means inline while no other executor exists; #240 and #242 set the shipped defaults.
    name = raw.lower() or INLINE
    if name not in RUN_EXECUTORS:
        raise ValueError(
            f"{RUN_EXECUTOR_ENV} must be one of {', '.join(RUN_EXECUTORS)}, not {raw!r}"
        )
    return name


def check_run_executor(env: Mapping[str, str] | None = None) -> str:
    """The configured executor, or ``ValueError`` for a combination the worker refuses (FR-022)."""
    source = os.environ if env is None else env
    name = configured_executor(source)
    if name == INLINE:
        return name
    kind = source.get("FORGE_SANDBOX_KIND", SandboxKind.WORKTREE.value)
    if kind == SandboxKind.WORKTREE.value:
        raise ValueError(
            f"{RUN_EXECUTOR_ENV}={name} needs a container-backed sandbox tier, "
            "and FORGE_SANDBOX_KIND is worktree"
        )
    backend = source.get(SANDBOX_BACKEND_ENV) or "docker"
    if backend != name:
        raise ValueError(
            f"{RUN_EXECUTOR_ENV}={name} and {SANDBOX_BACKEND_ENV}={backend} "
            "name different platforms"
        )
    return name
