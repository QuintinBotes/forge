"""#316 (#187 criteria 2 to 4): the dependency gate, against a fake registry (no network)."""

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
    after = _six(
        "1.17.0",
        sdist=f"{FILES}/six-1.17.0.tar.gz",
        wheel=f"{FILES}/six-1.17.0-py2.py3-none-any.whl",
    )
    registry = FakeRegistry({("pypi", "six", "1.17.0"): NEW})

    result = _locked(_six(), after, registry)

    assert registry.asked == [("pypi", "six", "1.17.0")]
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
    assert result.reasons == [
        "package-lock.json:11: evil 1.0.0 is not that release's file on the registry"
    ]


@pytest.mark.parametrize(
    "path",
    [
        "dev-requirements.txt",
        "requirements.in",
        "constraints.txt",
        "requirements/ci.txt",
        "Requirements-Dev.TXT",
    ],
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


ROOT = "[tool.uv.sources]\nscf_contracts = { workspace = true }\n"


def test_a_workspace_package_is_local_not_looked_up() -> None:
    """The base branch's sources, the root's and the member's own, say what is local."""
    sources = (
        '[tool.uv.sources]\nscf-board = { workspace = true }\nlocal-lib = { path = "../lib" }\n'
    )
    before = "[project]\ndependencies = []\n" + sources
    after = '[project]\ndependencies = ["scf-board", "scf-contracts", "local-lib"]\n' + sources
    manifests = {"pyproject.toml": (ROOT, ROOT), "apps/x/pyproject.toml": (before, after)}
    registry = FakeRegistry({})

    result = _gate(manifests, registry, changed=["apps/x/pyproject.toml"])

    assert result.passed and registry.asked == []


def test_a_manifest_that_could_not_be_read_cannot_verify() -> None:
    result = _gate({"pyproject.toml": ("", None)}, FakeRegistry({}))

    assert result.reasons == ["pyproject.toml: not read as a regular file"]


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


# --------------------------------------------------------------------------- #
# #316: the security reviews' bypasses of the first implementation             #
# --------------------------------------------------------------------------- #

FILES = "https://files.pythonhosted.org/packages/ab/cd"
SIX_LOCK = (
    'version = 1\n\n[[package]]\nname = "six"\nversion = "{version}"\n'
    'source = { registry = "https://pypi.org/simple" }\n'
    'sdist = { url = "{sdist}", hash = "sha256:aa" }\n'
    'wheels = [{ url = "{wheel}", hash = "{hash}" }]\n'
)


def _six(
    version: str = "1.16.0",
    *,
    sdist: str = f"{FILES}/six-1.16.0.tar.gz",
    wheel: str = f"{FILES}/six-1.16.0-py2.py3-none-any.whl",
    hash: str = "sha256:bb",
) -> str:
    return (
        SIX_LOCK.replace("{version}", version)
        .replace("{sdist}", sdist)
        .replace("{wheel}", wheel)
        .replace("{hash}", hash)
    )


def _locked(before: str, after: str, registry: FakeRegistry) -> GateResult:
    return _gate({"pyproject.toml": ("", "[project]\n"), "uv.lock": (before, after)}, registry)


def test_a_new_locked_version_on_the_registrys_file_host_is_checked() -> None:
    after = _six(
        "1.17.0",
        sdist=f"{FILES}/six-1.17.0.tar.gz",
        wheel=f"{FILES}/six-1.17.0-py2.py3-none-any.whl",
    )
    registry = FakeRegistry({("pypi", "six", "1.17.0"): OLD})

    assert _locked(_six(), after, registry).passed
    assert registry.asked == [("pypi", "six", "1.17.0")]


@pytest.mark.parametrize(
    "after",
    [
        _six(wheel="https://example.com/six-1.16.0-py2.py3-none-any.whl", hash="sha256:ee"),
        _six(hash="sha256:ee"),
        _six(sdist=f"{FILES}/six-1.16.0.zip"),
    ],
    ids=["wheel-moved-off-host", "hash-changed", "sdist-changed"],
)
def test_a_locked_package_whose_files_change_cannot_verify(after: str) -> None:
    """criterion 4: the lock is identified by what uv downloads, not by name and version."""
    registry = FakeRegistry({})

    result = _locked(_six(), after, registry)

    assert result.status == "could_not_verify" and registry.asked == []
    assert result.reasons == ["uv.lock:4: the files it downloads changed"]


@pytest.mark.parametrize(
    ("sdist", "wheel"),
    [
        (f"{FILES}/six-1.17.0.tar.gz", "https://example.com/six-1.17.0-py3-none-any.whl"),
        (f"{FILES}/six-1.17.0.tar.gz", f"{FILES}/evil-1.0-py3-none-any.whl"),
        (f"{FILES}/six-1.16.0.tar.gz", f"{FILES}/six-1.17.0-py3-none-any.whl"),
    ],
    ids=["off-host", "another-package", "another-version"],
)
def test_a_new_locked_version_that_downloads_something_else_cannot_verify(
    sdist: str, wheel: str
) -> None:
    registry = FakeRegistry({("pypi", "six", "1.17.0"): OLD})

    result = _locked(_six(), _six("1.17.0", sdist=sdist, wheel=wheel), registry)

    assert result.status == "could_not_verify" and registry.asked == []
    assert result.reasons == ["uv.lock:4: six 1.17.0 is not that release's file on the registry"]


def _npm_lock(resolved: str, integrity: str = "sha512-aa") -> str:
    entry = {"version": "4.17.21", "resolved": resolved, "integrity": integrity}
    return json.dumps({"lockfileVersion": 3, "packages": {"node_modules/lodash": entry}})


LODASH = "https://registry.npmjs.org/lodash/-/lodash-4.17.21.tgz"


@pytest.mark.parametrize(
    "resolved",
    [
        "https://registry.npmjs.org/evil/-/evil-1.0.0.tgz",
        "https://registry.npmjs.org/lodash/-/lodash-4.17.20.tgz",
        LODASH + "?evil",
    ],
)
def test_an_npm_lock_entry_must_resolve_to_its_own_package_and_version(resolved: str) -> None:
    """criterion 4: npm downloads ``resolved``, whatever name the entry gives."""
    manifests = {"package.json": ("", "{}"), "package-lock.json": ("", _npm_lock(resolved))}
    registry = FakeRegistry({("npm", "lodash", "4.17.21"): OLD})

    result = _gate(manifests, registry)

    assert result.status == "could_not_verify" and registry.asked == []
    assert result.reasons == [
        "package-lock.json:1: lodash 4.17.21 is not that release's file on the registry"
    ]


def test_an_npm_lock_entry_named_like_a_path_cannot_verify() -> None:
    entry = {"name": "lodash/../evil", "version": "1.0.0"}
    lock = json.dumps({"packages": {"node_modules/lodash": entry}})
    manifests = {"package.json": ("", "{}"), "package-lock.json": ("", lock)}

    result = _gate(manifests, FakeRegistry({}))

    assert result.reasons == ["package-lock.json:1: lodash/../evil is not from the registry"]


def test_an_npm_lock_entry_whose_integrity_changes_cannot_verify() -> None:
    before, after = _npm_lock(LODASH), _npm_lock(LODASH, integrity="sha512-ee")
    manifests = {"package.json": ("", "{}"), "package-lock.json": (before, after)}

    result = _gate(manifests, FakeRegistry({}))

    assert result.reasons == ["package-lock.json:1: the files it downloads changed"]


@pytest.mark.parametrize(
    "source",
    [
        '{ url = "https://example.com/evil-1.0.tar.gz" }',
        '{ git = "https://example.com/evil.git" }',
        '{ index = "attacker" }',
        '[{ path = "vendor/evil", marker = "sys_platform == \'linux\'" }, { index = "x" }]',
    ],
)
def test_a_source_from_elsewhere_cannot_verify_even_for_a_local_name(source: str) -> None:
    """criterion 5: a member's source overrides the root's, so no local name hides one."""
    root = '[tool.uv.sources]\nevil = { path = "vendor/evil" }\n'
    before = '[project]\nname = "app"\ndependencies = []\n'
    after = (
        f'[project]\nname = "app"\ndependencies = ["evil"]\n[tool.uv.sources]\nevil = {source}\n'
    )
    manifests = {"pyproject.toml": (root, root), "apps/m/pyproject.toml": (before, after)}
    registry = FakeRegistry({("pypi", "evil", None): OLD})

    result = _gate(manifests, registry, changed=["apps/m/pyproject.toml"])

    assert result.status == "could_not_verify" and registry.asked == []
    assert result.reasons == [
        "apps/m/pyproject.toml: changes how or where packages are installed "
        "(a table the gate does not parse)",
        "apps/m/pyproject.toml:3: evil is not from the registry ([tool.uv.sources])",
    ]


@pytest.mark.parametrize("path", ["../outside", "/opt/evil", "~/evil", "${HOME}/evil", "C:/x"])
def test_a_path_source_outside_the_repository_is_not_local(path: str) -> None:
    before = f'[project]\nname = "app"\n[tool.uv.sources]\nevil = {{ path = "{path}" }}\n'
    after = before.replace('name = "app"', 'name = "app"\ndependencies = ["evil"]')
    registry = FakeRegistry({("pypi", "evil", None): OLD})

    result = _gate({"pyproject.toml": (before, after)}, registry)

    assert registry.asked == [], "nor from the registry, whose namesake would decide"
    assert result.reasons == ["pyproject.toml:3: evil is not from the registry ([tool.uv.sources])"]


MEMBER = ('[project]\nname = "app"\n', '[project]\nname = "app"\ndependencies = ["evil"]\n')
LOCAL_ROOT = "[tool.uv.sources]\nevil = { workspace = true }\n"


@pytest.mark.parametrize(
    ("root", "member"),
    [
        (("", LOCAL_ROOT), MEMBER),
        (("", ""), (MEMBER[0], MEMBER[1] + '[tool.uv.sources]\nevil = { path = "vendor/evil" }\n')),
        ((LOCAL_ROOT, ""), MEMBER),
    ],
    ids=["the-root-adds-it", "the-member-adds-it", "the-root-drops-it"],
)
def test_only_a_local_source_the_change_leaves_as_it_was_makes_a_package_local(
    root: tuple[str, str], member: tuple[str, str]
) -> None:
    """criterion 5: only the base branch says what is local; the change cannot."""
    manifests = {"pyproject.toml": root, "apps/m/pyproject.toml": member}
    registry = FakeRegistry({})

    result = _gate(manifests, registry, changed=["apps/m/pyproject.toml"])

    assert registry.asked == [("pypi", "evil", None)]
    assert [f.rule for f in result.findings] == ["unknown-dependency"]


@pytest.mark.parametrize("source", ['{ directory = "../../outside" }', '{ path = "/opt/x.whl" }'])
def test_a_locked_path_outside_the_repository_cannot_verify(source: str) -> None:
    after = UV_LOCK.format(name="evil", version="1.0", source=source)

    result = _locked("", after, FakeRegistry({}))

    assert result.reasons == ["uv.lock:4: evil is not from the registry"]


@pytest.mark.parametrize("spec", ["^2.0.0-beta.0", ">=3.0.0-rc.1 <4", "next", "latest", "*", ""])
def test_an_npm_range_that_may_install_a_pre_release_or_a_tag_cannot_verify(spec: str) -> None:
    """criterion 7: the newest final release does not date what npm installs here."""
    registry = FakeRegistry({("npm", "foo", None): OLD})

    result = _gate({"package.json": ("", json.dumps({"dependencies": {"foo": spec}}))}, registry)

    assert result.status == "could_not_verify" and registry.asked == []


def test_an_npm_range_and_a_pre_release_pin_are_still_looked_up() -> None:
    deps = {"a": "^1.2.0", "b": "~1.2 || >= 2.0.0 <3", "c": "2.0.0-beta.1", "d": "1.x"}
    registry = FakeRegistry({})

    _gate({"package.json": ("", json.dumps({"dependencies": deps}))}, registry)

    assert registry.asked == [
        ("npm", "a", None),
        ("npm", "b", None),
        ("npm", "c", "2.0.0-beta.1"),
        ("npm", "d", None),
    ]


@pytest.mark.parametrize(
    "line", ["stream.zip", "./pkg", "pkg.tar.gz", "pkg.whl", "pkg.TGZ", "stream.zip[extra]"]
)
def test_a_requirement_pip_reads_as_a_local_file_cannot_verify(line: str) -> None:
    """criterion 8: pip installs these from the working tree, not the registry."""
    registry = FakeRegistry({})

    result = _gate({"requirements.txt": ("", f"{line}\n")}, registry)

    assert result.status == "could_not_verify" and registry.asked == []


def test_every_line_pip_splits_is_read() -> None:
    """pip splits a requirements file as ``str.splitlines`` does, form feeds included."""
    registry = FakeRegistry({("pypi", "httpx", "0.27.0"): OLD})

    result = _gate({"requirements.txt": ("", "httpx==0.27.0\x0cevil==1.0\n")}, registry)

    assert ("pypi", "evil", "1.0") in registry.asked and not result.passed


@pytest.mark.parametrize(
    "path",
    [
        "requirements.lock",
        "requirements-dev.lock",
        "pylock.toml",
        "pylock.dev.toml",
        "hatch.toml",
        "pixi.toml",
        "pixi.lock",
        "environment.yml",
        "environment.yaml",
        "web/bunfig.toml",
    ],
)
def test_an_install_input_the_first_gate_missed_cannot_verify(path: str) -> None:
    """criterion 9."""
    result = _gate({}, FakeRegistry({}), changed=[path])

    assert result.status == "could_not_verify"
    assert result.reasons == [f"{path}: the gate does not parse this manifest"]
