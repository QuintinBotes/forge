"""Unit tests for the ao-complexity deterministic sizing module."""

from __future__ import annotations

import pytest
from forge_orchestration_policy import ComplexitySizing, SizingSignals, score_complexity
from forge_orchestration_policy.complexity import signals_from_spec

from forge_contracts import (
    AcceptanceCriterion,
    OpenQuestion,
    Priority,
    Requirement,
    SpecManifest,
    TaskKind,
)


def _sizing(**kwargs: object) -> ComplexitySizing:
    return score_complexity(SizingSignals(**kwargs))


class TestDefaults:
    def test_all_defaults_is_junior_single(self) -> None:
        result = _sizing()
        assert result.tier == "junior"
        assert result.strategy == "single"
        assert result.score == 2  # FEATURE kind (+1) + MEDIUM priority (+1)

    def test_result_reasons_are_non_empty(self) -> None:
        result = _sizing()
        assert result.reasons
        assert any("tier=junior" in r for r in result.reasons)
        assert any("strategy=single" in r for r in result.reasons)


class TestDeterminism:
    def test_same_signals_produce_identical_result(self) -> None:
        signals = SizingSignals(
            kind=TaskKind.FEATURE,
            priority=Priority.HIGH,
            file_count=7,
            repo_count=1,
            requirement_count=5,
        )
        first = score_complexity(signals)
        second = score_complexity(signals)
        assert first == second


class TestFixtureSpectrum:
    """Fixture tasks spanning trivial -> junior -> medior -> senior/swarm."""

    def test_trivial_doc_chore_is_junior_single(self) -> None:
        result = _sizing(
            kind=TaskKind.DOC,
            priority=Priority.LOW,
            file_count=1,
            repo_count=1,
            requirement_count=1,
            acceptance_criteria_count=1,
        )
        assert result.tier == "junior"
        assert result.strategy == "single"

    def test_small_bug_fix_is_junior(self) -> None:
        result = _sizing(
            kind=TaskKind.BUG,
            priority=Priority.MEDIUM,
            blast_radius="low",
            file_count=2,
            repo_count=1,
            requirement_count=1,
            acceptance_criteria_count=2,
            dependency_count=0,
        )
        assert result.tier == "junior"
        assert result.strategy == "single"

    def test_moderate_feature_is_medior_single(self) -> None:
        result = _sizing(
            kind=TaskKind.FEATURE,
            priority=Priority.HIGH,
            blast_radius="medium",
            file_count=8,
            repo_count=1,
            requirement_count=5,
            acceptance_criteria_count=5,
            dependency_count=1,
        )
        assert result.tier == "medior"
        assert result.strategy == "single"

    def test_large_change_request_touching_contracts_is_senior_swarm(self) -> None:
        result = _sizing(
            kind=TaskKind.CHANGE_REQUEST,
            priority=Priority.URGENT,
            blast_radius="high",
            file_count=25,
            repo_count=1,
            requirement_count=12,
            acceptance_criteria_count=12,
            touches_contracts=True,
            touches_security=True,
            dependency_count=4,
            open_questions_count=3,
        )
        assert result.tier == "senior"
        assert result.strategy == "swarm"

    def test_multi_repo_forces_swarm_even_at_medior(self) -> None:
        result = _sizing(
            kind=TaskKind.FEATURE,
            priority=Priority.MEDIUM,
            file_count=3,
            repo_count=2,
            requirement_count=2,
            acceptance_criteria_count=2,
        )
        assert result.strategy == "swarm"
        assert any("repo_count=2" in r for r in result.reasons)

    def test_contracts_and_security_together_forces_swarm(self) -> None:
        result = _sizing(
            kind=TaskKind.BUG,
            priority=Priority.LOW,
            file_count=1,
            repo_count=1,
            touches_contracts=True,
            touches_security=True,
        )
        assert result.strategy == "swarm"
        assert any("touches_contracts and touches_security" in r for r in result.reasons)

    def test_incident_with_high_blast_radius_escalates_tier(self) -> None:
        result = _sizing(
            kind=TaskKind.INCIDENT,
            priority=Priority.URGENT,
            blast_radius="high",
            file_count=4,
            repo_count=1,
            dependency_count=2,
        )
        assert result.tier in {"medior", "senior"}

    def test_underspecified_flag_raises_score(self) -> None:
        specified = _sizing(kind=TaskKind.FEATURE, requirement_count=4, acceptance_criteria_count=4)
        underspecified = _sizing(
            kind=TaskKind.FEATURE,
            requirement_count=4,
            acceptance_criteria_count=4,
            underspecified=True,
        )
        assert underspecified.score == specified.score + 3
        assert any("underspecified" in r for r in underspecified.reasons)

    def test_ambiguity_via_open_questions_raises_score_monotonically(self) -> None:
        none_open = _sizing(open_questions_count=0)
        some_open = _sizing(open_questions_count=2)
        many_open = _sizing(open_questions_count=5)
        assert none_open.score < some_open.score < many_open.score


class TestMonotonicity:
    """Increasing any single complexity signal never decreases the score."""

    @pytest.mark.parametrize(
        "field,low,high",
        [
            ("file_count", 1, 20),
            ("repo_count", 1, 3),
            ("requirement_count", 1, 10),
            ("acceptance_criteria_count", 1, 10),
            ("dependency_count", 0, 5),
            ("open_questions_count", 0, 5),
        ],
    )
    def test_field_increase_does_not_decrease_score(self, field: str, low: int, high: int) -> None:
        base_low = _sizing(**{field: low})
        base_high = _sizing(**{field: high})
        assert base_high.score >= base_low.score

    def test_touches_contracts_never_decreases_score(self) -> None:
        without = _sizing(touches_contracts=False)
        with_ = _sizing(touches_contracts=True)
        assert with_.score > without.score

    def test_touches_security_never_decreases_score(self) -> None:
        without = _sizing(touches_security=False)
        with_ = _sizing(touches_security=True)
        assert with_.score > without.score


class TestTierBoundaries:
    def test_tier_is_one_of_the_three_allowed_values(self) -> None:
        for kind in TaskKind:
            result = _sizing(kind=kind)
            assert result.tier in {"junior", "medior", "senior"}

    def test_strategy_is_single_or_swarm(self) -> None:
        for kind in TaskKind:
            result = _sizing(kind=kind)
            assert result.strategy in {"single", "swarm"}

    def test_only_senior_or_explicit_triggers_yield_swarm(self) -> None:
        result = _sizing(
            kind=TaskKind.FEATURE,
            priority=Priority.LOW,
            file_count=1,
            repo_count=1,
            touches_contracts=False,
            touches_security=False,
        )
        assert result.tier != "senior"
        assert result.strategy == "single"


class TestSignalsFromSpec:
    def _manifest(self, **overrides: object) -> SpecManifest:
        defaults: dict[str, object] = {
            "id": "spec-1",
            "name": "Example spec",
            "repos": ["forge"],
            "requirements": [Requirement(id="R1", text="Do the thing")],
            "acceptance_criteria": [
                AcceptanceCriterion(id="AC1", text="Given...When...Then...", req_refs=["R1"])
            ],
            "open_questions": [],
        }
        defaults.update(overrides)
        return SpecManifest(**defaults)

    def test_derives_counts_from_manifest(self) -> None:
        manifest = self._manifest()
        signals = signals_from_spec(manifest)
        assert signals.repo_count == 1
        assert signals.requirement_count == 1
        assert signals.acceptance_criteria_count == 1
        assert signals.open_questions_count == 0
        assert signals.underspecified is False

    def test_flags_underspecified_when_requirements_without_acceptance_criteria(self) -> None:
        manifest = self._manifest(acceptance_criteria=[])
        signals = signals_from_spec(manifest)
        assert signals.underspecified is True

    def test_empty_manifest_is_not_underspecified(self) -> None:
        manifest = self._manifest(requirements=[], acceptance_criteria=[])
        signals = signals_from_spec(manifest)
        assert signals.underspecified is False

    def test_multi_repo_manifest_derives_repo_count(self) -> None:
        manifest = self._manifest(repos=["forge", "forge-web"])
        signals = signals_from_spec(manifest)
        assert signals.repo_count == 2

    def test_overrides_take_precedence_over_manifest_derived_defaults(self) -> None:
        manifest = self._manifest()
        signals = signals_from_spec(manifest, touches_contracts=True, dependency_count=3)
        assert signals.touches_contracts is True
        assert signals.dependency_count == 3

    def test_open_questions_counted(self) -> None:
        manifest = self._manifest(open_questions=[OpenQuestion(id="Q1", text="What about X?")])
        signals = signals_from_spec(manifest)
        assert signals.open_questions_count == 1

    def test_signals_from_spec_feeds_score_complexity(self) -> None:
        manifest = self._manifest(
            requirements=[Requirement(id=f"R{i}", text="x") for i in range(12)],
            acceptance_criteria=[AcceptanceCriterion(id=f"AC{i}", text="x") for i in range(12)],
        )
        signals = signals_from_spec(
            manifest,
            touches_contracts=True,
            touches_security=True,
            dependency_count=5,
            priority=Priority.URGENT,
        )
        result = score_complexity(signals)
        assert result.tier == "senior"
        assert result.strategy == "swarm"
