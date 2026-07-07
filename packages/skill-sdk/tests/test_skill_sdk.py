"""Tests for the skill-sdk (plan Task 1.11).

Covers the three behaviours the plan names explicitly:

- ``backend-tdd`` injects test-first + an 80% coverage gate,
- ``incident-response`` forbids ``deploy_prod``,
- an unknown profile raises ``UnknownSkillProfileError``,

plus the full built-in profile set (matching ``docs/FORGE_SPEC.md`` verbatim),
the YAML loader, registry behaviour, structural Protocol conformance, and the
non-mutating semantics of behaviour injection into an ``AgentObjective``.

Everything here is hermetic: no external services, no network.
"""

from __future__ import annotations

import pytest

import forge_skill as fs
from forge_contracts import AgentObjective, ApprovalPolicy, SkillProfile
from forge_contracts import SkillProfileRegistry as SkillProfileRegistryProtocol
from forge_contracts.exceptions import UnknownSkillProfileError
from forge_skill import (
    BUILTIN_PROFILE_NAMES,
    SkillProfileRegistry,
    builtin_profiles,
    inject_profile,
    load_profile,
    load_profiles,
)

# The seven profiles named in plan Task 1.11 and FORGE_SPEC.md "Skill Profiles".
EXPECTED_NAMES = (
    "backend-tdd",
    "backend-fast",
    "frontend-ui",
    "incident-response",
    "spec-analyst",
    "security-review",
    "chore-fast",
)


# --------------------------------------------------------------------------- #
# Public surface                                                              #
# --------------------------------------------------------------------------- #


def test_public_exports_present() -> None:
    for symbol in (
        "SkillProfileRegistry",
        "load_profiles",
        "load_profile",
        "inject_profile",
        "builtin_profiles",
        "BUILTIN_PROFILE_NAMES",
    ):
        assert hasattr(fs, symbol), symbol


def test_registry_is_structural_protocol_conformant() -> None:
    registry = SkillProfileRegistry()
    assert isinstance(registry, SkillProfileRegistryProtocol)


# --------------------------------------------------------------------------- #
# Built-in profile set                                                        #
# --------------------------------------------------------------------------- #


def test_builtin_profile_names_match_spec() -> None:
    assert set(BUILTIN_PROFILE_NAMES) == set(EXPECTED_NAMES)
    assert set(builtin_profiles()) == set(EXPECTED_NAMES)


def test_default_registry_contains_all_builtins() -> None:
    registry = SkillProfileRegistry()
    for name in EXPECTED_NAMES:
        assert name in registry
        assert isinstance(registry.get(name), SkillProfile)
    assert set(registry.names()) == set(EXPECTED_NAMES)


def test_builtin_profiles_are_independent_copies() -> None:
    # Mutating one registry's resolved profile must not leak into a fresh one.
    a = SkillProfileRegistry().get("backend-tdd")
    a.forbidden_shortcuts.append("MUTATED")
    b = SkillProfileRegistry().get("backend-tdd")
    assert "MUTATED" not in b.forbidden_shortcuts


def test_backend_tdd_profile_fields() -> None:
    profile = SkillProfileRegistry().get("backend-tdd")
    assert profile.requires_plan is True
    assert profile.requires_tests_before_implementation is True
    assert profile.min_test_coverage == 80
    assert profile.review_required is True
    assert profile.verification_steps == [
        "lint",
        "type_check",
        "unit_tests",
        "integration_tests",
    ]
    assert "skip_tests" in profile.forbidden_shortcuts
    assert "hardcoded_secrets" in profile.forbidden_shortcuts


def test_backend_fast_profile_fields() -> None:
    profile = SkillProfileRegistry().get("backend-fast")
    assert profile.requires_plan is False
    assert profile.min_test_coverage == 60
    assert profile.review_required is True


def test_frontend_ui_profile_fields() -> None:
    profile = SkillProfileRegistry().get("frontend-ui")
    assert profile.requires_plan is True
    assert profile.accessibility_check is True
    assert profile.review_required is True


def test_incident_response_profile_fields() -> None:
    profile = SkillProfileRegistry().get("incident-response")
    assert profile.requires_human_approval_before_action is True
    assert profile.max_blast_radius == "low"
    assert "deploy_prod" in profile.forbidden_actions
    assert "delete_data" in profile.forbidden_actions
    assert "read_logs" in profile.allowed_actions


def test_spec_analyst_profile_fields() -> None:
    profile = SkillProfileRegistry().get("spec-analyst")
    assert profile.output_type == "spec_document"
    assert profile.human_review_required is True
    assert "write_spec" in profile.allowed_actions


def test_security_review_profile_fields() -> None:
    profile = SkillProfileRegistry().get("security-review")
    assert profile.output_type == "security_report"
    assert profile.report_format == "sarif"
    assert "sast" in profile.tools


def test_chore_fast_profile_fields() -> None:
    profile = SkillProfileRegistry().get("chore-fast")
    assert profile.requires_plan is False
    assert profile.review_required is False


# --------------------------------------------------------------------------- #
# get() error path                                                            #
# --------------------------------------------------------------------------- #


def test_get_unknown_profile_raises() -> None:
    registry = SkillProfileRegistry()
    with pytest.raises(UnknownSkillProfileError):
        registry.get("does-not-exist")


def test_unknown_profile_error_is_keyerror() -> None:
    # Contract: UnknownSkillProfileError subclasses KeyError for ergonomic catching.
    registry = SkillProfileRegistry()
    with pytest.raises(KeyError):
        registry.get("nope")


def test_unknown_profile_error_message_names_profile() -> None:
    registry = SkillProfileRegistry()
    with pytest.raises(UnknownSkillProfileError) as exc:
        registry.get("ghost")
    assert "ghost" in str(exc.value)


# --------------------------------------------------------------------------- #
# Registration                                                                #
# --------------------------------------------------------------------------- #


def test_register_custom_profile_then_get() -> None:
    registry = SkillProfileRegistry(include_builtins=False)
    custom = SkillProfile(name="my-custom", requires_plan=True)
    registry.register(custom)
    assert registry.get("my-custom") is custom
    assert registry.names() == ["my-custom"]


def test_custom_profile_overrides_builtin() -> None:
    registry = SkillProfileRegistry()
    override = SkillProfile(name="backend-tdd", min_test_coverage=95)
    registry.register(override)
    assert registry.get("backend-tdd").min_test_coverage == 95


def test_registry_seeded_from_mapping_and_iterable() -> None:
    p1 = SkillProfile(name="alpha")
    p2 = SkillProfile(name="beta")
    from_map = SkillProfileRegistry({"alpha": p1, "beta": p2}, include_builtins=False)
    assert set(from_map.names()) == {"alpha", "beta"}
    from_iter = SkillProfileRegistry([p1, p2], include_builtins=False)
    assert set(from_iter.names()) == {"alpha", "beta"}


# --------------------------------------------------------------------------- #
# YAML loader                                                                 #
# --------------------------------------------------------------------------- #


WRAPPED_YAML = """
skill_profiles:
  backend-tdd:
    description: Backend feature development with test-driven discipline
    requires_plan: true
    requires_tests_before_implementation: true
    min_test_coverage: 80
    verification_steps: [lint, type_check, unit_tests, integration_tests]
    review_required: true
    forbidden_shortcuts: [skip_tests, no_error_handling, hardcoded_secrets]
  chore-fast:
    requires_plan: false
    review_required: false
"""

FLAT_YAML = """
custom-a:
  requires_plan: true
custom-b:
  requires_plan: false
"""


def test_load_profiles_from_wrapped_yaml_string() -> None:
    profiles = load_profiles(WRAPPED_YAML)
    assert set(profiles) == {"backend-tdd", "chore-fast"}
    assert profiles["backend-tdd"].name == "backend-tdd"
    assert profiles["backend-tdd"].min_test_coverage == 80
    assert profiles["chore-fast"].review_required is False


def test_load_profiles_from_flat_mapping_string() -> None:
    profiles = load_profiles(FLAT_YAML)
    assert set(profiles) == {"custom-a", "custom-b"}
    assert profiles["custom-a"].requires_plan is True


def test_load_profiles_from_dict() -> None:
    profiles = load_profiles({"x": {"requires_plan": True}})
    assert profiles["x"].name == "x"
    assert profiles["x"].requires_plan is True


def test_load_profiles_from_path(tmp_path) -> None:
    path = tmp_path / "profiles.yaml"
    path.write_text(WRAPPED_YAML, encoding="utf-8")
    profiles = load_profiles(path)
    assert set(profiles) == {"backend-tdd", "chore-fast"}


def test_load_single_profile_doc() -> None:
    profile = load_profile({"name": "solo", "requires_plan": True})
    assert isinstance(profile, SkillProfile)
    assert profile.name == "solo"
    assert profile.requires_plan is True


def test_registry_from_yaml_classmethod() -> None:
    registry = SkillProfileRegistry.from_yaml(WRAPPED_YAML, include_builtins=False)
    assert set(registry.names()) == {"backend-tdd", "chore-fast"}


# --------------------------------------------------------------------------- #
# Behaviour injection                                                         #
# --------------------------------------------------------------------------- #


def _bare_objective() -> AgentObjective:
    return AgentObjective(objective="Add a customer search endpoint")


def test_inject_backend_tdd_sets_test_first_and_coverage_gate() -> None:
    registry = SkillProfileRegistry()
    profile = registry.get("backend-tdd")
    result = registry.inject(profile, _bare_objective())

    # The full behaviour profile is carried on the objective.
    assert result.skill_profile is not None
    assert result.skill_profile.name == "backend-tdd"
    assert result.skill_profile.requires_tests_before_implementation is True
    assert result.skill_profile.min_test_coverage == 80

    # ...and flattened into a directives surface for downstream consumers.
    directives = result.context["skill"]
    assert directives["requires_plan"] is True
    assert directives["requires_tests_before_implementation"] is True
    assert directives["min_test_coverage"] == 80
    assert "skip_tests" in directives["forbidden_shortcuts"]


def test_inject_incident_response_forbids_deploy_prod() -> None:
    registry = SkillProfileRegistry()
    profile = registry.get("incident-response")
    result = registry.inject(profile, _bare_objective())

    assert "deploy_prod" in result.restricted_actions
    assert "delete_data" in result.restricted_actions
    # Allowed actions from the profile are merged in too.
    assert "read_logs" in result.allowed_actions


def test_inject_human_approval_profile_tightens_gates() -> None:
    registry = SkillProfileRegistry()
    profile = registry.get("incident-response")
    result = registry.inject(profile, _bare_objective())
    assert result.requires_approval.deploy is True
    assert result.requires_approval.pr is True
    assert result.context["skill"]["requires_human_approval_before_action"] is True


def test_inject_merges_without_dropping_existing_actions() -> None:
    objective = AgentObjective(
        objective="x",
        allowed_actions=["read_repo"],
        restricted_actions=["push_to_main"],
    )
    profile = SkillProfileRegistry().get("incident-response")
    result = inject_profile(profile, objective)
    # Pre-existing entries preserved, profile entries appended, no duplicates.
    assert result.allowed_actions[0] == "read_repo"
    assert "read_logs" in result.allowed_actions
    assert "push_to_main" in result.restricted_actions
    assert "deploy_prod" in result.restricted_actions
    assert len(result.restricted_actions) == len(set(result.restricted_actions))


def test_inject_does_not_mutate_input_objective() -> None:
    objective = _bare_objective()
    profile = SkillProfileRegistry().get("backend-tdd")
    inject_profile(profile, objective)
    # Original is untouched.
    assert objective.skill_profile is None
    assert objective.allowed_actions == []
    assert objective.restricted_actions == []
    assert objective.context == {}
    assert objective.requires_approval == ApprovalPolicy()


def test_inject_is_idempotent_on_actions() -> None:
    registry = SkillProfileRegistry()
    profile = registry.get("incident-response")
    once = registry.inject(profile, _bare_objective())
    twice = registry.inject(profile, once)
    assert twice.restricted_actions == once.restricted_actions
    assert twice.allowed_actions == once.allowed_actions
