"""Risk classification: the contracts, ``derive`` and its properties (#192, spec A §3, §9)."""

from __future__ import annotations

import ast
import glob
import random
from pathlib import Path

import pydantic
import pytest

import scf_contracts
from scf_contracts import (
    POLICY_CONDITION_FIELDS,
    ChangeSignals,
    Condition,
    ConditionalRule,
    ConditionGroup,
    ConditionOp,
    RiskClass,
    RiskRule,
    RiskRules,
    RuleEffect,
    evaluate_condition,
)
from scf_risk.classifier import derive
from scf_risk.rules import matches

SEED = 192
ITERATIONS = 500
PACKAGE_DIR = Path(__file__).resolve().parents[1] / "scf_risk"


def _rule(paths: list[str], risk_class: RiskClass, rule_id: str | None = None) -> RiskRule:
    return RiskRule.model_validate({"id": rule_id, "paths": paths, "class": risk_class})


def _rules(*derive_rules: RiskRule, default: RiskClass = RiskClass.LOW) -> RiskRules:
    return RiskRules(default=default, derive=list(derive_rules))


# --------------------------------------------------------------------------- #
# Contracts (AC1, AC9)                                                          #
# --------------------------------------------------------------------------- #


def test_contracts_export_the_risk_models() -> None:
    for name in (
        "RiskClass",
        "RiskRules",
        "RiskClassRequirements",
        "ChangeSignals",
        "DerivedRisk",
        "RiskEvaluation",
        "RiskOverride",
        "GateRequirements",
    ):
        assert name in scf_contracts.__all__
        assert hasattr(scf_contracts, name)


def test_risk_class_is_ordered_minimal_low_high_critical() -> None:
    assert RiskClass.MINIMAL < RiskClass.LOW < RiskClass.HIGH < RiskClass.CRITICAL
    assert RiskClass.CRITICAL > RiskClass.HIGH >= RiskClass.HIGH > RiskClass.LOW
    assert max(RiskClass.LOW, RiskClass.CRITICAL, RiskClass.MINIMAL) is RiskClass.CRITICAL
    assert sorted(RiskClass, reverse=True) == [
        RiskClass.CRITICAL,
        RiskClass.HIGH,
        RiskClass.LOW,
        RiskClass.MINIMAL,
    ]


def test_a_plain_string_is_ordered_as_a_risk_class() -> None:
    # A class read from JSON or a database column arrives as str, not as RiskClass.
    # The str is on the left on purpose: that order fell back to alphabetical.
    assert not "minimal" >= RiskClass.CRITICAL  # noqa: SIM300
    assert "critical" >= RiskClass.HIGH  # noqa: SIM300
    assert RiskClass.LOW < "high"
    assert RiskClass.CRITICAL > "minimal"
    assert RiskClass.HIGH <= "high"
    with pytest.raises(ValueError, match="severe"):
        _ = RiskClass.LOW < "severe"


def test_change_signals_carry_no_author_team_or_agent_field() -> None:
    assert set(ChangeSignals.model_fields) == {
        "paths",
        "migration_present",
        "touches_contracts",
        "lines_added",
        "lines_removed",
        "repo_count",
        "available",
        "unavailable_reason",
    }


# --------------------------------------------------------------------------- #
# derive (AC2, AC4)                                                             #
# --------------------------------------------------------------------------- #


def test_a_critical_path_makes_the_whole_change_critical() -> None:
    rules = _rules(
        _rule(["docs/**", "**/*.md"], RiskClass.MINIMAL, "docs"),
        _rule(["app/auth/**"], RiskClass.HIGH, "auth"),
        _rule(["app/billing/**"], RiskClass.CRITICAL, "billing"),
    )
    signals = ChangeSignals(paths=["docs/guide.md", "app/billing/charge.py", "app/auth/login.py"])

    derived = derive(signals, rules)

    assert derived.risk_class is RiskClass.CRITICAL
    assert derived.matched_rule_ids == ["docs", "auth", "billing"]
    assert derived.fallback_applied is False


def test_no_matching_rule_takes_the_policy_default() -> None:
    rules = _rules(_rule(["app/billing/**"], RiskClass.CRITICAL), default=RiskClass.HIGH)

    derived = derive(ChangeSignals(paths=["src/main.py"]), rules)

    assert derived.risk_class is RiskClass.HIGH
    assert derived.matched_rule_ids == []
    assert derived.fallback_applied is False


def test_the_default_is_the_floor_under_matching_rules() -> None:
    # Spec A §3.1: derived = max(class of every matching rule, policy default).
    docs = _rule(["docs/**"], RiskClass.MINIMAL, "docs")
    signals = ChangeSignals(paths=["docs/index.md"])

    assert derive(signals, _rules(docs, default=RiskClass.LOW)).risk_class is RiskClass.LOW
    assert derive(signals, _rules(docs, default=RiskClass.MINIMAL)).risk_class is RiskClass.MINIMAL


def test_rules_without_an_id_are_named_by_position() -> None:
    rules = _rules(_rule(["a/**"], RiskClass.HIGH), _rule(["b/**"], RiskClass.HIGH, "b"))

    derived = derive(ChangeSignals(paths=["a/x", "b/y"]), rules)

    assert derived.matched_rule_ids == ["derive[0]", "b"]


def test_unavailable_signals_take_the_fallback_never_the_default() -> None:
    rules = _rules(_rule(["**"], RiskClass.MINIMAL), default=RiskClass.MINIMAL)
    signals = ChangeSignals(paths=["docs/a.md"], available=False, unavailable_reason="diff timeout")

    derived = derive(signals, rules)

    assert derived.risk_class is RiskClass.CRITICAL
    assert derived.fallback_applied is True
    assert derived.reason == "diff timeout"
    assert derived.matched_rule_ids == []


def test_a_configured_fallback_replaces_critical_and_a_reason_is_always_given() -> None:
    rules = RiskRules(default=RiskClass.MINIMAL, fallback=RiskClass.HIGH)

    derived = derive(ChangeSignals(available=False), rules)

    assert derived.risk_class is RiskClass.HIGH
    assert derived.fallback_applied is True
    assert derived.reason


# --------------------------------------------------------------------------- #
# Properties (AC2 determinism, AC3)                                             #
# --------------------------------------------------------------------------- #

_SEGMENTS = ["docs", "app", "auth", "billing", "infra", "prod", "src", "a.md", "x.py", "", "[z]"]
_GLOB_CHARS = "ab/.*?[]!-\\"


def _random_path(rng: random.Random) -> str:
    return "/".join(rng.choice(_SEGMENTS) for _ in range(rng.randint(0, 4)))


def _random_glob(rng: random.Random) -> str:
    if rng.random() < 0.5:
        return rng.choice(["**", "docs/**", "**/*.md", "app/*/**", "*.py", "infra/prod/**"])
    while True:  # only globs a policy may load, so derive() is tested on what it can be given
        candidate = "".join(rng.choice(_GLOB_CHARS) for _ in range(rng.randint(1, 6)))
        try:
            return _rule([candidate], RiskClass.LOW).paths[0]
        except pydantic.ValidationError:
            continue


def _random_rules(rng: random.Random) -> RiskRules:
    classes = list(RiskClass)
    return RiskRules(
        default=rng.choice(classes),
        fallback=rng.choice(classes),
        derive=[
            _rule(
                [_random_glob(rng) for _ in range(rng.randint(1, 3))],
                rng.choice(classes),
                rng.choice([None, f"r{i}"]),
            )
            for i in range(rng.randint(0, 5))
        ],
    )


def _random_signals(rng: random.Random) -> ChangeSignals:
    return ChangeSignals(
        paths=[_random_path(rng) for _ in range(rng.randint(0, 5))],
        migration_present=rng.random() < 0.5,
        touches_contracts=rng.random() < 0.5,
        lines_added=rng.randint(0, 5000),
        lines_removed=rng.randint(0, 5000),
        repo_count=rng.randint(1, 4),
        available=rng.random() < 0.9,
    )


def test_derive_is_total_and_deterministic_across_generated_inputs() -> None:
    rng = random.Random(SEED)
    for _ in range(ITERATIONS):
        rules, signals = _random_rules(rng), _random_signals(rng)

        derived = derive(signals, rules)

        assert derived == derive(signals, rules)
        shuffled = signals.model_copy(
            update={"paths": rng.sample(signals.paths, len(signals.paths))}
        )
        assert derive(shuffled, rules) == derived
        assert derived.risk_class in RiskClass
        if derived.fallback_applied:
            assert derived.risk_class is rules.fallback
        else:
            assert derived.risk_class >= rules.default


def test_derive_never_raises_on_empty_paths() -> None:
    rng = random.Random(SEED)
    for _ in range(ITERATIONS):
        rules = _random_rules(rng)
        derived = derive(ChangeSignals(paths=[]), rules)
        assert derived.risk_class is rules.default
        assert derived.matched_rule_ids == []


def test_adding_a_matching_rule_never_lowers_the_derived_class() -> None:
    rng = random.Random(SEED)
    checked = 0
    for _ in range(ITERATIONS):
        rules, signals = _random_rules(rng), _random_signals(rng)
        if not signals.paths:
            continue
        before = derive(signals, rules)
        target = glob.escape(rng.choice(signals.paths).lstrip("/"))
        if not target or target.endswith("/"):
            target += "**"  # a loadable glob that still matches this exact path
        added = _rule([target], rng.choice(list(RiskClass)), "added")
        assert matches(added, signals.paths)
        position = rng.randint(0, len(rules.derive))
        grown = rules.model_copy(
            update={"derive": [*rules.derive[:position], added, *rules.derive[position:]]}
        )

        assert derive(signals, grown).risk_class >= before.risk_class
        checked += 1
    assert checked > ITERATIONS // 2


# --------------------------------------------------------------------------- #
# Glob parity with conditional policy rules (AC8)                               #
# --------------------------------------------------------------------------- #

GLOB_TABLE: list[tuple[str, str]] = [
    ("docs/**", "docs/index.md"),
    ("docs/**", "docs/a/b/c.md"),
    ("docs/**", "src/docs/index.md"),
    ("**/*.md", "README.md"),
    ("**/*.md", "a/b/README.md"),
    ("*.md", "a/b/README.md"),
    ("infra/**", "infra/prod/main.tf"),
    ("infra/**", "/infra/prod/main.tf"),
    ("infra/**", "infra\\prod\\main.tf"),
    ("infra/**", "infrastructure/x"),
    ("app/auth/**", "app/auth"),
    ("app/auth/**", "app/auth/login.py"),
    ("*.pem", "secrets/key.pem"),
    (".env*", "config/.env.local"),
    ("alembic/versions/**", "alembic/versions/0041_x.py"),
    ("[!a]*.py", "b.py"),
    ("[!a]*.py", "a.py"),
    ("?.py", "x.py"),
    ("app/billing/**", "app/billing/"),
]


@pytest.mark.parametrize(("glob", "path"), GLOB_TABLE)
def test_risk_rules_and_conditional_rules_agree_on_globs(glob: str, path: str) -> None:
    conditional = ConditionalRule(
        id="same-glob",
        effect=RuleEffect.DENY,
        reason="parity",
        when=ConditionGroup(
            conditions=[Condition(field="path", op=ConditionOp.MATCHES_GLOB, value=glob)]
        ),
    )
    policy_says = evaluate_condition(
        conditional.when, {"path": path}, field_whitelist=POLICY_CONDITION_FIELDS
    )

    assert matches(_rule([glob], RiskClass.HIGH), [path]) is policy_says


# --------------------------------------------------------------------------- #
# Purity (AC10)                                                                 #
# --------------------------------------------------------------------------- #

ALLOWED_IMPORTS = {"__future__", "collections", "pydantic", "scf_contracts", "scf_risk", "typing"}


def test_scf_risk_imports_nothing_that_does_io() -> None:
    imported: set[str] = set()
    for module in PACKAGE_DIR.glob("*.py"):
        for node in ast.walk(ast.parse(module.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add("scf_risk" if node.level else str(node.module).split(".")[0])
    assert imported, "no imports found; the scan is broken"
    assert imported <= ALLOWED_IMPORTS, imported - ALLOWED_IMPORTS
