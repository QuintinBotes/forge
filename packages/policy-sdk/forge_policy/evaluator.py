"""Evaluate a :class:`~forge_contracts.ToolCall` against a repo
:class:`~forge_contracts.Policy`.

Design (plan Task 1.10 / spec "Repo Policy System" + "Security"):

* **Default deny.** An action the policy does not explicitly permit is denied.
  The agent never self-assigns permissions or expands its own scope.
* **Precedence (first match wins):**

  1. Empty / unidentifiable tool call -> ``DENY``.
  2. Action in ``restricted_actions`` -> ``DENY`` (explicitly forbidden).
  3. Write actions -> governed by ``write_rules`` globs against the target path
     (``deny`` beats ``allow``; an unlisted path is denied).
  4. Deploy actions -> governed by ``deploy_rules`` (restricted environments
     require human approval; agent deploys disabled -> deny).
  5. Action in ``allowed_actions`` -> ``ALLOW``.
  6. Anything else -> ``DENY`` (default deny).

Evaluation is a pure function: it never mutates its inputs and never performs
I/O, so a decision is fully reproducible for the audit log.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable
from pathlib import Path

from forge_contracts import (
    ApprovalGate,
    Decision,
    DecisionEffect,
    Policy,
    PolicyEvaluator,
    ToolCall,
)

#: Tool/action names that write to (or delete from) the filesystem and are
#: therefore governed by ``write_rules`` path globs.
WRITE_ACTIONS: frozenset[str] = frozenset(
    {
        "write_code",
        "write_file",
        "write",
        "edit",
        "edit_file",
        "create_file",
        "apply_patch",
        "modify_file",
        "delete_file",
        "delete_files",
    }
)

#: Environment-name aliases normalised before comparing against deploy rules.
_ENV_ALIASES: dict[str, str] = {
    "prod": "production",
    "prd": "production",
    "production": "production",
    "stg": "staging",
    "stage": "staging",
    "staging": "staging",
    "dev": "dev",
    "develop": "dev",
    "development": "dev",
}


def _effective_action(call: ToolCall) -> str:
    """The action name policy acts on: explicit ``action`` else ``tool``."""
    return (call.action or call.tool or "").strip()


def _target_path(call: ToolCall) -> str | None:
    """Resolve the write target path from the call (field or arguments)."""
    if call.path:
        return call.path
    for key in ("path", "file", "filename", "target"):
        value = call.arguments.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _normalize_path(path: str) -> str:
    """Normalise a path for glob matching (forward slashes, no leading ``./``)."""
    norm = path.replace("\\", "/").lstrip("/")
    if norm.startswith("./"):
        norm = norm[2:]
    return norm


def _match_glob(path: str, pattern: str) -> bool:
    """True if ``path`` matches ``pattern`` (full path or basename)."""
    norm = _normalize_path(path)
    base = norm.rsplit("/", 1)[-1]
    return fnmatch.fnmatch(norm, pattern) or fnmatch.fnmatch(base, pattern)


def _first_match(path: str, patterns: Iterable[str]) -> str | None:
    """Return the first pattern that matches ``path``, or ``None``."""
    for pattern in patterns:
        if _match_glob(path, pattern):
            return pattern
    return None


def _normalize_env(env: str) -> str:
    key = env.strip().lower()
    return _ENV_ALIASES.get(key, key)


def _target_environment(call: ToolCall, action: str) -> str | None:
    """Resolve the deploy target environment from arguments or the action name."""
    for key in ("environment", "env", "target", "stage"):
        value = call.arguments.get(key)
        if isinstance(value, str) and value:
            return _normalize_env(value)
    if action.startswith("deploy_"):
        suffix = action[len("deploy_") :]
        if suffix:
            return _normalize_env(suffix)
    return None


def _evaluate_write(call: ToolCall, policy: Policy) -> Decision:
    path = _target_path(call)
    if not path:
        return Decision(
            effect=DecisionEffect.DENY,
            reason="write action has no target path; cannot verify against write_rules",
        )

    denied = _first_match(path, policy.write_rules.deny)
    if denied is not None:
        return Decision(
            effect=DecisionEffect.DENY,
            reason=f"path {path!r} matches a write_rules.deny pattern",
            matched_rule=f"write_rules.deny:{denied}",
        )

    allowed = _first_match(path, policy.write_rules.allow)
    if allowed is not None:
        return Decision(
            effect=DecisionEffect.ALLOW,
            reason=f"path {path!r} matches a write_rules.allow pattern",
            matched_rule=f"write_rules.allow:{allowed}",
        )

    return Decision(
        effect=DecisionEffect.DENY,
        reason=f"path {path!r} is not in any write_rules.allow pattern (default deny)",
    )


def _evaluate_deploy(call: ToolCall, policy: Policy, action: str) -> Decision:
    rules = policy.deploy_rules
    env = _target_environment(call, action)

    restricted = {_normalize_env(e) for e in rules.restricted_environments}
    allowed_envs = {_normalize_env(e) for e in rules.environments}

    if env is not None and env in restricted:
        return Decision(
            effect=DecisionEffect.REQUIRES_APPROVAL,
            reason=f"deploy to restricted environment {env!r} requires human approval",
            matched_rule=f"deploy_rules.restricted_environments:{env}",
            requires_approval=True,
            approval_gate=ApprovalGate.DEPLOY,
        )

    if not rules.allow_agent_deploy:
        return Decision(
            effect=DecisionEffect.DENY,
            reason="agent deploys are disabled (deploy_rules.allow_agent_deploy is false)",
            matched_rule="deploy_rules.allow_agent_deploy",
        )

    if env is not None and env in allowed_envs:
        return Decision(
            effect=DecisionEffect.ALLOW,
            reason=f"deploy to {env!r} permitted by deploy_rules.environments",
            matched_rule=f"deploy_rules.environments:{env}",
        )

    return Decision(
        effect=DecisionEffect.REQUIRES_APPROVAL,
        reason=f"deploy to environment {env!r} is not whitelisted; requires approval",
        matched_rule="deploy_rules",
        requires_approval=True,
        approval_gate=ApprovalGate.DEPLOY,
    )


def _is_deploy_action(action: str) -> bool:
    return action == "deploy" or action.startswith("deploy_")


def evaluate(action: ToolCall, policy: Policy) -> Decision:
    """Evaluate ``action`` against ``policy`` and return a :class:`Decision`."""
    name = _effective_action(action)

    if not name:
        return Decision(
            effect=DecisionEffect.DENY,
            reason="tool call has no tool/action name",
        )

    if name in policy.restricted_actions:
        return Decision(
            effect=DecisionEffect.DENY,
            reason=f"action {name!r} is explicitly restricted by policy",
            matched_rule=f"restricted_actions:{name}",
        )

    if name in WRITE_ACTIONS:
        return _evaluate_write(action, policy)

    if _is_deploy_action(name):
        return _evaluate_deploy(action, policy, name)

    if name in policy.allowed_actions:
        return Decision(
            effect=DecisionEffect.ALLOW,
            reason=f"action {name!r} is explicitly allowed by policy",
            matched_rule=f"allowed_actions:{name}",
        )

    return Decision(
        effect=DecisionEffect.DENY,
        reason=f"action {name!r} is not permitted by policy (default deny)",
    )


class RepoPolicyEvaluator:
    """Concrete :class:`~forge_contracts.PolicyEvaluator` over ``.forge/policy.yaml``."""

    def load(self, repo_root: str | Path) -> Policy:
        from forge_policy.loader import load_policy

        return load_policy(repo_root)

    def evaluate(self, action: ToolCall, policy: Policy) -> Decision:
        return evaluate(action, policy)


# Structural-conformance guard: fail import if the class drifts from the
# frozen contract (mirrors the runtime ``isinstance`` check in the tests).
_: PolicyEvaluator = RepoPolicyEvaluator()


__all__ = [
    "WRITE_ACTIONS",
    "RepoPolicyEvaluator",
    "evaluate",
]
