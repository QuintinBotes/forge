"""F19 — ``.forge/policy.yaml`` ``sandbox:`` block loads into the Policy DTO."""

from __future__ import annotations

import textwrap
from pathlib import Path

from scf_policy import load_policy

SANDBOX_POLICY_YAML = textwrap.dedent(
    """
    repo_id: github.com/org/api
    languages: [python]
    commands:
      install: uv sync
      test: pytest -q
    sandbox:
      isolation: container
      image: ghcr.io/quintinbotes/swarm-code-factory/sandbox-python:0.1.0
      network: egress
      egress_allowlist: [pypi.org, files.pythonhosted.org]
      cpus: 2
      memory: 4g
      pids_limit: 512
      exec_timeout_seconds: 1800
      setup_commands: [uv sync]
    """
).strip()


def test_policy_without_sandbox_block_defaults_to_none() -> None:
    minimal = "repo_id: github.com/org/api\ncommands:\n  test: pytest -q\n"
    import yaml

    from scf_contracts import Policy

    policy = Policy.model_validate(yaml.safe_load(minimal))
    assert policy.sandbox is None


def test_loads_sandbox_block(tmp_path: Path) -> None:
    scf_dir = tmp_path / ".forge"
    scf_dir.mkdir()
    (scf_dir / "policy.yaml").write_text(SANDBOX_POLICY_YAML, encoding="utf-8")

    policy = load_policy(tmp_path)
    assert policy.sandbox is not None
    block = policy.sandbox
    assert block.isolation == "container"
    assert block.network == "egress"
    assert block.egress_allowlist == ["pypi.org", "files.pythonhosted.org"]
    assert block.memory == "4g"
    assert block.cpus == 2
    assert block.setup_commands == ["uv sync"]
