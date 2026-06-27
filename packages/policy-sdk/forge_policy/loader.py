"""Load ``.forge/policy.yaml`` into a frozen :class:`~forge_contracts.Policy` DTO.

The on-disk schema is the one fixed in ``docs/FORGE_SPEC.md`` ("policy.yaml
Schema"). Loading is pure parsing/validation: no merging, no I/O beyond reading
the file. A repo with no policy is treated as an error rather than silently
permissive — per the spec, every task must know its policy before execution.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from forge_contracts import Policy

#: Conventional location of the machine-readable policy inside a repo.
POLICY_RELATIVE_PATH = Path(".forge") / "policy.yaml"


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


__all__ = ["POLICY_RELATIVE_PATH", "load_policy", "resolve_policy_path"]
