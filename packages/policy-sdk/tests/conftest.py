"""Shared fixtures for the policy-sdk test suite."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from forge_contracts import Policy

# The canonical ``.forge/policy.yaml`` from ``docs/FORGE_SPEC.md`` (policy.yaml
# Schema), extended with the action allow/deny lists the ``Policy`` DTO supports.
SPEC_POLICY_YAML = textwrap.dedent(
    """
    repo_id: github.com/org/api
    name: Core API Service
    purpose: Backend REST API for customer operations.
    languages: [python]
    entrypoints:
      - app/main.py
    commands:
      install: uv sync
      lint: ruff check . && ruff format --check .
      type_check: mypy app/
      test: pytest -q
      build: docker build -t api .
    write_rules:
      allow: [app/**, tests/**, docs/**, alembic/versions/**]
      deny: [infra/prod/**, .env*, secrets/**, "*.pem", "*.key"]
    review_rules:
      required_reviewers: [team-backend]
      approval_required_for_merge: true
      min_approvals: 1
    deploy_rules:
      allow_agent_deploy: false
      environments: [dev]
      restricted_environments: [staging, production]
    knowledge_rules:
      index_paths: [app/**, docs/**, specs/**]
      exclude_paths: [.venv/**, __pycache__/**, "*.pyc"]
      freshness_sla_hours: 24
    skill_profiles:
      default: backend-tdd
      allowed: [backend-tdd, backend-fast, security-review, spec-analyst]
    subagent_rules:
      allow_subagents: true
      allowed_roles: [reviewer, tester]
      max_parallel: 2
    allowed_actions: [read_repo, write_code, run_tests, open_pr, read_knowledge, query_mcp]
    restricted_actions: [deploy_prod, delete_files, push_to_main, modify_access_controls]
    """
).strip()


@pytest.fixture
def spec_policy() -> Policy:
    """The spec example policy as a parsed :class:`Policy` DTO."""
    import yaml

    return Policy.model_validate(yaml.safe_load(SPEC_POLICY_YAML))


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    """A temp repo containing ``.forge/policy.yaml``."""
    forge_dir = tmp_path / ".forge"
    forge_dir.mkdir()
    (forge_dir / "policy.yaml").write_text(SPEC_POLICY_YAML, encoding="utf-8")
    return tmp_path
