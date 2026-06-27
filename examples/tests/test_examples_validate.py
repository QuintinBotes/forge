"""Validate every shipped example against the real Forge loader for its kind.

Task 1.17's gate is that the example YAMLs *actually parse and validate* against
the frozen contracts via each owning package's loader (cross-check with Tasks
1.10 policy / 1.11 skill / 1.8 workflow / 1.12 MCP / 1.7 spec). These are not
illustrative snippets — they are executable fixtures that fail CI the moment an
example drifts from the schema or a loader changes shape.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from forge_contracts import (
    DecisionEffect,
    MCPConnection,
    Policy,
    SkillProfile,
    SpecManifest,
    ToolCall,
    WorkflowDefinition,
)
from forge_mcp import load_connection_file
from forge_policy import RepoPolicyEvaluator, load_policy
from forge_skill import SkillProfileRegistry, load_profile, load_profiles
from forge_spec import load_manifest
from forge_workflow import load_definition

EXAMPLES_ROOT = Path(__file__).resolve().parents[1]
POLICIES_DIR = EXAMPLES_ROOT / "policies"
SKILLS_DIR = EXAMPLES_ROOT / "skills"
WORKFLOWS_DIR = EXAMPLES_ROOT / "workflows"
MCP_DIR = EXAMPLES_ROOT / "mcp-connectors"
SPECS_DIR = EXAMPLES_ROOT / "specs"


def _yaml_files(directory: Path) -> list[Path]:
    return sorted(p for p in directory.glob("*.yaml"))


# --------------------------------------------------------------------------- #
# Policies (5 repo types) — Task 1.10                                          #
# --------------------------------------------------------------------------- #


def test_there_are_five_repo_policy_examples() -> None:
    assert len(_yaml_files(POLICIES_DIR)) >= 5


@pytest.mark.parametrize("path", _yaml_files(POLICIES_DIR), ids=lambda p: p.name)
def test_policy_example_loads_and_evaluates(path: Path) -> None:
    policy = load_policy(path)
    assert isinstance(policy, Policy)
    assert policy.repo_id, f"{path.name}: repo_id is required and must be non-empty"
    assert isinstance(policy.write_rules.allow, list)
    # The evaluator must accept the loaded policy and return a real Decision.
    decision = RepoPolicyEvaluator().evaluate(ToolCall(tool="read_repo"), policy)
    assert decision.effect in {
        DecisionEffect.ALLOW,
        DecisionEffect.DENY,
        DecisionEffect.REQUIRES_APPROVAL,
    }


def test_policy_examples_cover_distinct_repo_types() -> None:
    repo_ids = {load_policy(p).repo_id for p in _yaml_files(POLICIES_DIR)}
    assert len(repo_ids) == len(_yaml_files(POLICIES_DIR)), "each example needs a unique repo_id"


# --------------------------------------------------------------------------- #
# Skill profiles — Task 1.11                                                   #
# --------------------------------------------------------------------------- #


def test_skill_examples_exist() -> None:
    assert _yaml_files(SKILLS_DIR), "expected at least one skill example"


def test_community_skill_collection_loads_into_registry() -> None:
    collection = SKILLS_DIR / "community-profiles.yaml"
    profiles = load_profiles(collection)
    assert profiles, "collection must contain at least one profile"
    for name, profile in profiles.items():
        assert isinstance(profile, SkillProfile)
        assert profile.name == name
    # Community profiles must be registrable alongside the built-ins.
    registry = SkillProfileRegistry()
    for profile in profiles.values():
        registry.register(profile)
        assert registry.get(profile.name).name == profile.name


def test_single_skill_profile_loads() -> None:
    single = SKILLS_DIR / "single-profile.yaml"
    profile = load_profile(single)
    assert isinstance(profile, SkillProfile)
    assert profile.name


def test_community_profiles_do_not_shadow_builtins() -> None:
    profiles = load_profiles(SKILLS_DIR / "community-profiles.yaml")
    builtins = set(SkillProfileRegistry().names())
    assert not (set(profiles) & builtins), "examples should add NEW profiles, not shadow built-ins"


# --------------------------------------------------------------------------- #
# Workflows — Task 1.8                                                         #
# --------------------------------------------------------------------------- #


def test_there_are_workflow_examples() -> None:
    assert _yaml_files(WORKFLOWS_DIR), "expected workflow DSL examples"


@pytest.mark.parametrize("path", _yaml_files(WORKFLOWS_DIR), ids=lambda p: p.name)
def test_workflow_example_parses_and_builds_graph(path: Path) -> None:
    # load_definition validates the transition graph at parse time, so a returned
    # definition proves the DSL is structurally sound (no orphan/duplicate edges).
    definition = load_definition(path)
    assert isinstance(definition, WorkflowDefinition)
    assert definition.name
    assert definition.transitions, "a workflow needs at least one transition"


# --------------------------------------------------------------------------- #
# MCP connectors — Task 1.12                                                   #
# --------------------------------------------------------------------------- #


def test_there_are_mcp_connector_examples() -> None:
    assert len(_yaml_files(MCP_DIR)) >= 4


@pytest.mark.parametrize("path", _yaml_files(MCP_DIR), ids=lambda p: p.name)
def test_mcp_connector_example_loads(path: Path) -> None:
    conn = load_connection_file(path)
    assert isinstance(conn, MCPConnection)
    assert conn.id and conn.name
    # Security rule 1: write access must never be silently enabled by an example.
    assert conn.allow_write is False, f"{path.name}: examples must keep allow_write=false"


# --------------------------------------------------------------------------- #
# Spec manifests — Task 1.7                                                    #
# --------------------------------------------------------------------------- #


def _manifest_files() -> list[Path]:
    return sorted(SPECS_DIR.rglob("manifest.yaml"))


def test_there_is_a_spec_manifest_example() -> None:
    assert _manifest_files(), "expected at least one spec manifest example"


@pytest.mark.parametrize("path", _manifest_files(), ids=lambda p: p.parent.name)
def test_spec_manifest_example_loads(path: Path) -> None:
    manifest = load_manifest(path.read_text(encoding="utf-8"))
    assert isinstance(manifest, SpecManifest)
    assert manifest.id and manifest.name
    # Acceptance criteria must reference declared requirements (traceability).
    req_ids = {r.id for r in manifest.requirements}
    for ac in manifest.acceptance_criteria:
        for ref in ac.req_refs:
            assert ref in req_ids, f"{path}: acceptance {ac.id} references unknown requirement {ref}"
