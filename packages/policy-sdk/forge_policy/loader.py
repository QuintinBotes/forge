"""Load ``.forge/policy.yaml`` into a frozen :class:`~forge_contracts.Policy` DTO.

The on-disk schema is the one fixed in ``docs/FORGE_SPEC.md`` ("policy.yaml
Schema"). Loading is pure parsing/validation: no merging, no I/O beyond reading
the file. A repo with no policy is treated as an error rather than silently
permissive — per the spec, every task must know its policy before execution.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from forge_contracts import ForgeError, Policy

#: Conventional location of the machine-readable policy inside a repo.
POLICY_RELATIVE_PATH = Path(".forge") / "policy.yaml"


class PolicyLoadError(ForgeError):
    """Raised when a repo's ``.forge/policy.yaml`` cannot be loaded (F22).

    Multi-repo loading is fail-closed *per repo*: a single bad/missing policy
    fails the whole run, named by ``repo`` — never a partial, silently-permissive
    run (spec §8 "Fail-closed config").
    """

    def __init__(self, repo: str, cause: Exception) -> None:
        self.repo = repo
        self.cause = cause
        super().__init__(f"failed to load policy for repo {repo!r}: {cause}")


def resolve_policy_path(repo_root: str | Path) -> Path:
    """Return the policy-file path for ``repo_root``.

    ``repo_root`` may be a repository directory (the policy is looked up at
    ``<repo_root>/.forge/policy.yaml``) or a direct path to a policy ``.yaml``
    file, which is returned as-is.
    """
    path = Path(repo_root)
    if path.is_file():
        return path
    return path / POLICY_RELATIVE_PATH


def load_policy(repo_root: str | Path) -> Policy:
    """Load and validate the policy for ``repo_root``.

    Raises:
        FileNotFoundError: if no policy file exists.
        ValueError: if the file is empty or does not parse to a YAML mapping.
        pydantic.ValidationError: if the contents violate the ``Policy`` schema.
    """
    path = resolve_policy_path(repo_root)
    if not path.is_file():
        raise FileNotFoundError(f"No policy file found at {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if raw is None:
        raise ValueError(f"Policy file is empty: {path}")
    if not isinstance(raw, dict):
        raise ValueError(
            f"Policy file must contain a YAML mapping, got {type(raw).__name__}: {path}"
        )

    return Policy.model_validate(raw)


def load_policies(worktree_roots: dict[str, str | Path]) -> dict[str, Policy]:
    """Load + validate the policy for every repo in a multi-repo run (F22).

    ``worktree_roots`` maps each ``repo_id`` to the repo directory (or a direct
    policy-file path). Loading is **fail-closed per repo**: the first repo whose
    policy is missing/invalid raises :class:`PolicyLoadError` naming that repo, so
    a run never starts with a partially-loaded (and therefore unsafe) policy set.

    Returns a ``repo_id -> Policy`` mapping with one entry per input repo.
    """
    policies: dict[str, Policy] = {}
    for repo_id, root in worktree_roots.items():
        try:
            policies[repo_id] = load_policy(root)
        except Exception as exc:
            raise PolicyLoadError(repo_id, exc) from exc
    return policies


__all__ = [
    "POLICY_RELATIVE_PATH",
    "PolicyLoadError",
    "load_policies",
    "load_policy",
    "resolve_policy_path",
]
