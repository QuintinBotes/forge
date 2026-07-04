"""The runtime-supplied :class:`PolicyContext` for conditional evaluation (F29).

F04 evaluates a :class:`~forge_contracts.ToolCall` context-free. F29's
conditional layer additionally evaluates against a ``PolicyContext`` — the
branch, target environment, task kind, run-initiator's RBAC role, active skill
profile, execution mode, and the **runtime-supplied UTC clock** (``now``).

The evaluator is pure and reads no wall clock: the runtime MUST set ``now`` so a
decision is reproducible/replayable (F12) and tests are hermetic.

The condition-field whitelist (:data:`POLICY_CONDITION_FIELDS`) and the action
vocabulary (``KNOWN_ACTIONS``) live in ``forge_contracts`` (the ``Policy`` model
self-validates against them); they are re-exported here for runtime convenience.
This is a deviation from the slice's idealised layout — the whitelist must live
with the frozen ``Policy`` model that validates it. See the slice notes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from forge_contracts import POLICY_CONDITION_FIELDS, ToolCall

__all__ = ["POLICY_CONDITION_FIELDS", "PolicyContext", "build_context_from_run"]

_PATH_KEYS = ("path", "file", "filename", "target")
_ENV_KEYS = ("environment", "env", "target", "stage")
_REDACTED_PATH_MAXLEN = 120


def _effective_action(call: ToolCall) -> str:
    return (call.action or call.tool or "").strip()


def _arg_str(call: ToolCall, keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = call.arguments.get(key)
        if isinstance(value, str) and value:
            return value
    return None


class PolicyContext(BaseModel):
    """The whitelisted evaluation context supplied by the agent runtime (F29)."""

    model_config = ConfigDict(extra="forbid")

    environment: str | None = None
    command: str | None = None
    role: str | None = None
    skill_profile: str | None = None
    branch: str | None = None
    base_branch: str | None = None
    task_kind: str | None = None
    actor_role: str | None = None
    execution_mode: str | None = None
    labels: list[str] = Field(default_factory=list)
    repo_id: str | None = None
    now: datetime | None = None

    @classmethod
    def empty(cls) -> PolicyContext:
        """An all-defaults context (the F04 / context-free equivalent)."""
        return cls()

    def to_fields(self, action: ToolCall) -> dict[str, Any]:
        """Flatten context + action into the :data:`POLICY_CONDITION_FIELDS` namespace.

        Derives ``action``/``path``/``file_ext``/``command`` from the tool call,
        falls back to call arguments for ``environment``/``role``/``skill_profile``
        when not set explicitly on the context, and exposes the raw UTC ``now``
        (operand for the ``*_time_window`` ops) plus ``weekday`` (0=Mon..6=Sun)
        and ``hour`` (0..23, UTC) derived from it.
        """
        path = action.path or _arg_str(action, _PATH_KEYS)
        file_ext: str | None = None
        if path and "." in path.rsplit("/", 1)[-1]:
            file_ext = path.rsplit(".", 1)[-1]

        now = self.now
        weekday: int | None = None
        hour: int | None = None
        if now is not None:
            utc = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
            weekday = utc.weekday()
            hour = utc.hour

        return {
            "action": _effective_action(action),
            "path": path,
            "file_ext": file_ext,
            "environment": self.environment or _arg_str(action, _ENV_KEYS),
            "command": self.command or _arg_str(action, ("command",)),
            "role": self.role or _arg_str(action, ("role",)),
            "skill_profile": self.skill_profile or _arg_str(action, ("skill_profile",)),
            "branch": self.branch,
            "base_branch": self.base_branch,
            "task_kind": self.task_kind,
            "actor_role": self.actor_role,
            "execution_mode": self.execution_mode,
            "labels": list(self.labels),
            "repo_id": self.repo_id,
            "now": now,
            "weekday": weekday,
            "hour": hour,
        }

    def to_redacted_fields(self) -> dict[str, Any]:
        """Audit-safe JSON projection for ``PolicyRuleEvaluation.context_redacted``.

        Drops ``command`` (may carry secrets/tokens) entirely and truncates any
        free-form values; keeps branch/env/task_kind/role/etc. and ``now`` (so a
        time-conditional decision is reproducible in the audit). ``command`` and
        raw ``ToolCall.args`` never enter this projection.
        """
        data: dict[str, Any] = {
            "environment": self.environment,
            "role": self.role,
            "skill_profile": self.skill_profile,
            "branch": self.branch,
            "base_branch": self.base_branch,
            "task_kind": self.task_kind,
            "actor_role": self.actor_role,
            "execution_mode": self.execution_mode,
            "repo_id": self.repo_id,
            "now": self.now.isoformat() if self.now is not None else None,
        }
        if self.labels:
            data["labels"] = list(self.labels)
        return {k: v for k, v in data.items() if v is not None}


def build_context_from_run(
    *,
    branch: str,
    base_branch: str,
    environment: str | None,
    task_kind: str,
    actor_role: str,
    skill_profile: str,
    execution_mode: str,
    repo_id: str,
    now: datetime,
    labels: list[str] | None = None,
) -> PolicyContext:
    """Build a :class:`PolicyContext` from a run (the runtime's contract).

    The agent-runtime tool gate calls this on each tool dispatch; ``now`` is the
    UTC evaluation clock the runtime is required to supply.
    """
    return PolicyContext(
        branch=branch,
        base_branch=base_branch,
        environment=environment,
        task_kind=task_kind,
        actor_role=actor_role,
        skill_profile=skill_profile,
        execution_mode=execution_mode,
        repo_id=repo_id,
        now=now,
        labels=list(labels or []),
    )
