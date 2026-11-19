"""#187 criteria 2 to 4: the dependency gate, against a fake registry (no network)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pydantic
import pytest

from scf_contracts import Policy
from scf_contracts.dtos import MIN_DEPENDENCY_AGE_DAYS
from scf_policy.dependency_gate import Ecosystem, check_dependencies
from scf_policy.diff_gate import GateResult
from scf_policy.loader import load_policy

NOW = datetime(2026, 11, 19, tzinfo=UTC)
OLD = NOW - timedelta(days=400)
NEW = NOW - timedelta(days=2)


class FakeRegistry:
    """Answers from ``known``: a publication time, or an exception to raise."""

    def __init__(self, known: dict[tuple[str, str, str | None], datetime | Exception]) -> None:
        self.known = known
        self.asked: list[tuple[str, str, str | None]] = []

    def published(self, ecosystem: Ecosystem, name: str, version: str | None) -> datetime | None:
        self.asked.append((ecosystem, name, version))
        answer = self.known.get((ecosystem, name, version))
        if isinstance(answer, Exception):
            raise answer
        return answer


def _gate(
    manifests: dict[str, tuple[str, str]],
    registry: FakeRegistry,
    *,
    changed: list[str] | None = None,
    min_age_days: int = MIN_DEPENDENCY_AGE_DAYS,
    allowed: list[str] | None = None,
) -> GateResult:
    return check_dependencies(
        changed if changed is not None else list(manifests),
        manifests,
        registry,
        min_age_days=min_age_days,
        allowed_packages=allowed or [],
        now=NOW,
    )


REQUIREMENTS = "httpx==0.27.0\n"


def test_a_name_the_registry_does_not_know_fails() -> None:
    registry = FakeRegistry({("pypi", "httpx", "0.27.0"): OLD})
    after = REQUIREMENTS + "reqeusts-toolbelt2==1.0  # typo-squat\n"

    result = _gate({"requirements.txt": (REQUIREMENTS, after)}, registry)

    assert result.status == "failed"
    [finding] = result.findings
    assert (finding.file, finding.line, finding.rule) == (
        "requirements.txt",
        2,
        "unknown-dependency",
    )
    assert registry.asked == [("pypi", "reqeusts-toolbelt2", "1.0")], "only what is added"


def test_a_known_old_dependency_passes() -> None:
    registry = FakeRegistry({("pypi", "rich", "13.7.1"): OLD})

    result = _gate({"requirements-dev.txt": ("", "rich==13.7.1\n")}, registry)

    assert result.passed and result.findings == []


def test_pyproject_dependencies_and_groups_are_read() -> None:
    before = '[project]\nname = "app"\ndependencies = ["httpx==0.27.0"]\n'
    after = (
        '[project]\nname = "app"\ndependencies = ["httpx==0.27.0", "Typer_Slim[all]>=0.9"]\n'
        '[dependency-groups]\ndev = ["pytest-fakeplugin==9.9.9"]\n'
    )
    registry = FakeRegistry({("pypi", "typer-slim", None): OLD})

    result = _gate({"pyproject.toml": (before, after)}, registry)

    assert registry.asked == [("pypi", "typer-slim", None), ("pypi", "pytest-fakeplugin", "9.9.9")]
    assert [(f.line, f.rule) for f in result.findings] == [(5, "unknown-dependency")]


def test_package_json_dependencies_are_read() -> None:
    before = json.dumps({"name": "web", "dependencies": {"react": "18.3.1"}}, indent=2)
    after = json.dumps(
        {
            "name": "web",
            "dependencies": {"react": "18.3.1", "@acme/widgets": "^2.0.0"},
            "devDependencies": {"left-pad": "1.3.0", "shared": "workspace:*"},
        },
        indent=2,
    )
    registry = FakeRegistry({("npm", "@acme/widgets", None): OLD})

    result = _gate({"web/package.json": (before, after)}, registry)

    assert registry.asked == [("npm", "@acme/widgets", None), ("npm", "left-pad", "1.3.0")]
    [finding] = result.findings
    assert (finding.file, finding.line, finding.rule) == (
        "web/package.json",
        8,
        "unknown-dependency",
    )


def test_a_version_younger_than_the_minimum_age_fails() -> None:
    registry = FakeRegistry({("pypi", "httpx", "0.28.0"): NEW})

    result = _gate({"requirements.txt": ("", "httpx==0.28.0\n")}, registry)

    assert result.status == "failed"
    [finding] = result.findings
    assert (finding.line, finding.rule) == (1, "dependency-too-new")


def test_a_range_is_judged_by_the_newest_release() -> None:
    registry = FakeRegistry({("pypi", "httpx", None): NEW})

    result = _gate({"requirements.txt": ("", "httpx>=0.27\n")}, registry)

    assert [f.rule for f in result.findings] == ["dependency-too-new"]


def test_the_minimum_age_comes_from_the_policy() -> None:
    registry = FakeRegistry({("pypi", "httpx", "0.28.0"): NEW})

    assert _gate({"requirements.txt": ("", "httpx==0.28.0\n")}, registry, min_age_days=1).passed


def test_a_package_the_policy_allows_by_name_skips_the_age_check_only() -> None:
    registry = FakeRegistry({("pypi", "httpx", "0.28.0"): NEW})
    manifests = {"requirements.txt": ("", "httpx==0.28.0\nnot-on-pypi==1.0\n")}

    result = _gate(manifests, registry, allowed=["HTTPX", "not_on_pypi"])

    assert [f.rule for f in result.findings] == ["unknown-dependency"]


def test_the_minimum_age_and_allowlist_are_policy_fields_with_a_default(tmp_path: Path) -> None:
    default = Policy(repo_id="r").diff_gates
    assert MIN_DEPENDENCY_AGE_DAYS == 7
    assert (default.min_dependency_age_days, default.allowed_packages) == (7, [])
    (tmp_path / "policy.yaml").write_text(
        "repo_id: r\ndiff_gates:\n  min_dependency_age_days: 14\n  allowed_packages: [httpx]\n"
    )
    loaded = load_policy(tmp_path / "policy.yaml").diff_gates
    assert (loaded.min_dependency_age_days, loaded.allowed_packages) == (14, ["httpx"])


@pytest.mark.parametrize(
    "block",
    [
        "min_dependency_age_days: -1",
        "min_dependency_age_days: soon",
        "allowed_packages: httpx",
        "sast_severity: critical",
        "sast_analyzer: semgrep",
        "min_dependency_age_day: 0",
    ],
)
def test_an_invalid_gate_block_is_refused_when_the_policy_loads(tmp_path: Path, block: str) -> None:
    (tmp_path / "policy.yaml").write_text(f"repo_id: r\ndiff_gates:\n  {block}\n")

    with pytest.raises(pydantic.ValidationError):
        load_policy(tmp_path / "policy.yaml")


@pytest.mark.parametrize("error", [TimeoutError("read timed out"), RuntimeError("HTTP 503")])
def test_a_registry_that_errors_or_times_out_cannot_verify(error: Exception) -> None:
    registry = FakeRegistry({("pypi", "httpx", "0.28.0"): error})

    result = _gate({"requirements.txt": ("", "httpx==0.28.0\n")}, registry)

    assert result.status == "could_not_verify" and not result.passed
    assert result.reasons == [f"requirements.txt:1: pypi did not answer ({type(error).__name__})"]


@pytest.mark.parametrize("changed", [["uv.lock"], ["web/pnpm-lock.yaml"], ["go.mod"], [".npmrc"]])
def test_a_manifest_the_gate_does_not_parse_cannot_verify(changed: list[str]) -> None:
    result = _gate({}, FakeRegistry({}), changed=changed)

    assert result.status == "could_not_verify"
    assert result.reasons == [f"{changed[0]}: the gate does not parse this manifest"]


def test_a_lockfile_changed_with_a_manifest_it_pairs_with_is_not_refused() -> None:
    registry = FakeRegistry({("pypi", "rich", "13.7.1"): OLD})
    manifests = {"pyproject.toml": ("", '[project]\ndependencies = ["rich==13.7.1"]\n')}

    assert _gate(manifests, registry, changed=["pyproject.toml", "uv.lock"]).passed


@pytest.mark.parametrize(
    ("path", "after"),
    [
        ("requirements.txt", "evil @ https://example.com/evil-1.0.tar.gz\n"),
        ("requirements.txt", "-e git+https://example.com/evil.git#egg=evil\n"),
        ("requirements.txt", "--extra-index-url https://example.com/simple\n"),
        ("package.json", '{"dependencies": {"evil": "github:someone/evil"}}'),
        ("pyproject.toml", '[tool.uv.sources]\nhttpx = { git = "https://example.com/h" }\n'),
        ("package.json", '{"overrides": {"react": "0.0.1-evil"}}'),
        ("pyproject.toml", "[project\n"),
        ("package.json", "{"),
    ],
)
def test_what_does_not_come_from_the_registry_cannot_verify(path: str, after: str) -> None:
    registry = FakeRegistry({})

    result = _gate({path: ("", after)}, registry)

    assert result.status == "could_not_verify" and not result.passed
    assert registry.asked == []
    assert after.strip() not in result.report(), "a requirement may hold a token in its URL"
