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
    manifests: dict[str, tuple[str | None, str | None]],
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
    # the worker's fault, not the change's: a person fixes it, not the builder
    assert result.unavailable == [
        f"requirements.txt:1: pypi did not answer ({type(error).__name__})"
    ]
    assert result.reasons == []


@pytest.mark.parametrize(
    "changed",
    [
        ["uv.lock"],
        ["web/pnpm-lock.yaml"],
        ["go.mod"],
        [".npmrc"],
        ["uv.toml"],
        ["web/.yarnrc.yml"],
        ["pip.ini"],
    ],
)
def test_a_manifest_the_gate_does_not_parse_cannot_verify(changed: list[str]) -> None:
    result = _gate({}, FakeRegistry({}), changed=changed)

    assert result.status == "could_not_verify"
    assert result.reasons == [f"{changed[0]}: the gate does not parse this manifest"]


@pytest.mark.parametrize(
    "changed", [["uv.lock", "web/package.json"], ["sub/uv.lock", "pyproject.toml"]]
)
def test_a_lockfile_pairs_only_with_its_own_ecosystems_manifest_beside_it(
    changed: list[str],
) -> None:
    result = _gate(dict.fromkeys(changed[1:], ("", "")), FakeRegistry({}), changed=changed)

    assert result.reasons == [f"{changed[0]}: the gate does not parse this manifest"]


def test_a_lockfile_whose_versions_the_gate_does_not_read_cannot_verify() -> None:
    changed = ["web/package.json", "web/pnpm-lock.yaml"]

    result = _gate({"web/package.json": ("", "{}")}, FakeRegistry({}), changed=changed)

    assert result.reasons == ["web/pnpm-lock.yaml: the gate does not read its locked versions"]


UV_LOCK = 'version = 1\n\n[[package]]\nname = "{name}"\nversion = "{version}"\nsource = {source}\n'
PYPI_SOURCE = '{ registry = "https://pypi.org/simple" }'


def test_a_locked_version_is_checked_like_an_added_dependency() -> None:
    before = UV_LOCK.format(name="httpx", version="0.27.0", source=PYPI_SOURCE)
    after = UV_LOCK.format(name="httpx", version="0.28.1", source=PYPI_SOURCE)
    registry = FakeRegistry({("pypi", "httpx", "0.28.1"): NEW})
    manifests = {"pyproject.toml": ("", "[project]\n"), "uv.lock": (before, after)}

    result = _gate(manifests, registry)

    assert registry.asked == [("pypi", "httpx", "0.28.1")]
    [finding] = result.findings
    assert (finding.file, finding.line, finding.rule) == ("uv.lock", 4, "dependency-too-new")


@pytest.mark.parametrize(
    "source",
    ['{ git = "https://example.com/h.git" }', '{ registry = "https://example.com/simple" }'],
)
def test_a_locked_package_from_elsewhere_cannot_verify(source: str) -> None:
    after = UV_LOCK.format(name="httpx", version="0.28.1", source=source)
    manifests = {"pyproject.toml": ("", "[project]\n"), "uv.lock": ("", after)}

    result = _gate(manifests, FakeRegistry({}))

    assert result.reasons == ["uv.lock:4: httpx is not from the registry"]


def test_a_locked_workspace_package_is_local() -> None:
    after = UV_LOCK.format(name="scf-board", version="0.1.0", source='{ editable = "." }')
    manifests = {"pyproject.toml": ("", "[project]\n"), "uv.lock": ("", after)}

    assert _gate(manifests, FakeRegistry({})).passed


def test_an_npm_lockfile_is_checked() -> None:
    after = json.dumps(
        {
            "lockfileVersion": 3,
            "packages": {
                "": {"name": "web"},
                "node_modules/left-pad": {
                    "version": "1.3.0",
                    "resolved": "https://registry.npmjs.org/left-pad/-/left-pad-1.3.0.tgz",
                },
                "node_modules/evil": {"version": "1.0.0", "resolved": "git+ssh://x/evil.git"},
                "node_modules/shared": {"resolved": "packages/shared", "link": True},
            },
        },
        indent=2,
    )
    manifests = {"package.json": ("", "{}"), "package-lock.json": ("", after)}
    registry = FakeRegistry({})

    result = _gate(manifests, registry)

    assert registry.asked == [("npm", "left-pad", "1.3.0")]
    assert [(f.file, f.rule) for f in result.findings] == [
        ("package-lock.json", "unknown-dependency")
    ]
    assert result.reasons == ["package-lock.json:11: evil is not from the registry"]


@pytest.mark.parametrize(
    "path", ["dev-requirements.txt", "requirements.in", "constraints.txt", "requirements/ci.txt"]
)
def test_every_pip_requirements_file_is_read(path: str) -> None:
    result = _gate({path: ("", "not-on-pypi==1.0\n")}, FakeRegistry({}))

    assert [f.rule for f in result.findings] == ["unknown-dependency"]


def test_optional_build_and_marked_dependencies_are_read() -> None:
    after = (
        '[build-system]\nrequires = ["hatchling==1.25.0"]\n'
        "[project]\ndependencies = ['tomli==2.0.1; python_version < \"3.11\"']\n"
        '[project.optional-dependencies]\nfast = ["orjson==3.10.0"]\n'
    )
    registry = FakeRegistry({})

    _gate({"pyproject.toml": ("", after)}, registry)

    assert registry.asked == [
        ("pypi", "tomli", "2.0.1"),
        ("pypi", "hatchling", "1.25.0"),
        ("pypi", "orjson", "3.10.0"),
    ]


def test_npm_optional_and_peer_dependencies_are_read() -> None:
    after = json.dumps(
        {"optionalDependencies": {"fsevents": "2.3.3"}, "peerDependencies": {"react": "^18"}}
    )
    registry = FakeRegistry({})

    _gate({"package.json": ("", after)}, registry)

    assert registry.asked == [("npm", "fsevents", "2.3.3"), ("npm", "react", None)]


def test_a_workspace_package_is_local_not_looked_up() -> None:
    after = (
        '[project]\ndependencies = ["scf-board", "scf-contracts", "local-lib"]\n'
        '[tool.uv.sources]\nscf-board = { workspace = true }\nlocal-lib = { path = "../lib" }\n'
    )
    registry = FakeRegistry({})

    result = check_dependencies(
        ["pyproject.toml"],
        {"pyproject.toml": ("", after)},
        registry,
        min_age_days=MIN_DEPENDENCY_AGE_DAYS,
        allowed_packages=[],
        local_packages=["scf_contracts"],
        now=NOW,
    )

    assert result.passed and registry.asked == []


def test_a_manifest_that_could_not_be_read_cannot_verify() -> None:
    result = _gate({"pyproject.toml": ("", None)}, FakeRegistry({}))

    assert result.reasons == ["pyproject.toml: not read"]


@pytest.mark.parametrize(
    ("path", "after"),
    [
        ("requirements.txt", "evil @ https://example.com/evil-1.0.tar.gz\n"),
        ("requirements.txt", "-e git+https://example.com/evil.git#egg=evil\n"),
        ("requirements.txt", "--extra-index-url https://example.com/simple\n"),
        ("package.json", '{"dependencies": {"evil": "github:someone/evil"}}'),
        ("requirements.txt", "http://example.com/evil-1.0.tar.gz\n"),
        ("requirements.txt", "git+https://example.com/evil.git\n"),
        ("requirements.txt", "./vendor/evil\n"),
        ("requirements.txt", "file:///tmp/evil-1.0-py3-none-any.whl\n"),
        ("pyproject.toml", '[tool.uv.sources]\nhttpx = { git = "https://example.com/h" }\n'),
        ("pyproject.toml", '[tool.hatch.envs.default]\ndependencies = ["evil==1.0"]\n'),
        ("pyproject.toml", '[tool.rye]\ndev-dependencies = ["evil==1.0"]\n'),
        ("pyproject.toml", '[tool.pixi.dependencies]\nevil = "==1.0"\n'),
        ("pyproject.toml", '[tool.setuptools.dynamic]\ndependencies = { file = "r.txt" }\n'),
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
